"""Parse MotionCam GPMF sensor samples and reconstruct an experimental GCSV timeline.

This implementation reconstructs sample timing and preserves native GPMF axis order.
Camera-to-IMU orientation must be determined separately (e.g. in Gyroflow).
This is not a general GPMF decoder or a fix for dropped/variable-rate samples.
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

GRAVITY = 9.80665


class RepairError(Exception):
    """A recoverable input, decoding, or conversion error."""


@dataclass(frozen=True)
class SensorBatch:
    timestamp_us: int
    scale: int
    samples: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class SensorData:
    gyro: tuple[SensorBatch, ...]
    accel: tuple[SensorBatch, ...]


@dataclass(frozen=True)
class RepairReport:
    source: str
    gyro_batches: int
    accel_batches: int
    samples: int
    accel_samples: int
    rate_hz: float
    interval_us: float
    accel_interval_us: float | None
    alignment: str
    accel_clamp_start_us: float
    accel_clamp_end_us: float
    timestamp_mismatch_max_ms: float
    timestamp_mismatch_median_ms: float
    output: str | None


def gpmf_entries(data: bytes) -> Iterator[tuple[bytes, int, bytes]]:
    """Yield (fourcc, type, payload) for bounded 4-byte-aligned GPMF records.

    MotionCam may append incomplete vendor trailers. We ignore those after
    complete entries, but reject malformed first entries.
    """
    i = 0
    while i + 8 <= len(data):
        if not any(data[i:]):
            return
        key = data[i : i + 4]
        type_code = data[i + 4]
        record_size = data[i + 5] * int.from_bytes(data[i + 6 : i + 8], "big")
        end = i + 8 + record_size
        if record_size <= 0 or end > len(data):
            if i == 0:
                raise RepairError(f"Malformed GPMF entry at byte offset {i} ({key!r})")
            return
        yield key, type_code, data[i + 8 : end]
        i += 8 + ((record_size + 3) // 4) * 4


def sensor_streams(packet: bytes) -> Iterator[dict[bytes, bytes]]:
    if len(packet) >= 8 and packet[4:8] == b"GPMF":
        packet = packet[8:]

    def visit(node: bytes) -> Iterator[dict[bytes, bytes]]:
        for key, typ, value in gpmf_entries(node):
            if key == b"STRM":
                fields = {k: v for k, _, v in gpmf_entries(value)}
                if b"STMP" in fields and b"SCAL" in fields:
                    yield fields
            elif typ == 0:
                yield from visit(value)

    yield from visit(packet)


def _run(command: list[str]) -> bytes:
    if not shutil.which(command[0]):
        raise RepairError(f"Required executable not found on PATH: {command[0]}")
    try:
        return subprocess.check_output(command, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace")[-1200:]
        raise RepairError(f"{command[0]} failed (exit {exc.returncode}): {detail}") from exc


def extract_gpmd(path: Path) -> tuple[list[int], bytes]:
    """Extract the embedded gpmd track and its packet sizes with FFmpeg."""
    probe = json.loads(_run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))
    streams = [s for s in probe.get("streams", []) if s.get("codec_tag_string") == "gpmd"]
    if len(streams) != 1:
        raise RepairError(f"Expected one embedded gpmd track; found {len(streams)}. Try a separate .gcv log if one exists.")
    index = int(streams[0]["index"])
    packets = json.loads(_run([
        "ffprobe", "-v", "error", "-select_streams", str(index),
        "-show_packets", "-show_entries", "packet=size", "-of", "json", str(path),
    ]))
    sizes = [int(p["size"]) for p in packets.get("packets", [])]
    if not sizes or any(size <= 0 for size in sizes):
        raise RepairError("No usable GPMF metadata packets found")
    raw = _run(["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:{index}", "-c", "copy", "-f", "data", "-"])
    if sum(sizes) != len(raw):
        raise RepairError(f"GPMF packet boundaries mismatch: expected {sum(sizes)} bytes, got {len(raw)}")
    return sizes, raw


def parse_sensors(packet_sizes: list[int], raw: bytes) -> SensorData:
    if sum(packet_sizes) != len(raw):
        raise RepairError("Provided packet sizes do not match GPMF payload length")
    tracks: dict[bytes, list[SensorBatch]] = {b"GYRO": [], b"ACCL": []}
    offset = 0
    for length in packet_sizes:
        packet = raw[offset : offset + length]
        offset += length
        for fields in sensor_streams(packet):
            for sensor in tracks:
                if sensor not in fields:
                    continue
                blob = fields[sensor]
                if len(blob) % 6:
                    raise RepairError(f"Unexpected {sensor.decode()} sample format (expected signed 16-bit XYZ)")
                scale = int.from_bytes(fields[b"SCAL"], "big", signed=True)
                if scale <= 0:
                    raise RepairError("Invalid GPMF sensor scale")
                ts = int.from_bytes(fields[b"STMP"], "big", signed=False)
                tracks[sensor].append(SensorBatch(ts, scale, tuple(struct.iter_unpack(">hhh", blob))))
    return SensorData(tuple(tracks[b"GYRO"]), tuple(tracks[b"ACCL"]))


def _sensor_scale(batches: tuple[SensorBatch, ...], label: str) -> int:
    if not batches:
        raise RepairError(f"No {label} samples in the metadata track")
    scales = {b.scale for b in batches}
    if len(scales) != 1:
        raise RepairError(f"Variable {label} sensor scales are not supported")
    return next(iter(scales))


def _fitted_period_us(batches: tuple[SensorBatch, ...], label: str) -> float:
    """Estimate a uniform sensor clock from all GPMF batch start anchors.

    Batch timestamps can be delayed/jittery (e.g. when batches span video
    frames), so individual timestamp residuals are *diagnostic* rather than
    proof of dropped samples. Enforce chronological anchors and reject clear
    changes in long-term rate without assuming device-specific sample rates.
    """
    if len(batches) < 2:
        raise RepairError(f"At least two {label} batches are needed to infer a sample period")
    indices: list[int] = []
    timestamps: list[int] = []
    cumulative = 0
    previous_timestamp = None
    for batch in batches:
        if not batch.samples:
            raise RepairError(f"Empty {label} sensor batch")
        if previous_timestamp is not None and batch.timestamp_us <= previous_timestamp:
            raise RepairError(f"Non-increasing {label} batch timestamps")
        indices.append(cumulative)
        timestamps.append(batch.timestamp_us)
        cumulative += len(batch.samples)
        previous_timestamp = batch.timestamp_us

    def slope(start: int, stop: int) -> float:
        xs = indices[start:stop]
        ys = timestamps[start:stop]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        denominator = sum((x - mean_x) ** 2 for x in xs)
        if denominator <= 0:
            raise RepairError(f"Cannot infer {label} sampling rate")
        return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator

    interval = slope(0, len(indices))
    if not math.isfinite(interval) or not 250 <= interval <= 20000:
        raise RepairError(f"Implausible {label} sample period: {interval} us")
    if len(batches) >= 20:
        middle = len(batches) // 2
        early = slope(0, middle)
        late = slope(middle, len(batches))
        if not math.isfinite(early) or not math.isfinite(late) or (
            abs(early - late) / interval > 0.005
        ):
            raise RepairError(
                f"Inconsistent {label} sampling rate between recording halves; "
                "possible timestamp discontinuity or missing samples"
            )
    return interval


def analyze(source: Path, sensors: SensorData, *, gyro_only: bool = False) -> RepairReport:
    gyro, accel = sensors.gyro, sensors.accel
    _sensor_scale(gyro, "GYRO")
    n_g = sum(len(batch.samples) for batch in gyro)
    n_a = sum(len(batch.samples) for batch in accel)

    # Always infer the gyro clock the same way, regardless of sample counts.
    interval_us = _fitted_period_us(gyro, "gyro")
    rate_hz = 1_000_000 / interval_us
    if not math.isfinite(rate_hz) or not 50 <= rate_hz <= 4000:
        raise RepairError(f"Implausible gyro rate: {rate_hz:.2f} Hz")

    alignment = "gyro-only" if gyro_only else "interpolated"
    accel_interval_us = None
    accel_clamp_start_us = 0.0
    accel_clamp_end_us = 0.0
    if not gyro_only:
        _sensor_scale(accel, "ACCL")
        if abs(n_g - n_a) > 1:
            raise RepairError(
                f"Mismatched sensor sample counts (gyro={n_g}, accel={n_a}); "
                "automatic alignment currently supports count differences of at most one. "
                "Inspect the IMU diagnostic for dropped or independently sampled streams."
            )
        # Fit the independent accelerometer clock even if sample counts MATCH.
        # Equal counts alone do not establish temporal alignment.
        accel_interval_us = _fitted_period_us(accel, "accelerometer")
        drift = abs(interval_us - accel_interval_us) / interval_us
        if drift > 0.001:
            raise RepairError(
                f"Gyro/accelerometer sample rates disagree ({drift * 100:.3f}%); "
                "not safe to resample automatically."
            )
        gyro_first = gyro[0].timestamp_us
        gyro_last = gyro_first + (n_g - 1) * interval_us
        accel_first = accel[0].timestamp_us
        accel_last = accel_first + (n_a - 1) * accel_interval_us
        # Use nearest observed acceleration at short uncovered boundaries.
        # Do NOT linearly extrapolate, and do NOT fabricate missing samples.
        accel_clamp_start_us = max(0.0, accel_first - gyro_first)
        accel_clamp_end_us = max(0.0, gyro_last - accel_last)
        if max(accel_clamp_start_us, accel_clamp_end_us) > accel_interval_us * (1 + 1e-6):
            raise RepairError(
                "Gyro timestamps extend more than one sample interval outside "
                "the accelerometer timeline; refusing to fill a large gap. "
                "Inspect the IMU diagnostic."
            )

    deviations = []
    cumulative = 0
    for batch in gyro:
        expected = gyro[0].timestamp_us + cumulative * interval_us
        deviations.append(abs(batch.timestamp_us - expected) / 1000)
        cumulative += len(batch.samples)
    deviations.sort()
    middle = len(deviations) // 2
    median = (deviations[middle] if len(deviations) % 2
              else (deviations[middle - 1] + deviations[middle]) / 2)
    return RepairReport(
        source=source.name, gyro_batches=len(gyro), accel_batches=len(accel),
        samples=n_g, accel_samples=n_a, rate_hz=rate_hz, interval_us=interval_us,
        accel_interval_us=accel_interval_us, alignment=alignment,
        accel_clamp_start_us=accel_clamp_start_us, accel_clamp_end_us=accel_clamp_end_us,
        timestamp_mismatch_max_ms=deviations[-1],
        timestamp_mismatch_median_ms=median, output=None,
    )


def validate_imu_orientation(value: str | None) -> None:
    """Validate optional Gyroflow axis mapping without applying it to samples."""
    if value is not None and (len(value) != 3 or set(value.upper()) != {"X", "Y", "Z"}):
        raise RepairError("IMU orientation must be exactly one of each X, Y, Z (case indicates axis inversion)")


def write_gcsv(source: Path, dest: Path, sensors: SensorData, report: RepairReport, *,
               gyro_only: bool = False, imu_orientation: str | None = None) -> None:
    """Write original GPMF XYZ values with reconstructed timestamps.

    An optional orientation is saved as Gyroflow metadata. It does not modify
    the measurements, so Gyroflow applies that mapping exactly once.
    """
    validate_imu_orientation(imu_orientation)
    scale_g = _sensor_scale(sensors.gyro, "GYRO")
    include_accel = not gyro_only and report.alignment != "gyro-only"
    scale_a = _sensor_scale(sensors.accel, "ACCL") if include_accel else None
    with dest.open("w", newline="", encoding="utf-8") as handle:
        handle.write("GYROFLOW IMU LOG\n")
        writer = csv.writer(handle, lineterminator="\n")
        metadata = [
            ("version", "1.3"),
            ("id", f"{source.stem}_MotionCam_retimed"),
            ("vendor", "MotionCam"),
            ("note", "Experimental reconstructed GPMF timing; raw sensor axes preserved; verify orientation and sync"),
            ("videofilename", source.name),
            ("tscale", "0.000001"),
            ("gscale", str(1.0 / scale_g)),
        ]
        if imu_orientation is not None:
            metadata.append(("orientation", imu_orientation))
        if include_accel:
            metadata.append(("ascale", str(1.0 / (scale_a * GRAVITY))))
        metadata.append(("t", "gx", "gy", "gz", "ax", "ay", "az") if include_accel else ("t", "gx", "gy", "gz"))
        for row in metadata:
            writer.writerow(row)
        first = sensors.gyro[0].timestamp_us
        gyro_samples = (xyz for batch in sensors.gyro for xyz in batch.samples)
        if include_accel:
            # Both equal- and unequal-count recordings take this same path.
            # Sample clocks are fitted independently and acceleration is
            # interpolated at each reconstructed gyro timestamp.
            acceleration = [sample for batch in sensors.accel for sample in batch.samples]
            accel_first = sensors.accel[0].timestamp_us
            accel_period = report.accel_interval_us
            if accel_period is None:
                raise RepairError("Missing accelerometer sample period for interpolation")
            if len(acceleration) < 2:
                raise RepairError("Need at least two accelerometer readings for interpolation")
            for idx, (gx, gy, gz) in enumerate(gyro_samples):
                time_us = first + idx * report.interval_us
                accel_position = (time_us - accel_first) / accel_period
                lower = min(len(acceleration) - 2, max(0, math.floor(accel_position)))
                # At uncovered edges, hold the first/last recorded sample.
                weight = min(1.0, max(0.0, accel_position - lower))
                ax, ay, az = (
                    acceleration[lower][axis] * (1.0 - weight)
                    + acceleration[lower + 1][axis] * weight
                    for axis in range(3)
                )
                writer.writerow([
                    round(time_us), gx, gy, gz,
                    round(ax, 6), round(ay, 6), round(az, 6),
                ])
        else:
            for idx, (gx, gy, gz) in enumerate(gyro_samples):
                writer.writerow([round(first + idx * report.interval_us), gx, gy, gz])



def repair(source: Path, destination: Path | None = None, *, overwrite: bool = False,
           dry_run: bool = False, gyro_only: bool = False,
           imu_orientation: str | None = None) -> RepairReport:
    validate_imu_orientation(imu_orientation)
    if not source.is_file():
        raise RepairError(f"File does not exist: {source}")
    destination = destination or source.with_name(source.stem + "_retimed.gcsv")
    if source.resolve() == destination.resolve():
        raise RepairError("Output must differ from input")
    if not dry_run and destination.exists() and not overwrite:
        raise RepairError(f"Output already exists: {destination} (pass --overwrite to replace)")
    sizes, raw = extract_gpmd(source)
    sensors = parse_sensors(sizes, raw)
    report = analyze(source, sensors, gyro_only=gyro_only)
    if not dry_run:
        if not destination.parent.is_dir():
            raise RepairError(f"Output folder does not exist: {destination.parent}")
        import os
        import tempfile
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".motioncam-", suffix=".gcsv", delete=False
            ) as tmp:
                temp_name = Path(tmp.name)
            write_gcsv(source, temp_name, sensors, report, gyro_only=gyro_only,
                       imu_orientation=imu_orientation)
            os.replace(temp_name, destination)
        finally:
            if temp_name is not None:
                temp_name.unlink(missing_ok=True)
    return RepairReport(**{**report.__dict__, "output": None if dry_run else str(destination)})
