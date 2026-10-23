#!/usr/bin/env python3
"""
gn30_patch.py - fix the GPS week-number rollover in a Simrad/Shipmate GN30 MKII
(MC68341) flash dump.

The firmware turns the 10-bit GPS week from the satellites into a full week with:

    week10 = subframe1 & 0x3FF
    week   = week10 + (2048 if week10 < 107 else 1024)     ; valid 2001-09 .. 2021-04

so since April 2021 every date is 1024 weeks (19.6 years) early. The patch makes it

    week   = week10 + (3072 if week10 < 107 else 2048)     ; valid 2021-04-25 .. 2040-12-08

by changing three bytes. The bootloader only starts the firmware if four byte-lane
sums over 0x4000-0xFFFFF equal their expected value ('SHIP' on the GN30); the patch
swaps BGE for the equivalent BCC so the three changes cancel out in that check and
no other byte has to be touched.

No firmware is included: the code is located in YOUR dump by its instruction pattern.

Usage:
    python3 gn30_patch.py my_dump.bin                 # analyse only
    python3 gn30_patch.py my_dump.bin -o patched.bin  # analyse and write patched image
"""
import argparse
import datetime as dt
import hashlib
import re
import struct
import sys

GPS_EPOCH = dt.date(1980, 1, 6)
CHECK_START, CHECK_END = 0x4000, 0x100000          # bootloader checksum range (0x1186)
KNOWN = {
    "66d262b38413e65f31db251336d5fa870be99fec78cb82258b84808c72cca957":
        "GN30 MKII, bootloader build 20824_1999-08-16 - original image the patch was tested on",
    "f93526ea17f37fc337852a63bd33a7801338ab15308a3617d346dde385a12eb9":
        "GN30 MKII, bootloader build 20824_1999-08-16 - tested image, already patched",
}
# 29F800BA bottom-boot sector map (for the flashing hint)
SECTORS = [(0x00000, 0x4000), (0x04000, 0x2000), (0x06000, 0x2000), (0x08000, 0x8000)] + \
          [(0x10000 * i, 0x10000) for i in range(1, 16)]

# Instruction pattern (68k, big-endian). Register numbers and the (d16,An) displacement
# are wildcards; everything else must match exactly.
#   andi.w #$3FF,d0          0240 03FF
#   move.w d0,(d16,An)       3x40 dddd           (x = 0x81..0x8F -> An)
#   cmpi.w #PIVOT,(d16,An)   0C6x PPPP dddd
#   bge.b  +8 / bcc.b +8     6C08 | 6408
#   addi.w #HI,(d16,An)      066x HHHH dddd
#   bra.b  +6                6006
#   addi.w #LO,(d16,An)      066x LLLL dddd
PATTERN = re.compile(
    rb"\x02\x40\x03\xff"
    rb"[\x31\x33\x35\x37\x39\x3b\x3d\x3f]\x40(..)"
    rb"\x0c[\x68-\x6f](..)\1"
    rb"([\x6c\x64])\x08"
    rb"\x06[\x68-\x6f](..)\1"
    rb"\x60\x06"
    rb"\x06[\x68-\x6f](..)\1",
    re.S)


def lanes(img):
    return bytes(sum(img[CHECK_START + k:CHECK_END:4]) & 0xFF for k in range(4))


def week_window(pivot, add_lo, add_hi):
    """True GPS weeks decoded correctly: week10 < pivot -> +add_hi, else +add_lo."""
    good = [w for w in range(0, 5000)
            if (w % 1024) + (add_hi if (w % 1024) < pivot else add_lo) == w]
    first, last = good[0], good[-1]
    return (first, last, GPS_EPOCH + dt.timedelta(weeks=first),
            GPS_EPOCH + dt.timedelta(weeks=last + 1) - dt.timedelta(days=1))


