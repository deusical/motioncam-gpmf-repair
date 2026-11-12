"""Inspect GPMF sensor-clock evidence and optional Gyroflow synchronization.

All output is an aggregate: no raw IMU samples, locations, or full file paths.
GPMF batch STMP interpretation is not established by these measurements.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .core import RepairError, SensorBatch, _fitted_period_us, _run, extract_gpmd
from .diagnostics import _packet_times, collect_diagnostic
from .validation import (
    packet_pts_summary, packet_video_pts_alignment, quality_assessment,
    video_pts_summary,
)


class InspectError(Exception):
    """Unusable diagnostic, project, or input file."""


def _rounded(value: float | int, digits: int = 4) -> float:
    return round(float(value), digits)


def _file_basename(value: str) -> str:
    """Return a filename from a Gyroflow URI without disclosing directories."""
    path = unquote(urlsplit(value).path) if "://" in value else value
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _fit_anchor_intercept(batches: list[dict], period: float) -> tuple[float, list[int]]:
    indices: list[int] = []
    cumulative = 0
    for batch in batches:
        indices.append(cumulative)
        cumulative += len(batch["samples"])
    intercept = sum(
        float(batch["timestamp_us"]) - index * period
        for batch, index in zip(batches, indices)
    ) / len(batches)
    return intercept, indices


def _stream_summary(name: str, batches: list[dict]) -> dict:
    count = sum(len(batch["samples"]) for batch in batches)
    report: dict = {"batches": len(batches), "samples": count}
    if len(batches) < 2:
        report["error"] = f"At least two {name} batches required"
        return report
    try:
        converted = tuple(SensorBatch(
            int(batch["timestamp_us"]), int(batch["scale"]),
            tuple(tuple(x) for x in batch["samples"]),
        ) for batch in batches)
        if any(not b.samples for b in converted):
            raise RepairError("Empty sensor batch")
        if len({b.scale for b in converted}) != 1:
            raise RepairError("Variable sensor scale")
        period = _fitted_period_us(converted, name)
    except (RepairError, KeyError, TypeError, ValueError, OverflowError) as error:
        report["error"] = str(error)
        return report

    intercept, indices = _fit_anchor_intercept(batches, period)
    first_ts = int(batches[0]["timestamp_us"])
    fitted_residual_ms = [
        (float(b["timestamp_us"]) - intercept - index * period) / 1000
        for b, index in zip(batches, indices)
    ]
    anchored_residual_ms = [
        (float(b["timestamp_us"]) - first_ts - index * period) / 1000
        for b, index in zip(batches, indices)
    ]
    size_counts = Counter(len(b["samples"]) for b in batches)
    return {
        **report,
        "rate_hz": _rounded(1_000_000 / period, 6),
        "interval_us": _rounded(period, 8),
        "first_batch_timestamp_us": first_ts,
        "last_sample_estimate_us": _rounded(first_ts + (count - 1) * period, 3),
        "batch_sizes_most_common": [
            {"samples_per_batch": size, "batches": n}
            for size, n in size_counts.most_common(5)
        ],
        "stmp": {
            "first_anchored_median_abs_residual_ms": _rounded(
                statistics.median(map(abs, anchored_residual_ms))
            ),
            "fitted_intercept_median_abs_residual_ms": _rounded(
                statistics.median(map(abs, fitted_residual_ms))
            ),
            "fitted_intercept_max_abs_residual_ms": _rounded(
                max(map(abs, fitted_residual_ms))
            ),
            "first_timestamp_minus_fitted_intercept_ms": _rounded(
                (first_ts - intercept) / 1000
            ),
            "interpretation": (
                "Diagnostic only. STMP may not timestamp the first sample in each "
                "GPMF batch; these residuals do not measure per-sample gyro jitter."
            ),
        },
    }


def inspect_sensor_diagnostic(data: dict) -> dict:
    """Summarize a diagnostic without exposing its raw motion samples."""
    if not isinstance(data, dict) or data.get("format") != "motioncam-gpmf-imu-diagnostic":
        raise InspectError("Expected a motioncam-gpmf-imu-diagnostic JSON object")
    tracks = data.get("tracks")
    if not isinstance(tracks, dict):
        raise InspectError("Diagnostic has no sensor tracks")
    gyro = tracks.get("gyro", [])
    accel = tracks.get("accel", [])
    if not isinstance(gyro, list) or not isinstance(accel, list):
        raise InspectError("Diagnostic tracks must be lists")
    result: dict = {
        "status": "ok",
        "gyro": _stream_summary("gyro", gyro),
        "accel": _stream_summary("accelerometer", accel) if accel else None,
        "packet_count": len(data.get("packet_sizes") or []),
        "clock_alignment": {"status": "unknown"},
        "packet_timing": packet_pts_summary(data),
        "warnings": [],
    }
    warnings = result["warnings"]
    g, a = result["gyro"], result["accel"]
    declared = data.get("counts") or {}
    for name, summary in (("gyro", g), ("accel", a)):
        if summary is not None and name in declared and declared[name] != summary["samples"]:
            warnings.append(f"{name} diagnostic sample count disagrees with track contents")
            result["status"] = "warning"

    if "error" in g:
        result["status"] = "error"
        warnings.append(f"Gyroscope clock invalid: {g['error']}")
        return result
    if a is None:
        result["status"] = "warning"
        warnings.append("No accelerometer samples; cross-sensor alignment cannot be validated")
        return result
    if "error" in a:
        result["status"] = "error"
        warnings.append(f"Accelerometer clock invalid: {a['error']}")
        return result

    g_period = g["interval_us"]
    a_period = a["interval_us"]
    rate_difference_ppm = abs(g_period - a_period) / g_period * 1_000_000
    first_delta_us = a["first_batch_timestamp_us"] - g["first_batch_timestamp_us"]
    start_gap_us = max(0.0, float(first_delta_us))
    end_gap_us = max(0.0, g["last_sample_estimate_us"] - a["last_sample_estimate_us"])
    count_diff = g["samples"] - a["samples"]
    # These thresholds match the current converter's fail-closed policy.
    issues = []
    if abs(count_diff) > 1:
        issues.append("Sensor sample counts differ by more than one")
    if rate_difference_ppm > 1000:
        issues.append("Sensor clock rates differ by more than 1000 ppm")
    if max(start_gap_us, end_gap_us) > a_period * (1 + 1e-6):
        issues.append("Accelerometer does not cover gyro endpoints within one sample period")
    result["clock_alignment"] = {
        "status": "warning" if issues else "compatible",
        "gyro_minus_accel_sample_count": count_diff,
        "sample_interval_difference_ppm": _rounded(rate_difference_ppm, 3),
        "accel_start_minus_gyro_start_us": _rounded(first_delta_us, 3),
        "accel_clamp_start_us": _rounded(start_gap_us, 3),
        "accel_clamp_end_us": _rounded(end_gap_us, 3),
        "model": "Independent constant-rate clocks, resampled at gyro timestamps",
        "limitations": (
            "Clock compatibility does not verify absolute video synchronization, "
            "sensor-axis orientation, or the absence of individual missing samples."
        ),
    }
    if issues:
        result["status"] = "warning"
        warnings.extend(issues)
    if g["stmp"]["first_anchored_median_abs_residual_ms"] > 10:
        warnings.append(
            "Large first-anchored STMP residual; compare fitted-intercept residual "
            "before interpreting it as jitter or video-sync error"
        )
    return result


def inspect_gyroflow_project(path: Path) -> dict:
    """Read only relevant small project fields; ignore embedded motion caches."""
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            project = json.load(f)
    except (OSError, ValueError) as error:
        raise InspectError(f"Cannot parse Gyroflow project: {error}") from error
    if not isinstance(project, dict) or project.get("title") != "Gyroflow data file":
        raise InspectError("Not a recognized Gyroflow project")
    source = project.get("gyro_source") or {}
    video = project.get("video_info") or {}
    calibration = project.get("calibration_data") or {}
    raw_offsets = project.get("offsets") or {}
    if not isinstance(raw_offsets, dict):
        raise InspectError("Gyroflow offsets must be an object")
    points = []
    for time_us, offset_ms in raw_offsets.items():
        try:
            t = int(time_us) / 1_000_000
            offset = float(offset_ms)
        except (TypeError, ValueError, OverflowError) as error:
            raise InspectError("Invalid Gyroflow sync-point timestamp or offset") from error
        if not math.isfinite(t) or not math.isfinite(offset):
            raise InspectError("Non-finite Gyroflow sync-point value")
        points.append({"video_time_s": _rounded(t, 6), "offset_ms": _rounded(offset, 6)})
    points.sort(key=lambda x: x["video_time_s"])
    offsets = [point["offset_ms"] for point in points]
    report = {
        "app_version": project.get("app_version"),
        "video_filename": _file_basename(str(project.get("videofile") or "")),
        "motion_filename": _file_basename(str(source.get("filepath") or "")),
        "lens_profile": calibration.get("name"),
        "video_resolution": (
            f"{video.get('width')}x{video.get('height')}"
            if video.get("width") and video.get("height") else None
        ),
        "video_fps": video.get("fps"),
        "imu_orientation": source.get("imu_orientation"),
        "rolling_shutter_readout_ms": calibration.get("frame_readout_time"),
        "sync_points": points,
        "sync_point_count": len(points),
        "interpretation": (
            "Optical synchronization is an external validation signal. "
            "A few points cannot prove the absence of drift or dropped samples."
        ),
    }
    if offsets:
        report["median_offset_ms"] = _rounded(statistics.median(offsets), 4)
        report["offset_span_ms"] = _rounded(max(offsets) - min(offsets), 4)
    if len(points) >= 2:
        times = [point["video_time_s"] for point in points]
        mean_t = statistics.mean(times)
        mean_y = statistics.mean(offsets)
        denominator = sum((x - mean_t) ** 2 for x in times)
        if denominator > 0:
            slope = sum(
                (x - mean_t) * (y - mean_y) for x, y in zip(times, offsets)
            ) / denominator
            report["exploratory_linear_trend_ms_per_minute"] = _rounded(slope * 60, 5)
            report["exploratory_linear_trend_ppm"] = _rounded(slope * 1000, 3)
    return report


def inspect_file(path: Path, project_path: Path | None = None) -> dict:
    if not path.is_file():
        raise InspectError(f"Input file not found: {path}")
    result: dict = {
        "format": "motioncam-inspection",
        "version": 1,
        "source_filename": path.name,
        "video": None,
        "video_timing": None,
        "packet_video_timing": None,
        "imu": None,
        "gyroflow": None,
        "warnings": [],
    }
    suffix = path.name.lower()
    diagnostic = None
    if suffix.endswith(".json.gz"):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                diagnostic = json.load(handle)
        except (OSError, EOFError, ValueError) as error:
            raise InspectError(f"Unable to read compressed diagnostic: {error}") from error
        result["imu"] = inspect_sensor_diagnostic(diagnostic)
    elif path.suffix.lower() in {".mov", ".mp4"}:
        try:
            probe = json.loads(_run([
                "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)
            ]))
        except (RepairError, ValueError) as error:
            raise InspectError(f"Cannot inspect video streams: {error}") from error
        streams = probe.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        result["video"] = ({
            "width": video.get("width"),
            "height": video.get("height"),
            "codec": video.get("codec_name"),
            "fps": video.get("avg_frame_rate"),
        } if video else None)
        try:
            video_packets = json.loads(_run([
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_packets", "-show_entries", "packet=pts_time,duration_time",
                "-of", "json", str(path),
            ]))
            result["video_timing"] = video_pts_summary(video_packets)
        except (RepairError, ValueError) as error:
            video_packets = None
            result["video_timing"] = {
                "status": "unavailable",
                "reason": "Cannot inspect video frame PTS; conversion checks remain independent",
            }
            result["warnings"].append("Video packet PTS unavailable for comparison")
        if not any(s.get("codec_tag_string") == "gpmd" for s in streams):
            result["imu"] = {"status": "missing", "warnings": [
                "Video has no embedded gpmd track; check for a separate motion log"
            ]}
        else:
            try:
                sizes, raw = extract_gpmd(path)
                diagnostic = collect_diagnostic(sizes, raw, _packet_times(path, sizes))
                result["imu"] = inspect_sensor_diagnostic(diagnostic)
                if video_packets is not None:
                    result["packet_video_timing"] = packet_video_pts_alignment(
                        diagnostic, video_packets,
                    )
            except RepairError as error:
                result["imu"] = {"status": "error", "warnings": [str(error)]}
    else:
        raise InspectError("Input must be a MotionCam MOV/MP4 or an IMU .json.gz diagnostic")

    result["warnings"].extend(result["imu"].get("warnings", []))
    if project_path:
        if not project_path.is_file():
            raise InspectError(f"Gyroflow project file not found: {project_path}")
        result["gyroflow"] = inspect_gyroflow_project(project_path)
        if (
            path.suffix.lower() in {".mov", ".mp4"}
            and result["gyroflow"]["video_filename"]
            and result["gyroflow"]["video_filename"] != path.name
        ):
            result["warnings"].append("Gyroflow project references a different video filename")
    else:
        result["warnings"].append(
            "No Gyroflow project supplied; optical synchronization cannot be assessed"
        )
    result["quality"] = quality_assessment(result)
    return result


def format_report(report: dict) -> str:
    lines = [f"MotionCam inspection: {report['source_filename']}"]
    quality = report.get("quality", {})
    if quality:
        lines.append(f"  Automated check: {quality['status'].upper()} "
                     "(not a guarantee of good stabilization)")
    video = report.get("video")
    if video:
        lines.append(
            f"  Video: {video['width']}x{video['height']}, {video['codec']}, {video['fps']} fps"
        )
    imu = report["imu"]
    if imu["status"] in {"missing", "error"}:
        lines.append(f"  IMU: {imu['status']}")
    else:
        gyro = imu["gyro"]
        accel = imu.get("accel")
        lines.append(
            f"  Gyro: {gyro['samples']:,} samples / {gyro['batches']:,} batches; "
            f"{gyro.get('rate_hz', 'unknown')} Hz"
        )
        if accel:
            lines.append(
                f"  Accel: {accel['samples']:,} samples / {accel['batches']:,} batches; "
                f"{accel.get('rate_hz', 'unknown')} Hz"
            )
        alignment = imu["clock_alignment"]
        if alignment.get("status") != "unknown":
            lines.append(
                f"  Relative clocks: {alignment['status']}; "
                f"difference {alignment['sample_interval_difference_ppm']} ppm; "
                f"accel starts {alignment['accel_start_minus_gyro_start_us']} us "
                "after gyro"
            )
            lines.append(
                f"  Accel edge hold: start {alignment['accel_clamp_start_us']} us; "
                f"end {alignment['accel_clamp_end_us']} us"
            )
        if "stmp" in gyro:
            stmp = gyro["stmp"]
            lines.append(
                "  GPMF STMP residual (median absolute): "
                f"first-anchored {stmp['first_anchored_median_abs_residual_ms']} ms; "
                f"fitted-intercept {stmp['fitted_intercept_median_abs_residual_ms']} ms"
            )
            lines.append(
                "  First timestamp vs fitted intercept: "
                f"{stmp['first_timestamp_minus_fitted_intercept_ms']} ms "
                "(timestamp semantics not established)"
            )
        packet = imu.get("packet_timing") or {}
        if packet.get("status") in {"observed", "warning"}:
            for name in ("gyro", "accel"):
                stream = packet.get("streams", {}).get(name) or {}
                if "median_packet_pts_minus_stmp_ms" in stream:
                    lines.append(
                        f"  {name.title()} metadata packet PTS - STMP: median "
                        f"{stream['median_packet_pts_minus_stmp_ms']} ms; "
                        f"5-95% spread {stream['p05_p95_spread_ms']} ms"
                    )
    frame_timing = report.get("video_timing") or {}
    if frame_timing.get("status") == "observed":
        lines.append(
            f"  Video frame packet PTS: median step "
            f"{frame_timing['median_frame_pts_interval_ms']} ms; "
            f"{frame_timing['intervals_longer_than_1_5x_median']} larger gaps"
        )
    packet_alignment = report.get("packet_video_timing") or {}
    if packet_alignment.get("status") == "observed":
        lines.append(
            f"  Nearest video/metadata packet PTS distance: median "
            f"{packet_alignment['median_nearest_video_pts_distance_ms']} ms "
            "(container timing only)"
        )
    project = report.get("gyroflow")
    if project:
        lines.append(
            f"  Gyroflow {project['app_version']}: "
            f"{project['sync_point_count']} optical sync points; "
            f"orientation {project['imu_orientation'] or 'unspecified'}"
        )
        if project["sync_point_count"]:
            lines.append(
                f"  Sync offset: median {project['median_offset_ms']} ms; "
                f"range {project['offset_span_ms']} ms"
            )
        if "exploratory_linear_trend_ppm" in project:
            lines.append(
                "  Exploratory sync trend: "
                f"{project['exploratory_linear_trend_ppm']} ppm "
                "(not proof of clock drift)"
            )
    if quality:
        optical = quality["optical_sync"]
        lines.append(f"  Optical validation: {optical['status']} — {optical['reason']}")
        lines.append("  Manual visual verification is still required")
    for warning in report["warnings"]:
        lines.append(f"  NOTE: {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="motioncam-gpmf-inspect",
        description="Inspect sensor clock integrity, batch timing, and optional Gyroflow sync"
    )
    parser.add_argument("input", type=Path, help="MOV/MP4 or previously exported IMU .json.gz diagnostic")
    parser.add_argument("--project", type=Path, help="Optional synchronized .gyroflow project")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary to stdout")
    parser.add_argument("-o", "--output", type=Path, help="Write sanitized JSON summary to a file")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing JSON summary")
    args = parser.parse_args(argv)
    try:
        result = inspect_file(args.input, args.project)
        if args.output:
            protected = [args.input.resolve()]
            if args.project is not None:
                protected.append(args.project.resolve())
            if args.output.resolve() in protected:
                raise InspectError("Report destination cannot overwrite input or Gyroflow project")
            if args.output.exists() and not args.overwrite:
                raise InspectError(f"Report exists: {args.output} (use --overwrite)")
            args.output.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
    except (InspectError, OSError, TypeError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_report(result))
        if args.output:
            print(f"  Saved report: {args.output.name}")
    return 2 if result["imu"]["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
