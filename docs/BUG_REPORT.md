# Troubleshooting and bug reports

This page explains how to investigate MotionCam GPMF synchronization problems and prepare a useful report without sharing private recordings.

## When to use this tool

Gyroflow may have difficulty aligning video motion with embedded sensor data. This converter reconstructs a uniform sample timeline and writes a GCSV motion log for testing as an alternative to the embedded track.

Possible symptoms include inconsistent synchronization offsets, unstable compensation, or a mismatch between visible camera movement and gyroscope traces. These symptoms are not sufficient to identify the underlying cause.

## Basic checks

1. Confirm that the MOV contains an embedded `gpmd` data stream and can be opened in Gyroflow.
2. Check the correct lens profile, sensor orientation, and synchronization method.
3. Run the converter and load the resulting GCSV as the motion-data source.
4. Recalculate synchronization points, compare the result against the original, and verify stabilization visually.
5. If synchronization remains inconsistent, do not assume the reconstructed timeline is accurate.

## What to include in a report

- Device and camera/lens model, operating system, MotionCam and Gyroflow versions
- Container format, recording mode, and FFmpeg version
- Whether the MOV includes a `gpmd` track
- The command used, any errors, and the converter's diagnostic summary
- Whether the original and reconstructed motion data behave differently
- Synthetic or redacted GPMF packet summaries, if relevant, including `STMP` and sensor sample counts

Do **not** post private videos, raw gyro logs, identifying locations, or unreviewed metadata in public issues. When practical, create a short, non-sensitive reproduction clip.

## Scope of investigation

Unexpected results may originate from metadata generation, timestamp interpretation, dropped samples, variable sampling rates, coordinate mapping, or synchronization settings. The converter modifies both timing and coordinate mapping; an improved result does not by itself isolate the cause.

Reference: [GoPro GPMF common header](https://github.com/gopro/gpmf-parser/blob/main/GPMF_common.h).
