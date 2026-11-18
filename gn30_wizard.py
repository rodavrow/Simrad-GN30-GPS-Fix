#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 rodavrow
# Provided "as is", without warranty; use at your own risk. See LICENSE and DISCLAIMER.md.
"""
gn30_wizard.py - step-by-step guide through the whole GN30 MKII date fix.

Needs only the Python standard library and runs on Linux, macOS and Windows:

    python3 gn30_wizard.py                 (Windows: py gn30_wizard.py)
    python3 gn30_wizard.py --port COM5     (skip the port question)

It sets up the .venv (the same one venv.sh / venv.ps1 use), walks you through the
wiring, checks the debug link, reads the flash twice and compares the reads, patches
the image with gn30_patch.py and flashes the one changed sector with bp3_bdm.py.
Every safety check of those two tools stays in force; this script only adds checks.

It never overwrites a flash dump. You can stop it at any point (Ctrl+C) and run it
again: dumps from an earlier run are offered for reuse.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

HERE = Path(__file__).resolve().parent
VENV_DIR = HERE / ".venv"
REQUIREMENTS = "requirements.txt"
BP3 = "bp3_bdm.py"
PATCHER = "gn30_patch.py"
DUMP_A = "gn30_a.bin"
DUMP_B = "gn30_b.bin"
PATCHED = "gn30_patched.bin"
IMAGE_SIZE = 0x100000
EXPECTED_VECTORS = "SSP=0x00180000  PC=0x00000D78"     # as printed by `bp3_bdm.py probe`
BUS_PIRATE_USB_IDS = {(0x0403, 0x6001)}                  # FTDI FT232RL on the Bus Pirate v3
TOTAL_STEPS = 8

SECTOR_RE = re.compile(r"flash sector to rewrite: 0x([0-9A-Fa-f]+)-0x([0-9A-Fa-f]+)")
ALREADY_PATCHED_MARK = "ALREADY PATCHED"
PATCHER_HINT_MARK = "Flash it with e.g.:"   # gn30_patch.py's own next-step hint; the wizard does that step

RISK_PHRASE = "I UNDERSTAND"
RISK_NOTICE = """\
BEFORE YOU START - please read. The full text is in DISCLAIMER.md.

  * This is at YOUR OWN RISK. The tools come "as is", with no warranty, and the
    author accepts no liability for any damage, loss or injury.
  * The procedure rewrites part of the GN30's firmware. If something goes wrong
    the unit may no longer start, and it may not be recoverable.
  * The modified firmware is not tested, approved or supported by the manufacturer
    or any authority. It was tested on ONE unit. Modifying the unit may void its
    warranty, approvals and your insurance cover.
  * NEVER rely on a modified unit as your only means of navigation.
  * No firmware is supplied. You read it from your own unit; do not share dumps.
    Making sure this is lawful where you live is your responsibility.
  * This project is not affiliated with or endorsed by Simrad, Shipmate or any
    other company."""

WIRING = """\
    Header pad   MC68341 signal   Bus Pirate v3
    ----------   --------------   -------------
        1        BKPT / DSCLK     CLK
        2        FREEZE           AUX
        3        IFETCH / DSI     MOSI
        4        IPIPE / DSO      MISO
        5        -                not connected
        6        GND              GND
        7        RESET            CS
        8        +5 V             Vpu"""


class WizardStop(Exception):
    """Ends the walk-through early; the message tells the user where things stand."""


class SerialPort(NamedTuple):
    device: str
    description: str
    usb_id: Optional[Tuple[int, int]]

    @property
    def looks_like_bus_pirate(self) -> bool:
        return self.usb_id in BUS_PIRATE_USB_IDS


# ---------------------------------------------------------------- pure helpers
def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_full_dump(path: Path) -> bool:
    return path.is_file() and path.stat().st_size == IMAGE_SIZE


def next_dump_name(directory: Path, now: dt.datetime) -> str:
    """gn30_a.bin, then gn30_b.bin, then gn30_b_<timestamp>.bin: never an existing file."""
    for name in (DUMP_A, DUMP_B):
        if not (directory / name).exists():
            return name
    stem, suffix = os.path.splitext(DUMP_B)
    return f"{stem}_{now:%Y%m%d-%H%M%S}{suffix}"


def parse_port_listing(text: str) -> list[SerialPort]:
    """Parse the tab-separated lines printed by PORT_LISTING_CODE."""
    ports = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        device, description, vid, pid = fields
        usb_id = (int(vid), int(pid)) if vid and pid else None
        ports.append(SerialPort(device, description, usb_id))
    return ports


def parse_patch_sector(output: str) -> Optional[Tuple[int, int]]:
    """(first, last) address of the sector gn30_patch.py says to rewrite, or None."""
    match = SECTOR_RE.search(output)
    if not match:
        return None
    return int(match.group(1), 16), int(match.group(2), 16)


def is_already_patched(output: str) -> bool:
    return ALREADY_PATCHED_MARK in output


def without_patcher_hint(output: str) -> str:
    """Drop gn30_patch.py's 'Flash it with e.g.' hint: its example port would only confuse here."""
    cut = output.find(PATCHER_HINT_MARK)
    return output if cut < 0 else output[:cut]


