# Simrad / Shipmate GN30 MKII: GPS week-rollover fix

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/R5O720EQBI)

The Simrad (Shipmate) GN30 MKII GPS navigator has shown the wrong date since **April 2021**. After a cold start it reports a date 1024 weeks (about 19.6 years) in the past: in October 2026 it shows **March 2007**. The date can be set by hand, but the GPS overwrites it as soon as it decodes satellite data.

This project fixes that by changing 3 bytes of the unit's firmware.

> ## ⚠️ Warning: read before you do anything
>
> - **You do this entirely at your own risk.** The tools come "as is", with **no warranty**, and the author accepts **no liability** for any damage, loss or injury.
> - **You can permanently break your unit.** The procedure means opening it, connecting to its circuit board and rewriting part of its firmware.
> - **Never rely on a modified unit as your only means of navigation.** The modified firmware is not tested, approved or supported by the manufacturer or any authority. It was tested on **one** unit.
> - Modifying the unit may void its warranty, its approvals and your insurance cover.
> - **No firmware is included.** You read it from your own unit. Do not share firmware dumps. Making sure this is lawful where you live is your responsibility.
> - This is an independent hobby project, **not affiliated with or endorsed by** Simrad, Shipmate or any other company.
>
> The full terms are in [DISCLAIMER.md](DISCLAIMER.md) and [LICENSE](LICENSE). **By using this project you accept them.**

## What's in the repository

| File | Purpose |
|---|---|
| `gn30_wizard.py` | **Start here.** Guided walk-through of the whole procedure. It runs the two tools below for you, with extra checks. |
| `gn30_patch.py` | Analyses **your own** firmware dump and applies the date fix. No dependencies. |
| `bp3_bdm.py` | Reads, flashes and verifies the GN30's flash over the CPU's debug port (BDM), using a **Bus Pirate v3**. |
| `flashprog.s` | Source of the small flash-programming routine that `bp3_bdm.py` loads into the GN30's RAM. It is already built into `bp3_bdm.py`; it's included for review. |
| `venv.sh` | Linux/macOS helper: creates and activates a Python venv with `pyserial`. |
| `venv.ps1` | Windows (PowerShell) helper: creates and activates a Python venv with `pyserial`. |
| `requirements.txt` | `pyserial` |
| `test_gn30_wizard.py` | Unit tests for the wizard: `python -m unittest test_gn30_wizard`. |
| `LICENSE` | GNU General Public License v3. |
| `DISCLAIMER.md` | The full disclaimer. Read it first. |

> **No firmware is included or distributed.** The firmware is the copyrighted code of its owner. You read it from your own unit, patch it locally and write it back to the same unit. Do not upload dumps (`*.bin`) to this repository, to issues or anywhere else. The `.gitignore` blocks them.

---

## How the fix works

GPS satellites broadcast the week number as only **10 bits** (0–1023), so it rolls over every 1024 weeks (1999, 2019, 2038 …). Receivers have to decide which 1024-week "era" they are in. The GN30 firmware does:

```
week10 = subframe1_week & 0x3FF
if week10 < 107:  week = week10 + 2048
else:             week = week10 + 1024
```

That only gives correct dates from **2001-09-09 to 2021-04-24**. The patch shifts both branches up by one era:

```
if week10 < 107:  week = week10 + 3072
else:             week = week10 + 2048
```

That gives correct dates from **2021-04-25 to 2040-12-08**.

In 68000 code, that is three bytes:

| Instruction | Original | Patched |
|---|---|---|
| `bge.b` → `bcc.b` (same result here, see below) | `6C 08` | `64 08` |
| `addi.w #$0800` → `#$0C00` (+2048 → +3072) | `08` | `0C` |
| `addi.w #$0400` → `#$0800` (+1024 → +2048) | `04` | `08` |

**Why change the branch?** The bootloader only starts the firmware if four byte-lane sums over `0x4000–0xFFFFF` spell `"SHIP"`; otherwise it stays in *SOFTWARE UPDATE* mode. The two constant changes add +8 to one byte lane. Changing `bge` to `bcc` (signed ≥ to unsigned ≥) subtracts 8 from the same lane. Both compare a value already masked to 0–1023 against 107, so they behave identically. Net result: the checksum is unchanged and no other byte needs touching.

On the tested firmware the code is at `0x0C2750` and the patch lies in flash sector `0x0C0000–0x0CFFFF`. `gn30_patch.py` doesn't rely on that address. It finds the instruction sequence by pattern, so other firmware versions with the same logic work too.

---

## What you need

- A **Bus Pirate v3** (v3.x hardware with the standard firmware; binary bit-bang mode is used).
- Jumper wires and soldered header pins, or pogo pins, for the 8-pad header on the GN30 board.
- A Linux, macOS or Windows PC with Python 3.8+.
- Time. A full 1 MB read takes roughly 40 minutes over the Bus Pirate.

