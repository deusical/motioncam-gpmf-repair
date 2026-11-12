"""Synthetic inspection fixtures: never commit private sensor data or project caches."""
import contextlib
import gzip
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from motioncam_gpmf_repair.inspect import (  # noqa: E402
    InspectError, format_report, inspect_file, inspect_gyroflow_project,
    inspect_sensor_diagnostic, main,
)
from motioncam_gpmf_repair.cli import main as repair_main  # noqa: E402


def fake_diagnostic():
    gyro = [
        {"timestamp_us": 1000, "scale": 1000, "samples": [[1, 2, 3]] * 3},
        {"timestamp_us": 8500, "scale": 1000, "samples": [[4, 5, 6]] * 3},
    ]
    accel = [
        {"timestamp_us": 1500, "scale": 1000, "samples": [[10, 20, 30]] * 3},
        {"timestamp_us": 9000, "scale": 1000, "samples": [[40, 50, 60]] * 3},
    ]
    return {
        "format": "motioncam-gpmf-imu-diagnostic", "version": 1,
        "counts": {"gyro": 6, "accel": 6}, "packet_sizes": [42, 42],
        "packet_times": [{"pts_time": "0"}, {"pts_time": "0.033"}],
        "tracks": {"gyro": gyro, "accel": accel},
    }


def fake_project():
    return {
        "title": "Gyroflow data file", "version": 4, "app_version": "1.7.0",
        "videofile": "file:///C:/Private/folder/example.mov",
        "gyro_source": {
            "filepath": "file:///C:/Private/folder/example_retimed.gcsv",
            "imu_orientation": "XYZ",
            "integrated_quaternions": "private-large-gyro-cache",
        },
        "calibration_data": {"name": "Generic Camera 24mm", "frame_readout_time": None},
        "video_info": {"width": 3840, "height": 2160},
        "offsets": {"30000000": -15, "90000000": -18, "150000000": -18},
    }