def accepts_risk(answer: str) -> bool:
    """True only for the full acknowledgement phrase: a bare Enter or 'y' is not enough."""
    return answer.strip().upper() == RISK_PHRASE


def display_command(args: list[str]) -> str:
    return "python " + " ".join(f'"{a}"' if " " in a else a for a in args)


# ---------------------------------------------------------------- console helpers
def heading(step: int, title: str) -> None:
    print("\n" + "=" * 72)
    print(f" Step {step} of {TOTAL_STEPS}: {title}")
    print("=" * 72)


def ask_yes_no(question: str, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def wait_for_enter(action: str = "continue") -> None:
    input(f"\nPress Enter to {action} (Ctrl+C to stop) ... ")


def run_tool(python: Path, args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    """Run one of the project's scripts with the venv's Python, from the project folder."""
    print(f"\n$ {display_command(args)}")
    if not capture:
        return subprocess.run([str(python), *args], cwd=HERE)
    return subprocess.run([str(python), *args], cwd=HERE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)


# ---------------------------------------------------------------- steps
def confirm_risk_understood() -> None:
    print("\n" + "!" * 72)
    print(RISK_NOTICE)
    print("!" * 72)
    answer = input(f'\nType "{RISK_PHRASE}" to accept this and continue (anything else stops): ')
    if not accepts_risk(answer):
        raise WizardStop("You did not accept the disclaimer. Nothing was done.")


def step_python_env() -> Path:
    heading(1, "Python environment")
    python = venv_python(VENV_DIR)
    created = False
    if not python.exists():
        if VENV_DIR.exists():
            raise WizardStop(f"{VENV_DIR} exists but has no Python in it. Delete that folder and run again.")
        print(f"Creating virtual environment in {VENV_DIR} ...")
        try:
            venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        except (OSError, subprocess.CalledProcessError) as e:
            shutil.rmtree(VENV_DIR, ignore_errors=True)
            hint = " On Debian/Ubuntu: sudo apt install python3-venv" if sys.platform.startswith("linux") else ""
            raise WizardStop(f"Could not create the virtual environment ({e}).{hint}") from e
        created = True

    has_pyserial = subprocess.run([str(python), "-c", "import serial"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not has_pyserial:
        print("Installing pyserial ...")
        install = subprocess.run([str(python), "-m", "pip", "install", "-r", REQUIREMENTS], cwd=HERE)
        if install.returncode != 0:
            if created:
                shutil.rmtree(VENV_DIR, ignore_errors=True)
            raise WizardStop("pip could not install pyserial. Check your internet connection and run again.")
    print("Python environment ready (pyserial installed).")
    return python


def missing_serial_group() -> bool:
    """True on Linux when the user is not in a group that may open /dev/ttyUSB*."""
    if sys.platform != "linux":
        return False
    if os.geteuid() == 0:
        return False
    import grp
    names = set()
    for gid in os.getgroups():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return not names & {"dialout", "uucp"}


def step_hardware_checklist() -> None:
    heading(2, "Hardware checklist")
    print("Connect the Bus Pirate to the GN30's 8-pad debug header like this:\n")
    print(WIRING)
    print("""
Before going on:
  [ ] Each pad checked with a meter: unit powered -> pad 8 reads 5 V, pad 6 is ground.
  [ ] The Bus Pirate's +3V3 and +5V supply pins are NOT connected.
  [ ] Bus Pirate plugged into USB FIRST, THEN the GN30 powered up.
  [ ] The GN30 stays powered for the whole procedure (reads take ~40 min each).""")
    if os.name == "nt":
        print("""  [ ] Windows: USB latency timer set to 1 ms, or reads are much slower:
      Device Manager -> Ports (COM & LPT) -> the Bus Pirate's COM port -> Properties
      -> Port Settings -> Advanced -> Latency Timer (msec) = 1
      (Only FTDI USB-serial chips have this setting; skip it if yours has none.)""")
    if missing_serial_group():
        print("""
NOTE: you are not in the 'dialout' group, so opening /dev/ttyUSB* may fail with
'permission denied'. Fix: sudo usermod -aG dialout $USER  (then log out and back in).""")
    wait_for_enter("continue when everything above is done")


PORT_LISTING_CODE = (
    "import serial.tools.list_ports as lp\n"
    "for p in lp.comports():\n"
    "    print('\\t'.join([p.device, p.description or '', str(p.vid or ''), str(p.pid or '')]))\n")


def step_choose_port(python: Path, port_arg: Optional[str]) -> str:
    heading(3, "Bus Pirate serial port")
    if port_arg:
        print(f"Using port {port_arg} (from --port).")
        return port_arg
    while True:
        listing = subprocess.run([str(python), "-c", PORT_LISTING_CODE], capture_output=True, text=True)
        if listing.returncode != 0:
            raise WizardStop(f"Could not list serial ports:\n{listing.stderr}")
        ports = parse_port_listing(listing.stdout)
        likely = [p for p in ports if p.looks_like_bus_pirate]
        if len(likely) == 1:
            default = likely[0].device
        elif len(ports) == 1:
            default = ports[0].device
        else:
            default = None
        if ports:
            print("Serial ports found:")
            for number, port in enumerate(ports, 1):
                mark = "   <- probably the Bus Pirate" if port.looks_like_bus_pirate else ""
                print(f"  {number}. {port.device:<14} {port.description}{mark}")
        else:
            print("No serial ports found. Is the Bus Pirate plugged into USB?")
        prompt = "Enter a number or a port name, or just Enter to scan again"
        if default:
            prompt = f"Enter a number or a port name [Enter = {default}]"
        choice = input(prompt + ": ").strip()
        if not choice:
            if default:
                return default
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(ports):
            return ports[int(choice) - 1].device
        if choice.isdigit():
            print(f"There is no port number {choice}.")
            continue
        return choice


def step_check_link(python: Path, port: str) -> None:
    heading(4, "Check the debug link")
    print("First the line levels (doesn't touch the CPU), then a probe that stops the CPU")
    print("in debug mode and reads its reset vectors. Nothing is written.")
    while True:
        pins = run_tool(python, [BP3, "--port", port, "pins"])
        if pins.returncode == 0:
            probe = run_tool(python, [BP3, "--port", port, "probe"], capture=True)
            print(probe.stdout, end="")
            if probe.returncode == 0 and EXPECTED_VECTORS in probe.stdout:
                print(f"\nLink OK: reset vectors match the GN30 MKII ({EXPECTED_VECTORS}).")
                return
            if probe.returncode == 0:
                print(f"\nThe probe worked, but the reset vectors are not the expected {EXPECTED_VECTORS}.")
                print("This may be a different bootloader version. The patch tool will still check")
                print("the firmware image itself before anything is changed.")
                if ask_yes_no("Continue anyway?", False):
                    return
                raise WizardStop("Stopped after the probe. Nothing was written to the unit.")
        print("""
The link check failed. Things to check:
  - the port is right and nothing else (PuTTY, screen, minicom) has it open;
  - the wiring, especially pads 1, 2 and 7 (CLK, AUX, CS);
  - the GN30 is powered, and was powered up AFTER the Bus Pirate was plugged in.""")
        if not ask_yes_no("Try again?", True):
            raise WizardStop("Stopped at the link check. Nothing was written to the unit.")


def read_dump(python: Path, port: str, which: str) -> str:
    """Read the whole flash into a new file and return its name. Never overwrites a file."""
    while True:
        name = next_dump_name(HERE, dt.datetime.now())
        path = HERE / name
        print(f"\nReading the whole flash into {name} ({which} read). This takes about 40 minutes.")
        try:
            result = run_tool(python, [BP3, "--port", port, "dump", name])
        except KeyboardInterrupt:
            path.unlink(missing_ok=True)       # incomplete file this wizard just started
            raise
        if result.returncode == 0 and is_full_dump(path):
            return name
        path.unlink(missing_ok=True)
        print("\nThe read failed (incomplete file removed). Check the wiring and that the GN30 is powered.")
        if not ask_yes_no("Try this read again?", True):
            raise WizardStop("Stopped during the flash read. Nothing was written to the unit.")


def step_read_flash(python: Path, port: str) -> str:
    """Return the name of a dump that two independent reads agree on."""
    heading(5, "Back up the flash (read it twice and compare)")
    print("The dump is your only backup of the original firmware, so it is read twice and")
    print("the two reads must be identical. Existing dumps are never overwritten.")
    hashes: dict[str, str] = {}

    def digest(name: str) -> str:
        if name not in hashes:
            hashes[name] = sha256_of(HERE / name)
        return hashes[name]

    reads: list[str] = []
    if is_full_dump(HERE / DUMP_A):
        print(f"\nFound an existing full dump: {DUMP_A}")
        if is_full_dump(HERE / DUMP_B) and digest(DUMP_A) == digest(DUMP_B):
            print(f"and {DUMP_B} is identical to it (SHA-256 {digest(DUMP_A)}).")
            if ask_yes_no("Were both read from THIS unit, which has not been flashed since? Reuse them?", True):
                return DUMP_A
        elif ask_yes_no(f"Was {DUMP_A} read from THIS unit, which has not been flashed since? "
                        "Use it as the first read?", True):
            reads.append(DUMP_A)
    if not reads:
        reads.append(read_dump(python, port, "first"))

    while True:
        new = read_dump(python, port, "comparison")
        match = next((r for r in reads if digest(r) == digest(new)), None)
        if match:
            print(f"\n{new} is identical to {match} (SHA-256 {digest(new)}). Backup verified.")
            return match
        reads.append(new)
        print(f"\nWARNING: {new} differs from {', '.join(reads[:-1])}.")
        print("Either the link is unreliable or the chip changed since the earlier read.")
        print("Another read is compared with all of them; two that agree are good enough.")
        if not ask_yes_no("Read again?", True):
            raise WizardStop("Stopped: no two reads agree. Check the wiring and connections, then run again.")


def step_patch(python: Path, current: str) -> Optional[Tuple[int, int]]:
    """Patch the verified dump. Return the sector to flash, or None if already patched."""
    heading(6, "Analyse and patch the firmware image")
    if (HERE / PATCHED).exists():
        print(f"({PATCHED} from an earlier run will be replaced; it is regenerated from {current}.)")
    result = run_tool(python, [PATCHER, current, "-o", PATCHED], capture=True)
    print(without_patcher_hint(result.stdout), end="")
    if result.returncode != 0:
        raise WizardStop("gn30_patch.py refused to patch this image (see the message above). "
                         "Nothing was written to the unit.")
    if is_already_patched(result.stdout):
        return None
    sector = parse_patch_sector(result.stdout)
    if sector is None or not is_full_dump(HERE / PATCHED):
        raise WizardStop("Unexpected output from gn30_patch.py. Nothing was written to the unit.")
    print(f"\nPatched image written to {PATCHED}.")
    return sector


def step_flash(python: Path, port: str, current: str, sector: Tuple[int, int]) -> None:
    heading(7, "Flash the one changed sector")
    first, last = sector
    sector_arg = f"0x{first:X}"
    print(f"""This rewrites ONLY sector 0x{first:06X}-0x{last:06X}. bp3_bdm.py will first:
  - check that {PATCHED} differs from {current} only inside that sector;
  - compare the bootloader and the sector on the chip with {current};
  - read the flash chip ID;
  - and then ask you to type ERASE.
The erase and programming run on the GN30's own CPU; the result is verified.
Do NOT power off the GN30 or unplug anything while it runs (a few minutes).
If it does get interrupted, the bootloader is never touched, so it can be redone.""")
    if not ask_yes_no("\nFlash the patched sector now?", False):
        raise WizardStop("Stopped before flashing. Nothing was written. Run again any time; "
                         "your dumps will be offered for reuse.")
    resume = False
    while True:
        args = [BP3, "--port", port, "flash", PATCHED, "--sector", sector_arg, "--current", current]
        result = run_tool(python, args + (["--resume"] if resume else []))
        if result.returncode == 0:
            print("\nDouble-checking: reading the sector back and comparing it with the patched image ...")
            check = run_tool(python, [BP3, "--port", port, "verify", PATCHED,
                                      "--start", sector_arg, "--length", f"0x{last - first + 1:X}"])
            if check.returncode == 0:
                print("\nFlashing done and verified.")
                return
            print("\nThe sector does not contain the patched data (did you answer something other than ERASE?).")
        else:
            print("\nFlashing stopped with an error (see above).")
        if not ask_yes_no("Try flashing again?", True):
            raise WizardStop(f"Stopped: sector 0x{first:06X} is not flashed. If an erase had started, the unit "
                             "stays in SOFTWARE UPDATE mode until this is finished. Run the wizard again "
                             "and answer yes to resuming.")
        resume = ask_yes_no("Did the last attempt get as far as erasing or programming "
                            "(you typed ERASE)? Resume it?", True)


def step_full_verify(python: Path, port: str) -> None:
    heading(8, "Optional full verify, then restart")
    print("The flashed sector has been verified already. A full read-back compares the")
    print("whole chip with the patched image as well; it takes about 40 minutes.")
    if not ask_yes_no("Run the full verify?", False):
        return
    result = run_tool(python, [BP3, "--port", port, "verify", PATCHED])
    if result.returncode != 0:
        print("\nWARNING: the full verify did not pass. Do not rely on the unit; keep the setup connected")
        print("and look at the differences listed above before doing anything else.")


def print_finish(current: str, flashed: bool) -> None:
    print("\n" + "=" * 72)
    if flashed:
        print(" All done.")
    else:
        print(" This unit's firmware is already patched: nothing to flash.")
    print("=" * 72)
    print(f"""
1. Power off the GN30 and disconnect the Bus Pirate from the header.
2. Power the GN30 back on with the antenna connected and a clear view of the sky.
3. After a cold start it needs the full almanac before it shows the right date:
   allow 15-20 minutes.

Keep {current} somewhere safe: it is the backup of your unit's firmware.""")
    if flashed:
        print("The sram_backup_*.bin file(s) are only needed if bp3_bdm.py reported an SRAM restore problem.")
    print("Do not rely on the unit for navigation until you have seen correct dates and positions.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="Bus Pirate serial port, e.g. /dev/ttyUSB0 or COM5 (asked for if omitted)")
    args = parser.parse_args()
    if sys.version_info < (3, 8):
        print("Python 3.8 or newer is needed.")
        return 1

    print("GN30 MKII GPS date fix - guided procedure")
    print("Answers in [brackets] are the defaults: just press Enter to accept them.")
    try:
        confirm_risk_understood()
        python = step_python_env()
        step_hardware_checklist()
        port = step_choose_port(python, args.port)
        step_check_link(python, port)
        current = step_read_flash(python, port)
        sector = step_patch(python, current)
        if sector is None:
            print_finish(current, flashed=False)
            return 0
        step_flash(python, port, current, sector)
        step_full_verify(python, port)
        print_finish(current, flashed=True)
        return 0
    except WizardStop as stop:
        print(f"\nSTOPPED: {stop}")
    except (KeyboardInterrupt, EOFError):
        print("\n\nStopped. Run the wizard again to carry on; finished dumps are offered for reuse.")
        print("If this happened while flashing, answer yes when asked whether to resume.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