## Wiring

The GN30 MKII main board has an unpopulated **8-pad header** wired to the MC68341 CPU's debug (BDM) pins.

| Header pad | MC68341 signal | Bus Pirate v3 |
|---|---|---|
| 1 | BKPT / DSCLK | CLK |
| 2 | FREEZE | AUX |
| 3 | IFETCH / DSI | MOSI |
| 4 | IPIPE / DSO | MISO |
| 5 | — | not connected |
| 6 | GND | GND |
| 7 | RESET | CS |
| 8 | +5 V | Vpu |

<!-- TODO: add a photo of the header with pad 1 marked -->

- **Check each pad with a meter before connecting.** With the unit powered, pad 8 should read 5 V and pad 6 ground. With it unpowered, check continuity from pads 1–4 and 7 to the CPU pins.
- **Do not connect the Bus Pirate's +3V3 or +5V supply pins.** The GN30 is powered from its own supply.
- The Bus Pirate drives 3.3 V highs into a 5 V CPU. That is within spec (the 68341 needs only 2.0 V for a high), and its pins tolerate 5 V.
- Plug the Bus Pirate into USB **before** powering up the GN30.

## Quick start: the guided wizard

Once the wiring is done, the wizard takes you through everything else:

```bash
python3 gn30_wizard.py          # Windows: py gn30_wizard.py
```

It needs only Python 3.8+; it creates the `.venv` and installs `pyserial` itself. It then:

1. shows the disclaimer and asks you to type `I UNDERSTAND`;
2. goes through the wiring checklist and finds the Bus Pirate's serial port;
3. checks the debug link (`pins`, then `probe`);
4. reads the flash twice and compares the reads (it never overwrites an existing dump);
5. patches the image and works out which sector to flash;
6. flashes that sector (you still type `ERASE`), then reads it back as a separate check;
7. offers the optional full verify and tells you how to restart the unit.

You can stop it with Ctrl+C at any point and run it again: finished dumps are offered for reuse.

The rest of this README describes the same procedure done by hand.

## Setup

Linux/macOS:

```bash
source ./venv.sh          # first run creates .venv and installs pyserial
```

Windows (PowerShell):

```powershell
. .\venv.ps1              # note the leading dot; first run creates .venv and installs pyserial
```

If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. Use `--port COM5` (or similar, see Device Manager) instead of `/dev/ttyUSB0`.

On Linux, add yourself to the `dialout` group if `/dev/ttyUSB0` gives *permission denied*.

**Windows: lower the USB latency timer.** The Bus Pirate's USB-serial chip (FTDI) has a latency timer that defaults to 16 ms on Windows, which can make dumps much slower. Set it to 1 ms: Device Manager → Ports (COM & LPT) → the Bus Pirate's COM port → Properties → Port Settings → Advanced → Latency Timer (msec). Do this before the dump steps below.

> If you edit the scripts on Windows, save them with **LF** line endings (Notepad++: *Edit → EOL Conversion → Unix*). CRLF breaks `venv.sh`. The `.gitattributes` file keeps `venv.sh` as LF when you clone with git.

---

## Procedure

The commands below are written for Linux/macOS. On Windows, with the venv active, use `python` instead of `python3` and your `COM` port instead of `/dev/ttyUSB0`.

### 1. Check the connection

```bash
python3 bp3_bdm.py --port /dev/ttyUSB0 pins     # line levels only, doesn't touch the CPU
python3 bp3_bdm.py --port /dev/ttyUSB0 probe    # stops the CPU in debug mode, reads a few registers
```

`probe` should report the reset vectors `SSP=0x00180000 PC=0x00000D78`. If it says *FREEZE is low: CPU did not enter background mode*, check the wiring, especially pads 1, 2 and 7.

### 2. Read the flash twice and compare

```bash
python3 bp3_bdm.py --port /dev/ttyUSB0 dump gn30_a.bin
python3 bp3_bdm.py --port /dev/ttyUSB0 dump gn30_b.bin
sha256sum gn30_a.bin gn30_b.bin                    # must be identical
```

On Windows, compare with `Get-FileHash gn30_a.bin, gn30_b.bin` instead of `sha256sum`.

Keep this dump safe. It is your only backup of the original firmware. Keep it private: do not share or upload it.

### 3. Analyse and patch

```bash
python3 gn30_patch.py gn30_a.bin                    # analyse only
python3 gn30_patch.py gn30_a.bin -o gn30_patched.bin
```

The script checks:
- the size is exactly 1 MB;
- the bootloader checksum spells `SHIP`, which catches bad reads;
- the week-decode code is found **exactly once**;
- the constants are the expected original ones.

It then shows the date range the unit currently handles, what today's date displays as, the 3 bytes it changes, that the checksum is unchanged, which sector to flash, and the exact flash command. It refuses to patch anything it doesn't recognise. If the image is already patched, it says so and does nothing.

