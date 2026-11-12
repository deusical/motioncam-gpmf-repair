"""Build a review-ready Gyroflow project via Gyroflow's supported CLI (1.7+).

This stage never performs optical synchronization or renders footage. It
accepts explicit camera calibration, and refuses to reuse another clip's
precomputed sync offsets or gyro caches. Gyroflow remains the owner of the
.gyroflow project schema; this module does not forge an export directly.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .core import RepairError, _run
from .prepare import PrepareError, _fingerprint, _read_known_package
from .sidecar import SidecarError, inspect_gcsv, sha256_file


class ProjectError(Exception):
    """Unsafe or unsuccessful Gyroflow project generation."""


# No automatic sync or optical flow: the user must review in Gyroflow.
_NO_SYNC = {
    "auto_sync_points": False,
    "do_autosync": False,
    "do_optical_correction": False,
    "ignore_file_motion": False,
}


def _read_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProjectError(f"Cannot parse JSON: {path.name}") from exc
    if not isinstance(obj, dict):
        raise ProjectError(f"Expected JSON object in {path.name}")
    return obj


def _source_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            return None
        decoded = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[a-zA-Z]:/", decoded):
            decoded = decoded[1:]
        return Path(decoded).resolve()
    if "://" in value:
        return None
    return Path(value).expanduser().resolve()


def _identical_path(expected: Path, reference: object) -> bool:
    value = _source_path(reference)
    return value is not None and os.path.normcase(str(value)) == os.path.normcase(str(expected))


def _reference_preset(reference: Path, video_data: dict | None) -> dict:
    """Extract transferable settings from a project, never clip-specific caches.

    This references a user's *explicitly chosen* project. Project sample
    offsets, filepath, frame keyframes, and gyro caches must never leak into
    the next clip.
    """
    source = _read_json(reference)
    if not source.get("videofile"):
        raise ProjectError("--reference needs a saved project, not a settings-only preset")
    lens = source.get("calibration_data")
    if not isinstance(lens, dict) or not lens.get("name"):
        raise ProjectError("Reference project does not have a named lens calibration")
    if video_data and isinstance(video_data, dict):
        info = source.get("video_info") or {}
        for dim in ("width", "height"):
            current = video_data.get(dim)
            previous = info.get(dim) if isinstance(info, dict) else None
            if current and previous and int(current) != int(previous):
                raise ProjectError(
                    f"Reference video {dim} differs ({previous} vs {current}); "
                    "choose a matching calibration explicitly"
                )
    gyro = source.get("gyro_source")
    settings: dict = {"version": 4, "calibration_data": dict(lens)}
    settings["calibration_data"].pop("sync_settings", None)
    if isinstance(source.get("stabilization"), dict):
        # Copy only settings, not frame-specific tracks or cached motion.
        settings["stabilization"] = dict(source["stabilization"])
    if isinstance(gyro, dict):
        allowed = ("imu_orientation", "integration_method", "rotation", "acc_rotation", "lpf", "mf")
        settings["gyro_source"] = {k: gyro[k] for k in allowed if gyro.get(k) is not None}
    settings["synchronization"] = dict(_NO_SYNC)
    return settings


def _verify_preset(preset: Path, *, require_calibration: bool = True) -> dict:
    obj = _read_json(preset)
    if obj.get("videofile"):
        raise ProjectError("A full Gyroflow project cannot be used as --preset; use --reference")
    if obj.get("offsets") not in (None, {}):
        raise ProjectError("Preset contains old synchronization offsets")
    gyro = obj.get("gyro_source")
    if gyro is not None and (
        not isinstance(gyro, dict) or
        any(key in gyro for key in (
            "filepath", "file_metadata", "synced_imu_timestamps",
            "integrated_quaternions", "smoothed_quaternions",
        ))
    ):
        raise ProjectError("Preset contains file-specific gyro data; export a settings preset")
    sync = obj.get("synchronization") or {}
    if not isinstance(sync, dict) or sync.get("do_autosync") is True or (
        sync.get("do_optical_correction") is True
    ):
        raise ProjectError("Preset requests automatic synchronization or optical correction")
    if not obj.get("version"):
        raise ProjectError("Gyroflow preset needs a project format version")
    if require_calibration and (
        not isinstance(obj.get("calibration_data"), dict)
        or not obj["calibration_data"].get("name")
    ):
        raise ProjectError(
            "Preset has no explicit lens calibration; provide --lens-profile "
            "or use --reference with an existing calibrated project"
        )
    return obj


def _selected_files(video: Path) -> tuple[Path, Path, dict, dict]:
    video = video.expanduser().resolve()
    if not video.is_file() or video.suffix.lower() not in {".mov", ".mp4"}:
        raise ProjectError("A real MOV or MP4 video is required")
    package = video.with_name(video.stem + "_prepared")
    if not package.is_dir():
        raise ProjectError("Prepared package missing; run 'prepare VIDEO' first")
    try:
        manifest = _read_known_package(package, expected_source=video.name)
    except PrepareError as exc:
        raise ProjectError(str(exc)) from exc
    if manifest.get("source") != _fingerprint(video):
        raise ProjectError("Original video changed since preparation; regenerate the package")
    files = manifest["files"]
    motion = (package / files["motion"]).resolve()
    if not motion.is_file():
        raise ProjectError("Prepared GCSV is missing")
    inspection = _read_json(package / files["inspection"])
    if (inspection.get("quality") or {}).get("status") == "fail":
        raise ProjectError("Preparation inspection failed; refuse project generation")
    try:
        inspect_gcsv(motion)
    except SidecarError as exc:
        raise ProjectError(f"Prepared GCSV is invalid: {exc}") from exc
    if manifest.get("version") == 2:
        motion_input = manifest.get("motion_input") or {}
        if motion_input.get("kind") == "sidecar_gcsv":
            expected = (motion_input.get("file") or {}).get("sha256")
            if not expected or expected != sha256_file(motion):
                raise ProjectError("Prepared RAW GCSV differs from original sidecar checksum")
    working_video = video
    if files.get("video"):
        working_video = (package / files["video"]).resolve()
        if not working_video.is_file():
            raise ProjectError("Normalized video listed in manifest is missing")
    return working_video, motion, manifest, inspection


def _selected_video_info(original: Path, selected: Path, inspected: dict) -> dict | None:
    """Compare explicit reference calibration against the video Gyroflow will open.

    When prepare created a normalized copy, the original recording may be
    padded (e.g. 3840x2176 -> 3840x2160), so its inspection dimensions
    would incorrectly reject a compatible profile.
    """
    if selected == original:
        return inspected.get("video")
    try:
        probe = json.loads(_run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json",
            str(selected),
        ]))
        info = (probe.get("streams") or [])[0]
        if not isinstance(info.get("width"), int) or not isinstance(info.get("height"), int):
            raise ProjectError("Normalized video dimensions are not available")
        return info
    except (RepairError, ValueError, IndexError, KeyError, OSError) as exc:
        raise ProjectError("Cannot inspect normalized video dimensions") from exc


def _binary(binary: str | None) -> str | None:
    if binary:
        path = Path(binary).expanduser()
        if path.is_file():
            return str(path.resolve())
        return shutil.which(binary)
    for name in ("Gyroflow.exe", "gyroflow", "Gyroflow"):
        match = shutil.which(name)
        if match:
            return match
    return None


def _check_version(executable: str) -> str:
    try:
        r = subprocess.run([executable, "--version"], capture_output=True, text=True,
                           check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectError("Unable to run Gyroflow --version") from exc
    output = (r.stdout + r.stderr).strip()
    m = re.search(r"(?:Gyroflow\s*v?)(\d+)\.(\d+)(?:\.(\d+))?", output, re.I)
    if r.returncode or not m:
        raise ProjectError("Unable to determine Gyroflow version; Gyroflow 1.7+ required")
    major, minor = int(m[1]), int(m[2])
    if (major, minor) < (1, 7):
        raise ProjectError(f"Gyroflow {m[0]} is too old; 1.7+ required")
    return m[0]


def _verify_export(
    project: Path, video: Path, motion: Path, *, lens_name: str | None,
    allow_sync: bool = False,
) -> dict:
    """Verify Gyroflow wrote the intended sources and inspect saved offsets.

    The ordinary project command deliberately rejects all sync points. Probe
    mode accepts only well-formed finite offsets, but makes no quality claim.
    """
    if not project.is_file() or project.stat().st_size <= 0:
        raise ProjectError("Gyroflow did not produce the requested project file")
    obj = _read_json(project)
    if not isinstance(obj.get("version"), (int, float)) or "videofile" not in obj:
        raise ProjectError("Gyroflow produced an unrecognized project")
    if not _identical_path(video, obj["videofile"]):
        raise ProjectError("Exported project does not point to the selected video")
    gyro = obj.get("gyro_source") or {}
    if not isinstance(gyro, dict) or not _identical_path(motion, gyro.get("filepath")):
        raise ProjectError("Exported project does not point to the prepared motion file")
    lens = obj.get("calibration_data") or {}
    if not isinstance(lens, dict) or not lens.get("name"):
        raise ProjectError("Exported project has no lens calibration")
    if lens_name and lens["name"] != lens_name:
        raise ProjectError("Exported project lens profile differs from the explicit selection")
    raw_offsets = obj.get("offsets") or {}
    if not isinstance(raw_offsets, dict):
        raise ProjectError("Exported synchronization offsets are not a valid mapping")
    if raw_offsets and not allow_sync:
        raise ProjectError("Export contains old or automatic synchronization offsets")
    points = []
    for stamp, offset in raw_offsets.items():
        try:
            stamp_us = int(stamp)
            offset_ms = float(offset)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProjectError("Exported synchronization point has invalid numbers") from exc
        if stamp_us < 0 or not math.isfinite(offset_ms):
            raise ProjectError("Exported synchronization point has non-finite/negative time")
        points.append((stamp_us / 1_000_000.0, offset_ms))
    points.sort()
    if len({x[0] for x in points}) != len(points):
        raise ProjectError("Exported synchronization point timestamps are duplicated")
    return {
        "version": obj["version"], "lens": lens["name"],
        "orientation": gyro.get("imu_orientation"),
        "sync_point_count": len(points),
        "sync_points": [{"video_time_s": round(t, 6), "offset_ms": round(v, 6)}
                        for t, v in points],
        "offset_span_ms": round(max(v for _, v in points) - min(v for _, v in points), 6)
        if points else None,
        "time_span_s": round(points[-1][0] - points[0][0], 6)
        if points else None,
    }


def _sync_probe_params(search_size: float, max_points: int, resolution: int) -> dict:
    """Gyroflow v1.7 uses LENS sync_settings, not merely --sync-params.

    Keep intentionally simple, deterministic and review-first. We do not
    overwrite preexisting clip offsets or enable optical correction/render.
    """
    if not math.isfinite(search_size) or not 0.05 <= search_size <= 10:
        raise ProjectError("--search-size must be between 0.05 and 10 seconds")
    if not 1 <= max_points <= 12:
        raise ProjectError("--max-sync-points must be between 1 and 12")
    if resolution < 240 or resolution > 2160 or resolution % 2:
        raise ProjectError("--sync-resolution must be an even height between 240 and 2160")
    return {
        "do_autosync": True,
        "do_optical_correction": False,
        "ignore_file_motion": False,
        "initial_offset": 0,
        "search_size": search_size,
        "max_sync_points": max_points,
        "auto_sync_points": False,
        "every_nth_frame": 1,
        "time_per_syncpoint": 0.5,
        "processing_resolution": resolution,
    }

def _finalize_render_destination(project: Path, destination: Path, video: Path) -> None:
    """Re-home Gyroflow's temporary export settings before publishing.

    Gyroflow's export-project CLI records the staging directory as its video
    output_folder and the project filename as its output_filename. Both become
    invalid/misleading after the .gyroflow project is moved into place.
    Touch ONLY these two output settings in the Gyroflow-authored JSON.
    """
    contents = _read_json(project)
    render = contents.get("output")
    if not isinstance(render, dict):
        raise ProjectError("Exported project is missing Gyroflow output settings")
    render["output_folder"] = destination.parent.as_uri()
    render["output_filename"] = video.stem + "_stabilized.mp4"
    project.write_text(
        json.dumps(contents, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build_project(
    video: Path, *, reference: Path | None = None, preset: Path | None = None,
    lens_profile: Path | None = None, gyroflow: str | None = None,
    output: Path | None = None, dry_run: bool = False,
    sync_probe: bool = False, search_size: float = 0.5,
    max_sync_points: int = 5, sync_resolution: int = 720,
) -> dict:
    """Export a real Gyroflow-authored project in a temporary staging folder."""
    probe_params = _sync_probe_params(search_size, max_sync_points, sync_resolution) if sync_probe else None
    if not sync_probe and (search_size != 0.5 or max_sync_points != 5 or sync_resolution != 720):
        raise ProjectError("Synchronization options require --sync-probe")
    if reference is not None and (preset is not None or lens_profile is not None):
        raise ProjectError("--reference cannot be combined with --preset or --lens-profile")
    if reference is None and preset is None and lens_profile is None:
        raise ProjectError("Explicit --reference, --preset, or --lens-profile is required")
    video = video.expanduser().resolve()
    selected_video, motion, manifest, inspection = _selected_files(video)
    package = video.with_name(video.stem + "_prepared")
    suffix = "_sync_probe.gyroflow" if sync_probe else "_review.gyroflow"
    destination = (output or video.with_name(video.stem + suffix)).expanduser().resolve()
    report_dest = destination.with_name(destination.stem + "_report.json") if sync_probe else None
    if destination.suffix.lower() != ".gyroflow" or not destination.parent.is_dir():
        raise ProjectError("Project output must be a .gyroflow file in an existing folder")
    if destination.exists():
        raise ProjectError("Project already exists; choose a new --output (never overwrites)")
    if report_dest is not None and report_dest.exists():
        raise ProjectError("Sync probe report already exists; choose a new --output")
    if destination == video or destination in package.parents or package in destination.parents:
        raise ProjectError("Project output cannot modify the video or prepared package")
    if destination in (selected_video, motion):
        raise ProjectError("Project output cannot replace a source file")

    preset_content = None
    lens_name = None
    preset_path = None
    if reference is not None:
        reference = reference.expanduser().resolve()
        preset_content = _reference_preset(
            reference, _selected_video_info(video, selected_video, inspection)
        )
        lens_name = preset_content["calibration_data"]["name"]
    if preset is not None:
        preset_path = preset.expanduser().resolve()
        preset_obj = _verify_preset(preset_path, require_calibration=lens_profile is None)
        lens = preset_obj.get("calibration_data")
        if isinstance(lens, dict):
            lens_name = lens.get("name")
        # Only forward transferable settings, never a project cache, output
        # configuration or an automatic sync instruction from a user preset.
        preset_content = {
            "version": preset_obj["version"],
            "synchronization": dict(_NO_SYNC),
        }
        for key in ("stabilization", "gyro_source", "calibration_data"):
            if key in preset_obj and isinstance(preset_obj[key], dict):
                preset_content[key] = dict(preset_obj[key])
        if "gyro_source" in preset_content:
            allowed = ("imu_orientation", "integration_method", "rotation", "acc_rotation", "lpf", "mf")
            preset_content["gyro_source"] = {
                key: val for key, val in preset_content["gyro_source"].items()
                if key in allowed and val is not None
            }
        if "calibration_data" in preset_content:
            preset_content["calibration_data"].pop("sync_settings", None)
    lens_path = None
    if lens_profile is not None:
        lens_path = lens_profile.expanduser().resolve()
        if lens_path.suffix.lower() != ".json" or not lens_path.is_file():
            raise ProjectError("--lens-profile must reference an existing Gyroflow .json profile")
        lens_data = _read_json(lens_path)
        if not lens_data.get("name"):
            raise ProjectError("Lens profile JSON is missing its name")
        if lens_name is None:
            lens_name = lens_data["name"]
        # A lens JSON may include sync_settings that auto-start optical
        # tracking during export. Pass a temporary copy without that field.
        lens_data = dict(lens_data)
        lens_data.pop("sync_settings", None)

    if probe_params is not None:
        # Set the same settings in the actual lens profile and CLI sync args.
        # Gyroflow's render queue checks lens.sync_settings.do_autosync.
        if preset_content is None and lens_path is None:
            raise ProjectError("Sync probe requires an explicit calibrated reference or profile")
        if preset_content is not None:
            lens_preset = preset_content.get("calibration_data")
            if isinstance(lens_preset, dict):
                lens_preset["sync_settings"] = dict(probe_params)
            preset_content["synchronization"] = dict(probe_params)
        if lens_path is not None:
            lens_data["sync_settings"] = dict(probe_params)

    # Gyroflow can invoke optical synchronization by default. Our sync params
    # explicitly turn it OFF, while export-project 2 excludes video rendering.
    executable = _binary(gyroflow)
    if not dry_run:
        if not executable:
            raise ProjectError(
                "Gyroflow executable not found. Pass --gyroflow 'C:\\path\\Gyroflow.exe'"
            )
        version = _check_version(executable)
    else:
        version = "not checked (dry run)"
    intended = {
        "status": "dry_run" if dry_run else "created",
        "source": str(video), "video": str(selected_video),
        "motion": str(motion), "output": str(destination),
        "lens": lens_name, "gyroflow_version": version,
        "source_mode": (manifest.get("motion_input") or {}).get("kind", "embedded_gpmd"),
        "optical_sync": ("experimental autosync requested; results unverified"
                         if sync_probe else "not performed; review required"),
        "probe_report": str(report_dest) if report_dest else None,
    }
    if dry_run:
        return intended

    stage = Path(tempfile.mkdtemp(prefix=f".{destination.stem}-project-", dir=destination.parent))
    try:
        staged_out = stage / destination.name
        if preset_content is not None:
            preset_path = stage / "review_settings.gyroflow"
            preset_path.write_text(json.dumps(preset_content, indent=2), encoding="utf-8")
        if lens_path is not None:
            staged_lens = stage / "review_lens.json"
            staged_lens.write_text(json.dumps(lens_data, indent=2), encoding="utf-8")
            lens_path = staged_lens
        # --out-params output_path determines the output basename; Gyroflow
        # --export-project 2 changes/retains the extension to .gyroflow.
        cmd = [
            executable, str(selected_video), "--gyro-file", str(motion),
            "--export-project", "2",
            "--out-params", json.dumps({"output_path": str(staged_out)}),
            "--sync-params", json.dumps(probe_params if probe_params is not None else _NO_SYNC),
        ]
        if preset_path is not None:
            cmd.extend(["--preset", str(preset_path)])
        if lens_path is not None:
            cmd.append(str(lens_path))
        try:
            run = subprocess.run(cmd, capture_output=True, text=True,
                                 check=False, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectError("Gyroflow project export failed or timed out") from exc
        if run.returncode != 0:
            error = (run.stderr or run.stdout).strip()[-900:]
            raise ProjectError(f"Gyroflow project export exited {run.returncode}: {error}")
        exported = _verify_export(
            staged_out, selected_video, motion, lens_name=lens_name,
            allow_sync=sync_probe,
        )
        _finalize_render_destination(staged_out, destination, selected_video)
        # Recheck after modifying the output folder only.
        _verify_export(staged_out, selected_video, motion, lens_name=lens_name,
                       allow_sync=sync_probe)
        probe_status = "offsets_present_unverified" if exported["sync_point_count"] else "no_sync_points"
        if report_dest is not None:
            report = {
                "format": "motioncam-gyroflow-sync-probe",
                "version": 1,
                "status": probe_status,
                "source_mode": intended["source_mode"],
                "gyroflow_version": version,
                "lens": lens_name,
                "settings": probe_params,
                "project": exported,
                "interpretation": (
                    "New offsets found; this does not verify physical synchronization."
                    if exported["sync_point_count"] else
                    "CLI completed but saved no offsets. Gyroflow may have skipped autosync "
                    "because motion metadata claims accurate timestamps, or optical tracking "
                    "failed. This is not evidence of successful synchronization."
                ),
                "review_required": True,
            }
            staged_report = stage / report_dest.name
            staged_report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if destination.exists() or (report_dest is not None and report_dest.exists()):
            raise ProjectError("Destination appeared during export; refusing to overwrite")
        os.replace(staged_out, destination)
        try:
            if report_dest is not None:
                os.replace(staged_report, report_dest)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        return {
            **intended,
            "status": probe_status if sync_probe else "created",
            "project": exported,
        }
    finally:
        shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="motioncam-gpmf-repair project",
        description="Export a Gyroflow project, or experimentally probe headless autosync (never render)",
    )
    parser.add_argument("video", type=Path, help="Original MOV/MP4 with an existing _prepared package")
    calibration = parser.add_mutually_exclusive_group()
    calibration.add_argument("--reference", type=Path,
                             help="Previous, manually calibrated .gyroflow project (extract safe settings only)")
    calibration.add_argument("--preset", type=Path,
                             help="Gyroflow settings preset containing lens calibration, no clip-specific data")
    parser.add_argument("--lens-profile", type=Path,
                        help="Verified Gyroflow lens JSON; may be used without a preset")
    parser.add_argument("--gyroflow", help="Path to Gyroflow.exe or command on PATH")
    parser.add_argument("--sync-probe", action="store_true",
                        help="EXPERIMENT: request headless Gyroflow autosync and inspect new offsets; no rendering")
    parser.add_argument("--search-size", type=float, default=0.5,
                        help="Probe search window in seconds (default: 0.5)")
    parser.add_argument("--max-sync-points", type=int, default=5,
                        help="Probe number of evenly spaced regions (default: 5)")
    parser.add_argument("--sync-resolution", type=int, default=720,
                        help="Probe optical processing height (default: 720)")
    parser.add_argument("-o", "--output", type=Path, help="New destination .gyroflow file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate inputs and plan only; no Gyroflow launch or file writes")
    args = parser.parse_args(argv)
    try:
        report = build_project(
            args.video, reference=args.reference, preset=args.preset,
            lens_profile=args.lens_profile, gyroflow=args.gyroflow,
            output=args.output, dry_run=args.dry_run,
            sync_probe=args.sync_probe, search_size=args.search_size,
            max_sync_points=args.max_sync_points, sync_resolution=args.sync_resolution,
        )
    except (ProjectError, OSError, TypeError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Gyroflow project: " + report["status"].replace("_", " "))
    print("  Video: " + report["video"])
    print("  Motion: " + report["motion"])
    print("  Lens: " + str(report["lens"]))
    print("  Output: " + report["output"])
    print("  Gyroflow: " + report["gyroflow_version"])
    if args.sync_probe:
        print("  Sync probe: " + report["status"])
        if report.get("project"):
            print(f"  Saved sync points: {report['project']['sync_point_count']}")
            print("  Report: " + str(report["probe_report"]))
        print("  A completed CLI call is NOT proof of correct optical synchronization.")
        return 2 if report["status"] == "no_sync_points" else 0
    print("  Optical sync: NOT performed; review in Gyroflow before rendering")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
