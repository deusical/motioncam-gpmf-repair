"""Export GPMF gyro/accelerometer data for offline diagnosis, without video.

Preserves original integer samples, batch boundaries, GPMF STMP fields, and
container packet times. Does not export other GPMF fields (e.g. GPS).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

from .core import RepairError, _run, extract_gpmd, sensor_streams


def _packet_times(source: Path, expected_sizes: list[int]) -> list[dict]:
    streams = json.loads(
        _run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)])
    ).get("streams", [])
    gpmd = [s for s in streams if s.get("codec_tag_string") == "gpmd"]
    if len(gpmd) != 1:
        raise RepairError("Expected exactly one embedded gpmd stream")
    index = str(gpmd[0]["index"])
    data = json.loads(_run([
        "ffprobe", "-v", "error", "-select_streams", index,
        "-show_packets", "-show_entries", "packet=size,pts_time,dts_time,duration_time",
        "-of", "json", str(source),
    ]))
    packets = data.get("packets", [])
    if [int(p["size"]) for p in packets] != expected_sizes:
        raise RepairError("GPMF packet sizes changed between metadata extraction and timestamp probe")
    return [
        {name: p[name] for name in ("pts_time", "dts_time", "duration_time") if name in p}
        for p in packets
    ]


def collect_diagnostic(packet_sizes: list[int], raw: bytes, packet_times: list[dict] | None = None) -> dict:
    """Create an uploadable IMU-only dump; does not require equal stream counts."""
    if sum(packet_sizes) != len(raw):
        raise RepairError("GPMF packet boundaries do not match the raw data")
    if packet_times is not None and len(packet_times) != len(packet_sizes):
        raise RepairError("GPMF packet timestamp count mismatch")

    tracks: dict[str, list[dict]] = {"gyro": [], "accel": []}
    counts = {"gyro": 0, "accel": 0}
    pos = 0
    for packet_index, size in enumerate(packet_sizes):
        packet = raw[pos:pos + size]
        pos += size
        for stream_index, fields in enumerate(sensor_streams(packet)):
            for sensor, name in ((b"GYRO", "gyro"), (b"ACCL", "accel")):
                if sensor not in fields:
                    continue
                blob = fields[sensor]
                if len(blob) % 6:
                    raise RepairError(f"Malformed {name} XYZ sample bytes in packet {packet_index}")
                timestamp_us = int.from_bytes(fields[b"STMP"], "big", signed=False)
                scale = int.from_bytes(fields[b"SCAL"], "big", signed=True)
                if scale <= 0:
                    raise RepairError(f"Invalid {name} scale in packet {packet_index}")
                values = [list(sample) for sample in struct.iter_unpack(">hhh", blob)]
                counts[name] += len(values)
                record = {
                    "packet_index": packet_index,
                    "stream_index": stream_index,
                    "timestamp_us": timestamp_us,
                    "scale": scale,
                    "samples": values,
                }
                # Preserve input/output axis labels for later investigation,
                # without assuming they describe camera-to-IMU alignment.
                for key, label in ((b"ORIN", "orientation_in"), (b"ORIO", "orientation_out")):
                    if key in fields:
                        record[label] = fields[key].decode("ascii", "replace")
                tracks[name].append(record)
    if not tracks["gyro"]:
        raise RepairError("No gyro samples found")
    return {
        "format": "motioncam-gpmf-imu-diagnostic",
        "version": 1,
        "counts": counts,
        "packet_sizes": packet_sizes,
        "packet_times": packet_times,
        "tracks": tracks,
    }


def dump_diagnostic(source: Path, output: Path | None = None, *, overwrite: bool = False) -> tuple[Path, dict]:
    """Write a compact, gzip-compressed JSON diagnostic preserving both streams."""
    if not source.is_file():
        raise RepairError(f"File does not exist: {source}")
    output = output or source.with_name(source.stem + "_imu_diagnostic.json.gz")
    if source.resolve() == output.resolve():
        raise RepairError("Output must differ from input")
    if not output.parent.is_dir():
        raise RepairError(f"Output directory does not exist: {output.parent}")
    if output.exists() and not overwrite:
        raise RepairError(f"Output already exists: {output} (pass --overwrite to replace)")

    sizes, raw = extract_gpmd(source)
    packet_times = _packet_times(source, sizes)
    result = collect_diagnostic(sizes, raw, packet_times)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=".motioncam-imu-", suffix=".json.gz", delete=False
        ) as tmp:
            temp_path = Path(tmp.name)
            with gzip.GzipFile(fileobj=tmp, mode="wb", compresslevel=9, mtime=0) as compressed:
                compressed.write(json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
        os.replace(temp_path, output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return output, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a compact MotionCam GPMF IMU diagnostic without video")
    parser.add_argument("video", type=Path, help="MotionCam MOV with embedded gpmd track")
    parser.add_argument("-o", "--output", type=Path, help="Destination .json.gz (default: beside video)")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        out, result = dump_diagnostic(args.video, args.output, overwrite=args.overwrite)
    except (RepairError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Saved: {out} ({out.stat().st_size:,} bytes)")
    print(f"Gyro: {result['counts']['gyro']:,}; accel: {result['counts']['accel']:,} samples")
    for stream_name in ("gyro", "accel"):
        first = result["tracks"][stream_name][0] if result["tracks"][stream_name] else {}
        if "orientation_in" in first or "orientation_out" in first:
            print(f"  GPMF {stream_name} orientation labels: "
                  f"ORIN={first.get('orientation_in', '?')}, ORIO={first.get('orientation_out', '?')} "
                  "(not a validated camera-to-IMU mapping)")
    print("The dump contains sensor movement and timestamps but no video or GPS fields; treat it as private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