If your dump's SHA-256 matches the image the patch was tested on, it says so. A different hash is fine: it just means a different version or unit, patched by pattern match.

### 4. Flash the one changed sector

```bash
python3 bp3_bdm.py --port /dev/ttyUSB0 flash gn30_patched.bin --sector 0xC0000 --current gn30_a.bin
```

Use the sector that `gn30_patch.py` printed. Before erasing anything, `flash`:
- checks that the new image differs from `--current` **only inside that sector**, and that its checksum spells `SHIP`;
- refuses to touch the bootloader sector;
- re-reads the bootloader area and the target sector from the chip and compares them with `--current`;
- reads the flash chip ID (Fujitsu MBM29F800BA expected);
- asks you to type `ERASE`.

Then:
- **Erase and programming run on the GN30's own CPU.** A small routine is loaded into its RAM. The debug link never sends flash command sequences itself, so a glitch on the wires is not turned into a stray flash write.
- The sector is verified after programming, and re-programmed up to 3 times if needed.
- The part of the GN30's battery-backed RAM used as a work area is saved to `sram_backup_<date>.bin` first and restored afterwards.

If it is interrupted, run the same command again with `--resume`. The tools never write to the bootloader sector, so the unit should stay recoverable over the debug port. That is the design intent, not a guarantee.

### 5. Optional full verify

```bash
python3 bp3_bdm.py --port /dev/ttyUSB0 verify gn30_patched.bin
```

This reads the whole chip back and compares (~40 min). It's not needed: `flash` already verifies the sector it wrote.

### 6. Restart

Disconnect the Bus Pirate (or run `python3 bp3_bdm.py --port /dev/ttyUSB0 run`) and power-cycle the unit. After a cold start the GPS needs the full almanac before it shows the corrected date. That takes **15–20 minutes** with a clear sky view.

---

## Notes

- **Backup battery:** the GN30 keeps its settings and almanac in battery-backed RAM, powered by a **CR2450** cell (3-pin tabbed version on the author's unit). If the cell is flat, every power-up is a cold start. Replacing it is worth doing at the same time.
- **Flash layout (tested firmware):** bootloader at `0x0000–0x3FFF`; application `0x4000–~0xD3FFF`; data to `~0xF2FD6`; the rest erased. Vector table at `0x4000`.
- **Rebuilding the flash routine:** `m68k-linux-gnu-as -mcpu32 flashprog.s -o flashprog.o && m68k-linux-gnu-ld -Ttext=0x177000 flashprog.o -o flashprog.elf && m68k-linux-gnu-objcopy -O binary flashprog.elf flashprog.bin`. The 168 bytes must match `ROUTINE` in `bp3_bdm.py`.
- **Commands:** `python3 bp3_bdm.py --help` and `python3 bp3_bdm.py <command> --help` list all commands and options (`chipid`, `restore-sram`, `--poweron`, `--batch`, …).
- **Tested on:** one GN30 MKII, bootloader build `20824_1999-08-16_12:46:10`, flashed successfully with a Bus Pirate v3 (`VERIFY OK`).

## Support

If this saved your GN30 and you'd like to say thanks:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/R5O720EQBI)

## Licence

Copyright (c) 2026 rodavrow.

This program is free software: you can redistribute it and/or modify it under the terms of the [GNU General Public License](LICENSE) as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

In practice: you may use, copy, modify and share these tools, but if you distribute them or anything built on them, you must do so under the same licence, with the source code and this copyright notice.

The licence covers the tools and documentation in this repository only. It grants no rights in the GN30's firmware, which belongs to its copyright holder and is not part of this project.

## Legal / disclaimer

**The full disclaimer is in [DISCLAIMER.md](DISCLAIMER.md). By using this project you accept it.** In short:

- **No warranty, no liability.** Everything here is provided "as is". Opening the unit and flashing its firmware is entirely at your own risk. The tools include many safety checks, but they can fail, and you can end up with a unit that no longer works.
- **Navigation safety.** The modified firmware is not tested, certified or approved by the manufacturer or any authority, and was tested on one unit only. **Never rely on a modified unit as your only means of navigation.** Check its date, time and position against a trusted source.
- **Approvals and insurance.** Modifying navigation equipment may void its warranty and type approval, and may affect your insurance or your vessel's compliance with carriage rules. Checking that is your responsibility.
- **Firmware and copyright.** This repository contains only original tools and a description of a 3-byte change. It contains no manufacturer firmware. You patch firmware you have read from a unit you own, and you do not share it. Laws on analysing, modifying and repairing your own devices vary by country; making sure what you do is lawful is your responsibility.
- **Trademarks.** Simrad, Shipmate and Bus Pirate are trademarks of their respective owners, used here only to identify the equipment. This project is not affiliated with, endorsed by or supported by them.
- **No advice.** Nothing here is professional, legal or navigational advice.
