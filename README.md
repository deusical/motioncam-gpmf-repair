# MotionCam GPMF Repair

**Experimental tool for repairing MotionCam's embedded gyro timestamp timeline for Gyroflow.**

This tool reconstructs a continuous timeline from MotionCam's embedded GPMF gyroscope and accelerometer samples and exports a Gyroflow-compatible GCSV log. It is intended for recordings where the embedded motion-data track produces unreliable synchronization in Gyroflow.

> **Status: experimental (alpha).** It has been preliminarily shown to work on the **Vivo X100 Ultra**. Compatibility is not guaranteed; verify motion synchronization before relying on converted data.

## Quick start (Windows, no Python packages)

1. Install [Python 3](https://www.python.org/downloads/) and [FFmpeg](https://ffmpeg.org/download.html), ensuring `ffmpeg` and `ffprobe` are in `PATH`.
2. Download/extract this repository or clone it. Drag a MotionCam `.mov` onto **`Convert_MOV_to_GCSV.bat`**.
3. Find the resulting `<video>_retimed.gcsv` beside your MOV.
4. Open the original video in **Gyroflow**. Under **Motion data → Open file**, select the new `.gcsv`.
5. Keep the exported log's orientation metadata rather than manually applying another axis remap. Load a lens profile captured using the *same lens, crop, resolution, and processing mode*.
6. Remove old synchronization points, run Auto sync (`rs-sync` is a useful alternative), and inspect the resulting offsets before exporting.

Neither the MOV nor its audio/video content is changed. Do not delete your originals.

## Command line

From the extracted project folder, with no Python installation step beyond Python itself:

```powershell
python .\repair_motioncam_gpmf.py "C:\path\clip.mov"
python .\repair_motioncam_gpmf.py "C:\path\clip.mov" --output "C:\path\clip_retimed.gcsv"
python .\repair_motioncam_gpmf.py "C:\path\clip.mov" --dry-run
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

This writes `clip_imu_diagnostic.json.gz` alongside the MOV. The command also works after package installation as `motioncam-gpmf-dump clip.mov`. The diagnostic preserves the original streams and batch timestamps; **no samples are dropped or interpolated in the dump**. Treat the dump as private sensor data, even though images and location fields are omitted.

If automatic alignment cannot establish compatible sensor clocks, use the diagnostic to investigate. To intentionally omit acceleration, run `python .\\repair_motioncam_gpmf.py "C:\\path\\clip.mov" --gyro-only`. This explicit fallback does not attempt to align the streams.

## How it works

1. `ffprobe` finds the embedded `gpmd` track and reads packet boundaries; `ffmpeg` extracts the compressed-container-free GPMF payload.
2. A limited GPMF parser reads sequential `GYRO` and `ACCL` batches and their scaling and `STMP` timestamps.
3. The tool checks sample counts, scale consistency, and inferred sampling rate. It treats packet timestamps as unreliable **inside** the recording and uniformly spaces samples between its first and last GYRO packet timestamps.
4. It writes Gyroflow GCSV with a fixed axis mapping and reconstructed sample timestamps, rather than preserving the original per-batch timing.

The converter prints how far per-batch timestamp positions disagree with the uniform reconstruction. These deviations are diagnostics, not proof of which software component is responsible. The reconstruction replaces variable per-packet timing with one uniform average and cannot recover missing samples.

## Compatibility and limitations

This is **not** a generic GoPro GPMF parser, a Video stabilization application, or an official MotionCam patch. Only use it when the embedded GPMF track is inconsistent and you can validate motion synchronization. Some files may work better with their original embedded gyro or with separately exported MotionCam motion data.

## Limitations and safety

- **Axis mapping is device-specific.** Other hardware orientations, ultrawide/telephoto lenses, or formats may need different mapping.
- **Uniform timing is an assumption**, not a restoration of missing timestamps. Dropped samples, varying IMU rates, and clock drift will degrade results. If sync offsets vary substantially across a clip, don't trust the output.
- When sensor counts match, the converter pairs samples by index. When they differ by exactly one, it attempts to **fit each sensor's sample clock independently and interpolate acceleration at gyro timestamps**, retaining both signals. It refuses this operation if the sample rates disagree or the time ranges do not fully overlap. Larger mismatches require inspection. Equal counts do not prove correct alignment.
- The original issue could be in metadata generation, decoding, or axis mapping. The working GCSV changes **both** timing and axes, so root cause is not conclusively isolated.
- Generated GCSV may expose device motion/location context; `.gitignore` excludes MOVs and gyro logs. Never commit actual private recordings.
- This tool does not stabilize video by itself; Gyroflow does.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI runs the synthetic tests in one Linux job when code or workflow files change; documentation-only commits skip CI. A manual run is also available. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) for development and privacy guidance.

See [troubleshooting and bug-report guidance](docs/BUG_REPORT.md) for reporting synchronization problems. This project is licensed under the [MIT License](LICENSE). MotionCam, Vivo, GoPro GPMF, and Gyroflow are referenced for interoperability; this independent project is not affiliated with or endorsed by them.