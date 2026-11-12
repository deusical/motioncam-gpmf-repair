"""Synthetic Gyroflow GCSV sidecar fixtures (including MotionCam's trailing comma)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from motioncam_gpmf_repair.sidecar import (  # noqa: E402
    SidecarError, find_sidecar, inspect_gcsv, sidecar_fingerprint,
)


def sample_gcsv(*, tscale="0.0022743480", timestamps=("0", "1", "2", "3"),
                headers="t,gx,gy,gz,ax,ay,az", end_comma=True):
    content = [
        "GYROFLOW IMU LOG",
        "version,1.2",
        "id,vivo.synthetic",
        "orientation,XYZ",
        "timestamp,1234567890",
        f"tscale,{tscale}",
        "gscale,0.0000000100",
        "ascale,0.0000000100",
        headers,
    ]
    for tick in timestamps:
        row = f"{tick},-8612946,21104772,7024743,945760671,11668716,386934974"
        content.append(row + ("," if end_comma else ""))
    return "\n".join(content) + "\n"


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / "261111_161432_VIDEO_24mm.gcsv"
        self.log.write_text(sample_gcsv(), encoding="utf-8")

    def test_realistic_motioncam_csv_trailing_empty_column(self):
        result = inspect_gcsv(self.log)
        self.assertEqual(result["format"], "gyroflow-gcsv")
        self.assertEqual(result["sample_count"], 4)
        self.assertTrue(result["accel_present"])
        self.assertEqual(result["orientation"], "XYZ")
        self.assertAlmostEqual(result["median_sample_rate_hz"], 439.686,
                               delta=0.2)
        self.assertEqual(result["validation"], "structural_only")
        self.assertNotIn("timestamp", result)
        self.assertNotIn("samples", result)

    def test_four_column_gyro_only_csv(self):
        data = "\n".join([
            "GYROFLOW IMU LOG", "tscale,0.001", "gscale,0.001",
            "t,gx,gy,gz", "0,1,2,3", "1,4,5,6",
        ]) + "\n"
        self.log.write_text(data, encoding="utf-8")
        result = inspect_gcsv(self.log)
        self.assertFalse(result["accel_present"])
        self.assertEqual(result["sample_count"], 2)

    def test_nonmonotonic_times_rejected(self):
        self.log.write_text(sample_gcsv(timestamps=("0", "1", "1", "2")),
                            encoding="utf-8")
        with self.assertRaisesRegex(SidecarError, "Non-increasing"):
            inspect_gcsv(self.log)

    def test_not_a_gyroflow_header_rejected(self):
        self.log.write_text("not a GCSV\n" + sample_gcsv(), encoding="utf-8")
        with self.assertRaisesRegex(SidecarError, "missing GYROFLOW"):
            inspect_gcsv(self.log)

    def test_negative_nan_and_infinite_scale_rejected(self):
        for scale in ("0", "-1", "nan", "inf", "garbage"):
            with self.subTest(scale=scale):
                self.log.write_text(sample_gcsv(tscale=scale), encoding="utf-8")
                with self.assertRaises(SidecarError):
                    inspect_gcsv(self.log)

    def test_invalid_sample_numeric_values_rejected(self):
        self.log.write_text(sample_gcsv().replace("7024743", "nan"), encoding="utf-8")
        with self.assertRaisesRegex(SidecarError, "must be finite"):
            inspect_gcsv(self.log)

    def test_wrong_extra_columns_rejected(self):
        self.log.write_text(sample_gcsv().replace(
            "386934974,", "386934974,unexpected,"), encoding="utf-8")
        with self.assertRaisesRegex(SidecarError, "column count"):
            inspect_gcsv(self.log)

    def test_missing_accel_scale_rejected(self):
        self.log.write_text(sample_gcsv().replace(
            "ascale,0.0000000100\n", ""), encoding="utf-8")
        with self.assertRaisesRegex(SidecarError, "ascale"):
            inspect_gcsv(self.log)

    def test_large_timing_gap_reported_not_retimed(self):
        self.log.write_text(sample_gcsv(timestamps=("0", "1", "2", "10", "11")),
                            encoding="utf-8")
        result = inspect_gcsv(self.log)
        self.assertEqual(result["large_timestamp_intervals"], 1)
        self.assertEqual(result["sample_count"], 5)

    def test_autodetect_raw_render_suffix(self):
        video = self.directory / "261111_161432_VIDEO_24mm_BT.709_BT709.mov"
        video.write_bytes(b"rendered movie")
        self.assertEqual(find_sidecar(video), self.log)

    def test_exact_name_preferred_to_prefix(self):
        video = self.directory / "sample.mov"
        video.write_bytes(b"mov")
        exact = self.directory / "sample.gcsv"
        exact.write_text(sample_gcsv(), encoding="utf-8")
        self.assertEqual(find_sidecar(video), exact)

    def test_ambiguous_adjacent_gcsv_requires_explicit_selection(self):
        video = self.directory / "clip_A_B.mov"
        video.write_bytes(b"mov")
        for name in ("clip.gcsv", "clip_A.gcsv"):
            (self.directory / name).write_text(sample_gcsv(), encoding="utf-8")
        with self.assertRaisesRegex(SidecarError, "Multiple matching"):
            find_sidecar(video)

    def test_generated_retimed_gcsv_is_not_used_as_raw_source(self):
        video = self.directory / "render_retimed_extra.mov"
        video.write_bytes(b"mov")
        (self.directory / "render_retimed.gcsv").write_text(
            sample_gcsv(), encoding="utf-8"
        )
        self.assertIsNone(find_sidecar(video))

    def test_sidecar_sha256_changes_when_content_changes(self):
        before = sidecar_fingerprint(self.log)
        self.log.write_text(sample_gcsv(tscale="0.0023"), encoding="utf-8")
        after = sidecar_fingerprint(self.log)
        self.assertNotEqual(before["sha256"], after["sha256"])
        self.assertNotIn(str(self.directory), str(after))

    def test_utf8_bom_is_accepted(self):
        self.log.write_text("\ufeff" + sample_gcsv(), encoding="utf-8")
        self.assertEqual(inspect_gcsv(self.log)["sample_count"], 4)


if __name__ == "__main__":
    unittest.main()
