from pathlib import Path
import tempfile
import unittest

from png_webm_exporter.sequence import detect_sequence
from png_webm_exporter.size_search import SizeSearch, megabytes_to_bytes


class SequenceTests(unittest.TestCase):
    def sequence(self, names):
        with tempfile.TemporaryDirectory(prefix="frames % ") as directory:
            files = [Path(directory) / name for name in names]
            for path in files:
                path.touch()
            return detect_sequence(files)

    def test_ae_order_and_padding(self):
        sequence = self.sequence(["shot_v2_[01003].png", "shot_v2_[01001].png", "shot_v2_[01002].png"])
        self.assertEqual((sequence.start, sequence.end, sequence.count), (1001, 1003, 3))
        self.assertIn("shot_v2_[%05d].png", sequence.pattern)
        self.assertIn("%%", sequence.pattern)

    def test_unpadded_transition(self):
        sequence = self.sequence(["frame_10.png", "frame_9.png"])
        self.assertEqual(sequence.start, 9)
        self.assertIn("frame_%d.png", sequence.pattern)

    def test_gap(self):
        with self.assertRaisesRegex(ValueError, "Frame 2 is missing"):
            self.sequence(["frame_0001.png", "frame_0003.png"])

    def test_mixed(self):
        with self.assertRaises(ValueError):
            self.sequence(["shot_a_1.png", "shot_b_2.png"])

    def test_single(self):
        self.assertEqual(self.sequence(["still.png"]).count, 1)

    def test_stem_derivation(self):
        self.assertEqual(self.sequence(["shot_v2_[01001].png", "shot_v2_[01002].png"]).stem, "shot_v2")
        self.assertEqual(self.sequence(["frame_9.png", "frame_10.png"]).stem, "frame")
        self.assertEqual(self.sequence(["still.png"]).stem, "still")


class SearchTests(unittest.TestCase):
    def test_all_integer_thresholds(self):
        for threshold in range(64):
            search = SizeSearch(100)
            while (crf := search.next_crf()) is not None:
                search.record(crf, 101 if crf < threshold else 100)
            self.assertEqual(search.best, threshold)
            self.assertLessEqual(len(search.results), 9)

    def test_unreachable(self):
        search = SizeSearch(1)
        while (crf := search.next_crf()) is not None:
            search.record(crf, 10)
        self.assertIsNone(search.best)

    def test_observed_best(self):
        search = SizeSearch(100)
        for crf, size in [(12, 99), (0, 300), (6, 98), (2, 150), (4, 105)]:
            search.record(crf, size)
        self.assertEqual(search.best, 6)

    def test_mb(self):
        self.assertEqual(megabytes_to_bytes("42.125"), 42125000)
        for value in ("nan", "inf", "0", "-1", "oops"):
            with self.assertRaises(ValueError):
                megabytes_to_bytes(value)