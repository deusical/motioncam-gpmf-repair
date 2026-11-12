"""Synthetic-only tests for IMU diagnostic dumping (no user footage)."""
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from motioncam_gpmf_repair.diagnostics import collect_diagnostic, dump_diagnostic  # noqa: E402
from motioncam_gpmf_repair.core import RepairError  # noqa: E402
from test_gpmf import batch, rec  # noqa: E402


class DiagnosticsTests(unittest.TestCase):
    def test_retains_all_samples_and_packet_timing_when_counts_differ(self):
        first = b"0000GPMF" + rec(b"DEVC", 0, 1,
            batch(b"GYRO", 1000, 1000, [(1, 2, 3), (2, 3, 4)]) +
            batch(b"ACCL", 1010, 1000, [(11, 12, 13), (12, 13, 14), (13, 14, 15)]))
        second = b"0000GPMF" + rec(b"DEVC", 0, 1,
            batch(b"GYRO", 3000, 1000, [(3, 4, 5)]) +
            batch(b"ACCL", 3010, 1000, [(14, 15, 16)]))
        report = collect_diagnostic(
            [len(first), len(second)], first + second,
            [{"pts_time": "0.000", "duration_time": "0.033"},
             {"pts_time": "0.033", "duration_time": "0.033"}],
        )
        self.assertEqual(report["counts"], {"gyro": 3, "accel": 4})
        self.assertEqual(report["tracks"]["accel"][0]["samples"][0], [11, 12, 13])
        self.assertEqual(report["tracks"]["accel"][0]["packet_index"], 0)
        self.assertEqual(report["tracks"]["gyro"][1]["packet_index"], 1)
        self.assertEqual(report["packet_times"][1]["pts_time"], "0.033")
        self.assertNotIn("source", report)

    def test_gpmf_axis_labels_preserved_as_metadata(self):
        import struct
        sensor_stream = (
            rec(b"STMP", ord("J"), 8, (1000).to_bytes(8, "big"))
            + rec(b"SCAL", ord("l"), 4, (100).to_bytes(4, "big", signed=True))
            + rec(b"ORIN", ord("c"), 1, b"ZXY")
            + rec(b"ORIO", ord("c"), 1, b"XYZ")
            + rec(b"GYRO", ord("s"), 2, struct.pack(">hhh", 1, 2, 3))
        )
        pkt = b"0000GPMF" + rec(b"DEVC", 0, 1, rec(b"STRM", 0, 1, sensor_stream))
        result = collect_diagnostic([len(pkt)], pkt)
        self.assertEqual(result["tracks"]["gyro"][0]["orientation_in"], "ZXY")
        self.assertEqual(result["tracks"]["gyro"][0]["orientation_out"], "XYZ")
        self.assertEqual(result["tracks"]["gyro"][0]["samples"], [[1, 2, 3]])

    def test_dump_writes_small_gzip_and_rejects_overwrite(self):
        pkt = b"0000GPMF" + rec(b"DEVC", 0, 1,
            batch(b"GYRO", 1000, 1000, [(1, 2, 3)]) +
            batch(b"ACCL", 1000, 1000, [(4, 5, 6), (7, 8, 9)]))
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "input.mov"
            src.write_bytes(b"stub video")
            with patch("motioncam_gpmf_repair.diagnostics.extract_gpmd", return_value=([len(pkt)], pkt)), \
                 patch("motioncam_gpmf_repair.diagnostics._packet_times", return_value=[{"pts_time": "0"}]):
                out, result = dump_diagnostic(src)
                with gzip.open(out, "rt", encoding="utf-8") as compressed:
                    loaded = json.load(compressed)
                self.assertEqual(loaded["counts"], {"gyro": 1, "accel": 2})
                self.assertEqual(result, loaded)
                with self.assertRaisesRegex(RepairError, "already exists"):
                    dump_diagnostic(src)
                dump_diagnostic(src, overwrite=True)

    def test_rejects_wrong_packet_boundaries(self):
        with self.assertRaises(RepairError):
            collect_diagnostic([3], b"1234", [])

if __name__ == "__main__":
    unittest.main()
