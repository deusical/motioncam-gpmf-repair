"""Conservative quality signals for MotionCam timing inspections.

A 'pass' here means only that the specified, automated checks passed.
It does not establish correct camera-to-IMU orientation, absolute capture
timestamps, successful stabilization, or absence of dropped IMU samples.
"""
from __future__ import annotations

import bisect
import math
import statistics


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _percentile(values: list[float], fraction: float) -> float:
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * fraction
    low = int(math.floor(position))
    high = int(math.ceil(position))
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def packet_pts_summary(diagnostic: dict) -> dict:
    """Compare per-stream GPMF STMP to the enclosing metadata packet PTS.

    PTS belongs to the metadata *packet*, not necessarily an IMU sample or
    the corresponding video exposure. Do not derive an automatic correction.
    """
    times = diagnostic.get("packet_times")
    sizes = diagnostic.get("packet_sizes")
    if not isinstance(times, list) or not times:
        return {"status": "unavailable", "reason": "No metadata packet PTS in diagnostic"}
    if not isinstance(sizes, list) or len(sizes) != len(times):
        return {"status": "warning", "reason": "Packet PTS/count boundary mismatch"}
    packet_pts = []
    for item in times:
        pts = _number(item.get("pts_time")) if isinstance(item, dict) else None
        packet_pts.append(pts)
    if not any(x is not None for x in packet_pts):
        return {"status": "unavailable", "reason": "Metadata packets have no numeric PTS"}
    valid_pts = [x for x in packet_pts if x is not None]
    monotonic_violations = sum(
        right < left for left, right in zip(valid_pts, valid_pts[1:])
    )
    result: dict = {
        "status": "observed" if not monotonic_violations else "warning",
        "packet_count": len(times),
        "packets_with_pts": len(valid_pts),
        "nonmonotonic_packet_pts": monotonic_violations,
        "streams": {},
        "interpretation": (
            "Metadata-packet presentation time minus batch STMP, not an "
            "absolute video/IMU synchronization offset. A near-frame offset "
            "must not be automatically applied as a timing correction."
        ),
    }
    for name in ("gyro", "accel"):
        batches = diagnostic.get("tracks", {}).get(name, [])
        differences_ms: list[float] = []
        unavailable = 0
        for batch in batches:
            index = batch.get("packet_index") if isinstance(batch, dict) else None
            stamp = _number(batch.get("timestamp_us")) if isinstance(batch, dict) else None
            if not isinstance(index, int) or not 0 <= index < len(packet_pts) or (
                packet_pts[index] is None or stamp is None
            ):
                unavailable += 1
                continue
            differences_ms.append(packet_pts[index] * 1000 - stamp / 1000)
        stream: dict = {"matched_batches": len(differences_ms), "unmatched_batches": unavailable}
        if differences_ms:
            stream.update({
                "median_packet_pts_minus_stmp_ms": _round(statistics.median(differences_ms)),
                "p05_packet_pts_minus_stmp_ms": _round(_percentile(differences_ms, 0.05)),
                "p95_packet_pts_minus_stmp_ms": _round(_percentile(differences_ms, 0.95)),
                "p05_p95_spread_ms": _round(
                    _percentile(differences_ms, 0.95) -
                    _percentile(differences_ms, 0.05)
                ),
            })
        if unavailable:
            result["status"] = "warning"
        result["streams"][name] = stream
    return result


def video_pts_summary(packet_data: dict) -> dict:
    """Describe video-frame PTS spacing without decoding frames.

    Presentation timestamps are sorted because encoded packets can be stored
    in decode order (including reordered B-frames).
    """
    packets = packet_data.get("packets")
    if not isinstance(packets, list):
        return {"status": "unavailable", "reason": "No video packet list"}
    pts = sorted(set(
        value for packet in packets
        if isinstance(packet, dict)
        if (value := _number(packet.get("pts_time"))) is not None
    ))
    if len(pts) < 2:
        return {"status": "unavailable", "reason": "Fewer than two valid video PTS values"}
    intervals_ms = [
        (right - left) * 1000 for left, right in zip(pts, pts[1:])
    ]
    typical = statistics.median(intervals_ms)
    return {
        "status": "observed",
        "video_packet_count": len(packets),
        "valid_distinct_presentation_timestamps": len(pts),
        "median_frame_pts_interval_ms": _round(typical, 6),
        "p05_frame_pts_interval_ms": _round(_percentile(intervals_ms, 0.05), 6),
        "p95_frame_pts_interval_ms": _round(_percentile(intervals_ms, 0.95), 6),
        "intervals_longer_than_1_5x_median": sum(
            interval > 1.5 * typical for interval in intervals_ms
        ),
        "interpretation": (
            "Video packet PTS spacing, not physical exposure timing; "
            "gaps can reflect variable-frame-rate recording or missing frames."
        ),
    }


