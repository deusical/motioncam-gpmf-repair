"""Synthetic tests for optional lossless HEVC display-height normalization."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from motioncam_gpmf_repair.video import VideoError, normalize, plan_crop  # noqa: E402


def probe(width=3840, height=2176, codec="hevc", coded_height=None):
    return {"streams": [
        {"codec_type": "video", "codec_name": codec,
         "width": width, "height": height,
         "coded_height": height if coded_height is None else coded_height,
         "nb_frames": "225", "duration": "7.5"},
        {"codec_type": "audio", "codec_name": "pcm_s16le", "duration": "7.5"},
    ]}


class VideoTests(unittest.TestCase):
    def test_explicit_target_height(self):
        self.assertEqual(plan_crop(probe(), target_height=2160), (2160, 0, 16))

    def test_aspect_ratio_is_generic(self):
        self.assertEqual(plan_crop(probe(), aspect="16:9"), (2160, 0, 16))
        self.assertEqual(plan_crop(probe(1920, 1092), aspect="16:9"), (1080, 0, 12))
        self.assertEqual(plan_crop(probe(), target_height=2160, crop_top=4), (2160, 4, 12))

    def test_no_implicit_assumptions(self):
        with self.assertRaisesRegex(VideoError, "exactly one"):
            plan_crop(probe())
        with self.assertRaisesRegex(VideoError, "exactly one"):
            plan_crop(probe(), target_height=2160, aspect="16:9")

    def test_safety_guards(self):
        with self.assertRaisesRegex(VideoError, "HEVC"):
            plan_crop(probe(codec="h264"), target_height=2160)
        with self.assertRaisesRegex(VideoError, "existing crop"):
            plan_crop(probe(coded_height=2240), target_height=2160)
        with self.assertRaisesRegex(VideoError, "exceeds --max-padding"):
            plan_crop(probe(), target_height=1600)
        with self.assertRaisesRegex(VideoError, "even numbers"):
            plan_crop(probe(), target_height=2160, crop_top=1)
        with self.assertRaises(VideoError):
            plan_crop(probe(), aspect="x:y")

    def test_dry_run_never_changes_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mov"
            src.write_bytes(b"synthetic placeholder")
            with patch("motioncam_gpmf_repair.video._probe", return_value=probe()):
                output, info = normalize(src, target_height=2160, dry_run=True)
            self.assertEqual(info["crop_bottom"], 16)
            self.assertEqual(output.name, "input_3840x2160.mov")
            self.assertFalse(output.exists())
            self.assertEqual(src.read_bytes(), b"synthetic placeholder")


if __name__ == "__main__":
    unittest.main()
