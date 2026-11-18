# These independent reference checks run with the standard-library unittest runner.
# ruff: noqa: PT009, PT027
import unittest

from iniconfig import IniConfig


class GetControls(unittest.TestCase):
    def setUp(self):
        self.section = IniConfig("example.ini", data="[worker]\nretries=7\nempty=\nbad=invalid")[
            "worker"
        ]

    def test_convert_present_value(self):
        self.assertIs(type(self.section.get("retries", convert=int)), int)
        self.assertEqual(self.section.get("retries", convert=int), 7)

    def test_default_is_not_a_converter(self):
        self.assertEqual(self.section.get("retries", 99), "7")

    def test_missing_default_identity(self):
        marker = object()
        self.assertIs(self.section.get("missing", marker), marker)

    def test_converter_never_receives_missing_default(self):
        marker = object()

        def refuse(value):
            raise AssertionError("converter called on missing default")

        self.assertIs(self.section.get("missing", marker, refuse), marker)

    def test_converter_receives_only_stored_text(self):
        seen = []

        def convert(value):
            seen.append(value)
            return "converted:" + value

        self.assertEqual(self.section.get("retries", 99, convert), "converted:7")
        self.assertEqual(seen, ["7"])

    def test_empty_stored_value_is_not_missing(self):
        self.assertIs(self.section.get("empty", "fallback", bool), False)

    def test_conversion_error_propagates(self):
        with self.assertRaises(ValueError):
            self.section.get("bad", convert=int)

    def test_missing_without_default(self):
        self.assertIsNone(self.section.get("missing"))