def packet_video_pts_alignment(diagnostic: dict, video_packets: dict) -> dict:
    """Check whether GPMF metadata packets share the video's PTS clock.

    Nearest presentation timestamp distance detects gross container timeline
    mismatches, not motion/optical synchronization.
    """
    video_pts = sorted(set(
        value for packet in video_packets.get("packets", [])
        if isinstance(packet, dict)
        if (value := _number(packet.get("pts_time"))) is not None
    ))
    metadata_times = diagnostic.get("packet_times") or []
    if not video_pts or not metadata_times:
        return {"status": "unavailable", "reason": "Missing video or metadata packet timestamps"}
    gaps: list[float] = []
    for packet in metadata_times:
        pts = _number(packet.get("pts_time")) if isinstance(packet, dict) else None
        if pts is None:
            continue
        position = bisect.bisect_left(video_pts, pts)
        candidates = []
        if position < len(video_pts):
            candidates.append(abs(video_pts[position] - pts))
        if position:
            candidates.append(abs(video_pts[position - 1] - pts))
        if candidates:
            gaps.append(min(candidates) * 1000)
    if not gaps:
        return {"status": "unavailable", "reason": "No comparable packet PTS values"}
    return {
        "status": "observed",
        "matched_metadata_packets": len(gaps),
        "median_nearest_video_pts_distance_ms": _round(statistics.median(gaps), 6),
        "p95_nearest_video_pts_distance_ms": _round(_percentile(gaps, 0.95), 6),
        "interpretation": (
            "Alignment of container presentation timestamps only. "
            "This is not a measurement of gyro-to-optical synchronization."
        ),
    }


def quality_assessment(report: dict) -> dict:
    """Screen for conversion blockers and provisional optical consistency.

    Use video-frame fraction as an *explicit heuristic*, not a device-specific
    calibration constant or a scientifically validated acceptance threshold.
    """
    imu = report.get("imu") or {}
    alignment = imu.get("clock_alignment") or {}
    imu_status = imu.get("status")
    if imu_status in {"error", "missing"} or "error" in (imu.get("gyro") or {}):
        sensor = "fail" if imu_status == "error" else "unavailable"
        sensor_reason = ("IMU parser/clock analysis failed" if sensor == "fail" else
                         "No embedded IMU stream; a separate motion log may exist")
    elif imu_status == "warning":
        sensor = "warning"
        sensor_reason = "Diagnostic contains warnings that require review"
    elif alignment.get("status") == "compatible":
        sensor = "pass"
        sensor_reason = "Independent sensor clocks and boundary checks are compatible"
    else:
        sensor = "warning"
        sensor_reason = "Cross-sensor alignment is incomplete or outside the converter's limits"

    project = report.get("gyroflow")
    optical = "unverified"
    optical_reason = "No Gyroflow optical synchronization project provided"
    optical_metrics = {}
    if project:
        points = project.get("sync_points") or []
        count = len(points)
        span = (
            max(p["video_time_s"] for p in points) -
            min(p["video_time_s"] for p in points)
            if count >= 2 else 0.0
        )
        fps = _number(project.get("video_fps"))
        optical_metrics = {"sync_point_count": count, "sync_time_span_s": _round(span, 3)}
        if count < 3 or span < 30:
            optical = "insufficient"
            optical_reason = "Need at least three sync points spanning 30 seconds"
        elif fps is None or fps <= 0:
            optical = "insufficient"
            optical_reason = "Cannot judge offset variation in frame units without video fps"
        else:
            quarter_frame_ms = 250 / fps
            offset_span_ms = project.get("offset_span_ms")
            optical_metrics["quarter_frame_threshold_ms"] = _round(quarter_frame_ms, 4)
            if isinstance(offset_span_ms, (float, int)) and offset_span_ms <= quarter_frame_ms:
                optical = "provisionally_consistent"
                optical_reason = (
                    "Sync offsets vary by no more than 1/4 video frame across the sampled span; "
                    "heuristic only, not proof of absolute sync"
                )
            else:
                optical = "review"
                optical_reason = "Optical sync offsets differ by more than 1/4 video frame"

    packet = (imu.get("packet_timing") or {})
    packet_status = packet.get("status", "unavailable")
    video_timing_status = (report.get("video_timing") or {}).get("status", "unavailable")
    overall = (
        "fail" if sensor == "fail" else
        "warning" if sensor != "pass" or optical != "provisionally_consistent" else
        "pass"
    )
    if packet_status == "warning":
        overall = "warning" if overall == "pass" else overall
    if any("different video filename" in str(item) for item in report.get("warnings", [])):
        overall = "warning" if overall == "pass" else overall
    return {
        "status": overall,
        "scope": "Automated evidence checks, not a verified stabilization/export guarantee",
        "sensor_clock": {"status": sensor, "reason": sensor_reason},
        "gpmf_packet_timing": {
            "status": packet_status,
            "reason": "Container packet PTS comparison is diagnostic, not a capture-time calibration",
        },
        "video_packet_timing": {"status": video_timing_status},
        "optical_sync": {
            "status": optical,
            "reason": optical_reason,
            **optical_metrics,
        },
        "manual_visual_validation_required": True,
        "heuristics": {
            "optical_consistency": (
                "At least 3 synchronization points spanning >=30 s, "
                "with offset range <=25% of one video frame; exploratory heuristic"
            ),
            "imu_sensor_checks": (
                "Use the converter's actual sensor-clock agreement, sample-count, "
                "and one-sample edge-coverage rules"
            ),
        },
    }
