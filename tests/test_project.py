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
            "output": {
                "output_folder": export_path.parent.as_uri(),
                "output_filename": export_path.name,
            },
        }
        export_path.write_text(json.dumps(data), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="Export completed", stderr="")

    def _fake_sync_probe(self, args, **kwargs):
        if "--version" in args:
            return SimpleNamespace(returncode=0, stdout="Gyroflow v1.7.0", stderr="")
        settings = json.loads(args[args.index("--sync-params") + 1])
        self.assertTrue(settings["do_autosync"])
        self.assertEqual(settings["do_optical_correction"], False)
        self.assertEqual(settings["processing_resolution"], 720)
        self.assertEqual(settings["max_sync_points"], 5)
        self.assertIn("--export-project", args)
        self.assertEqual(args[args.index("--export-project") + 1], "2")
        self.assertNotIn("--optical-correction", args)
        path = Path(json.loads(args[args.index("--out-params") + 1])["output_path"])
        preset_path = Path(args[args.index("--preset") + 1])
        preset = json.loads(preset_path.read_text())
        self.assertTrue(preset["calibration_data"]["sync_settings"]["do_autosync"])
        self.assertTrue(preset["synchronization"]["do_autosync"])
        self.assertNotIn("videofile", preset)
        self.assertNotIn("offsets", preset)
        self.assertNotIn("old.gcsv", json.dumps(preset))
        data = {
            "version": 4, "videofile": self.source.resolve().as_uri(),
            "gyro_source": {"filepath": self.motion.resolve().as_uri(), "imu_orientation": "XYZ"},
            "calibration_data": preset["calibration_data"],
            "offsets": {"1000000": -8.2, "2000000": -8.4, "3000000": -8.1},
            "output": {"output_folder": path.parent.as_uri(), "output_filename": path.name},
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="Export completed", stderr="")

    def test_probe_single_invocation_saves_new_offsets_and_report(self):
        with patch("motioncam_gpmf_repair.project._binary", return_value="Gyroflow.exe"), \
             patch("motioncam_gpmf_repair.project.subprocess.run", side_effect=self._fake_sync_probe):
            result = build_project(self.source, reference=self.reference, sync_probe=True)
        self.assertEqual(result["status"], "offsets_present_unverified")
        self.assertEqual(result["project"]["sync_point_count"], 3)
        self.assertAlmostEqual(result["project"]["offset_span_ms"], 0.3)
        output = self.root / "recording_sync_probe.gyroflow"
        report = self.root / "recording_sync_probe_report.json"
        self.assertTrue(output.exists())
        saved = json.loads(report.read_text())
        self.assertTrue(saved["review_required"])
        self.assertEqual(saved["project"]["sync_point_count"], 3)
        self.assertNotIn("old/source", output.read_text())
        self.assertEqual(json.loads(output.read_text())["output"]["output_folder"],
                         self.root.resolve().as_uri())

    def test_probe_zero_points_is_not_claimed_success(self):
        def fake_zero(args, **kwargs):
            result = self._fake_sync_probe(args, **kwargs)
            if "--version" not in args:
                path = Path(json.loads(args[args.index("--out-params") + 1])["output_path"])
                data = json.loads(path.read_text())
                data["offsets"] = {}
                path.write_text(json.dumps(data))
            return result
        with patch("motioncam_gpmf_repair.project._binary", return_value="Gyroflow.exe"), \
             patch("motioncam_gpmf_repair.project.subprocess.run", side_effect=fake_zero):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main([str(self.source), "--reference", str(self.reference),
                             "--sync-probe"])
        self.assertEqual(code, 2)
        self.assertIn("no_sync_points", output.getvalue())
        report = json.loads((self.root / "recording_sync_probe_report.json").read_text())
        self.assertEqual(report["status"], "no_sync_points")
        self.assertIn("may have skipped", report["interpretation"])

    def test_probe_lens_profile_injects_autosync_settings(self):
        def fake_lens(args, **kwargs):
            if "--version" in args:
                return SimpleNamespace(returncode=0, stdout="Gyroflow v1.7.0", stderr="")
            staged = [Path(x) for x in args if x.endswith("review_lens.json")]
            self.assertEqual(len(staged), 1)
            lens = json.loads(staged[0].read_text())
            self.assertTrue(lens["sync_settings"]["do_autosync"])
            out = Path(json.loads(args[args.index("--out-params") + 1])["output_path"])
            out.write_text(json.dumps({
                "version": 4, "videofile": self.source.resolve().as_uri(),
                "gyro_source": {"filepath": self.motion.resolve().as_uri()},
                "calibration_data": {"name": "24mm"},
                "offsets": {"1000000": -10.0},
                "output": {"output_folder": out.parent.as_uri(), "output_filename": out.name},
            }))
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with patch("motioncam_gpmf_repair.project._binary", return_value="Gyroflow.exe"), \
             patch("motioncam_gpmf_repair.project.subprocess.run", side_effect=fake_lens):
            result = build_project(self.source, lens_profile=self.lens, sync_probe=True)
        self.assertEqual(result["project"]["sync_point_count"], 1)

    def test_probe_refuses_bad_params_and_existing_report(self):
        for opt in (
            {"search_size": 0},
            {"search_size": float("nan")},
            {"max_sync_points": 0},
            {"sync_resolution": 721},
        ):
            with self.subTest(opt=opt):
                with self.assertRaises(ProjectError):
                    build_project(self.source, reference=self.reference,
                                  sync_probe=True, dry_run=True, **opt)
        with self.assertRaisesRegex(ProjectError, "require --sync-probe"):
            build_project(self.source, reference=self.reference,
                          search_size=0.8, dry_run=True)
        report = self.root / "recording_sync_probe_report.json"
        report.write_text("USER DATA")
        with self.assertRaisesRegex(ProjectError, "report already exists"):
            build_project(self.source, reference=self.reference, sync_probe=True,
                          dry_run=True)
        self.assertEqual(report.read_text(), "USER DATA")

    def test_probe_dry_run_does_not_touch_files_or_run_gyroflow(self):
        with patch("motioncam_gpmf_repair.project.subprocess.run") as exe:
            result = build_project(self.source, reference=self.reference,
                                   sync_probe=True, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertIn("experimental autosync", result["optical_sync"])
        exe.assert_not_called()
        self.assertFalse((self.root / "recording_sync_probe.gyroflow").exists())

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
        exported = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(exported["output"]["output_folder"], self.root.resolve().as_uri())
        self.assertEqual(exported["output"]["output_filename"], "recording_stabilized.mp4")
        self.assertNotIn("-project-", exported["output"]["output_folder"])
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

    def test_missing_gyroflow_output_settings_rejected_without_writing(self):
        def mock_without_output(args, **kwargs):
            if "--version" in args:
                return SimpleNamespace(returncode=0, stdout="Gyroflow v1.7.0", stderr="")
            staged_path = Path(json.loads(args[args.index("--out-params") + 1])["output_path"])
            staged_path.write_text(json.dumps({
                "version": 4, "videofile": self.source.resolve().as_uri(),
                "gyro_source": {"filepath": self.motion.resolve().as_uri()},
                "calibration_data": {"name": "24mm"}, "offsets": {},
            }), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="Export completed", stderr="")
        with patch("motioncam_gpmf_repair.project._binary", return_value="Gyroflow.exe"), \
             patch("motioncam_gpmf_repair.project.subprocess.run",
                   side_effect=mock_without_output):
            with self.assertRaisesRegex(ProjectError, "missing Gyroflow output settings"):
                build_project(self.source, lens_profile=self.lens)
        self.assertFalse((self.root / "recording_review.gyroflow").exists())

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

    def test_reference_uses_normalized_video_dimensions(self):
        selected = self.package / "recording_3840x2160.mov"
        selected.write_bytes(b"synthetic normalized video")
        man = json.loads((self.package / "manifest.json").read_text(encoding="utf-8"))
        man["files"]["video"] = selected.name
        (self.package / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        # Original container is padded to 2176, but normalized output is 2160.
        self.inspection["video"]["height"] = 2176
        (self.package / "inspection.json").write_text(json.dumps(self.inspection))
        with patch("motioncam_gpmf_repair.project._run",
                   return_value=json.dumps({"streams": [{"width": 3840, "height": 2160}]})):
            result = build_project(self.source, reference=self.reference, dry_run=True)
        self.assertEqual(result["video"], str(selected.resolve()))
        self.assertEqual(result["lens"], "Vivo_X100 Ultra_24mm_4k")

    def test_invalid_normalized_video_ffprobe_rejected(self):
        selected = self.package / "recording_3840x2160.mov"
        selected.write_bytes(b"synthetic normalized video")
        man = json.loads((self.package / "manifest.json").read_text(encoding="utf-8"))
        man["files"]["video"] = selected.name
        (self.package / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        with patch("motioncam_gpmf_repair.project._run", return_value='{"streams": []}'):
            with self.assertRaisesRegex(ProjectError, "Cannot inspect normalized"):
                build_project(self.source, reference=self.reference, dry_run=True)

    def test_fail_quality_rejected(self):
        self.inspection["quality"]["status"] = "fail"
        (self.package / "inspection.json").write_text(json.dumps(self.inspection))
        with self.assertRaisesRegex(ProjectError, "inspection failed"):
            build_project(self.source, reference=self.reference, dry_run=True)


if __name__ == "__main__":
    unittest.main()
