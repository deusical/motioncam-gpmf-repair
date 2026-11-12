# Gyroflow project generation

PR #6 adds the project stage after `prepare`. It uses Gyroflow 1.7+ to export a real
`.gyroflow` project with the prepared GCSV. It neither renders video nor
performs optical synchronization.

## Commands (Windows)

```powershell
cd C:\Project\motioncam-gpmf-repair
python .\repair_motioncam_gpmf.py prepare "C:\path\clip.mov"
python .\repair_motioncam_gpmf.py project "C:\path\clip.mov" --reference "C:\path\previous_clip.gyroflow" --dry-run
python .\repair_motioncam_gpmf.py project "C:\path\clip.mov" --reference "C:\path\previous_clip.gyroflow" --gyroflow "C:\path\Gyroflow.exe"
```

If Gyroflow is on your PATH, the `--gyroflow` argument can be omitted.
You can also provide a verified lens profile with `--lens-profile lens.json`
or a settings-only `--preset settings.gyroflow`, optionally combined with
`--lens-profile`. Full saved Gyroflow projects belong under `--reference`,
not `--preset`.

## Reference project mode

A prior calibrated project is used only as an explicit source of lens
calibration, IMU orientation/integration, and reusable stabilization settings.
The old video's paths, offsets, motion caches, keyframes, and export settings
are never copied. A temporary sanitized preset is passed to Gyroflow.
Normal video dimensions must match the reference. No lens is inferred from
the recording filename, and no automatic per-device calibrations are shipped.

## Result

By default the new project is placed at `<video>_review.gyroflow` beside
the original MOV/MP4, *outside* the protected `<video>_prepared` directory.
This is important: the `prepare` command deliberately refuses to reuse or
overwrite a package containing unexpected files. Existing projects are
never overwritten; use a different `--output` name instead.

Gyroflow initially writes the temporary project staging directory into its
render-output settings. The exporter re-homes only those two output fields
before publishing: `output_folder` points beside the final project, and
`output_filename` becomes `<selected_video>_stabilized.mp4`. This prevents
an unusable temporary render path and an accidental `.gyroflow` video
filename. Review export settings in Gyroflow before rendering.

The project exporter checks the prepared manifest, source-file identity,
GCSV structure, RAW sidecar SHA-256 where applicable, and available lens
calibration. It calls the official Gyroflow CLI with `--gyro-file`,
`--export-project 2`, explicit output settings and disabled autosync.
The generated project is parsed and checked for the correct video/IMU
references, selected calibration, and absence of synchronization points.
Validation failure leaves the original media and package unchanged.

After export, open the project in Gyroflow and **manually run and review
optical synchronization** before rendering. The tool does not guarantee
the resulting footage is correctly stabilized.

The Gyroflow executable isn't installed in public CI, so the CLI
integration is tested with synthetic Gyroflow responses. A real
Windows/Gyroflow run is required to complete field validation.

## Next phase

PR #7 will focus on optional optical synchronization, quality gates,
and controlled video rendering.
