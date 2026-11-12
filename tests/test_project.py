"""Synthetic integration tests for safe Gyroflow 1.7 project export."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from motioncam_gpmf_repair.cli import main as repair_main  # noqa: E402
from motioncam_gpmf_repair.prepare import _fingerprint  # noqa: E402
from motioncam_gpmf_repair.project import (  # noqa: E402
    ProjectError, _reference_preset, _selected_files, _verify_export,
    _verify_preset, build_project, main,
)


def gcsv_text():
    return (
        "GYROFLOW IMU LOG\nversion,1.3\norientation,XYZ\n"
        "tscale,0.001\ngscale,0.001\nascale,0.001\n"
        "t,gx,gy,gz,ax,ay,az\n0,1,2,3,4,5,6\n1,2,3,4,5,6,7\n2,3,4,5,6,7,8\n"
    )


def reference_data():
    return {
        "version": 4,
        "videofile": "file:///C:/old/source.mov",
        "video_info": {"width": 3840, "height": 2160, "fps": 30},
        "calibration_data": {
            "name": "Vivo_X100 Ultra_24mm_4k",
            "camera_model": "X100 Ultra",
            "sync_settings": {"do_autosync": True},
        },
        "stabilization": {"method": "Default", "fov": 1.1},
        "gyro_source": {
            "filepath": "file:///C:/old/old.gcsv",
            "imu_orientation": "XYZ", "integration_method": 2,
            "file_metadata": "PERSONAL-RAW-IMU-CACHE",
            "integrated_quaternions": "PRIVATE-QUATERNIONS",
        },
        "offsets": {"12345": -100.0},
        "keyframes": {"private": True},
    }


class GyroflowProjectTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "recording.mov"
        self.source.write_bytes(b"synthetic video")
        self.package = self.root / "recording_prepared"
        self.package.mkdir()
        self.motion = self.package / "recording_retimed.gcsv"
        self.motion.write_text(gcsv_text(), encoding="utf-8")
        self.inspection = {"format": "motioncam-inspection", "quality": {"status": "warning"},
                           "video": {"width": 3840, "height": 2160}}
        (self.package / "inspection.json").write_text(json.dumps(self.inspection), encoding="utf-8")
        manifest = {
            "format": "motioncam-prepared-package", "version": 2,
            "source": _fingerprint(self.source), "project": None,
            "settings": {}, "files": {
                "motion": self.motion.name, "inspection": "inspection.json", "video": None,
            },
            "motion_input": {"kind": "embedded_gpmd"},
        }
        (self.package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.reference = self.root / "calibrated.gyroflow"
        self.reference.write_text(json.dumps(reference_data()), encoding="utf-8")
        self.preset = self.root / "preset.gyroflow"
        self.preset.write_text(json.dumps({
            "version": 4, "calibration_data": {"name": "Vivo_X100 Ultra_24mm_4k"},
            "gyro_source": {"imu_orientation": "XYZ"},
        }), encoding="utf-8")
        self.lens = self.root / "lens.json"
        self.lens.write_text(json.dumps({"name": "24mm", "camera_brand": "Vivo"}),
                             encoding="utf-8")

    def _fake_gyroflow(self, args, **kwargs):
        if "--version" in args:
            return SimpleNamespace(returncode=0, stdout="Gyroflow v1.7.0\n", stderr="")
        self.assertIn("--export-project", args)
        self.assertEqual(args[args.index("--export-project") + 1], "2")
        self.assertEqual(Path(args[args.index("--gyro-file") + 1]), self.motion.resolve())
        sync = json.loads(args[args.index("--sync-params") + 1])
        self.assertEqual(sync["do_autosync"], False)
        self.assertEqual(sync["do_optical_correction"], False)
        self.assertFalse(any(a in args for a in ("--overwrite", "--ignore-file-motion")))
        export_path = Path(json.loads(args[args.index("--out-params") + 1])["output_path"])
        preset_path = (Path(args[args.index("--preset") + 1])
                       if "--preset" in args else None)
        calibration = {"name": "24mm"}
        orientation = "XYZ"
        if preset_path:
            data = json.loads(preset_path.read_text(encoding="utf-8"))
            calibration = data["calibration_data"]
            orientation = data.get("gyro_source", {}).get("imu_orientation", "XYZ")
            self.assertNotIn("videofile", data)
            self.assertNotIn("offsets", data)
            self.assertNotIn("file_metadata", str(data))
        data = {
            "version": 4, "videofile": Path(args[1]).resolve().as_uri(),
            "gyro_source": {"filepath": self.motion.resolve().as_uri(),
                            "imu_orientation": orientation},
            "calibration_data": calibration, "offsets": {},
        }
        export_path.write_text(json.dumps(data), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="Export completed", stderr="")

    def test_prepared_package_paths_and_structural_gcsv(self):
        video, motion, manifest, inspection = _selected_files(self.source)
        self.assertEqual(video, self.source.resolve())
        self.assertEqual(motion, self.motion.resolve())
        self.assertEqual(manifest["version"], 2)

    def test_reference_extraction_never_copies_old_offsets_or_paths(self):
        preset = _reference_preset(self.reference, self.inspection["video"])
        text = json.dumps(preset)
        self.assertIn("Vivo_X100 Ultra_24mm_4k", text)
        self.assertEqual(preset["gyro_source"]["imu_orientation"], "XYZ")
        self.assertNotIn("old/source.mov", text)
        self.assertNotIn("old.gcsv", text)
        self.assertNotIn("PRIVATE-QUATERNIONS", text)
        self.assertNotIn("offsets", text)
        self.assertNotIn("keyframes", text)
        self.assertNotIn("sync_settings", preset["calibration_data"])
        self.assertFalse(preset["synchronization"]["do_autosync"])

    def test_mismatched_reference_resolution_refused(self):
        data = reference_data()
        data["video_info"]["height"] = 1080
        self.reference.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "height differs"):
            build_project(self.source, reference=self.reference, dry_run=True)

    def test_reference_dry_run_never_calls_gyroflow(self):
        with patch("motioncam_gpmf_repair.project.subprocess.run") as subprocess_run:
            report = build_project(self.source, reference=self.reference, dry_run=True)
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["lens"], "Vivo_X100 Ultra_24mm_4k")
        subprocess_run.assert_not_called()
        self.assertFalse((self.root / "recording_review.gyroflow").exists())

    def test_real_project_export_invokes_supported_cli_and_validates_json(self):
        with patch("motioncam_gpmf_repair.project._binary", return_value="Gyroflow.exe"), \
             patch("motioncam_gpmf_repair.project.subprocess.run", side_effect=self._fake_gyroflow):
            result = build_project(self.source, reference=self.reference)
        out = self.root / "recording_review.gyroflow"
        self.assertTrue(out.exists())
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["project"]["lens"], "Vivo_X100 Ultra_24mm_4k")
        self.assertEqual(result["project"]["sync_point_count"], 0)
        self.assertEqual(self.source.read_bytes(), b"synthetic video")
        self.assertEqual(self.motion.read_text(encoding="utf-8"), gcsv_text())
        self.assertEqual(sorted(p.name for p in self.package.iterdir()),
                         ["inspection.json", "manifest.json", "recording_retimed.gcsv"])

    def test_gyroscope_source_references_are_verified(self):
        wrong = self.root / "bad.gyroflow"
        wrong.write_text(json.dumps({
            "version": 4, "videofile": self.source.resolve().as_uri(),
            "gyro_source": {"filepath": self.reference.resolve().as_uri()},
            "calibration_data": {"name": "24mm"}, "offsets": {},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "prepared motion file"):
            _verify_export(wrong, self.source.resolve(), self.motion.resolve(),
                           lens_name="24mm")

    def test_exported_offsets_are_rejected(self):
        wrong = self.root / "bad.gyroflow"
        wrong.write_text(json.dumps({
            "version": 4, "videofile": self.source.resolve().as_uri(),
            "gyro_source": {"filepath": self.motion.resolve().as_uri()},
            "calibration_data": {"name": "24mm"}, "offsets": {"1000000": 5},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "synchronization offsets"):
            _verify_export(wrong, self.source.resolve(), self.motion.resolve(),
                           lens_name="24mm")

    def test_failed_export_leaves_no_project_or_staging(self):
        def mock(args, **kw):
            if "--version" in args:
                return SimpleNamespace(returncode=0, stdout="Gyroflow v1.7.0", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="synthetic failure")
        with patch("motioncam_gpmf_repair.project._binary", return_value="Gyroflow.exe"), \
             patch("motioncam_gpmf_repair.project.subprocess.run", side_effect=mock):
            with self.assertRaisesRegex(ProjectError, "synthetic failure"):
                build_project(self.source, lens_profile=self.lens)
        self.assertFalse((self.root / "recording_review.gyroflow").exists())
        self.assertFalse(any("-project-" in p.name for p in self.root.iterdir()))

    def test_output_never_overwrites_user_project(self):
        dest = self.root / "recording_review.gyroflow"
        dest.write_text("MY USER PROJECT", encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "already exists"):
            build_project(self.source, lens_profile=self.lens, dry_run=True)
        self.assertEqual(dest.read_text(encoding="utf-8"), "MY USER PROJECT")

    def test_full_project_cannot_be_passed_as_preset(self):
        with self.assertRaisesRegex(ProjectError, "full Gyroflow project"):
            build_project(self.source, preset=self.reference, dry_run=True)

    def test_preset_with_stale_offsets_refused(self):
        data = json.loads(self.preset.read_text(encoding="utf-8"))
        data["offsets"] = {"9": 123.0}
        self.preset.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "old synchronization offsets"):
            _verify_preset(self.preset)

    def test_preset_with_stale_gyro_filepath_refused(self):
        data = json.loads(self.preset.read_text(encoding="utf-8"))
        data["gyro_source"]["filepath"] = "file:///C:/old/file.gcsv"
        self.preset.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "file-specific gyro"):
            _verify_preset(self.preset)

    def test_preset_or_lens_is_explicitly_required(self):
        with self.assertRaisesRegex(ProjectError, "Explicit"):
            build_project(self.source, dry_run=True)

    def test_missing_prep_package_and_modified_source_fail(self):
        with self.assertRaisesRegex(ProjectError, "changed since preparation"):
            self.source.write_bytes(b"new source")
            build_project(self.source, reference=self.reference, dry_run=True)
        self.source.write_bytes(b"synthetic video")
        (self.package / "manifest.json").unlink()
        with self.assertRaisesRegex(ProjectError, "manifest"):
            build_project(self.source, reference=self.reference, dry_run=True)

    def test_version_too_old_rejected(self):
        with patch("motioncam_gpmf_repair.project._binary", return_value="Gyroflow.exe"), \
             patch("motioncam_gpmf_repair.project.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout="Gyroflow v1.6.2", stderr="")):
            with self.assertRaisesRegex(ProjectError, "too old"):
                build_project(self.source, lens_profile=self.lens)

    def test_direct_lens_profile_can_export_without_preset(self):
        with patch("motioncam_gpmf_repair.project._binary", return_value="Gyroflow.exe"), \
             patch("motioncam_gpmf_repair.project.subprocess.run", side_effect=self._fake_gyroflow):
            result = build_project(self.source, lens_profile=self.lens)
        self.assertEqual(result["project"]["lens"], "24mm")

    def test_dispatch_and_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                repair_main(["project", "--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--reference", output.getvalue())

    def test_public_cli_dry_run(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = main([str(self.source), "--reference", str(self.reference), "--dry-run"])
        self.assertEqual(status, 0)
        self.assertIn("NOT performed", output.getvalue())
        self.assertFalse((self.root / "recording_review.gyroflow").exists())

    def test_normalized_video_is_selected_not_original(self):
        selected = self.package / "recording_3840x2160.mov"
        selected.write_bytes(b"synthetic normalized copy")
        man = json.loads((self.package / "manifest.json").read_text(encoding="utf-8"))
        man["files"]["video"] = selected.name
        (self.package / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        selected_video, _, _, _ = _selected_files(self.source)
        self.assertEqual(selected_video, selected.resolve())

    def test_fail_quality_rejected(self):
        self.inspection["quality"]["status"] = "fail"
        (self.package / "inspection.json").write_text(json.dumps(self.inspection))
        with self.assertRaisesRegex(ProjectError, "inspection failed"):
            build_project(self.source, reference=self.reference, dry_run=True)


if __name__ == "__main__":
    unittest.main()
