# MotionCam GPMF Repair

**Experimental tool for repairing MotionCam's embedded gyro timestamp timeline for Gyroflow.**

This tool reconstructs a continuous timeline from MotionCam's embedded GPMF gyroscope and accelerometer samples and exports a Gyroflow-compatible GCSV log. It is intended for recordings where the embedded motion-data track produces unreliable synchronization in Gyroflow.

> **Status: experimental (alpha).** It has been preliminarily shown to work on the **Vivo X100 Ultra**. Compatibility is not guaranteed; verify motion synchronization before relying on converted data.

## Quick start (Windows, no Python packages)

1. Install [Python 3](https://www.python.org/downloads/) and [FFmpeg](https://ffmpeg.org/download.html), ensuring `ffmpeg` and `ffprobe` are in `PATH`.
2. Download/extract this repository or clone it. Drag a MotionCam `.mov` onto **`Convert_MOV_to_GCSV.bat`**.
3. Find the resulting `<video>_retimed.gcsv` beside your MOV.
4. Open the original video in **Gyroflow**. Under **Motion data → Open file**, select the new `.gcsv`.
5. Load a lens profile matching the *camera lens, crop, and processing mode*. The converter **does not guess or change IMU axis orientation**; use Gyroflow's **Guess IMU orientation** against the footage, and verify the orientation against optical flow. A `.gcsv` without an `orientation` field starts with Gyroflow's default identity mapping.
6. Remove old synchronization points, run Auto sync (`rs-sync` is a useful alternative), and inspect the resulting offsets before exporting.

Neither the MOV nor its audio/video content is changed. Do not delete your originals.

## Command line

From the extracted project folder, with no Python installation step beyond Python itself:

```powershell
python .\repair_motioncam_gpmf.py "C:\path\clip.mov"
python .\repair_motioncam_gpmf.py "C:\path\clip.mov" --output "C:\path\clip_retimed.gcsv"
python .\repair_motioncam_gpmf.py "C:\path\clip.mov" --dry-run
# Optional, only after independently verifying a mapping in Gyroflow:
python .\repair_motioncam_gpmf.py "C:\path\clip.mov" --imu-orientation yXz
```

Or install as a standard Python CLI:

```sh
python -m pip install -e .
motioncam-gpmf-repair clip.mov
motioncam-gpmf-repair clip1.mov clip2.mov --dry-run
motioncam-gpmf-repair clip.mov --overwrite
```

Run `motioncam-gpmf-repair --help` for options. Requires external FFmpeg utilities; uses only the Python standard library at runtime.

## Extract sensor diagnostics (no video upload)

To investigate a mismatched or misaligned IMU stream, generate a small compressed diagnostic that contains **both** raw gyro and accelerometer samples, their original `STMP` values, per-batch packet indices, and container packet timestamps. It contains no video frames, audio, or GPS fields.

```powershell
python .\\extract_motioncam_imu.py "C:\\path\\clip.mov"
```

This writes `clip_imu_diagnostic.json.gz` alongside the MOV. The command also works after package installation as `motioncam-gpmf-dump clip.mov`. The diagnostic preserves original streams, batch timestamps, and any source GPMF `ORIN`/`ORIO` axis labels; **no samples are dropped or interpolated in the dump**. Those labels are *not automatically assumed to be the camera-to-IMU mapping*. Treat the dump as private sensor data even though images and location fields are omitted.

If automatic alignment cannot establish compatible sensor clocks, use the diagnostic to investigate. To intentionally omit acceleration, run `python .\\repair_motioncam_gpmf.py "C:\\path\\clip.mov" --gyro-only`. This explicit fallback does not attempt to align the streams.

## Inspect timing and Gyroflow synchronization

Run an inspection **without converting** the recording or changing its IMU axes.
The inspector fits both sensor clocks (including when sample counts match),
compares first-anchored and fitted-intercept GPMF batch timestamp residuals,
and optionally examines synchronization points from a Gyroflow project.

From the source checkout:


~~~powershell
python .\repair_motioncam_gpmf.py inspect "C:\path\clip.mov"
python .\repair_motioncam_gpmf.py inspect "C:\path\clip.mov" --project "C:\path\clip.gyroflow"
python .\repair_motioncam_gpmf.py inspect "C:\path\clip_imu_diagnostic.json.gz" --project "C:\path\clip.gyroflow"
python .\repair_motioncam_gpmf.py inspect "C:\path\clip.mov" --json --output "C:\path\inspect.json"
~~~

After installing the package, use `motioncam-gpmf-inspect` or
`motioncam-gpmf-repair inspect` with the same options.

The report now displays an **automated PASS / WARNING / FAIL screening result**,
plus independent gyro/accel clock checks, GPMF metadata-packet PTS versus
`STMP`, and (when the MOV/MP4 is available) video-frame PTS cadence and
metadata-to-video packet PTS proximity. PASS requires compatible sensor
clocks and at least three Gyroflow sync points spanning 30 seconds with an
offset range no greater than 25% of a video frame. This is an explicitly
provisional heuristic, **not** a guarantee of correct stabilization.
**Visual verification is always required.** See [validation methodology](docs/VALIDATION.md)
for the limits and precise criteria.

The JSON report contains aggregate statistics, filenames (not full paths),
lens and orientation metadata, and Gyroflow sync-point offsets. It does
**not** copy private raw IMU samples or embedded Gyroflow motion caches.
A video without an embedded `gpmd` stream is reported as missing
motion data rather than causing the inspector to crash.

**Interpretation:** GPMF `STMP` metadata may not correspond to
the first reading of each batch. A large first-anchored residual can reflect
a systematic timestamp-origin difference. The fitted-intercept residual
characterizes how well a uniform batch-timestamp model fits; neither
statistic measures physical gyro sample jitter or establishes the correct
video/IMU offset. Optical sync-point trends are exploratory and should
be reviewed in Gyroflow. The inspector does not repair or guess sensor axes.


## Optional lossless video-height normalization

Some camera encoders pad the displayed frame dimensions. This separate utility
uses HEVC conformance-window metadata to hide **explicitly specified** padding
without re-encoding the image. No device, lens, or intended aspect ratio is
inferred from the filename. The original recording is never overwritten.

For example, for HEVC footage known to have 16 unwanted rows **at the bottom**:


~~~powershell
python .\normalize_motioncam_video.py "C:\path\clip.mov" --target-height 2160 --dry-run
python .\normalize_motioncam_video.py "C:\path\clip.mov" --target-height 2160
# Or give the intended image aspect ratio explicitly:
python .\normalize_motioncam_video.py "C:\path\clip.mov" --aspect 16:9
~~~

This writes a new file named like `clip_3840x2160.mov`. It supports
only HEVC in MOV/MP4, refuses existing coded/display-size differences,
limits the number of removed rows, and checks output dimensions, streams,
durations, and frame counts. By default, all rows are cropped **from the bottom**;
use `--crop-top N` only when the image really contains top padding.
For best lens-profile compatibility, normalize **before** optical calibration
or stabilization.

This remuxes recognized streams with FFmpeg (`-map 0 -c copy`), preserving their
encoded media packets, but proprietary or unrecognized container atoms are not
guaranteed to survive a remux. Keep your source file. Use
`motioncam-video-normalize` instead of the script after installing the package.
The video-height tool does not create or fix gyro metadata.

## How it works

1. `ffprobe` finds the embedded `gpmd` track and reads packet boundaries; `ffmpeg` extracts the compressed-container-free GPMF payload.
2. A limited GPMF parser reads sequential `GYRO` and `ACCL` batches and their scaling and `STMP` timestamps.
3. The tool validates scales and monotonically increasing batch timestamps, fits each sensor's sample clock using all available batch-start timestamps, and checks gyro/accelerometer rate agreement. It preserves the first gyro timestamp as its timeline origin and evenly spaces subsequent readings according to the fitted rate.
4. It writes Gyroflow GCSV with reconstructed sample timestamps and the **original GPMF axis values**, without changing axis order or sign. Optionally, `--imu-orientation` embeds a user-supplied Gyroflow mapping in metadata but does not alter the samples.

All recordings use independent gyro and accelerometer clock fits, **even when sample counts match**. Acceleration is interpolated onto the reconstructed gyro times. Batch timestamp deviations are diagnostics, not proof of dropped samples: GPMF batch boundaries and packet scheduling can cause substantial apparent jitter. The converter rejects non-increasing batch timestamps, inconsistent long-term sampling rates, sensor-clock disagreement, and uncovered accelerometer edges exceeding one sample period. It cannot prove that all individual samples are present or reconstruct genuinely missing measurements.

## Compatibility and limitations

This is **not** a generic GoPro GPMF parser, a Video stabilization application, or an official MotionCam patch. Only use it when the embedded GPMF track is inconsistent and you can validate motion synchronization. Some files may work better with their original embedded gyro or with separately exported MotionCam motion data.

## Limitations and safety

- **No hardcoded device-specific axis remapping.** The output retains raw GPMF channel order and sign. Gyroflow defaults to `XYZ` when no orientation metadata is supplied, but this is only the identity transform, **not a claim that the sensor aligns with the camera**. Gyroflow's Guess IMU orientation uses video motion; validate its result and synchronization. If supplying `--imu-orientation`, use a mapping you have independently verified.
- **Uniform timing is an assumption**, not a restoration of missing timestamps. Dropped samples, varying IMU rates, and clock drift will degrade results. If sync offsets vary substantially across a clip, don't trust the output.
- **All recordings follow the same clock-fitting and accelerometer-interpolation path, irrespective of matching sensor counts.** The converter supports at most one sample of count difference by default. It holds the nearest observed acceleration measurement for uncovered edges no larger than one accelerometer sample interval (no linear extrapolation), and reports clamped durations. Large gaps, incompatible clocks, or non-monotonic timestamps fail closed. These checks detect some discontinuities but cannot prove the absence of dropped samples.
- The root problem may be in metadata generation, decoding, axis mapping, or synchronization; re-timing alone does not establish the cause. Logs from earlier versions of this tool used a fixed axis transformation; **regenerate them** to compare accurately with this version. Existing Gyroflow orientation settings may no longer apply.
- Generated GCSV may expose device motion/location context; `.gitignore` excludes MOVs and gyro logs. Never commit actual private recordings.
- This tool does not stabilize video by itself; Gyroflow does.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI runs the synthetic tests in one Linux job when code or workflow files change; documentation-only commits skip CI. A manual run is also available. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) for development and privacy guidance.

See [troubleshooting and bug-report guidance](docs/BUG_REPORT.md) for reporting synchronization problems. This project is licensed under the [MIT License](LICENSE). MotionCam, Vivo, GoPro GPMF, and Gyroflow are referenced for interoperability; this independent project is not affiliated with or endorsed by them.