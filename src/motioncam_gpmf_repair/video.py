"""Losslessly set the display height of a padded HEVC video.

Uses HEVC SPS conformance-window cropping, not pixel re-encoding.
The caller must specify the intended dimensions; no phone-specific assumptions.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class VideoError(Exception):
    """Unsupported or unsafe video-normalization request."""


def _command(cmd: list[str]) -> bytes:
    if not shutil.which(cmd[0]):
        raise VideoError(f"Required command not on PATH: {cmd[0]}")
    try:
        return subprocess.check_output(cmd, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        tail = (error.stderr or b"").decode("utf-8", "replace")[-1200:]
        raise VideoError(f"{cmd[0]} failed: {tail}") from error


def _probe(path: Path) -> dict:
    return json.loads(_command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ]))


def _first_video(probe: dict) -> dict:
    videos = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if len(videos) != 1:
        raise VideoError(f"Expected exactly one video stream, found {len(videos)}")
    return videos[0]


def plan_crop(
    probe: dict, *, target_height: int | None = None,
    aspect: str | None = None, crop_top: int = 0, max_padding: int = 128,
) -> tuple[int, int, int]:
    """Return target height, top crop, and bottom crop, without device presets."""
    video = _first_video(probe)
    if video.get("codec_name") != "hevc":
        raise VideoError("Lossless conformance-window adjustment currently supports HEVC/H.265 only")
    width, height = int(video["width"]), int(video["height"])
    if int(video.get("coded_height", height)) != height:
        raise VideoError("Source already has a coded/display-height difference; refusing to overwrite an existing crop")
    if (target_height is None) == (aspect is None):
        raise VideoError("Specify exactly one of --target-height or --aspect")
    if aspect is not None:
        try:
            numerator, denominator = (int(part) for part in aspect.split(":"))
            if numerator <= 0 or denominator <= 0 or (width * denominator) % numerator:
                raise ValueError
            target_height = (width * denominator) // numerator
        except ValueError as error:
            raise VideoError("--aspect must be W:H with positive integers and an exact pixel height") from error
    assert target_height is not None
    if target_height <= 0 or target_height >= height:
        raise VideoError(f"Target height must be positive and smaller than source height ({height})")
    total = height - target_height
    if not 0 < total <= max_padding:
        raise VideoError(f"Requested crop is {total} rows, exceeds --max-padding={max_padding}")
    if crop_top < 0 or crop_top > total:
        raise VideoError("--crop-top must be between zero and the total number of rows removed")
    crop_bottom = total - crop_top
    if (crop_top % 2) or (crop_bottom % 2):
        raise VideoError("4:2:0 HEVC conformance-window top/bottom crops must be even numbers")
    return target_height, crop_top, crop_bottom


def normalize(
    source: Path, *, output: Path | None = None,
    target_height: int | None = None, aspect: str | None = None,
    crop_top: int = 0, max_padding: int = 128,
    dry_run: bool = False, overwrite: bool = False,
) -> tuple[Path, dict]:
    if not source.is_file():
        raise VideoError(f"Input does not exist: {source}")
    if source.suffix.lower() not in {".mov", ".mp4"}:
        raise VideoError("This utility supports MOV and MP4 containers only")
    original = _probe(source)
    original_video = _first_video(original)
    final_height, top, bottom = plan_crop(
        original, target_height=target_height, aspect=aspect,
        crop_top=crop_top, max_padding=max_padding,
    )
    output = output or source.with_name(f"{source.stem}_{original_video['width']}x{final_height}{source.suffix}")
    if output.resolve() == source.resolve():
        raise VideoError("Output must differ from input")
    if output.suffix.lower() != source.suffix.lower():
        raise VideoError("Output extension must match input container (.mov or .mp4)")
    if not output.parent.is_dir():
        raise VideoError(f"Output folder does not exist: {output.parent}")
    if output.exists() and not overwrite and not dry_run:
        raise VideoError(f"Output already exists: {output}; use --overwrite to replace")
    result = {
        "source_width": int(original_video["width"]),
        "source_height": int(original_video["height"]),
        "target_height": final_height,
        "crop_top": top,
        "crop_bottom": bottom,
        "streams": [(s.get("codec_type"), s.get("codec_name")) for s in original.get("streams", [])],
        "has_gpmd": any(s.get("codec_tag_string") == "gpmd" for s in original.get("streams", [])),
    }
    if dry_run:
        return output, result

    temporary = None
    try:
        handle, name = tempfile.mkstemp(prefix=".hevc-crop-", suffix=output.suffix, dir=output.parent)
        os.close(handle)
        temporary = Path(name)
        _command([
            "ffmpeg", "-v", "error", "-i", str(source),
            "-map", "0", "-c", "copy", "-bsf:v:0",
            f"hevc_metadata=crop_top={top}:crop_bottom={bottom}",
            "-movflags", "+faststart", "-y", str(temporary),
        ])
        final = _probe(temporary)
        video = _first_video(final)
        if int(video["width"]) != result["source_width"] or int(video["height"]) != final_height:
            raise VideoError("Output dimensions do not match requested crop; original retained")
        streams = [(s.get("codec_type"), s.get("codec_name")) for s in final.get("streams", [])]
        if streams != result["streams"]:
            raise VideoError("Output stream types/codecs differ from source; original retained")
        if original_video.get("nb_frames") and video.get("nb_frames"):
            if int(original_video["nb_frames"]) != int(video["nb_frames"]):
                raise VideoError("Output video frame count changed; original retained")
        for before, after in zip(original.get("streams", []), final.get("streams", [])):
            if before.get("duration") and after.get("duration"):
                if abs(float(before["duration"]) - float(after["duration"])) > 0.01:
                    raise VideoError("Output stream duration changed; original retained")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Losslessly correct the displayed height of padded HEVC footage. "
                    "Padding location is not inferred."
    )
    parser.add_argument("video", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--target-height", type=int, help="Expected content height in pixels (e.g. 2160)")
    group.add_argument("--aspect", help="Expected content aspect W:H (e.g. 16:9)")
    parser.add_argument("--crop-top", type=int, default=0,
                        help="Rows removed from top; remaining rows removed from bottom (default: 0)")
    parser.add_argument("--max-padding", type=int, default=128,
                        help="Safety limit for total rows removed (default: 128)")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Show plan without writing a new video")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        output, result = normalize(
            args.video, output=args.output, target_height=args.target_height,
            aspect=args.aspect, crop_top=args.crop_top, max_padding=args.max_padding,
            dry_run=args.dry_run, overwrite=args.overwrite,
        )
    except (VideoError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"{result['source_width']}x{result['source_height']} -> "
          f"{result['source_width']}x{result['target_height']} "
          f"(crop top={result['crop_top']}, bottom={result['crop_bottom']})")
    if not result["has_gpmd"]:
        print("NOTE: No embedded gpmd track detected; external motion data may be needed for Gyroflow")
    print(("Would create: " if args.dry_run else "Wrote: ") + str(output))
    if not args.dry_run:
        print("HEVC was copied without re-encoding; FFmpeg remuxed recognized container streams.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
