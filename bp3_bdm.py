#!/usr/bin/env python3
"""
bp3_bdm.py - CPU32 BDM (MC68341) over a Bus Pirate v3 in binary bit-bang mode.

Wiring (GN30 8-pad header -> Bus Pirate v3):
    pad 1  BKPT/DSCLK  -> CLK
    pad 3  IFETCH/DSI  -> MOSI
    pad 4  IPIPE/DSO   -> MISO
    pad 7  RESET       -> CS
    pad 2  FREEZE      -> AUX
    pad 6  GND         -> GND
    pad 8  +5 V        -> Vpu   (pad 5 not used)
    (Do NOT connect the Bus Pirate's +3V3 or +5V supply pins.)

Line drive (Bus Pirate v3 firmware quirk taken into account):
  * BKPT/DSCLK and DSI are driven push-pull at 3.3 V / 0 V. The 68341's inputs
    need only VIH = 2.0 V (MC68341 UM 12.5), so 3.3 V is a valid high.
    DSI is only enabled as an output once the CPU is in BDM: before that the pin
    is IFETCH, a CPU output.
  * RESET is open-drain: driven low, or released (CPU/board pull-up raises it).
  * The v3 BBIO firmware sets pins with read-modify-write on PORTB, so a pin's
    output latch can pick up the level it was reading as an input. The script
    therefore switches a pin to output first, then sets and READS BACK its level,
    retrying until it matches.
  Plug the Bus Pirate into USB BEFORE powering the GN30.

Usage:
    python3 bp3_bdm.py --port /dev/ttyUSB0 probe
    python3 bp3_bdm.py --port /dev/ttyUSB0 dump gn30_a.bin
    python3 bp3_bdm.py --port COM5 dump gn30_b.bin
    python3 bp3_bdm.py --port /dev/ttyUSB0 run        # release the CPU (or just power-cycle)
    python3 bp3_bdm.py --port /dev/ttyUSB0 chipid     # read-only flash ID check
    python3 bp3_bdm.py --port /dev/ttyUSB0 flash gn30_patched.bin --sector 0xC0000 --current gn30_a.bin
    python3 bp3_bdm.py --port /dev/ttyUSB0 verify gn30_a.bin --skip-sector 0xC0000
    python3 bp3_bdm.py --port /dev/ttyUSB0 restore-sram sram_backup_YYYYMMDD-HHMMSS.bin
Flash erase/program run ON the GN30's CPU (routine in SRAM); BDM never writes to flash.

Command encodings are from the MC68341 User's Manual, section 5.6.2.8.
Requires: pip install pyserial
"""
import argparse
import sys
import time

# Bus Pirate BBIO pin bits (same layout for config, set and the reply byte)
CS, MISO, CLK, MOSI, AUX = 0x01, 0x02, 0x04, 0x08, 0x10
ALL = CS | MISO | CLK | MOSI | AUX
PULLUP = 0x20

# Signal mapping
RESET, DSO, DSCLK, DSI, FREEZE = CS, MISO, CLK, MOSI, AUX

# CPU32 BDM commands (MC68341 UM 5.6.2.8)
NOP = 0x0000
GO = 0x0C00
RSREG = 0x2580
WSREG = 0x2480
READ_W = 0x1940
READ_L = 0x1980
DUMP_W = 0x1D40
WRITE_W = 0x1840
WRITE_L = 0x1880
FILL_W = 0x1C40
WREG = 0x2080          # WDREG/WAREG: | 0-7 = D0-D7, 8-15 = A0-A7
RREG = 0x2180          # RDREG/RAREG
SYSREGS = {"RPC": 0x0, "PCC": 0x1, "ATEMP": 0x8, "FAR": 0x9, "VBR": 0xA,
           "SR": 0xB, "USP": 0xC, "SSP": 0xD, "SFC": 0xE, "DFC": 0xF}

ST_NOT_READY = (1, 0x0000)
ST_BERR = (1, 0x0001)
ST_ILLEGAL = (1, 0xFFFF)
ST_COMPLETE = (0, 0xFFFF)


class BDMError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


