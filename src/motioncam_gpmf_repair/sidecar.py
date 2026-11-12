"""Conservative discovery and validation for MotionCam RAW Gyroflow GCSV sidecars.

This does not infer IMU/video alignment or reinterpret sensor samples. GCSV
timestamp units are given by the log's own tscale (seconds per tick).
"""
from __future__ import annotations

import csv
import hashlib
import math
import statistics
from pathlib import Path


class SidecarError(Exception):
    """Missing, malformed, or ambiguous external motion data."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar_fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
    }


def find_sidecar(video: Path) -> Path | None:
    """Find an unambiguous adjacent source log, never a generated _retimed GCSV.

    Exact stem wins; otherwise accept a prefix separated by '_' (e.g. the
    MotionCam RAW log clip.gcsv and rendered clip_BT.709_BT709.mov). Multiple
    eligible prefixes are deliberately ambiguous, not guessed.
    """
    exact = []
    prefixed = []
    for candidate in sorted(video.parent.iterdir(), key=lambda p: p.name.lower()):
        if candidate.suffix.lower() != ".gcsv" or not candidate.is_file():
            continue
        if candidate.stem.lower().endswith(("_retimed", "_aligned", "_raw_axes")):
            continue
        if candidate.stem.lower() == video.stem.lower():
            exact.append(candidate)
        elif video.stem.lower().startswith(candidate.stem.lower() + "_"):
            prefixed.append(candidate)
    options = exact if exact else prefixed
    if len(options) > 1:
        raise SidecarError(
            "Multiple matching GCSV sidecars found: " +
            ", ".join(path.name for path in options) +
            "; specify --motion explicitly"
        )
    return options[0] if options else None


def _positive_number(text: str, label: str) -> float:
    try:
        value = float(text.strip())
    except (TypeError, ValueError, OverflowError) as error:
        raise SidecarError(f"Invalid {label} in GCSV") from error
    if not math.isfinite(value) or value <= 0:
        raise SidecarError(f"{label} must be a positive finite number")
    return value


def _finite_number(text: str, label: str) -> float:
    try:
        value = float(text.strip())
    except (ValueError, OverflowError) as error:
        raise SidecarError(f"Invalid {label} in GCSV") from error
    if not math.isfinite(value):
        raise SidecarError(f"{label} must be finite")
    return value


def inspect_gcsv(path: Path) -> dict:
    """Validate Gyroflow GCSV without changing or exporting individual samples.

    Accept the trailing empty CSV column emitted by MotionCam on sample rows.
    Output contains no raw sensor values, absolute file paths or timestamp
    metadata that might expose recording time.
    """
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".gcsv":
        raise SidecarError("External motion input must be an existing .gcsv file")
    if path.stat().st_size == 0:
        raise SidecarError("External motion GCSV is empty")

    meta: dict[str, str] = {}
    header = None
    samples = 0
    first_tick = None
    last_tick = None
    intervals: list[float] = []
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as stream:
            reader = csv.reader(stream)
            for lineno, row in enumerate(reader, 1):
                if not row or all(not field.strip() for field in row):
                    continue
                if lineno == 1:
                    if len(row) != 1 or row[0].strip() != "GYROFLOW IMU LOG":
                        raise SidecarError("Not a Gyroflow GCSV (missing GYROFLOW IMU LOG header)")
                    continue

                fields = [field.strip() for field in row]
                if header is None:
                    if fields[0].lower() == "t":
                        columns = [field.lower() for field in fields]
                        if columns not in (
                            ["t", "gx", "gy", "gz"],
                            ["t", "gx", "gy", "gz", "ax", "ay", "az"],
                        ):
                            raise SidecarError("Unsupported GCSV columns: expected t,gx,gy,gz[,ax,ay,az]")
                        header = columns
                        for key in ("tscale", "gscale"):
                            if key not in meta:
                                raise SidecarError(f"Missing required GCSV metadata: {key}")
                            _positive_number(meta[key], key)
                        if len(header) == 7:
                            if "ascale" not in meta:
                                raise SidecarError("Accelerometer columns require ascale metadata")
                            _positive_number(meta["ascale"], "ascale")
                        orientation = meta.get("orientation")
                        if orientation is not None and (
                            len(orientation) != 3 or
                            {axis.upper() for axis in orientation} != {"X", "Y", "Z"}
                        ):
                            raise SidecarError("Invalid GCSV orientation metadata")
                    else:
                        if len(fields) < 2 or not fields[0]:
                            raise SidecarError(f"Malformed GCSV metadata at line {lineno}")
                        key = fields[0].lower()
                        if key in meta:
                            raise SidecarError(f"Duplicate GCSV metadata: {key}")
                        meta[key] = ",".join(fields[1:])
                    continue

                # MotionCam may append one extra empty CSV column to each row.
                if len(fields) == len(header) + 1 and fields[-1] == "":
                    fields.pop()
                if len(fields) != len(header):
                    raise SidecarError(f"Unexpected GCSV sample column count at line {lineno}")
                values = [_finite_number(value, f"sample at line {lineno}") for value in fields]
                tick = values[0]
                if last_tick is not None:
                    interval = tick - last_tick
                    if interval <= 0:
                        raise SidecarError(
                            f"Non-increasing GCSV sample timestamps at line {lineno}"
                        )
                    intervals.append(interval)
                if first_tick is None:
                    first_tick = tick
                last_tick = tick
                samples += 1
    except UnicodeError as error:
        raise SidecarError("External motion GCSV is not UTF-8 text") from error
    except csv.Error as error:
        raise SidecarError(f"Malformed GCSV CSV formatting: {error}") from error

    if header is None:
        raise SidecarError("No GCSV sample column header found")
    if samples < 2 or first_tick is None or last_tick is None:
        raise SidecarError("GCSV requires at least two motion samples")

    scale = _positive_number(meta["tscale"], "tscale")
    median_tick_step = statistics.median(intervals)
    duration = (last_tick - first_tick) * scale
    sample_rate = 1.0 / (median_tick_step * scale)
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(sample_rate):
        raise SidecarError("GCSV timestamp scale produces invalid duration or sample rate")

    return {
        "format": "gyroflow-gcsv",
        "validation": "structural_only",
        "filename": path.name,
        "version": meta.get("version"),
        "sample_count": samples,
        "accel_present": len(header) == 7,
        "orientation": meta.get("orientation"),
        "tscale_seconds_per_tick": scale,
        "median_sample_rate_hz": round(sample_rate, 6),
        "duration_s": round(duration, 6),
        "large_timestamp_intervals": sum(
            interval > 3 * median_tick_step for interval in intervals
        ),
        "note": (
            "Original Gyroflow GCSV timestamps and raw motion samples preserved. "
            "No optical synchronization or physical sample accuracy is established."
        ),
    }
