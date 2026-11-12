"""Synthetic-only regression tests for confidence assessments and packet PTS."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from motioncam_gpmf_repair.validation import (  # noqa: E402
    packet_pts_summary, packet_video_pts_alignment, quality_assessment,
    video_pts_summary,
)


def metadata_example():
    return {
        "packet_sizes": [200, 200, 200],
        "packet_times": [
            {"pts_time": "0.033333"},
            {"pts_time": "0.066666"},
            {"pts_time": "0.099999"},
        ],
        "tracks": {
            "gyro": [
                {"packet_index": 0, "timestamp_us": 523, "samples": [[1, 2, 3]]},
                {"packet_index": 1, "timestamp_us": 34027, "samples": [[4, 5, 6]]},
                {"packet_index": 2, "timestamp_us": 67532, "samples": [[7, 8, 9]]},
            ],
            "accel": [
                {"packet_index": 0, "timestamp_us": 1053, "samples": [[1, 2, 3]]},
                {"packet_index": 1, "timestamp_us": 34557, "samples": [[4, 5, 6]]},
            ],
        },
    }


def project(offsets=( -15.2575, -17.9203, -18.0072 ), fps=30.0):
    return {
        "video_fps": fps,
        "sync_points": [
            {"video_time_s": t, "offset_ms": offset}
            for t, offset in zip((30.6, 91.8, 153.0), offsets)
        ],
        "offset_span_ms": max(offsets) - min(offsets),
    }


def report(imu_status="ok", compatible=True, gyroflow=None, packet_status="observed"):
    return {
        "imu": {
            "status": imu_status,
            "gyro": {"samples": 100},
            "clock_alignment": {"status": "compatible" if compatible else "warning"},
            "packet_timing": {"status": packet_status},
        },
        "gyroflow": gyroflow,
        "video_timing": {"status": "observed"},
    }


class ValidationTests(unittest.TestCase):
    def test_packet_pts_minus_stmp_does_not_imply_correction(self):
        result = packet_pts_summary(metadata_example())
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["streams"]["gyro"]["matched_batches"], 3)
        self.assertAlmostEqual(
            result["streams"]["gyro"]["median_packet_pts_minus_stmp_ms"], 32.639,
            places=3,
        )
        self.assertIn("not an", result["interpretation"])
        self.assertNotIn("automatic_correction_ms", result)

    def test_missing_packet_times_and_unmatched_batches(self):
        raw = metadata_example()
        raw["packet_times"] = None
        self.assertEqual(packet_pts_summary(raw)["status"], "unavailable")
        raw = metadata_example()
        raw["tracks"]["gyro"][0]["packet_index"] = 99
        result = packet_pts_summary(raw)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["streams"]["gyro"]["unmatched_batches"], 1)

    def test_nonmonotonic_packet_pts_flagged(self):
        raw = metadata_example()
        raw["packet_times"][1]["pts_time"] = "-0.1"
        result = packet_pts_summary(raw)
        self.assertEqual(result["status"], "warning")
        self.assertGreater(result["nonmonotonic_packet_pts"], 0)

    def test_video_pts_sorted_in_presentation_not_decode_order(self):
        data = {"packets": [
            {"pts_time": "0.033333"}, {"pts_time": "0.0"},
            {"pts_time": "0.066666"}, {"pts_time": "0.133332"},
        ]}
        timing = video_pts_summary(data)
        self.assertEqual(timing["status"], "observed")
        self.assertEqual(timing["valid_distinct_presentation_timestamps"], 4)
        self.assertEqual(timing["intervals_longer_than_1_5x_median"], 1)
        self.assertAlmostEqual(timing["median_frame_pts_interval_ms"], 33.333)

    def test_video_metadata_pts_nearest_distance(self):
        meta = metadata_example()
        video = {"packets": [
            {"pts_time": "0.0"}, {"pts_time": "0.033333"},
            {"pts_time": "0.066666"}, {"pts_time": "0.099999"},
        ]}
        matched = packet_video_pts_alignment(meta, video)
        self.assertEqual(matched["status"], "observed")
        self.assertEqual(matched["matched_metadata_packets"], 3)
        self.assertEqual(matched["median_nearest_video_pts_distance_ms"], 0)
        self.assertIn("not a measurement", matched["interpretation"])

    def test_missing_video_packets_remains_unknown(self):
        self.assertEqual(
            packet_video_pts_alignment(metadata_example(), {"packets": []})["status"],
            "unavailable",
        )
        self.assertEqual(video_pts_summary({"packets": []})["status"], "unavailable")

    def test_compatible_clocks_and_provisional_optics_pass_auto_checks(self):
        verdict = quality_assessment(report(gyroflow=project()))
        self.assertEqual(verdict["status"], "pass")
        self.assertEqual(verdict["sensor_clock"]["status"], "pass")
        self.assertEqual(verdict["optical_sync"]["status"], "provisionally_consistent")
        self.assertAlmostEqual(
            verdict["optical_sync"]["quarter_frame_threshold_ms"], 8.3333,
            places=3,
        )
        self.assertTrue(verdict["manual_visual_validation_required"])

    def test_missing_optical_project_does_not_claim_sync(self):
        verdict = quality_assessment(report(gyroflow=None))
        self.assertEqual(verdict["status"], "warning")
        self.assertEqual(verdict["optical_sync"]["status"], "unverified")

    def test_only_two_sync_points_are_insufficient(self):
        p = project()
        p["sync_points"] = p["sync_points"][:2]
        verdict = quality_assessment(report(gyroflow=p))
        self.assertEqual(verdict["status"], "warning")
        self.assertEqual(verdict["optical_sync"]["status"], "insufficient")

    def test_inconsistent_optical_offsets_require_review(self):
        verdict = quality_assessment(report(gyroflow=project(offsets=(-5, -20, -30))))
        self.assertEqual(verdict["status"], "warning")
        self.assertEqual(verdict["optical_sync"]["status"], "review")

    def test_missing_fps_is_not_treated_as_consistent(self):
        verdict = quality_assessment(report(gyroflow=project(fps=None)))
        self.assertEqual(verdict["optical_sync"]["status"], "insufficient")

    def test_sensor_clock_failure_blocks_pass(self):
        invalid = report(imu_status="error", gyroflow=project())
        verdict = quality_assessment(invalid)
        self.assertEqual(verdict["status"], "fail")
        self.assertEqual(verdict["sensor_clock"]["status"], "fail")

    def test_packet_warning_prevents_overall_pass(self):
        verdict = quality_assessment(report(gyroflow=project(), packet_status="warning"))
        self.assertEqual(verdict["status"], "warning")

    def test_no_sensor_data_does_not_pass_even_with_good_optical_offsets(self):
        verdict = quality_assessment(report(imu_status="missing", gyroflow=project()))
        self.assertEqual(verdict["status"], "warning")
        self.assertEqual(verdict["sensor_clock"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
