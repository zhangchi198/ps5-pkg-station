"""Reading PS4 package headers (ps5_ps4pkg). Standard library only."""
from __future__ import annotations

import struct
import unittest

from support import E, make_ps4_pkg, make_sfo

import ps5_ps4pkg as P  # noqa: E402

CID = "UP9000-CUSA12345_00-EXAMPLEGAME00000"


class Sfo(unittest.TestCase):
    def test_strings_are_read(self):
        sfo = P.parse_sfo(make_sfo(TITLE="Pokémon ゲーム", TITLE_ID="CUSA12345", APP_VER="01.04", CATEGORY="gp"))
        self.assertEqual((sfo["TITLE"], sfo["TITLE_ID"], sfo["APP_VER"], sfo["CATEGORY"]), ("Pokémon ゲーム", "CUSA12345", "01.04", "gp"))

    def test_an_integer_field(self):
        index, ktab, dtab = struct.pack("<HHIII", 0, 0x0404, 4, 4, 0), b"A\0", struct.pack("<I", 7)
        kstart = 0x14 + 16
        data = b"\0PSF" + struct.pack("<IIII", 0x101, kstart, kstart + len(ktab), 1) + index + ktab + dtab
        self.assertEqual(P.parse_sfo(data), {"A": 7})

    def test_what_is_not_an_sfo_or_is_cut_short(self):
        for data in (b"", b"nope" * 10, make_sfo(TITLE="x")[:10], make_sfo(TITLE="Cut")[:0x18]):
            self.assertEqual(P.parse_sfo(data), {})


class Header(unittest.TestCase):
    def test_a_ps4_package_says_what_it_is(self):
        h = P.parse_header(make_ps4_pkg(CID, "Example Game", "01.00", "gd"))
        self.assertEqual((h["ps4"], h["content_id"], h["title_id"], h["content_type"]), (True, CID, "CUSA12345", 0x1A))
        self.assertEqual((h["sfo"]["TITLE"], h["sfo"]["CATEGORY"], P.version_of(h["sfo"])), ("Example Game", "gd", "01.00"))

    def test_only_the_head_is_needed_and_a_package_without_an_sfo_in_it_still_names_its_title(self):
        h = P.parse_header(make_ps4_pkg(CID, "Example Game", "01.00", "gd")[:0x4000])
        self.assertEqual((h["ps4"], h["title_id"], h["sfo"]["TITLE"]), (True, "CUSA12345", "Example Game"))
        h = P.parse_header(make_ps4_pkg(CID, "x", "01.00", "gd")[:0x2800])                  # cut before the SFO
        self.assertEqual((h["ps4"], h["title_id"], h["sfo"]), (True, "CUSA12345", {}))
        h = P.parse_header(make_ps4_pkg(CID, "x", "01.00", "gd", with_sfo=False))
        self.assertEqual((h["title_id"], h["sfo"]), ("CUSA12345", {}))

    def test_other_files_and_other_packages(self):
        self.assertEqual(P.parse_header(b"\x7fFIH" + b"\0" * 0x100), {"ps4": False})        # a PS5 package
        self.assertEqual(P.parse_header(b"MZ" + b"\0" * 0x100), {"ps4": False})
        self.assertIsNone(P.parse_header(b"\x7fCNT"))                                          # too short to tell
        self.assertIsNone(P.parse_header(b""))

    def test_a_ps4_header_with_a_broken_entry_table_does_not_raise(self):
        buf = bytearray(make_ps4_pkg(CID, "x", "01.00", "gd"))
        struct.pack_into(">I", buf, 0x10, 5000)                                              # a huge entry count
        struct.pack_into(">I", buf, 0x18, 0xFFFFFFF0)                                        # a table far outside
        h = P.parse_header(bytes(buf))
        self.assertEqual((h["ps4"], h["sfo"]), (True, {}))

    def test_a_content_id_that_is_not_one(self):
        buf = bytearray(make_ps4_pkg("garbage", "x", "01.00", "gd", with_sfo=False))
        self.assertEqual(P.parse_header(bytes(buf))["title_id"], None)


class EnglishTitle(unittest.TestCase):
    def test_title_01_then_title_18(self):
        self.assertEqual(P.english_title({"TITLE": "\u7bc4\u4f8b", "TITLE_01": "Example Game", "TITLE_18": "Example Game UK"}), "Example Game")
        self.assertEqual(P.english_title({"TITLE": "\u7bc4\u4f8b", "TITLE_01": "  ", "TITLE_18": "Example Game UK"}), "Example Game UK")

    def test_an_asian_release_has_none(self):
        self.assertEqual(P.english_title({"TITLE": "\u7bc4\u4f8b\u904a\u6232", "TITLE_09": "\uc608\uc2dc \uac8c\uc784"}), "")
        self.assertEqual(P.english_title({}), "")

    def test_it_is_read_from_a_real_header(self):
        pkg = make_ps4_pkg(CID, "\u7bc4\u4f8b\u904a\u6232", "01.00", "gd", extra={"TITLE_09": "\uc608\uc2dc \uac8c\uc784", "TITLE_01": "Example Game"})
        sfo = P.parse_header(pkg)["sfo"]
        self.assertEqual((sfo["TITLE"], sfo["TITLE_09"], P.english_title(sfo)), ("\u7bc4\u4f8b\u904a\u6232", "\uc608\uc2dc \uac8c\uc784", "Example Game"))


class Classify(unittest.TestCase):
    def test_by_category(self):
        self.assertEqual([P.category_kind(c) for c in ("gd", "gda", "gdc", "gp", "ac", "GP", "", None, "xx")],
                         ["game", "game", "game", "patch", "dlc", "patch", "other", "other", "other"])

    def test_the_version(self):
        self.assertEqual(P.version_of({"APP_VER": "01.04"}), "01.04")
        self.assertEqual(P.version_of({"APP_VER": "", "VERSION": "01.10"}), "01.10")
        self.assertEqual(P.version_of({"APP_VER": "garbage"}), "")
        self.assertEqual(P.version_of({}), "")

    def test_by_name_when_the_header_cannot_be_read(self):
        self.assertEqual(P.kind_from_name("UP9000-CUSA12345_00-EXAMPLEGAME00000-A0100-V0100.pkg"), "game")
        self.assertEqual(P.kind_from_name("HP0177-CUSA12345_00-ZZZZZZZZZZZZZZZZ-A0101-V0100.pkg"), "patch")
        self.assertEqual(P.kind_from_name("Example Game DLC Pack.pkg"), "dlc")
        self.assertEqual(P.kind_from_name("Example Game Update 1.04.pkg"), "patch")
        self.assertEqual(P.kind_from_name("whatever.pkg"), "other")


if __name__ == "__main__":
    unittest.main()
