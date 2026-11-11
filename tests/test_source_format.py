import unittest
from collector_10.adapter import decode


class RosterExportTests(unittest.TestCase):
    def test_open_interval_and_generation_are_preserved(self):
        rows = decode(b"generation,person_id,start,end\n10,ID-A,2026-11-01,\n")
        self.assertEqual(rows[0]["end"], "")
        self.assertEqual(rows[0]["generation"], "10")

    def test_truncated_file_is_not_an_empty_roster(self):
        with self.assertRaises(ValueError):
            decode(b"generation,person_id,start,end\n10,ID-A\n")

    def test_empty_export_header_is_invalid(self):
        with self.assertRaises(ValueError):
            decode(b"")
