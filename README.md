# MotionCam GPMF Repair

**Experimental tool for repairing MotionCam's embedded gyro timestamp timeline for Gyroflow.**

A MotionCam MOV from a Vivo X100 Ultra contained valid gyro/accelerometer samples but apparently inconsistent GPMF packet timestamps. Gyroflow 1.7.0 generated incompatible sync offsets from the embedded track. Reconstructing a uniformly timed sensor log **and remapping its axes** restored consistent synchronization and stabilization.

> **Status: alpha, device-specific workaround.** Validated on **one** Vivo X100 Ultra, main camera (~24 mm equivalent), 4K/30 MotionCam HEVC MOV. Not a general-purpose GPMF repair or a proven fix for other phones. Always verify generated logs against the video.

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

## How it works

1. `ffprobe` finds the embedded `gpmd` track and reads packet boundaries; `ffmpeg` extracts the compressed-container-free GPMF payload.
2. A limited GPMF parser reads sequential `GYRO` and `ACCL` batches and their scaling and `STMP` timestamps.
3. The tool checks sample counts, scale consistency, and inferred sampling rate. It treats packet timestamps as unreliable **inside** the recording and uniformly spaces samples between its first and last GYRO packet timestamps.
4. It writes Gyroflow GCSV with the experimentally validated Vivo X100 Ultra axis transform, **not** the original per-batch timestamp layout.

The converter prints how far per-batch timestamp positions disagree with the uniform reconstruction. These deviations are diagnostics, not proof of which software component is responsible. The reconstruction replaces variable per-packet timing with one uniform average and cannot recover missing samples.

## Compatibility and limitations

This is **not** a generic GoPro GPMF parser, a Video stabilization application, or an official MotionCam patch. Only use it when the embedded GPMF track is inconsistent and you can validate motion synchronization. Some files may work better with their original embedded gyro or with separately exported MotionCam motion data.

## Limitations and safety

- **Axis mapping is device-specific.** Other hardware orientations, ultrawide/telephoto lenses, or formats may need different mapping.
- **Uniform timing is an assumption**, not a restoration of missing timestamps. Dropped samples, varying IMU rates, and clock drift will degrade results. If sync offsets vary substantially across a clip, don't trust the output.
- It pairs GYRO/ACCL samples by index only after checking counts; if data are missing but totals happen to match, problems may still be undetected.
- The original issue could be in metadata generation, decoding, or axis mapping. The working GCSV changes **both** timing and axes, so root cause is not conclusively isolated.
- Generated GCSV may expose device motion/location context; `.gitignore` excludes MOVs and gyro logs. Never commit actual private recordings.
- This tool does not stabilize video by itself; Gyroflow does.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI runs synthetic fixtures on Linux and Windows. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) for development and privacy guidance. The end-to-end test on the user's original MOV is deliberately **not** included because it contains private footage.

See [upstream bug-report notes](docs/BUG_REPORT.md) for the observed metadata discrepancy and reproduction details. This project is licensed under the [MIT License](LICENSE). MotionCam, Vivo, GoPro GPMF, and Gyroflow are referenced for interoperability; this independent project is not affiliated with or endorsed by them.