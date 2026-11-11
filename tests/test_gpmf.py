"""Synthetic GPMF fixtures only: no personal video or motion data in Git."""

import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from motioncam_gpmf_repair.core import (  # noqa: E402
    RepairError,
    analyze,
    gpmf_entries,
    parse_sensors,
    repair,
    sensor_streams,
    write_gcsv,
)


def rec(key, typ, size, body):
    assert isinstance(key, bytes) and len(key) == 4
    assert len(body) % size == 0
    head = key + bytes([typ, size]) + (len(body) // size).to_bytes(2, "big")
    return head + body + bytes((-len(body)) % 4)


def batch(sensor, timestamp, scale, samples):
    import struct
    content = (
        rec(b"STMP", ord("J"), 8, timestamp.to_bytes(8, "big"))
        + rec(b"SCAL", ord("l"), 4, scale.to_bytes(4, "big", signed=True))
        + rec(sensor, ord("s"), 2, b"".join(struct.pack(">hhh", *v) for v in samples))
    )
    return rec(b"STRM", 0, 1, content)


def fixture_packet():
    stream = (
        batch(b"GYRO", 1000, 1000, [(1, 2, 3), (2, 3, 4), (3, 4, 5)])
        + batch(b"ACCL", 1000, 1000, [(10, 20, 30), (11, 21, 31), (12, 22, 32)])
        + batch(b"GYRO", 10000, 1000, [(4, 5, 6), (5, 6, 7), (6, 7, 8)])
        + batch(b"ACCL", 10000, 1000, [(13, 23, 33), (14, 24, 34), (15, 25, 35)])
    )
    return b"0000GPMF" + rec(b"DEVC", 0, 1, stream)


class GpmfTests(unittest.TestCase):
    def test_nested_streams_and_branding_header(self):
        pkt = fixture_packet()
        streams = list(sensor_streams(pkt))
        self.assertEqual(len(streams), 4)
        self.assertEqual(len(streams[0][b"GYRO"]), 18)
        parsed = parse_sensors([len(pkt)], pkt)
        self.assertEqual(len(parsed.gyro), 2)
        self.assertEqual(len(parsed.accel), 2)
        self.assertEqual(parsed.gyro[0].samples[1], (2, 3, 4))

    def test_malformed_length_is_rejected(self):
        with self.assertRaises(RepairError):
            list(gpmf_entries(b"GYRO" + bytes([ord("s"), 2, 0, 10]) + b"\x00\x00"))

    def test_uniform_timestamps_and_axes(self):
        pkt = fixture_packet()
        parsed = parse_sensors([len(pkt)], pkt)
        report = analyze(Path("sample.mov"), parsed)
        self.assertEqual(report.samples, 6)
        self.assertAlmostEqual(report.rate_hz, 333.3333333333)
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "sample.gcsv"
            write_gcsv(Path("sample.mov"), output, parsed, report)
            with output.open(encoding="utf-8") as f:
                rows = list(csv.reader(f))[11:]
            self.assertEqual(rows[0], ["1000", "-2", "-1", "-3", "-20", "-10", "-30"])
            self.assertEqual(rows[-1], ["16000", "-7", "-6", "-8", "-25", "-15", "-35"])
            self.assertEqual(len(rows), 6)

    def test_mismatched_sample_counts_fail_closed(self):
        parsed = parse_sensors([len(fixture_packet())], fixture_packet())
        modified = type(parsed)(parsed.gyro, parsed.accel[:-1])
        with self.assertRaisesRegex(RepairError, "Mismatched sensor sample counts"):
            analyze(Path("sample.mov"), modified)

    def test_output_guard_and_dry_run(self):
        pkt = fixture_packet()
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "input.mov"
            source.write_bytes(b"synthetic placeholder")
            destination = Path(d) / "out.gcsv"
            with patch("motioncam_gpmf_repair.core.extract_gpmd", return_value=([len(pkt)], pkt)):
                result = repair(source, destination)
                self.assertEqual(result.samples, 6)
                self.assertTrue(destination.exists())
                with self.assertRaisesRegex(RepairError, "already exists"):
                    repair(source, destination)
                self.assertIsNone(repair(source, destination, dry_run=True).output)
                self.assertIsNotNone(repair(source, destination, overwrite=True).output)


if __name__ == "__main__":
    unittest.main()
