"""Non-destructive orchestration of MotionCam inspection, repair, and optional crop.

The original video is retained in place. Each run creates a self-contained
package of derived files, not a duplicated copy of the original video.
No lens, aspect, IMU orientation, or optical synchronization is guessed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .core import RepairError, _run, repair, validate_imu_orientation
from .inspect import InspectError, inspect_file
from .sidecar import (
    SidecarError, find_sidecar, inspect_gcsv, sha256_file, sidecar_fingerprint,
)
from .video import VideoError, normalize

_FORMAT = "motioncam-prepared-package"
_MANIFEST = "manifest.json"
_INSPECTION = "inspection.json"


class PrepareError(Exception):
    """Unsupported or unsafe preparation request."""


def _fingerprint(source: Path) -> dict:
    stat = source.stat()
    # This is a quick identity check, not a cryptographic hash.
    return {
        "filename": source.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _read_known_package(folder: Path, *, expected_source: str) -> dict:
    """Refuse to replace any unrecognized folder or unexpected user files."""
    if folder.is_symlink() or not folder.is_dir():
        raise PrepareError("Existing output must be a regular prepared-package directory")
    path = folder / _MANIFEST
    if not path.is_file() or path.is_symlink():
        raise PrepareError(
            "Output folder already exists but has no valid manifest; "
            "choose another --output-dir"
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PrepareError("Existing output manifest cannot be read") from error
    if not isinstance(manifest, dict) or manifest.get("format") != _FORMAT:
        raise PrepareError("Existing output folder is not a recognized prepared package")
    if manifest.get("version") not in (1, 2):
        raise PrepareError("Existing package manifest version is unsupported")
    source_info = manifest.get("source")
    if not isinstance(source_info, dict) or source_info.get("filename") != expected_source:
        raise PrepareError("Existing package belongs to a different source filename")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {"motion", "inspection", "video"} or (
        not isinstance(files.get("motion"), str) or files.get("inspection") != _INSPECTION
    ):
        raise PrepareError("Existing manifest has unsupported output names")
    names = [n for n in files.values() if n is not None]
    if any(not isinstance(n, str) or not n or n in {".", ".."} or
           Path(n).name != n or "/" in n or "\\" in n
           for n in names):
        raise PrepareError("Existing manifest contains unsafe filenames")
    expected = set(names + [_MANIFEST])
    if len(expected) != len(names) + 1:
        raise PrepareError("Existing manifest contains duplicate file names")
    if {p.name for p in folder.iterdir()} != expected:
        raise PrepareError(
            "Prepared folder contains unexpected or missing files; "
            "refusing to overwrite or reuse user data"
        )
    if any(not (folder / name).is_file() or (folder / name).is_symlink()
           or (folder / name).stat().st_size <= 0 for name in names):
        raise PrepareError("Existing prepared files are empty or not regular files")
    return manifest




def _video_duration_seconds(source: Path) -> float | None:
    """Read container duration as a weak advisory comparison, never a clock fix."""
    import math
    try:
        raw = _run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        ])
        value = float(raw.strip())
        return value if math.isfinite(value) and value > 0 else None
    except (RepairError, ValueError, OSError):
        return None

def prepare(
    source: Path, *,
    output_dir: Path | None = None,
    project: Path | None = None,
    motion: Path | None = None,
    target_height: int | None = None,
    aspect: str | None = None,
    crop_top: int = 0,
    max_padding: int = 128,
    gyro_only: bool = False,
    imu_orientation: str | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
    reuse: bool = False,
) -> dict:
    """Return preparation outcome; stage complete outputs before installing.

    Reuse is explicit and skips reinspection only when the manifest and quick
    source fingerprint match. An overwrite is restricted to an intact package
    from the same source, and uses rollback if installation fails.
    """
    if overwrite and reuse:
        raise PrepareError("--overwrite and --reuse are mutually exclusive")
    validate_imu_orientation(imu_orientation)
    source = source.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in {".mov", ".mp4"}:
        raise PrepareError("Input must be an existing MOV or MP4 recording")
    if project is not None:
        project = project.expanduser().resolve()
        if not project.is_file():
            raise PrepareError(f"Gyroflow project does not exist: {project}")
    if motion is not None:
        motion = motion.expanduser().resolve()
        if not motion.is_file() or motion.suffix.lower() != ".gcsv":
            raise PrepareError("--motion requires an existing .gcsv sidecar")
    if (target_height is None) == (aspect is None):
        if target_height is not None:
            raise PrepareError("Choose only one of --target-height and --aspect")
        if crop_top != 0:
            raise PrepareError("--crop-top requires --target-height or --aspect")
    if max_padding <= 0:
        raise PrepareError("--max-padding must be positive")

    requested_out = (output_dir or source.with_name(source.stem + "_prepared")).expanduser()
    if requested_out.is_symlink():
        raise PrepareError("Output destination must not be a symlink")
    out = requested_out.resolve()
    if out == source or out in source.parents or out == source.parent:
        raise PrepareError("Output directory must not contain the source video")
    if not out.parent.is_dir():
        raise PrepareError(f"Output parent does not exist: {out.parent}")
    if out.exists() and not out.is_dir():
        raise PrepareError("Output destination exists and is not a directory")
    if out.is_symlink():
        raise PrepareError("Output destination must not be a symlink")
    if motion is not None and (motion == out or out in motion.parents):
        raise PrepareError("Motion sidecar cannot reside in the output package being replaced")
    if reuse and not out.exists():
        raise PrepareError("--reuse requires an existing prepared package")

    settings = {
        "gyro_only": gyro_only,
        "imu_orientation": imu_orientation,
        "target_height": target_height,
        "aspect": aspect,
        "crop_top": crop_top,
        "max_padding": max_padding,
    }
    source_id = _fingerprint(source)
    project_id = _fingerprint(project) if project is not None else None
    existing = None
    if out.exists():
        if not (overwrite or reuse):
            raise PrepareError(
                f"Output folder already exists: {out} "
                "(use --reuse for an unchanged package or --overwrite to regenerate)"
            )
        existing = _read_known_package(out, expected_source=source.name)
        if reuse:
            if (existing.get("source") != source_id or
                    existing.get("project") != project_id or
                    existing.get("settings") != settings):
                raise PrepareError(
                    "Existing package differs from source/project metadata or options; "
                    "use --overwrite to regenerate"
                )
            motion_input = existing.get("motion_input", {"kind": "embedded_gpmd"})
            if motion_input.get("kind") == "sidecar_gcsv":
                candidate = motion if motion is not None else find_sidecar(source)
                if candidate is None or motion_input.get("file") != sidecar_fingerprint(candidate):
                    raise PrepareError(
                        "Existing package differs from source GCSV sidecar; "
                        "use --overwrite to regenerate"
                    )
            elif motion is not None:
                raise PrepareError(
                    "Existing package used embedded GPMF; --motion changes its processing path. "
                    "Use --overwrite to regenerate."
                )
            quality = json.loads((out / _INSPECTION).read_text(encoding="utf-8")).get("quality", {})
            return {
                "status": "reused", "output_dir": str(out), "source": str(source),
                "files": existing["files"], "quality": quality.get("status", "unknown"),
                "mode": motion_input.get("kind", "embedded_gpmd"),
                "warnings": [
                    "Reused files via source/project metadata, options, and sidecar "
                    "checksum when applicable; outputs and optical sync were not revalidated"
                ],
            }

    # The crop is always explicitly requested; never infer padded rows.
    normalized_name = None
    if target_height is not None or aspect is not None:
        _, crop = normalize(
            source, output=source.with_name(".motioncam-crop-plan" + source.suffix),
            target_height=target_height, aspect=aspect, crop_top=crop_top,
            max_padding=max_padding, dry_run=True,
        )
        normalized_name = (
            f"{source.stem}_{crop['source_width']}x{crop['target_height']}{source.suffix}"
        )

    # Inspect container video and embedded GPMF first. --motion explicitly
    # selects the sidecar, even when a video also contains an embedded track.
    inspected = inspect_file(source, project)
    imu = inspected.get("imu") or {}
    if motion is not None:
        selected_sidecar = motion
    elif imu.get("status") == "missing":
        selected_sidecar = find_sidecar(source)
    else:
        selected_sidecar = None

    if selected_sidecar is None:
        if imu.get("status") == "missing":
            raise PrepareError(
                "No embedded GPMF or matching adjacent RAW GCSV was found. "
                "Supply the MotionCam RAW motion log with --motion PATH.gcsv."
            )
        if imu.get("status") == "error":
            raise PrepareError(
                "Embedded GPMF timing could not be analyzed; "
                "supply a known-good GCSV with --motion or inspect the source"
            )
        checked = repair(
            source, dry_run=True, gyro_only=gyro_only,
            imu_orientation=imu_orientation,
        )
        motion_input = {"kind": "embedded_gpmd"}
        motion_kind = "embedded_gpmd"
        motion_filename = source.stem + "_retimed.gcsv"
        count = checked.samples
        rate = round(checked.rate_hz, 6)
    else:
        if gyro_only or imu_orientation is not None:
            raise PrepareError(
                "--gyro-only and --imu-orientation apply only to embedded GPMF. "
                "A RAW GCSV is preserved verbatim; configure orientation in Gyroflow."
            )
        sidecar_info = inspect_gcsv(selected_sidecar)
        motion_input = {
            "kind": "sidecar_gcsv",
            "file": sidecar_fingerprint(selected_sidecar),
        }
        motion_kind = "sidecar_gcsv"
        motion_filename = source.stem + "_motion.gcsv"
        count = sidecar_info["sample_count"]
        rate = sidecar_info["median_sample_rate_hz"]
        inspected["external_motion"] = sidecar_info
        inspected["motion_source"] = {
            "kind": "sidecar_gcsv",
            "processing": "copied_without_retiming",
            "filename": selected_sidecar.name,
        }
        inspected.setdefault("warnings", []).append(
            "Using structurally validated RAW GCSV unchanged; no GPMF "
            "timestamp reconstruction was applied"
        )
        if sidecar_info["large_timestamp_intervals"]:
            inspected["warnings"].append(
                f"GCSV has {sidecar_info['large_timestamp_intervals']} timestamp "
                "intervals >3x its median; review the source log"
            )
        video_duration = _video_duration_seconds(source)
        if video_duration is not None:
            inspected["external_motion"]["video_duration_s"] = round(video_duration, 6)
            difference = sidecar_info["duration_s"] - video_duration
            if abs(difference) > max(0.2, 0.025 * video_duration):
                inspected["warnings"].append(
                    f"Motion log duration differs from rendered video by "
                    f"{difference:+.3f} s; a RAW render may be trimmed. "
                    "No timeline stretching or offset correction was applied"
                )
        inspected.setdefault("quality", {})["status"] = "warning"
        inspected["quality"]["scope"] = (
            "RAW GCSV structure verified; physical sensor accuracy and "
            "video/motion synchronization are not established"
        )
        inspected["quality"]["sensor_clock"] = {
            "status": "not_assessed",
            "reason": "External GCSV was preserved without GPMF clock reconstruction",
        }

    if selected_sidecar is None:
        inspected["motion_source"] = {
            "kind": "embedded_gpmd",
            "processing": "reconstructed_timestamps",
            "filename": source.name,
        }

    files = {
        "motion": motion_filename,
        "inspection": _INSPECTION,
        "video": normalized_name,
    }
    quality = (inspected.get("quality") or {}).get("status", "unknown")
    status = "dry_run" if dry_run else ("updated" if existing else "created")
    result = {
        "status": status, "output_dir": str(out), "source": str(source),
        "files": files, "quality": quality,
        "mode": motion_kind,
        "gyro_samples": count, "gyro_rate_hz": rate,
        "warnings": list(inspected.get("warnings") or []),
    }
    if dry_run:
        return result

    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}-stage-", dir=out.parent))
    backup = None
    try:
        if selected_sidecar is not None:
            shutil.copyfile(selected_sidecar, stage / files["motion"])
            if sha256_file(stage / files["motion"]) != motion_input["file"]["sha256"]:
                raise PrepareError("Source GCSV changed during copy; refusing inconsistent package")
            if sidecar_fingerprint(selected_sidecar) != motion_input["file"]:
                raise PrepareError("Source GCSV changed during preparation; retry")
        else:
            repair(
                source, stage / files["motion"], gyro_only=gyro_only,
                imu_orientation=imu_orientation,
            )
        if normalized_name is not None:
            normalize(
                source, output=stage / normalized_name,
                target_height=target_height, aspect=aspect, crop_top=crop_top,
                max_padding=max_padding,
            )
        (stage / _INSPECTION).write_text(
            json.dumps(inspected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        manifest = {
            "format": _FORMAT, "version": 2, "source": source_id,
            "project": project_id, "settings": settings, "files": files,
            "motion_input": motion_input,
            "note": (
                "The original MOV/MP4 and any RAW motion sidecar were not modified. "
                "All motion data requires Gyroflow orientation and optical sync review."
            ),
        }
        (stage / _MANIFEST).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        for filename in (name for name in files.values() if name):
            if not (stage / filename).is_file() or (stage / filename).stat().st_size <= 0:
                raise PrepareError(f"Prepared output missing or empty: {filename}")
        if existing:
            # Re-check just before replacing to avoid deleting arbitrary files.
            _read_known_package(out, expected_source=source.name)
            backup = Path(tempfile.mkdtemp(prefix=f".{out.name}-backup-", dir=out.parent))
            backup.rmdir()
            os.replace(out, backup)
            try:
                os.replace(stage, out)
            except OSError:
                os.replace(backup, out)
                backup = None
                raise
        else:
            if out.exists():
                raise PrepareError("Output folder appeared during preparation; refusing to replace it")
            os.replace(stage, out)
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except OSError:
                result["warnings"].append(
                    "Previous prepared package backup could not be deleted automatically"
                )
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="motioncam-gpmf-repair prepare",
        description="Inspect and prepare a Gyroflow-ready motion file without modifying the source video",
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path,
                        help="Prepared package directory (default: <video>_prepared)")
    parser.add_argument("--project", type=Path, help="Existing synchronized Gyroflow project for validation")
    parser.add_argument("--motion", type=Path,
                        help="MotionCam RAW recording .gcsv (otherwise discover an unambiguous adjacent sidecar)")
    crop = parser.add_mutually_exclusive_group()
    crop.add_argument("--target-height", type=int,
                      help="Optional explicit lossless HEVC target height")
    crop.add_argument("--aspect", help="Optional explicit HEVC output aspect W:H")
    parser.add_argument("--crop-top", type=int, default=0,
                        help="Rows removed from top; remaining requested rows from bottom")
    parser.add_argument("--max-padding", type=int, default=128,
                        help="Maximum HEVC crop rows (default: 128)")
    parser.add_argument("--gyro-only", action="store_true", help="Explicitly omit acceleration")
    parser.add_argument("--imu-orientation",
                        help="Optional manually verified Gyroflow axis mapping; never guessed")
    parser.add_argument("--dry-run", action="store_true", help="Check and plan without writing files")
    existing = parser.add_mutually_exclusive_group()
    existing.add_argument("--overwrite", action="store_true",
                          help="Regenerate only a recognized, intact prepared package")
    existing.add_argument("--reuse", action="store_true",
                          help="Reuse a package matching source size, mtime and options; no revalidation")
    args = parser.parse_args(argv)
    try:
        result = prepare(
            args.video, output_dir=args.output_dir, project=args.project,
            motion=args.motion, target_height=args.target_height, aspect=args.aspect,
            crop_top=args.crop_top, max_padding=args.max_padding,
            gyro_only=args.gyro_only, imu_orientation=args.imu_orientation,
            dry_run=args.dry_run, overwrite=args.overwrite, reuse=args.reuse,
        )
    except (PrepareError, RepairError, InspectError, SidecarError, VideoError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Preparation: {result['status'].replace('_', ' ')}")
    print(f"  Source retained: {result['source']}")
    print(f"  Package: {result['output_dir']}")
    print(f"  Motion source: {result['mode']}")
    for key, filename in result["files"].items():
        if filename:
            print(f"  {key.capitalize()}: {filename}")
    print(f"  Automated screening: {result['quality']} (visual verification still required)")
    for warning in result["warnings"]:
        print(f"  NOTE: {warning}")
    if result["status"] == "dry_run":
        print("  Dry run: no files created or changed")
    elif result["status"] == "reused":
        print("  Existing package reused without reinspection or reconversion")
    else:
        print("  Open the source/normalized video in Gyroflow, load the GCSV, and verify sync and axes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