def sector_of(addr):
    for base, size in SECTORS:
        if base <= addr < base + size:
            return base, size
    return None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", help="1 MB flash dump read from your GN30 (e.g. with bp3_bdm.py dump)")
    ap.add_argument("-o", "--output", help="write the patched image here")
    ap.add_argument("--force", action="store_true", help="patch even if the dump's checksum looks wrong")
    args = ap.parse_args()

    img = bytearray(open(args.dump, "rb").read())
    sha = hashlib.sha256(img).hexdigest()
    print("File:      %s (%d bytes)" % (args.dump, len(img)))
    print("SHA-256:   %s" % sha)
    print("Tested:    %s" % KNOWN.get(sha, "no - different image from the tested one; patching by pattern match"))
    if len(img) != 0x100000:
        sys.exit("ERROR: expected a 1 MB (1048576 byte) image of the 29F800 flash.")

    ssp, pc = struct.unpack(">II", img[0:8])
    print("Vectors:   SSP=0x%08X  PC=0x%08X" % (ssp, pc))
    stamp = re.search(rb"\d{5}_\d{4}-\d\d-\d\d_\d\d:\d\d:\d\d", img[:0x4000])
    if stamp:
        print("Bootldr:   build %s" % stamp.group().decode())
    models = sorted(set(m.decode() for m in re.findall(rb"(GN30|CE32|CP32|EQ32|DE32) %5\.2lf", img)))
    if models:
        print("Models:    firmware contains version strings for %s" % ", ".join(models))
    ln = lanes(img)
    print("Checksum:  byte-lane sums 0x4000-0xFFFFF = %r %s" % (ln, "(GN30 'SHIP' - OK)" if ln == b"SHIP" else
                                                             "(not 'SHIP' - bad read or different model)"))
    if ln != b"SHIP" and not args.force:
        sys.exit("ERROR: checksum is not 'SHIP'. Re-read the flash (compare two dumps) or use --force "
                 "if you know this unit expects a different value.")

    hits = list(PATTERN.finditer(img))
    if len(hits) != 1:
        sys.exit("ERROR: found %d copies of the week-decode code (expected exactly 1) - "
                 "this firmware is not supported." % len(hits))
    m = hits[0]
    base = m.start()
    cmp_at = base + 8
    br_at = base + 14
    hi_at = base + 18                      # addi.w immediate (taken when week10 < pivot)
    lo_at = base + 26                      # addi.w immediate (week10 >= pivot)
    pivot = struct.unpack(">H", m.group(2))[0]
    branch = img[br_at]
    add_hi = struct.unpack(">H", img[hi_at:hi_at + 2])[0]
    add_lo = struct.unpack(">H", img[lo_at:lo_at + 2])[0]
    print("\nWeek decode found at 0x%06X:" % base)
    print("  week10 < %d -> +%d, else +%d   (branch: %s)" % (pivot, add_hi, add_lo,
                                                          "bge" if branch == 0x6C else "bcc"))
    f, l, fd, ld = week_window(pivot, add_lo, add_hi)
    print("  correct dates: %s .. %s  (GPS weeks %d..%d)" % (fd, ld, f, l))
    today = dt.date.today()
    tw = (today - GPS_EPOCH).days // 7
    shown = (tw % 1024) + (add_hi if (tw % 1024) < pivot else add_lo)
    print("  today (%s, week %d) would display as: %s" % (today, tw,
          GPS_EPOCH + dt.timedelta(days=shown * 7 + (today - GPS_EPOCH).days % 7)))

    if (branch, add_hi, add_lo) == (0x64, 0x0C00, 0x0800):
        print("\nThis image is ALREADY PATCHED. Nothing to do.")
        return
    if (branch, add_hi, add_lo) != (0x6C, 0x0800, 0x0400):
        sys.exit("\nERROR: unexpected constants - not the known original; not patching.")
    if pivot >= 0x8000:
        sys.exit("ERROR: unexpected pivot value.")

    new = bytearray(img)
    new[br_at] = 0x64                      # bge.b -> bcc.b (same for 0..1023, balances checksum)
    new[hi_at] = 0x0C                      # +2048 -> +3072
    new[lo_at] = 0x08                      # +1024 -> +2048
    assert br_at % 4 == hi_at % 4 == lo_at % 4
    if lanes(new) != ln:
        sys.exit("ERROR: internal - checksum would change.")
    f, l, fd, ld = week_window(pivot, 0x0800, 0x0C00)
    print("\nPatch (3 bytes):")
    for a in (br_at, hi_at, lo_at):
        print("  0x%06X: %02X -> %02X" % (a, img[a], new[a]))
    print("  new correct dates: %s .. %s  (GPS weeks %d..%d)" % (fd, ld, f, l))
    print("  checksum unchanged: %r" % lanes(new))
    sb, ss = sector_of(br_at)
    print("  flash sector to rewrite: 0x%06X-0x%06X" % (sb, sb + ss - 1))
    print("  patched SHA-256: %s" % hashlib.sha256(new).hexdigest())

    if args.output:
        with open(args.output, "wb") as fo:
            fo.write(new)
        print("\nWrote %s" % args.output)
        print("Flash it with e.g.:\n  python3 bp3_bdm.py --port /dev/ttyUSB0 flash %s --sector 0x%X --current %s"
              % (args.output, sb, args.dump))
    else:
        print("\n(analysis only - add -o patched.bin to write the patched image)")


if __name__ == "__main__":
    main()
