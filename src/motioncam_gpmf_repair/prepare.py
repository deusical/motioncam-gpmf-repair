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

from .core import RepairError, repair, validate_imu_orientation
from .inspect import InspectError, inspect_file
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
    if manifest.get("version") != 1:
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


def prepare(
    source: Path, *,
    output_dir: Path | None = None,
    project: Path | None = None,
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
            quality = json.loads((out / _INSPECTION).read_text(encoding="utf-8")).get("quality", {})
            return {
                "status": "reused", "output_dir": str(out), "source": str(source),
                "files": existing["files"], "quality": quality.get("status", "unknown"),
                "warnings": [
                    "Reused files via size/mtime/options fingerprint; "
                    "contents and optical synchronization were not revalidated"
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

    # Run all checks before creating a new folder or touching an existing one.
    inspected = inspect_file(source, project)
    imu = inspected.get("imu") or {}
    if imu.get("status") in {"error", "missing"}:
        raise PrepareError(
            "Embedded GPMF cannot be prepared: " +
            "; ".join(imu.get("warnings") or [imu.get("status", "unknown error")]) +
            ". External motion-file support is not implemented yet."
        )
    checked = repair(
        source, dry_run=True, gyro_only=gyro_only,
        imu_orientation=imu_orientation,
    )
    files = {
        "motion": source.stem + "_retimed.gcsv",
        "inspection": _INSPECTION,
        "video": normalized_name,
    }
    quality = (inspected.get("quality") or {}).get("status", "unknown")
    status = "dry_run" if dry_run else ("updated" if existing else "created")
    result = {
        "status": status, "output_dir": str(out), "source": str(source),
        "files": files, "quality": quality,
        "gyro_samples": checked.samples, "gyro_rate_hz": round(checked.rate_hz, 6),
        "warnings": list(inspected.get("warnings") or []),
    }
    if dry_run:
        return result

    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}-stage-", dir=out.parent))
    backup = None
    try:
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
            "format": _FORMAT, "version": 1, "source": source_id,
            "project": project_id, "settings": settings, "files": files,
            "note": (
                "The original MOV/MP4 was not copied or modified. "
                "The derived motion data requires Gyroflow orientation and optical sync review."
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
            target_height=args.target_height, aspect=args.aspect,
            crop_top=args.crop_top, max_padding=args.max_padding,
            gyro_only=args.gyro_only, imu_orientation=args.imu_orientation,
            dry_run=args.dry_run, overwrite=args.overwrite, reuse=args.reuse,
        )
    except (PrepareError, RepairError, InspectError, VideoError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Preparation: {result['status'].replace('_', ' ')}")
    print(f"  Source retained: {result['source']}")
    print(f"  Package: {result['output_dir']}")
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