class InspectionTests(unittest.TestCase):
    def test_equal_count_clocks_are_independently_examined(self):
        report = inspect_sensor_diagnostic(fake_diagnostic())
        self.assertEqual(report["gyro"]["samples"], report["accel"]["samples"])
        self.assertEqual(report["clock_alignment"]["status"], "compatible")
        self.assertEqual(report["gyro"]["rate_hz"], 400)
        self.assertAlmostEqual(report["clock_alignment"]["sample_interval_difference_ppm"], 0)
        self.assertAlmostEqual(report["clock_alignment"]["accel_clamp_start_us"], 500)
        self.assertAlmostEqual(report["clock_alignment"]["accel_clamp_end_us"], 0)
        self.assertEqual(report["gyro"]["stmp"]["fitted_intercept_median_abs_residual_ms"], 0)

    def test_counts_and_rate_disagreement_are_reported(self):
        data = fake_diagnostic()
        data["tracks"]["accel"][1]["timestamp_us"] = 10000
        report = inspect_sensor_diagnostic(data)
        self.assertEqual(report["status"], "warning")
        self.assertGreater(report["clock_alignment"]["sample_interval_difference_ppm"], 1000)
        self.assertIn("Sensor clock rates differ", " ".join(report["warnings"]))

    def test_timestamp_regression_and_first_anchor_are_separate(self):
        data = fake_diagnostic()
        data["tracks"]["gyro"] = [
            {"timestamp_us": 50000 if i == 0 else 80000 + (i-1)*7500,
             "scale": 1000, "samples": [[1, 2, 3]] * 3}
            for i in range(12)
        ]
        data["counts"]["gyro"] = 36
        report = inspect_sensor_diagnostic(data)
        stmp = report["gyro"]["stmp"]
        self.assertGreater(stmp["first_anchored_median_abs_residual_ms"], 0)
        self.assertGreater(abs(stmp["first_timestamp_minus_fitted_intercept_ms"]), 1)
        self.assertIn("STMP may not timestamp", stmp["interpretation"])

    def test_invalid_batch_order_fails_closed(self):
        data = fake_diagnostic()
        data["tracks"]["gyro"][1]["timestamp_us"] = 900
        report = inspect_sensor_diagnostic(data)
        self.assertEqual(report["status"], "error")
        self.assertIn("Non-increasing", report["gyro"]["error"])

    def test_missing_accel_is_diagnostic_not_crash(self):
        data = fake_diagnostic()
        data["tracks"]["accel"] = []
        data["counts"]["accel"] = 0
        report = inspect_sensor_diagnostic(data)
        self.assertEqual(report["status"], "warning")
        self.assertIsNone(report["accel"])
        self.assertEqual(report["clock_alignment"]["status"], "unknown")

    def test_gyroflow_project_summary_and_no_private_cached_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.gyroflow"
            path.write_text(json.dumps(fake_project()), encoding="utf-8")
            result = inspect_gyroflow_project(path)
        self.assertEqual(result["video_filename"], "example.mov")
        self.assertEqual(result["motion_filename"], "example_retimed.gcsv")
        self.assertEqual(result["imu_orientation"], "XYZ")
        self.assertEqual(result["sync_point_count"], 3)
        self.assertEqual(result["offset_span_ms"], 3)
        self.assertAlmostEqual(result["median_offset_ms"], -18)
        self.assertNotIn("Private", json.dumps(result))
        self.assertNotIn("private-large-gyro-cache", json.dumps(result))

    def test_no_gpmd_video_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            movie = Path(tmp) / "video.mov"
            movie.write_bytes(b"synthetic placeholder")
            streamdata = {"streams": [
                {"codec_type": "video", "codec_name": "hevc", "width": 3840,
                 "height": 2176, "avg_frame_rate": "30/1"}
            ]}
            with patch("motioncam_gpmf_repair.inspect._run",
                       return_value=json.dumps(streamdata).encode("utf-8")):
                report = inspect_file(movie)
        self.assertEqual(report["imu"]["status"], "missing")
        self.assertEqual(report["video"]["height"], 2176)
        self.assertIn("no embedded gpmd", format_report(report).lower())

    def test_diagnostic_and_project_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            diagnostic = folder / "imu.json.gz"
            project = folder / "sync.gyroflow"
            with gzip.open(diagnostic, "wt", encoding="utf-8") as f:
                json.dump(fake_diagnostic(), f)
            project.write_text(json.dumps(fake_project()), encoding="utf-8")
            result = inspect_file(diagnostic, project)
        self.assertEqual(result["imu"]["clock_alignment"]["status"], "compatible")
        self.assertEqual(result["gyroflow"]["sync_point_count"], 3)
        self.assertEqual(result["source_filename"], "imu.json.gz")
        self.assertNotIn("Private", json.dumps(result))

    def test_json_output_never_replaces_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as f:
                json.dump(fake_diagnostic(), f)
            original = path.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                result = main([str(path), "--output", str(path), "--overwrite"])
            self.assertEqual(result, 1)
            self.assertEqual(path.read_bytes(), original)

    def test_bad_format_rejected(self):
        with self.assertRaises(InspectError):
            inspect_sensor_diagnostic({"tracks": {}})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrong.gyroflow"
            path.write_text(json.dumps({"offsets": {}}), encoding="utf-8")
            with self.assertRaises(InspectError):
                inspect_gyroflow_project(path)

    def test_repair_entrypoint_dispatches_inspect_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as caught:
                repair_main(["inspect", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--project", out.getvalue())

    def test_cli_json_redacts_path_and_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            diagnostic = folder / "test.json.gz"
            with gzip.open(diagnostic, "wt", encoding="utf-8") as f:
                json.dump(fake_diagnostic(), f)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main([str(diagnostic), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["source_filename"], "test.json.gz")
        self.assertNotIn(str(folder), out.getvalue())
        self.assertNotIn("[1, 2, 3]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