class BusPirateBDM:
    def __init__(self, port, batch=8, verbose=False):
        import serial  # pyserial
        log("Opening %s ..." % port)
        self.ser = serial.Serial(port, 115200, timeout=1, write_timeout=2)
        self.batch = batch
        self.verbose = verbose
        self.out_mask = 0       # which of RESET/DSCLK/DSI are outputs
        self.repeat = 1         # send each pin-state byte this many times
        self._enter_bbio()
        self.release_all()
        self._cmd(0x80 | PULLUP)            # pull-ups on (to Vpu), PSU off

    # ---------- Bus Pirate low level ----------
    def _enter_bbio(self):
        s = self.ser
        log("Talking to Bus Pirate on %s ..." % s.port)
        s.reset_input_buffer()
        s.write(b"\n" * 10 + b"#\n")        # leave any menu, reset the BP
        time.sleep(1.0)
        banner = s.read(s.in_waiting or 0)
        if banner:
            log("  BP replied: %r" % banner[-120:])
        else:
            log("  (no text reply to reset - continuing)")
        s.reset_input_buffer()
        got = b""
        for i in range(25):
            s.write(b"\x00")
            time.sleep(0.05)
            got += s.read(s.in_waiting or 0)
            if b"BBIO1" in got:
                s.reset_input_buffer()
                log("  Binary bit-bang mode OK (BBIO1)")
                return
        raise BDMError("Bus Pirate did not enter binary bit-bang mode (no 'BBIO1'); got %r. "
                       "Check the port, that nothing else (screen/minicom) has it open, "
                       "and that the BP terminal is at 115200 baud." % got[-60:])

    def _cmd(self, b):
        self.ser.write(bytes([b]))
        r = self.ser.read(1)
        if len(r) != 1:
            raise BDMError("No reply from Bus Pirate")
        return r[0]

    @staticmethod
    def _dir_byte(out_mask):
        return 0x40 | (ALL & ~(out_mask & (RESET | DSCLK | DSI)))

    @staticmethod
    def _set_byte(high_mask):
        # CS/RESET latch is always 0 (open-drain low when it is an output).
        return 0x80 | PULLUP | (high_mask & (DSCLK | DSI))

    def _dir(self):
        return self._cmd(self._dir_byte(self.out_mask))

    def pins(self):
        return self._dir()

    def release_all(self):
        self.out_mask = 0
        return self._dir()

    def set_outputs(self, high_mask):
        """Set DSCLK/DSI latches (for pins that are outputs) and verify by readback."""
        want_mask = self.out_mask & (DSCLK | DSI)
        for _ in range(8):
            p = self._cmd(self._set_byte(high_mask))
            if (p & want_mask) == (high_mask & want_mask) and not (
                    self.out_mask & RESET and p & RESET):
                return p
        raise BDMError("Pin readback mismatch after setting %s (read %s)" % (
            fmt_pins(high_mask), fmt_pins(p)))

    def make_output(self, mask, high_mask=0):
        """Switch pins to outputs, then force their latches and verify."""
        self.out_mask |= mask
        self._dir()
        return self.set_outputs(high_mask)

    def make_input(self, mask):
        self.out_mask &= ~mask
        return self._dir()

    def close(self):
        try:
            self.release_all()
            self.ser.write(b"\x0F")          # back to BP terminal mode
        finally:
            self.ser.close()

    # ---------- BDM serial transfers ----------
    def _bits(self, word):
        out = bytearray()
        for i in range(16, -1, -1):          # 17 bits, MSB first; bit16 (S/C) = 0
            d = DSI if (i < 16 and (word >> i) & 1) else 0
            out += bytes([self._set_byte(d)]) * self.repeat           # DSCLK low, present DSI
            out += bytes([self._set_byte(DSCLK | d)]) * self.repeat   # rising edge: target latches
        return out

    def transfers(self, words):
        """Send 17-bit words; return list of (status, data) clocked out during each."""
        res = []
        per_word = 34 * self.repeat
        for k in range(0, len(words), self.batch):
            chunk = words[k:k + self.batch]
            payload = b"".join(self._bits(w) for w in chunk)
            self.ser.write(payload)
            reply = self.ser.read(len(payload))
            if len(reply) != len(payload):
                raise BDMError("Short read from Bus Pirate (%d/%d)" % (len(reply), len(payload)))
            if any(not (b & FREEZE) for b in reply):
                # CPU left background mode (e.g. a link error decoded as GO): stop driving DSI now
                self.make_input(DSI)
                raise BDMError("CPU left background mode during a transfer")
            bad = 0
            for i in range(len(payload)):
                exp = payload[i] & (DSCLK | DSI)
                if i % self.repeat == self.repeat - 1 and (reply[i] & (DSCLK | DSI)) != exp:
                    bad += 1
            if bad:
                if self.repeat == 1:
                    self.repeat = 2
                    log("\n  Pin readback mismatch - switching to repeated pin writes (slower).")
                raise BDMError("Pin readback mismatch on %d edges" % bad)
            for j in range(len(chunk)):
                r = reply[j * per_word:(j + 1) * per_word]
                v = 0
                for b in range(17):
                    # last copy of the DSCLK-low byte: sample DSO before the rising edge
                    v = (v << 1) | (1 if r[(2 * b + 1) * self.repeat - 1] & DSO else 0)
                res.append((v >> 16, v & 0xFFFF))
        return res

    def xfer(self, word):
        return self.transfers([word])[0]

    # ---------- BDM operations ----------
    def reset_halt(self, poweron=False, quiet=False):
        """Enter BDM: BKPT must be low at the rising edge of RESET (UM 5.6.2.1).
        poweron=False: pulse RESET from the Bus Pirate.
        poweron=True : hold BKPT low and let the user power-cycle the GN30, so the
                       CPU's own power-on reset provides the edge."""
        say = (lambda m: None) if quiet else log
        self._halted_ok = False
        say("Pins before: %s" % fmt_pins(self.release_all()))
        p = self.make_output(DSCLK, 0)                   # BKPT low
        say("BKPT driven low: %s" % fmt_pins(p))
        if poweron:
            log(">>> BKPT is held low. Switch the GN30 OFF, wait 3 s, switch it ON.")
            log(">>> Waiting up to 60 s for FREEZE ...")
            t0 = time.time()
            seen_low = False
            while time.time() - t0 < 60:
                p = self.pins()
                if not (p & RESET):
                    seen_low = True
                if seen_low and (p & RESET) and (p & FREEZE):
                    break
                time.sleep(0.02)
            else:
                p = self.pins()
                log("Pins now: %s  (RESET went low during power-up: %s)" % (fmt_pins(p), seen_low))
                self.release_all()
                raise BDMError("No FREEZE after power-up with BKPT low.")
        else:
            say("Asserting RESET with BKPT low ...")
            self.out_mask |= RESET
            self._dir()
            time.sleep(0.2)
            p = self.pins()
            say("Pins while held:   %s   (expect RESET=0 BKPT=0)" % fmt_pins(p))
            if p & RESET:
                self.release_all()
                raise BDMError("RESET did not go low when pulled - something on the board "
                               "drives it high (reset supervisor). Use: probe --poweron")
            if p & DSCLK:
                self.release_all()
                raise BDMError("BKPT did not go low - check pad 1 -> CLK.")
            self.make_input(RESET)                       # release RESET, BKPT still low
            t0 = time.time()
            while not (self.pins() & RESET):             # wait for the CPU to release RESET too
                if time.time() - t0 > 2:
                    self.release_all()
                    raise BDMError("RESET line stays low - check pad 7 wiring / pull-up")
                time.sleep(0.01)
            time.sleep(0.05)
        p = self.pins()
        say("Pins after reset:  %s   (expect FREEZE=1)" % fmt_pins(p))
        if not (p & FREEZE):
            self.release_all()
            raise BDMError("FREEZE is low: CPU did not enter background mode.")
        say("CPU halted in background mode.")
        self._halted_ok = True
        # In BDM, IFETCH becomes DSI (an input): now it is safe to drive it.
        # BKPT/DSCLK stays low: releasing it now would count as the first DSCLK edge
        # (UM 5.6.2.7.2). The first transfer's first rising edge is bit 16.
        self.make_output(DSI, 0)
        # Supervisor data space for memory accesses (SFC = DFC = 5)
        self.write_sysreg("SFC", 5)
        self.write_sysreg("DFC", 5)

    def read_sysreg(self, name):
        r = self.transfers([RSREG | SYSREGS[name], NOP, NOP])
        self._check(r[1], "RSREG %s (MS)" % name)
        self._check(r[2], "RSREG %s (LS)" % name)
        return (r[1][1] << 16) | r[2][1]

    def write_sysreg(self, name, value):
        r = self.transfers([WSREG | SYSREGS[name], value >> 16, value & 0xFFFF, NOP])
        if r[3] != ST_COMPLETE:
            raise BDMError("WSREG %s failed: %r" % (name, r[3]))

    def read_word(self, addr):
        r = self.transfers([READ_W, addr >> 16, addr & 0xFFFF, NOP])
        st = r[3]
        tries = 0
        while st == ST_NOT_READY and tries < 10:
            st = self.xfer(NOP)
            tries += 1
        self._check(st, "READ.W 0x%06X" % addr)
        return st[1]

    def read_block(self, addr, nwords):
        """READ.W then DUMP.W; each transfer clocks out the previous result."""
        words = [READ_W, addr >> 16, addr & 0xFFFF] + [DUMP_W] * (nwords - 1) + [NOP]
        r = self.transfers(words)
        data = r[3:]
        out = bytearray()
        for i, st in enumerate(data):
            self._check(st, "DUMP at 0x%06X" % (addr + 2 * i))
            out += st[1].to_bytes(2, "big")
        return bytes(out)

    def resync(self):
        """Recover from a BDM link error: re-enter background mode via RESET+BKPT and
        redo the chip-select setup / PC parking. Retries itself until it succeeds."""
        for t in range(20):
            self.resyncs = getattr(self, "resyncs", 0) + 1
            if self.resyncs > 500:
                raise BDMError("Too many BDM link errors - check the leads/connections.")
            time.sleep(0.2)
            try:
                self.reset_halt(quiet=True)
                if getattr(self, "prepared", False):
                    self.prepare_target()
                    if getattr(self, "parked", False) and \
                            self.read_block(RAM_CODE, len(ROUTINE) // 2) == ROUTINE:
                        self.park()
                return
            except BDMError:
                continue
        raise BDMError("Could not re-synchronise with the CPU after 20 attempts.")

    def halt(self, poweron=False):
        """Initial entry into BDM; if the follow-up register writes hit a link error,
        re-sync (RESET+BKPT) instead of giving up."""
        try:
            self.reset_halt(poweron=poweron)
        except BDMError as e:
            if poweron or not getattr(self, "_halted_ok", False):
                raise
            log("  link error after halting (%s) - re-syncing" % e)
            self.resync()

    def robust(self, fn, tries=10):
        """Run fn(); on a BDM link error re-sync and try again."""
        for t in range(tries):
            try:
                return fn()
            except BDMError:
                if t == tries - 1:
                    raise
                self.resync()

    def read_word_retry(self, addr, tries=5):
        for t in range(tries):
            try:
                return self.read_word(addr)
            except BDMError:
                if t == tries - 1:
                    raise
                self.resync()

    def write_words(self, pairs):
        """WRITE.W each (addr, value). The transfer after each WRITE's data word must
        return 'command complete' (UM 5.6.2.8.9)."""
        words = []
        for a, v in pairs:
            words += [WRITE_W, (a >> 16) & 0xFFFF, a & 0xFFFF, v & 0xFFFF]
        words.append(NOP)
        r = self.transfers(words)
        for k, (a, v) in enumerate(pairs):
            st = r[4 * k + 4]
            if st != ST_COMPLETE:
                raise BDMError("WRITE.W 0x%06X <- 0x%04X failed: %r" % (a, v, st))

    def write_long(self, addr, value):
        r = self.transfers([WRITE_L, addr >> 16, addr & 0xFFFF, value >> 16, value & 0xFFFF, NOP])
        if r[5] != ST_COMPLETE:
            raise BDMError("WRITE.L 0x%08X failed: %r" % (addr, r[5]))

    def write_reg(self, idx, value):
        r = self.transfers([WREG | idx, (value >> 16) & 0xFFFF, value & 0xFFFF, NOP])
        if r[3] != ST_COMPLETE:
            raise BDMError("write register %d failed: %r" % (idx, r[3]))

    def read_reg(self, idx):
        r = self.transfers([RREG | idx, NOP, NOP])
        self._check(r[1], "read register %d" % idx)
        self._check(r[2], "read register %d" % idx)
        return (r[1][1] << 16) | r[2][1]

    def fill_words(self, addr, data):
        """Write data (bytes, even length) to RAM with WRITE.W + FILL.W, then read back.
        Retries on link errors or mismatches."""
        words = [(data[i] << 8) | data[i + 1] for i in range(0, len(data), 2)]
        CH = 128
        for k in range(0, len(words), CH):
            chunk = words[k:k + CH]
            a = addr + 2 * k
            for t in range(10):
                try:
                    seq = [WRITE_W, a >> 16, a & 0xFFFF, chunk[0]]
                    for w in chunk[1:]:
                        seq += [FILL_W, w]
                    seq.append(NOP)
                    r = self.transfers(seq)
                    for j in range(len(chunk)):
                        if r[4 + 2 * j] != ST_COMPLETE:
                            raise BDMError("RAM write at 0x%06X: %r" % (a + 2 * j, r[4 + 2 * j]))
                    back = self.read_block(a, len(chunk))
                    want = b"".join(w.to_bytes(2, "big") for w in chunk)
                    if back != want:
                        raise BDMError("RAM read-back mismatch at 0x%06X" % a)
                    break
                except BDMError:
                    if t == 9:
                        raise
                    self.resync()

    def prepare_target(self):
        """Same MBAR / chip-select setup as the GN30 bootloader (0x000D8C-0x000DCA):
        flash on CS0 at 0 (1 MB, 16-bit), SRAM on CS2/CS3 at 0x100000-0x17FFFF."""
        self.write_sysreg("DFC", 7)                       # CPU space for MBAR
        self.write_long(0x0003FF00, 0xFFFFF001)
        self.write_sysreg("DFC", 5)
        for a, v in CS_SETUP:
            self.write_long(a, v)
        for a, v in CS_SETUP:
            got = (self.read_word(a) << 16) | self.read_word(a + 2)
            if got != v:
                raise BDMError("chip-select register 0x%08X reads 0x%08X, expected 0x%08X" % (a, got, v))
        self.prepared = True

    def park(self):
        """Point the PC at the routine's BGND loop, so an accidental GO (link error)
        drops straight back into background mode instead of running other code."""
        self.write_sysreg("RPC", RAM_CODE + EP_HALT)
        self.parked = True

    def go_and_release(self, pc):
        """Set the PC, send GO, and release DSI in the same USB write (DSI becomes the
        CPU's IFETCH output as soon as it leaves background mode)."""
        self.write_sysreg("RPC", pc)
        payload = self._bits(GO)
        self.out_mask &= ~DSI
        payload += bytes([self._dir_byte(self.out_mask)])
        self.ser.write(payload)
        reply = self.ser.read(len(payload))
        if len(reply) != len(payload):
            raise BDMError("Short read from Bus Pirate during GO")

    def wait_bgnd(self, timeout):
        """Wait for FREEZE (routine executed BGND). On timeout force BDM with BKPT."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.pins() & FREEZE:
                self.make_output(DSI, DSCLK)          # DSI is an input again; DSCLK idles high
                return True
            time.sleep(0.05)
        log("  routine did not return within %.0f s - forcing background mode with BKPT" % timeout)
        self.make_output(DSCLK, 0)                     # hold BKPT low until FREEZE (UM 5.6.2.7.2)
        t1 = time.time()
        while time.time() - t1 < 2:
            if self.pins() & FREEZE:
                self.make_output(DSI, 0)
                return False
            time.sleep(0.05)
        raise BDMError("CPU did not stop - power-cycle the GN30.")

    def read_range(self, start, length, label="", quiet=False):
        out = bytearray()
        t0 = time.time()
        while len(out) < length:
            n = min(256, (length - len(out)) // 2)
            for t in range(5):
                try:
                    blk = self.read_block(start + len(out), n)
                    break
                except BDMError:
                    if t == 4:
                        raise
                    self.resync()
            out += blk
            if quiet:
                continue
            el = time.time() - t0
            sys.stdout.write("\r  %sreading 0x%06X  %5.1f%%  ETA %4.0fs " % (
                label, start + len(out), 100.0 * len(out) / length,
                el / len(out) * (length - len(out))))
            sys.stdout.flush()
        if not quiet:
            print()
        return bytes(out)

    def go(self):
        self.xfer(GO)

    @staticmethod
    def _check(st, what):
        if st[0] == 0:
            return
        if st == ST_NOT_READY:
            raise BDMError("%s: CPU not ready" % what)
        if st == ST_BERR:
            raise BDMError("%s: bus error" % what)
        if st == ST_ILLEGAL:
            raise BDMError("%s: illegal command (link out of sync?)" % what)
        raise BDMError("%s: unexpected response %r" % (what, st))


def fmt_pins(p):
    return "RESET=%d BKPT=%d DSI=%d DSO=%d FREEZE=%d" % (
        bool(p & RESET), bool(p & DSCLK), bool(p & DSI), bool(p & DSO), bool(p & FREEZE))


# ---------------------------------------------------------------- commands
def cmd_test(bdm, args):
    """Hold BKPT low so it can be checked with a meter at pad 1."""
    log("Driving BKPT/DSCLK (BP CLK) LOW for %d s - pad 1 should read ~0 V ..." % args.seconds)
    p = bdm.make_output(DSCLK, 0)
    log("  read back: %s" % fmt_pins(p))
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        time.sleep(0.25)
    log("  read back: %s" % fmt_pins(bdm.pins()))
    bdm.release_all()
    log("  released - pad 1 should be back to ~5 V")
    log("(DSI is not tested: it is a CPU output until BDM is entered.)")


def cmd_pins(bdm, args):
    """Read the lines with everything released. Running CPU expected:
    RESET=1 BKPT=1 FREEZE=0, DSI/DSO toggling between reads."""
    for _ in range(10):
        log("  " + fmt_pins(bdm.release_all()))
        time.sleep(0.1)


def cmd_probe(bdm, args):
    bdm.reset_halt(poweron=args.poweron)
    for n in ("RPC", "PCC", "SSP", "SR", "VBR", "SFC", "DFC"):
        print("  %-4s = 0x%08X" % (n, bdm.read_sysreg(n)))
    v = [bdm.read_word(a) for a in (0, 2, 4, 6)]
    ssp, pc = (v[0] << 16) | v[1], (v[2] << 16) | v[3]
    print("Reset vectors at 0x000000: SSP=0x%08X  PC=0x%08X" % (ssp, pc))
    m = [bdm.read_word(0x100000 + a) for a in (0, 2, 4, 6)]
    print("Same words at 0x100000:    %s  (%s)" % (
        " ".join("%04X" % x for x in m),
        "mirror - 1 MB boot device confirmed" if m == v else "no mirror"))
    print("\nIf SSP/PC look sane (even, plausible addresses) the link works. Next: dump.")


def cmd_dump(bdm, args):
    bdm.reset_halt(poweron=args.poweron)
    total = args.length // 2
    block = args.block
    t0 = time.time()
    with open(args.file, "wb") as f:
        done = 0
        while done < total:
            n = min(block, total - done)
            addr = args.start + done * 2
            for attempt in range(3):
                try:
                    f.write(bdm.read_block(addr, n))
                    break
                except BDMError as e:
                    print("\n  retry block 0x%06X: %s" % (addr, e))
                    if attempt == 2:
                        raise
                    bdm.reset_halt(poweron=args.poweron)
            done += n
            el = time.time() - t0
            eta = el / done * (total - done)
            sys.stdout.write("\r  0x%06X / 0x%06X  %5.1f%%  elapsed %4.0fs  ETA %5.0fs" % (
                addr + n * 2, args.start + args.length, 100.0 * done / total, el, eta))
            sys.stdout.flush()
    print("\nWrote %s (%d bytes)." % (args.file, args.length))



# ---------------------------------------------------------------- 29F800 flash
# Fujitsu MBM29F800BA (bottom boot), 16-bit (word) mode on the 68341's CS0.
# All flash command sequences are executed by a small routine running on the
# GN30's own CPU from SRAM, so BDM link errors can never reach the flash.
FL_MANUF = 0x0004                      # Fujitsu
FL_DEVICE = 0x2258                     # 29F800BA (bottom boot), word mode
SECTORS = [(0x00000, 0x4000), (0x04000, 0x2000), (0x06000, 0x2000), (0x08000, 0x8000)] + \
          [(0x10000 * i, 0x10000) for i in range(1, 16)]
# Bootloader chip-select setup (MBAR = 0xFFFFF000): CS0 flash, CS2/CS3 SRAM
CS_SETUP = [(0xFFFFF040, 0x000FFFF1), (0xFFFFF044, 0x00000003),
            (0xFFFFF050, 0x0003FFF1), (0xFFFFF054, 0x00100003),
            (0xFFFFF058, 0x0003FFF1), (0xFFFFF05C, 0x00140003)]
RAM_CODE = 0x177000                    # routine
RAM_BUF = 0x178000                     # 32 KB data buffer (to 0x17FFFF)
RAM_AREA = (0x177000, 0x9000)          # saved before use, restored afterwards
HALF = 0x8000
# flashprog.s: m68k-linux-gnu-as -mcpu32, linked at 0x177000 (ld -Ttext=0x177000).
# Entries: +0x00 program, +0x3E erase, +0x7C id, +0xA2 halt loop (BGND; BRA.W back).
ROUTINE = bytes.fromhex(
    "32180c41ffff672a34bc00aa36bc005534bc00a032813411b44167160802000567f43411b441670a32bc00f07e01600000725489538066c87e006000006634bc00aa36bc005534bc008034bc00aa36bc005532bc003034110c42ffff67180802000567f234110c42ffff670a32bc00f07e026000002e7e006000002834bc00aa36bc005534bc0090760078007a0036380000383800023a29000431fc00f000007e004afa6000fffc")
EP_PROG, EP_ERASE, EP_ID, EP_HALT = 0x00, 0x3E, 0x7C, 0xA2
D0, D3, D4, D5, D7, A0, A1, A2, A3 = 0, 3, 4, 5, 7, 8, 9, 10, 11


def ship_lanes(img):
    """Bootloader check (0x1186): byte-lane sums over 0x4000-0xFFFFF must be 'SHIP'."""
    return bytes(sum(img[0x4000 + k:0x100000:4]) & 0xFF for k in range(4))


def sector_of(addr):
    for base, size in SECTORS:
        if base <= addr < base + size:
            return base, size
    raise BDMError("address 0x%X outside the 1 MB flash" % addr)


def load_image(path):
    with open(path, "rb") as f:
        img = f.read()
    if len(img) != 0x100000:
        raise BDMError("%s is %d bytes, expected 1048576" % (path, len(img)))
    return img


def run_routine(bdm, entry, regs, timeout):
    """Check the routine in RAM, load registers, run it, return (d7, a1, d3, d4, d5)."""
    for t in range(4):
        try:
            if bdm.read_block(RAM_CODE, len(ROUTINE) // 2) != ROUTINE:
                bdm.fill_words(RAM_CODE, ROUTINE)
            for idx, v in list(regs.items()) + [(A2, 0xAAA), (A3, 0x554), (D7, 0xFFFFFFFF)]:
                bdm.write_reg(idx, v)
            for idx, v in regs.items():                    # read back: no surprises
                if bdm.read_reg(idx) != v:
                    raise BDMError("register %d read-back mismatch" % idx)
            break
        except BDMError:
            if t == 3:
                raise
            bdm.resync()
    bdm.go_and_release(RAM_CODE + entry)
    returned = bdm.wait_bgnd(timeout)
    for t in range(4):
        try:
            res = tuple(bdm.read_reg(i) for i in (D7, A1, D3, D4, D5))
            break
        except BDMError:
            if t == 3:
                raise
            bdm.resync()
    if not returned:
        raise BDMError("flash routine did not finish (d7=%08X a1=%08X)" % res[:2])
    bdm.robust(bdm.park)
    return res


class TargetSession:
    """Prepare chip selects, save the SRAM area used, and restore it on exit."""
    def __init__(self, bdm, poweron=False, save_to=None, area=None):
        self.bdm = bdm; self.poweron = poweron; self.save_to = save_to; self.saved = None
        self.area = area or RAM_AREA          # (start, length) of SRAM saved/restored

    def __enter__(self):
        b = self.bdm
        b.halt(poweron=self.poweron)
        b.robust(b.prepare_target)
        area0, alen = self.area
        log("Chip selects set up (as the bootloader does). Saving SRAM 0x%06X-0x%06X ..." % (
            area0, area0 + alen - 1))
        # Save the small code area first, load the routine and park the PC on its BGND
        # loop as early as possible, then save the rest of the area.
        code_len = len(ROUTINE)
        saved_code = b.read_range(RAM_CODE, code_len, quiet=True)
        b.fill_words(RAM_CODE, ROUTINE)
        b.robust(b.park)
        rest = b.read_range(area0 + code_len, alen - code_len, "SRAM save ") if alen > code_len else b""
        self.saved = saved_code + rest
        if self.save_to:
            with open(self.save_to, "wb") as f:
                f.write(self.saved)
            log("  saved to %s" % self.save_to)
        return self

    def __exit__(self, et, ev, tb):
        if self.saved is None:
            return False
        b = self.bdm
        code_len = len(ROUTINE)
        try:
            log("Restoring SRAM 0x%06X-0x%06X ..." % (self.area[0], self.area[0] + self.area[1] - 1))
            # 1. everything except the routine, while the PC is still parked on its BGND loop
            if self.area[1] > code_len:
                b.fill_words(self.area[0] + code_len, self.saved[code_len:])
            # 2. un-park (PC -> the bootloader's normal start, harmless), then the routine area
            b.parked = False
            b.robust(lambda: b.write_sysreg("RPC", 0x00000D78))
            for attempt in range(5):
                b.fill_words(self.area[0], self.saved[:code_len])
                got = b.read_range(self.area[0], self.area[1], quiet=True)
                if got == self.saved:
                    break
                # put back any words that a link error disturbed, then compare again
                for i in range(0, self.area[1], 256):
                    if got[i:i + 256] != self.saved[i:i + 256]:
                        b.fill_words(self.area[0] + i, self.saved[i:i + 256])
            else:
                raise BDMError("final SRAM comparison failed")
            log("  SRAM restored and verified.")
        except Exception as e:
            log("  WARNING: SRAM restore failed (%s). Re-run with: restore-sram %s" % (e, self.save_to))
        return False


def check_id(bdm, sector_base):
    d7, a1, manuf, dev, prot = run_routine(bdm, EP_ID, {A1: sector_base}, timeout=5)
    manuf &= 0xFFFF; dev &= 0xFFFF; prot &= 0xFFFF
    log("  manufacturer ID:  0x%04X  (expect 0x%04X Fujitsu)" % (manuf, FL_MANUF))
    log("  device ID:        0x%04X  (expect 0x%04X 29F800BA)" % (dev, FL_DEVICE))
    log("  sector 0x%06X protect status: 0x%04X  (expect 0x0000)" % (sector_base, prot))
    if (manuf & 0xFF) != FL_MANUF or dev != FL_DEVICE or (prot & 1):
        raise BDMError("Unexpected flash ID / protection status - not continuing. Nothing was changed.")
    if bdm.read_word_retry(0x0) != 0x0018:
        raise BDMError("Flash did not return to read mode after ID.")


def program_sector(bdm, base, size, want_words):
    """want_words: dict addr -> word to program (others left as they are).
    Loads 32 KB halves into SRAM and lets the on-target routine program them."""
    for off in range(0, size, HALF):
        n = min(HALF, size - off)
        buf = bytearray(b"\xff" * n)
        count = 0
        for a, v in want_words.items():
            if base + off <= a < base + off + n:
                buf[a - base - off] = v >> 8; buf[a - base - off + 1] = v & 0xFF; count += 1
        if not count:
            continue
        log("  half 0x%06X-0x%06X: %d words - loading SRAM buffer ..." % (base + off, base + off + n - 1, count))
        bdm.fill_words(RAM_BUF, bytes(buf))
        log("  programming (on-target) ...")
        t = time.time()
        d7, a1, _, _, _ = run_routine(bdm, EP_PROG, {A0: RAM_BUF, A1: base + off, D0: n // 2}, timeout=60)
        if d7 != 0:
            raise BDMError("Program failed at 0x%06X (status %d)" % (a1, d7))
        log("    done in %.1f s" % (time.time() - t))


def cmd_chipid(bdm, args):
    with TargetSession(bdm, args.poweron, area=(RAM_CODE, len(ROUTINE))):
        log("Reading flash ID (on-target routine; no erase/program) ...")
        check_id(bdm, args.sector)
        log("Flash ID OK: Fujitsu 29F800BA, sector 0x%06X not protected." % args.sector)


def cmd_flash(bdm, args):
    img = load_image(args.file)
    base, size = sector_of(args.sector)
    if base != args.sector:
        raise BDMError("0x%X is not a sector start (sector is 0x%06X-0x%06X)" % (args.sector, base, base + size - 1))
    if base < 0x4000 and not args.allow_boot:
        raise BDMError("Refusing to touch the bootloader sector without --allow-boot")
    lanes = ship_lanes(img)
    log("Image %s: bootloader checksum lanes = %r %s" % (args.file, lanes, "OK" if lanes == b"SHIP" else "BAD"))
    if lanes != b"SHIP" and not args.ignore_checksum:
        raise BDMError("Image would fail the bootloader 'SHIP' check - not flashing.")
    new = img[base:base + size]
    cur_file = None
    if args.current:
        cur_file = load_image(args.current)
        outside = sum(1 for i in range(0x100000) if not (base <= i < base + size) and img[i] != cur_file[i])
        inside = [i for i in range(base, base + size) if img[i] != cur_file[i]]
        log("Compared with %s: %d byte(s) differ inside the sector, %d outside." % (args.current, len(inside), outside))
        for i in inside[:16]:
            log("    0x%06X: %02X -> %02X" % (i, cur_file[i], img[i]))
        if outside:
            raise BDMError("Image differs from the current dump outside sector 0x%06X - not flashing." % base)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    with TargetSession(bdm, args.poweron, save_to="sram_backup_%s.bin" % stamp):
        if cur_file is not None and not args.skip_boot_check and base != 0:
            log("Checking the bootloader sector 0x000000-0x003FFF against %s ..." % args.current)
            boot = bdm.read_range(0x0, 0x4000, "boot sector ")
            if boot != cur_file[:0x4000]:
                bad = [i for i in range(0, 0x4000, 2) if boot[i:i + 2] != cur_file[i:i + 2]]
                for i in bad[:8]:
                    log("    0x%06X: chip %04X  file %04X" % (i, int.from_bytes(boot[i:i + 2], "big"),
                                                            int.from_bytes(cur_file[i:i + 2], "big")))
                raise BDMError("Bootloader sector differs from %s in %d word(s) - not flashing. "
                               "Stop here and send me this output." % (args.current, len(bad)))
            log("  bootloader sector OK.")
        log("Reading current sector 0x%06X-0x%06X from the chip ..." % (base, base + size - 1))
        chip = bdm.read_range(base, size, "chip ")
        if chip == new:
            log("Sector already contains the image data - nothing to do.")
            return
        if cur_file is not None and chip != cur_file[base:base + size]:
            bad = sum(1 for x, y in zip(chip, cur_file[base:base + size]) if x != y)
            if not args.resume:
                raise BDMError("Chip sector does not match %s (%d bytes differ) - not flashing. If a previous "
                               "flash was interrupted, re-run with --resume." % (args.current, bad))
            log("Chip sector differs from %s in %d bytes (interrupted earlier flash) - resuming." % (args.current, bad))
        todo, needs_erase = {}, False
        for i in range(0, size, 2):
            have = (chip[i] << 8) | chip[i + 1]
            want = (new[i] << 8) | new[i + 1]
            if have != want:
                todo[base + i] = want
                if want & ~have & 0xFFFF:
                    needs_erase = True
        log("Checking flash ID ...")
        check_id(bdm, base)

        if not args.yes:
            if needs_erase:
                log("\nAbout to ERASE sector 0x%06X-0x%06X and program it from %s." % (base, base + size - 1, args.file))
            else:
                log("\nAbout to program the remaining %d word(s) of sector 0x%06X from %s (no erase)." % (
                    len(todo), base, args.file))
            if input("Type ERASE to continue: ").strip() != "ERASE":
                log("Aborted - nothing changed.")
                return

        if needs_erase:
            log("Erasing sector 0x%06X (on-target) ..." % base)
            t = time.time()
            d7, a1, _, _, _ = run_routine(bdm, EP_ERASE, {A1: base}, timeout=40)
            if d7 != 0:
                raise BDMError("Erase failed (status %d)" % d7)
            log("  erase complete in %.1f s" % (time.time() - t))
            blank = bdm.read_range(base, size, "blank check ")
            if blank != b"\xff" * size:
                raise BDMError("Sector not blank after erase (%d non-FF bytes)." % sum(1 for x in blank if x != 0xFF))
            todo = {base + i: (new[i] << 8) | new[i + 1] for i in range(0, size, 2)
                    if new[i] != 0xFF or new[i + 1] != 0xFF}
        log("Programming %d words ..." % len(todo))
        program_sector(bdm, base, size, todo)

        for attempt in range(3):
            got = bdm.read_range(base, size, "verify ")
            bad = [i for i in range(0, size, 2) if got[i:i + 2] != new[i:i + 2]]
            if not bad:
                log("VERIFY OK: sector 0x%06X matches %s." % (base, args.file))
                break
            retry = {}
            for i in bad:
                have = int.from_bytes(got[i:i + 2], "big"); want = int.from_bytes(new[i:i + 2], "big")
                if want & ~have & 0xFFFF:
                    raise BDMError("Word 0x%06X reads 0x%04X, needs bits 0->1 - re-run flash with --resume."
                                   % (base + i, have))
                retry[base + i] = want
            log("  %d word(s) differ - re-programming them ..." % len(retry))
            program_sector(bdm, base, size, retry)
        else:
            raise BDMError("Verify still failing after retries - re-run flash with --resume.")
    log("Done. Power-cycle the GN30 and check the date with an antenna connected.")
    log("(Optional, ~30-60 min: verify %s --skip-sector 0x%06X compares the rest of the chip.)" % (
        args.current or "gn30_a.bin", base))


def cmd_verify(bdm, args):
    img = load_image(args.file)
    bdm.halt(poweron=args.poweron)
    got = bdm.read_range(args.start, args.length, "verify ")
    want = img[args.start:args.start + args.length]
    skip = sector_of(args.skip_sector) if args.skip_sector is not None else None
    bad = [args.start + i for i in range(0, args.length, 2) if got[i:i + 2] != want[i:i + 2]
           and not (skip and skip[0] <= args.start + i < skip[0] + skip[1])]
    if not bad:
        log("VERIFY OK: 0x%06X-0x%06X matches %s%s." % (args.start, args.start + args.length - 1, args.file,
            " (sector 0x%06X skipped)" % skip[0] if skip else ""))
    else:
        log("VERIFY FAILED: %d word(s) differ:" % len(bad))
        for a in bad[:40]:
            o = a - args.start
            log("    0x%06X: chip %04X  file %04X" % (a, int.from_bytes(got[o:o + 2], "big"),
                                                    int.from_bytes(want[o:o + 2], "big")))
        sys.exit(2)


def cmd_restore_sram(bdm, args):
    data = open(args.file, "rb").read()
    if len(data) != RAM_AREA[1]:
        raise BDMError("%s is %d bytes, expected %d" % (args.file, len(data), RAM_AREA[1]))
    bdm.halt(poweron=args.poweron)
    bdm.robust(bdm.prepare_target)
    bdm.fill_words(RAM_AREA[0], data)
    log("SRAM 0x%06X-0x%06X restored from %s and verified." % (RAM_AREA[0], RAM_AREA[0] + RAM_AREA[1] - 1, args.file))


def cmd_run(bdm, args):
    bdm.go()
    print("GO sent; CPU resumed.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--batch", type=int, default=8, help="BDM transfers per USB write (lower if unstable)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pins", help="show line levels without touching the CPU")
    t = sub.add_parser("test", help="hold BKPT low so you can check it with a meter")
    t.add_argument("--seconds", type=int, default=10)
    pr = sub.add_parser("probe")
    pr.add_argument("--poweron", action="store_true", help="enter BDM via GN30 power-cycle instead of RESET")
    d = sub.add_parser("dump")
    d.add_argument("file")
    d.add_argument("--poweron", action="store_true", help="enter BDM via GN30 power-cycle instead of RESET")
    d.add_argument("--start", type=lambda x: int(x, 0), default=0)
    d.add_argument("--length", type=lambda x: int(x, 0), default=0x100000)
    d.add_argument("--block", type=int, default=256, help="words per READ+DUMP block")
    c = sub.add_parser("chipid", help="read the flash ID (read-only)")
    c.add_argument("--sector", type=lambda x: int(x, 0), default=0xC0000)
    c.add_argument("--poweron", action="store_true")
    f = sub.add_parser("flash", help="erase + program ONE sector from an image, then verify")
    f.add_argument("file", help="1 MB image to write (e.g. gn30_patched.bin)")
    f.add_argument("--sector", type=lambda x: int(x, 0), required=True, help="sector start, e.g. 0xC0000")
    f.add_argument("--current", help="dump of the chip as it is now (e.g. gn30_a.bin): checked first")
    f.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    f.add_argument("--resume", action="store_true", help="continue an interrupted flash of this sector")
    f.add_argument("--skip-boot-check", action="store_true",
                   help="don't compare the bootloader sector with --current before flashing")
    f.add_argument("--allow-boot", action="store_true")
    f.add_argument("--ignore-checksum", action="store_true")
    f.add_argument("--poweron", action="store_true")
    v = sub.add_parser("verify", help="compare a flash range with an image file")
    v.add_argument("file")
    v.add_argument("--start", type=lambda x: int(x, 0), default=0)
    v.add_argument("--length", type=lambda x: int(x, 0), default=0x100000)
    v.add_argument("--skip-sector", type=lambda x: int(x, 0), default=None,
                   help="ignore differences inside this sector (e.g. 0xC0000)")
    v.add_argument("--poweron", action="store_true")
    rs = sub.add_parser("restore-sram", help="write a saved SRAM backup back")
    rs.add_argument("file")
    rs.add_argument("--poweron", action="store_true")
    sub.add_parser("run")
    args = ap.parse_args()

    try:
        bdm = BusPirateBDM(args.port, batch=args.batch)
    except BDMError as e:
        print("\nERROR:", e)
        sys.exit(1)
    try:
        {"chipid": cmd_chipid, "flash": cmd_flash, "verify": cmd_verify, "restore-sram": cmd_restore_sram,
         "test": cmd_test, "pins": cmd_pins, "probe": cmd_probe, "dump": cmd_dump, "run": cmd_run}[args.cmd](bdm, args)
    except BDMError as e:
        print("\nERROR:", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    finally:
        bdm.close()


if __name__ == "__main__":
    main()
