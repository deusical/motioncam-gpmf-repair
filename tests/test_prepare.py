"""Purely synthetic integration tests for the non-destructive prepare workflow."""
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

from motioncam_gpmf_repair.prepare import PrepareError, main, prepare  # noqa: E402
from motioncam_gpmf_repair.cli import main as repair_cli  # noqa: E402
from motioncam_gpmf_repair.core import RepairError  # noqa: E402


def fake_inspect(source, project=None):
    return {
        "format": "motioncam-inspection", "version": 1,
        "source_filename": source.name,
        "imu": {"status": "ok", "clock_alignment": {"status": "compatible"}},
        "quality": {"status": "warning"},
        "warnings": ["Optical synchronization not yet verified"],
    }


def fake_repair(source, destination=None, *, dry_run=False, **kwargs):
    if not dry_run:
        destination.write_text("GYROFLOW IMU LOG\nsynthetic\n", encoding="utf-8")
    return SimpleNamespace(samples=6, rate_hz=400.123, output=None if dry_run else str(destination))


def fake_normalize(source, *, output=None, dry_run=False, **kwargs):
    if not dry_run:
        output.write_bytes(b"synthetic remuxed HEVC")
    return output, {"source_width": 3840, "source_height": 2176, "target_height": 2160}


@contextlib.contextmanager
def mocked_processing():
    with patch("motioncam_gpmf_repair.prepare.inspect_file", side_effect=fake_inspect) as inspect:
        with patch("motioncam_gpmf_repair.prepare.repair", side_effect=fake_repair) as repair:
            with patch("motioncam_gpmf_repair.prepare.normalize", side_effect=fake_normalize) as normalize:
                yield inspect, repair, normalize


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.source = self.folder / "sample.mov"
        self.source.write_bytes(b"original video must never change")
        self.original_bytes = self.source.read_bytes()
        self.output_dir = self.folder / "sample_prepared"

    def test_default_package_preserves_original_video(self):
        with mocked_processing() as (inspect, repair, normalize):
            result = prepare(self.source)
        self.assertEqual(result["status"], "created")
        self.assertTrue((self.output_dir / "sample_retimed.gcsv").is_file())
        self.assertTrue((self.output_dir / "inspection.json").is_file())
        self.assertTrue((self.output_dir / "manifest.json").is_file())
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(len(list(self.output_dir.iterdir())), 3)
        self.assertEqual(result["quality"], "warning")
        self.assertEqual(normalize.call_count, 0)
        saved = json.loads((self.output_dir / "inspection.json").read_text())
        self.assertEqual(saved["format"], "motioncam-inspection")
        manifest = json.loads((self.output_dir / "manifest.json").read_text())
        self.assertIsNone(manifest["files"]["video"])
        self.assertEqual(manifest["source"]["filename"], "sample.mov")

    def test_dry_run_writes_nothing(self):
        with mocked_processing() as (_, rep, norm):
            result = prepare(self.source, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(self.output_dir.exists())
        self.assertEqual(rep.call_count, 1)
        self.assertTrue(rep.call_args.kwargs["dry_run"])
        norm.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    def test_optional_crop_is_explicit_and_staged(self):
        with mocked_processing() as (_, rep, norm):
            result = prepare(self.source, target_height=2160, crop_top=0)
        self.assertEqual(result["files"]["video"], "sample_3840x2160.mov")
        self.assertEqual(norm.call_count, 2)
        self.assertEqual(norm.call_args.kwargs["target_height"], 2160)
        self.assertTrue((self.output_dir / "sample_3840x2160.mov").is_file())
        self.assertFalse((self.folder / ".motioncam-crop-plan.mov").exists())
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    def test_existing_package_defaults_to_refusal(self):
        with mocked_processing():
            prepare(self.source)
            with self.assertRaisesRegex(PrepareError, "already exists"):
                prepare(self.source)

    def test_reuse_matching_package_skips_expensive_work(self):
        with mocked_processing():
            prepare(self.source)
        with mocked_processing() as (ins, rep, norm):
            result = prepare(self.source, reuse=True)
        self.assertEqual(result["status"], "reused")
        ins.assert_not_called()
        rep.assert_not_called()
        norm.assert_not_called()

    def test_reuse_disallows_changed_options_and_source(self):
        with mocked_processing():
            prepare(self.source)
            with self.assertRaisesRegex(PrepareError, "differs"):
                prepare(self.source, reuse=True, gyro_only=True)
            self.source.write_bytes(self.original_bytes + b" changed")
            with self.assertRaisesRegex(PrepareError, "differs"):
                prepare(self.source, reuse=True)

    def test_reuse_requires_a_prepared_package(self):
        with mocked_processing() as (ins, rep, norm):
            with self.assertRaisesRegex(PrepareError, "requires an existing"):
                prepare(self.source, reuse=True)
        ins.assert_not_called()

    def test_gyroflow_project_change_invalidates_reuse(self):
        project = self.folder / "clip.gyroflow"
        project.write_text('{"synthetic": 1}', encoding="utf-8")
        with mocked_processing():
            prepare(self.source, project=project)
            project.write_text('{"synthetic": 22}', encoding="utf-8")
            with self.assertRaisesRegex(PrepareError, "differs"):
                prepare(self.source, project=project, reuse=True)

    def test_overwrite_replaces_intact_package_and_retains_video(self):
        with mocked_processing():
            prepare(self.source)
        (self.output_dir / "sample_retimed.gcsv").write_text("old file", encoding="utf-8")
        with mocked_processing():
            result = prepare(self.source, overwrite=True, gyro_only=True)
        self.assertEqual(result["status"], "updated")
        self.assertIn("synthetic", (self.output_dir / "sample_retimed.gcsv").read_text())
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(json.loads((self.output_dir / "manifest.json").read_text())["settings"]["gyro_only"], True)

    def test_overwrite_never_touches_unknown_directory(self):
        self.output_dir.mkdir()
        (self.output_dir / "keep.txt").write_text("important personal file", encoding="utf-8")
        with mocked_processing():
            with self.assertRaisesRegex(PrepareError, "no valid manifest"):
                prepare(self.source, overwrite=True)
        self.assertEqual((self.output_dir / "keep.txt").read_text(), "important personal file")

    def test_extra_user_file_blocks_overwrite_and_reuse(self):
        with mocked_processing():
            prepare(self.source)
        (self.output_dir / "user-notes.txt").write_text("keep", encoding="utf-8")
        with mocked_processing():
            with self.assertRaisesRegex(PrepareError, "unexpected or missing"):
                prepare(self.source, overwrite=True)
            with self.assertRaisesRegex(PrepareError, "unexpected or missing"):
                prepare(self.source, reuse=True)
        self.assertEqual((self.output_dir / "user-notes.txt").read_text(), "keep")

    def test_invalid_manifest_file_names_are_refused(self):
        with mocked_processing():
            prepare(self.source)
        manifest_file = self.output_dir / "manifest.json"
        data = json.loads(manifest_file.read_text())
        data["files"]["motion"] = "../private.txt"
        manifest_file.write_text(json.dumps(data), encoding="utf-8")
        with mocked_processing():
            with self.assertRaisesRegex(PrepareError, "unsafe filenames"):
                prepare(self.source, overwrite=True)

    def test_failure_mid_conversion_leaves_no_package(self):
        with mocked_processing() as (_, rep, norm):
            def fail_after_preflight(*args, **kwargs):
                if not kwargs.get("dry_run", False):
                    raise RepairError("synthetic conversion failure")
                return fake_repair(*args, **kwargs)
            rep.side_effect = fail_after_preflight
            with self.assertRaisesRegex(RepairError, "synthetic conversion failure"):
                prepare(self.source)
        self.assertFalse(self.output_dir.exists())
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertFalse(any("stage-" in p.name for p in self.folder.iterdir()))

    def test_failed_overwrite_keeps_prior_package_unchanged(self):
        with mocked_processing():
            prepare(self.source)
        previous = (self.output_dir / "sample_retimed.gcsv").read_bytes()
        with mocked_processing() as (_, rep, norm):
            def fail_after_preflight(*args, **kwargs):
                if not kwargs.get("dry_run", False):
                    raise RepairError("synthetic failure")
                return fake_repair(*args, **kwargs)
            rep.side_effect = fail_after_preflight
            with self.assertRaises(RepairError):
                prepare(self.source, overwrite=True)
        self.assertEqual((self.output_dir / "sample_retimed.gcsv").read_bytes(), previous)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    def test_missing_gpmd_blocks_conversion(self):
        with mocked_processing() as (inspect, rep, norm):
            inspect.side_effect = None
            inspect.return_value = {
                "imu": {"status": "missing", "warnings": ["No gpmd track"]},
                "quality": {"status": "warning"},
            }
            with self.assertRaisesRegex(PrepareError, "No embedded GPMF or matching"):
                prepare(self.source)
        rep.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def _create_raw_sidecar(self, *, name="sample.gcsv"):
        file = self.folder / name
        file.write_text(
            "GYROFLOW IMU LOG\nversion,1.2\norientation,XYZ\n"
            "tscale,0.0022743480\ngscale,0.0000000100\n"
            "ascale,0.0000000100\nt,gx,gy,gz,ax,ay,az\n"
            "0,11,12,13,21,22,23,\n1,12,13,14,22,23,24,\n"
            "2,13,14,15,23,24,25,\n",
            encoding="utf-8",
        )
        return file

    def test_raw_gcsv_autodiscovery_preserves_bytes(self):
        source_log = self._create_raw_sidecar()
        original = source_log.read_bytes()
        with mocked_processing() as (inspect, rep, norm):
            inspect.side_effect = None
            inspect.return_value = fake_inspect(self.source)
            inspect.return_value["imu"] = {"status": "missing"}
            with patch("motioncam_gpmf_repair.prepare._video_duration_seconds", return_value=0.2):
                result = prepare(self.source)
        self.assertEqual(result["mode"], "sidecar_gcsv")
        rep.assert_not_called()
        motion = self.output_dir / "sample_motion.gcsv"
        self.assertEqual(motion.read_bytes(), original)
        self.assertEqual(source_log.read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        report = json.loads((self.output_dir / "inspection.json").read_text())
        self.assertEqual(report["motion_source"]["processing"], "copied_without_retiming")
        self.assertEqual(report["external_motion"]["sample_count"], 3)
        self.assertEqual(report["quality"]["status"], "warning")
        manifest = json.loads((self.output_dir / "manifest.json").read_text())
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["motion_input"]["kind"], "sidecar_gcsv")
        self.assertEqual(len(manifest["motion_input"]["file"]["sha256"]), 64)

    def test_raw_gcsv_duration_mismatch_warns_without_resampling(self):
        sidecar = self._create_raw_sidecar()
        with mocked_processing() as (ins, rep, norm):
            ins.side_effect = None
            ins.return_value = fake_inspect(self.source)
            ins.return_value["imu"] = {"status": "missing"}
            with patch("motioncam_gpmf_repair.prepare._video_duration_seconds",
                       return_value=5):
                result = prepare(self.source, motion=sidecar, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertTrue(any("duration differs" in x for x in result["warnings"]))
        self.assertFalse(self.output_dir.exists())

    def test_raw_sidecar_requires_no_axes_or_gyro_only_overrides(self):
        sidecar = self._create_raw_sidecar()
        with mocked_processing() as (ins, rep, norm):
            for kwargs in ({"gyro_only": True}, {"imu_orientation": "XYZ"}):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaisesRegex(PrepareError, "preserved verbatim"):
                        prepare(self.source, motion=sidecar, **kwargs)
        rep.assert_not_called()

    def test_explicit_raw_sidecar_overrides_embedded_gpmd(self):
        sidecar = self._create_raw_sidecar()
        with mocked_processing() as (ins, rep, norm):
            with patch("motioncam_gpmf_repair.prepare._video_duration_seconds",
                       return_value=0.2):
                result = prepare(self.source, motion=sidecar)
        self.assertEqual(result["mode"], "sidecar_gcsv")
        rep.assert_not_called()

    def test_raw_reuse_checks_sidecar_checksum(self):
        sidecar = self._create_raw_sidecar()
        with mocked_processing() as (ins, rep, norm):
            with patch("motioncam_gpmf_repair.prepare._video_duration_seconds",
                       return_value=0.2):
                prepare(self.source, motion=sidecar)
            self.assertEqual(prepare(self.source, motion=sidecar, reuse=True)["status"], "reused")
            sidecar.write_text(sidecar.read_text().replace("11,12,13", "11,12,14"),
                               encoding="utf-8")
            with self.assertRaisesRegex(PrepareError, "source GCSV"):
                prepare(self.source, motion=sidecar, reuse=True)

    def test_legacy_v1_package_can_be_reused_and_overwritten(self):
        with mocked_processing() as (ins, rep, norm):
            prepare(self.source)
            path = self.output_dir / "manifest.json"
            content = json.loads(path.read_text())
            content["version"] = 1
            del content["motion_input"]
            path.write_text(json.dumps(content), encoding="utf-8")
            self.assertEqual(prepare(self.source, reuse=True)["status"], "reused")
            self.assertEqual(prepare(self.source, overwrite=True)["status"], "updated")

    def test_malformed_raw_gcsv_fails_without_touching_source(self):
        sidecar = self._create_raw_sidecar()
        sidecar.write_text(sidecar.read_text().replace("1,12,13", "0,12,13"),
                           encoding="utf-8")
        with mocked_processing() as (ins, rep, norm):
            with self.assertRaisesRegex(Exception, "Non-increasing"):
                prepare(self.source, motion=sidecar)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertFalse(self.output_dir.exists())

    def test_raw_copy_failure_keeps_old_package(self):
        sidecar = self._create_raw_sidecar()
        with mocked_processing() as (ins, rep, norm):
            with patch("motioncam_gpmf_repair.prepare._video_duration_seconds", return_value=0.2):
                prepare(self.source, motion=sidecar)
            old = (self.output_dir / "sample_motion.gcsv").read_bytes()
            with patch("motioncam_gpmf_repair.prepare.shutil.copyfile",
                       side_effect=OSError("injected copy failure")):
                with self.assertRaisesRegex(OSError, "injected copy failure"):
                    prepare(self.source, motion=sidecar, overwrite=True)
            self.assertEqual((self.output_dir / "sample_motion.gcsv").read_bytes(), old)
            self.assertEqual(sidecar.read_bytes(), old)

    def test_invalid_crop_and_output_directory_rejected(self):
        with mocked_processing():
            with self.assertRaisesRegex(PrepareError, "requires"):
                prepare(self.source, crop_top=2)
            with self.assertRaisesRegex(PrepareError, "must not contain"):
                prepare(self.source, output_dir=self.folder)
            with self.assertRaises(RepairError):
                prepare(self.source, imu_orientation="XXZ")
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

    def test_cli_dispatch_and_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as caught:
                repair_cli(["prepare", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--target-height", output.getvalue())

    def test_cli_prints_prepared_directory(self):
        with mocked_processing():
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main([str(self.source), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("no files created or changed", output.getvalue())
        self.assertFalse(self.output_dir.exists())


if __name__ == "__main__":
    unittest.main()
