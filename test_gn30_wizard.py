# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 rodavrow
"""Unit tests for gn30_wizard.py's pure helpers.  Run:  python -m unittest test_gn30_wizard"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import gn30_wizard as wiz

# Abridged real output of gn30_patch.py (text only, no firmware content).
PATCH_OUTPUT = """\
Checksum:  byte-lane sums 0x4000-0xFFFFF = b'SHIP' (GN30 'SHIP' - OK)

Patch (3 bytes):
  0x0C275E: 6C -> 64
  checksum unchanged: b'SHIP'
  flash sector to rewrite: 0x0C0000-0x0CFFFF
  patched SHA-256: f93526ea17f37fc337852a63bd33a7801338ab15308a3617d346dde385a12eb9

Wrote gn30_patched.bin
Flash it with e.g.:
  python3 bp3_bdm.py --port /dev/ttyUSB0 flash gn30_patched.bin --sector 0xC0000 --current gn30_a.bin
"""
ALREADY_PATCHED_OUTPUT = "  today would display as: 2026-10-23\n\nThis image is ALREADY PATCHED. Nothing to do.\n"
NOW = dt.datetime(2026, 10, 23, 14, 5, 9)


class ParsePatchOutputTests(unittest.TestCase):
    def test_parse_patch_sector_returns_first_and_last_address(self):
        self.assertEqual(wiz.parse_patch_sector(PATCH_OUTPUT), (0x0C0000, 0x0CFFFF))

    def test_parse_patch_sector_returns_none_without_sector_line(self):
        self.assertIsNone(wiz.parse_patch_sector(ALREADY_PATCHED_OUTPUT))

    def test_is_already_patched_detects_marker(self):
        self.assertTrue(wiz.is_already_patched(ALREADY_PATCHED_OUTPUT))

    def test_is_already_patched_false_for_fresh_patch(self):
        self.assertFalse(wiz.is_already_patched(PATCH_OUTPUT))

    def test_without_patcher_hint_drops_example_command(self):
        trimmed = wiz.without_patcher_hint(PATCH_OUTPUT)
        self.assertNotIn("/dev/ttyUSB0", trimmed)
        self.assertIn("Wrote gn30_patched.bin", trimmed)

    def test_without_patcher_hint_keeps_output_without_hint(self):
        self.assertEqual(wiz.without_patcher_hint(ALREADY_PATCHED_OUTPUT), ALREADY_PATCHED_OUTPUT)


class NextDumpNameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_dump_is_named_a(self):
        self.assertEqual(wiz.next_dump_name(self.folder, NOW), "gn30_a.bin")

    def test_second_dump_is_named_b(self):
        (self.folder / "gn30_a.bin").write_bytes(b"")
        self.assertEqual(wiz.next_dump_name(self.folder, NOW), "gn30_b.bin")

    def test_never_reuses_existing_names(self):
        (self.folder / "gn30_a.bin").write_bytes(b"")
        (self.folder / "gn30_b.bin").write_bytes(b"")
        self.assertEqual(wiz.next_dump_name(self.folder, NOW), "gn30_b_20261023-140509.bin")


class IsFullDumpTests(unittest.TestCase):
    def test_rejects_missing_file(self):
        self.assertFalse(wiz.is_full_dump(Path("does-not-exist.bin")))

    def test_rejects_short_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.bin"
            path.write_bytes(b"\xff" * 1000)
            self.assertFalse(wiz.is_full_dump(path))

    def test_accepts_1mb_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "full.bin"
            path.write_bytes(b"\xff" * wiz.IMAGE_SIZE)
            self.assertTrue(wiz.is_full_dump(path))


class ParsePortListingTests(unittest.TestCase):
    def test_marks_ftdi_ft232r_as_bus_pirate(self):
        ports = wiz.parse_port_listing("COM5\tUSB Serial Port (COM5)\t1027\t24577\n")
        self.assertTrue(ports[0].looks_like_bus_pirate)

    def test_generic_ch340_is_not_marked_as_bus_pirate(self):
        ports = wiz.parse_port_listing("COM7\tUSB-SERIAL CH340 (COM7)\t6790\t29987\n")
        self.assertFalse(ports[0].looks_like_bus_pirate)

    def test_port_without_usb_id_is_not_bus_pirate(self):
        ports = wiz.parse_port_listing("COM1\tCommunications Port (COM1)\t\t\n")
        self.assertEqual(ports, [wiz.SerialPort("COM1", "Communications Port (COM1)", None)])
        self.assertFalse(ports[0].looks_like_bus_pirate)

    def test_ignores_malformed_lines(self):
        self.assertEqual(wiz.parse_port_listing("garbage\n\n"), [])


class AcceptsRiskTests(unittest.TestCase):
    def test_accepts_exact_phrase_ignoring_case_and_spaces(self):
        self.assertTrue(wiz.accepts_risk("  i understand \n"))

    def test_rejects_empty_answer(self):
        self.assertFalse(wiz.accepts_risk(""))

    def test_rejects_plain_yes(self):
        self.assertFalse(wiz.accepts_risk("yes"))


class DisplayCommandTests(unittest.TestCase):
    def test_quotes_arguments_with_spaces(self):
        self.assertEqual(wiz.display_command(["bp3_bdm.py", "dump", "my dump.bin"]),
                         'python bp3_bdm.py dump "my dump.bin"')


if __name__ == "__main__":
    unittest.main()
