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
            with self.assertRaisesRegex(PrepareError, "External motion-file support"):
                prepare(self.source)
        rep.assert_not_called()
        self.assertFalse(self.output_dir.exists())

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
