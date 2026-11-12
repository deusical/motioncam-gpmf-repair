# Headless Gyroflow Auto Sync feasibility probe (PR #8A)

This is a diagnostic experiment, **not** the unattended stabilization pipeline.
It uses Gyroflow v1.7+ with an existing prepared video/GCSV package and a
previously verified calibration. The probe never renders video and never
changes the recording, original RAW log, prepared files, or reference project.

## Why this experiment is necessary

The Gyroflow 1.7 render queue calls Auto Sync before exporting a project.
However, its source code checks several gates before running it:

- motion data must exist;
- the selected video must have no existing synchronization offsets;
- Gyroflow must not consider the motion metadata accurately timestamped
  (with an exception for BRAW); and
- the loaded **lens profile** must have `sync_settings.do_autosync=true`.

The CLI argument `--sync-params` supplies parameters but is **not by
itself** proof that synchronization runs. The probe sets the request in
both the lens calibration's `sync_settings` and CLI sync parameters.

Source: Gyroflow 1.7.0 `src/rendering/render_queue.rs`,
`do_autosync` / `update_sync_settings`, and official lens-profile docs.

## Test with the known 24 mm direct-log recording

Run on Windows, where Gyroflow.exe is installed:

```powershell
cd C:\Project\motioncam-gpmf-repair
git pull

python .\repair_motioncam_gpmf.py project "C:\Users\ender\Downloads\261111_194049_VIDEO_24mm_BT.709_BT709.mov" --reference "C:\Users\ender\Downloads\261111_194049_VIDEO_24mm_BT.709_BT709.gyroflow" --sync-probe --dry-run

python .\repair_motioncam_gpmf.py project "C:\Users\ender\Downloads\261111_194049_VIDEO_24mm_BT.709_BT709.mov" --reference "C:\Users\ender\Downloads\261111_194049_VIDEO_24mm_BT.709_BT709.gyroflow" --sync-probe --gyroflow "C:\programs\Gyroflow\Gyroflow.exe"
```

This creates two **new** files beside the original video:

- `<video>_sync_probe.gyroflow` — a real project serialized by Gyroflow,
  with any sync offsets Gyroflow produced, and stable render-output settings.
- `<video>_sync_probe_report.json` — a diagnostic report recording requested
  parameters, the resulting sync-point count, offset range and time coverage.

No existing files are overwritten. Use `--output NEW.gyroflow` if a probe
output already exists; its report will be `NEW_report.json`.

The probe uses a 0.5-second search window, five evenly distributed regions,
and optical processing height of 720 pixels by default. To explore an
inconclusive result, use `--search-size`, `--max-sync-points`, or
`--sync-resolution` explicitly; each test should use a distinct output name
so results can be compared.

## How to interpret the result

- `offsets_present_unverified`: Gyroflow saved nonempty, numerically valid
  sync offsets, but they might still be wrong. The next step is comparison
  against the manually validated project and visual review. Exit code 0.
- `no_sync_points`: Gyroflow exported a project but saved **zero** offsets.
  A source-metadata accurate-timestamp flag may have suppressed Auto Sync,
  or the optical procedure may not have yielded usable points. This is
  **not a synchronization success**. Exit code 2.
- `ERROR`: CLI, input, malformed project, missing calibration, or output
  failure. No completed probe is published. Exit code 1.

The report is intentionally descriptive, not a confidence score. A stable
offset range does not establish correct physical video-to-gyro alignment.

The original non-probe `project` command stays unchanged and explicitly
disables synchronization. No video render occurs in either command.

## Next decision after the real Windows test

If offsets are present, validate their numerical consistency against
previous manual sync projects and build the review-first orchestration.
If the probe saves zero points, inspect why the accurate-timestamps gate
or optical matching blocked the operation before adding retries. Do not
silently change the GCSV's original timestamps or fake sync points.

Public CI uses simulated Gyroflow 1.7 command responses. Running the probe
against Gyroflow 1.7 on real Windows footage is still required.
