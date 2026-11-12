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
    validate_imu_orientation,
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
                contents = list(csv.reader(f))
            self.assertFalse(any(row and row[0] == "orientation" for row in contents))
            rows = contents[contents.index(["t", "gx", "gy", "gz", "ax", "ay", "az"]) + 1:]
            self.assertEqual(rows[0], ["1000", "1", "2", "3", "10", "20", "30"])
            self.assertEqual(rows[-1], ["16000", "6", "7", "8", "15", "25", "35"])
            self.assertEqual(len(rows), 6)

    def test_orientation_metadata_is_optional_and_never_changes_sensor_axes(self):
        pkt = fixture_packet()
        sensors = parse_sensors([len(pkt)], pkt)
        report = analyze(Path("sample.mov"), sensors)
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "sample.gcsv"
            write_gcsv(Path("sample.mov"), output, sensors, report, imu_orientation="yXz")
            with output.open(encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertIn(["orientation", "yXz"], rows)
            index = rows.index(["t", "gx", "gy", "gz", "ax", "ay", "az"])
            self.assertEqual(rows[index + 1], ["1000", "1", "2", "3", "10", "20", "30"])

    def test_invalid_orientations_are_rejected(self):
        for axes in ("XXZ", "YX", "ABZ", "XYZx"):
            with self.subTest(axes=axes):
                with self.assertRaises(RepairError):
                    validate_imu_orientation(axes)
        validate_imu_orientation(None)
        validate_imu_orientation("yXz")

    def test_mismatched_sample_counts_fail_closed(self):
        parsed = parse_sensors([len(fixture_packet())], fixture_packet())
        modified = type(parsed)(parsed.gyro, parsed.accel[:-1])
        with self.assertRaisesRegex(RepairError, "Mismatched sensor sample counts"):
            analyze(Path("sample.mov"), modified)


    def test_one_extra_accelerometer_sample_uses_gyro_only(self):
        pkt = fixture_packet()
        parsed = parse_sensors([len(pkt)], pkt)
        # Extra ACCL sample could be at the start of the stream.
        first = parsed.accel[0]
        extra = type(first)(first.timestamp_us, first.scale, ((999, 999, 999),) + first.samples)
        modified = type(parsed)(parsed.gyro, (extra,) + parsed.accel[1:])
        with self.assertRaisesRegex(RepairError, "sample rates disagree"):
            analyze(Path("sample.mov"), modified)
        report = analyze(Path("sample.mov"), modified, gyro_only=True)
        self.assertEqual(report.samples, 6)
        self.assertEqual(report.accel_samples, 7)
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "sample.gcsv"
            write_gcsv(Path("sample.mov"), output, modified, report, gyro_only=True)
            with output.open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            header_idx = rows.index(["t", "gx", "gy", "gz"])
            self.assertFalse(any(row and row[0] == "ascale" for row in rows))
            self.assertEqual(rows[header_idx + 1], ["1000", "1", "2", "3"])
            self.assertEqual(rows[-1], ["16000", "6", "7", "8"])
            self.assertEqual(len(rows) - header_idx - 1, 6)

    def test_one_extra_gyro_sample_uses_gyro_only(self):
        pkt = fixture_packet()
        parsed = parse_sensors([len(pkt)], pkt)
        last = parsed.gyro[-1]
        extra = type(last)(last.timestamp_us, last.scale, last.samples + ((7, 8, 9),))
        modified = type(parsed)(parsed.gyro[:-1] + (extra,), parsed.accel)
        with self.assertRaisesRegex(RepairError, "outside the accelerometer timeline"):
            analyze(Path("sample.mov"), modified)
        report = analyze(Path("sample.mov"), modified, gyro_only=True)
        self.assertEqual(report.samples, 7)
        self.assertEqual(report.accel_samples, 6)
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "sample.gcsv"
            write_gcsv(Path("sample.mov"), output, modified, report, gyro_only=True)
            with output.open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[-1], ["19000", "7", "8", "9"])


    def test_independent_sample_clocks_interpolate_one_extra_accel(self):
        # Accelerometer started 0.8 sample periods before the gyro, so
        # it has one extra sample across the same recording interval.
        parsed = parse_sensors([len(fixture_packet())], fixture_packet())
        Batch = type(parsed.gyro[0])
        Sensors = type(parsed)
        gyro = (
            Batch(2500, 1000, ((1, 2, 3), (2, 3, 4), (3, 4, 5))),
            Batch(10000, 1000, ((4, 5, 6), (5, 6, 7), (6, 7, 8))),
        )
        accel_values = [(10 * k, 20 * k, 30 * k) for k in range(7)]
        accel = (
            Batch(500, 1000, tuple(accel_values[:4])),
            Batch(10500, 1000, tuple(accel_values[4:])),
        )
        sensors = Sensors(gyro, accel)
        report = analyze(Path("sample.mov"), sensors)
        self.assertEqual(report.alignment, "interpolated")
        self.assertEqual((report.samples, report.accel_samples), (6, 7))
        self.assertAlmostEqual(report.interval_us, 2500)
        self.assertAlmostEqual(report.accel_interval_us, 2500)
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "aligned.gcsv"
            write_gcsv(Path("sample.mov"), output, sensors, report)
            with output.open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            row_index = rows.index(["t", "gx", "gy", "gz", "ax", "ay", "az"])
            samples = rows[row_index + 1:]
            self.assertEqual(len(samples), 6)
            self.assertEqual(samples[0], ["2500", "1", "2", "3", "8.0", "16.0", "24.0"])
            self.assertEqual(samples[-1], ["15000", "6", "7", "8", "58.0", "116.0", "174.0"])

    def test_mismatched_sensor_clocks_rejected(self):
        parsed = parse_sensors([len(fixture_packet())], fixture_packet())
        Batch = type(parsed.gyro[0])
        Sensors = type(parsed)
        gyro = (
            Batch(2500, 1000, ((1, 2, 3), (2, 3, 4), (3, 4, 5))),
            Batch(10000, 1000, ((4, 5, 6), (5, 6, 7), (6, 7, 8))),
        )
        accel = (
            Batch(500, 1000, ((1, 2, 3),) * 4),
            Batch(12000, 1000, ((1, 2, 3),) * 3),
        )
        with self.assertRaisesRegex(RepairError, "sample rates disagree"):
            analyze(Path("sample.mov"), Sensors(gyro, accel))

    def test_mismatched_sensor_timeline_requires_overlap(self):
        parsed = parse_sensors([len(fixture_packet())], fixture_packet())
        Batch = type(parsed.gyro[0])
        Sensors = type(parsed)
        gyro = (
            Batch(500, 1000, ((1, 2, 3), (2, 3, 4), (3, 4, 5))),
            Batch(8000, 1000, ((4, 5, 6), (5, 6, 7), (6, 7, 8))),
        )
        accel = (
            Batch(1000, 1000, ((1, 2, 3),) * 4),
            Batch(11000, 1000, ((1, 2, 3),) * 3),
        )
        with self.assertRaisesRegex(RepairError, "outside the accelerometer timeline"):
            analyze(Path("sample.mov"), Sensors(gyro, accel))

    def test_two_extra_accelerometer_samples_still_fail(self):
        pkt = fixture_packet()
        parsed = parse_sensors([len(pkt)], pkt)
        first = parsed.accel[0]
        extra = type(first)(first.timestamp_us, first.scale, first.samples + ((1, 1, 1), (2, 2, 2)))
        modified = type(parsed)(parsed.gyro, (extra,) + parsed.accel[1:])
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
