# Upstream investigation: MotionCam MOV GPMF gyro sync

## Reproduction (one confirmed recording)

- Device: Vivo X100 Ultra, main camera (~24 mm equivalent)
- Video: MotionCam 3840×2160 30 fps HEVC MOV, OIS/EIS disabled
- Stabilizer: Gyroflow 1.7.0, custom lens profile
- Embedded GPMF sensor log: ~25,033 GYRO and ACCL samples in ~60 seconds (~417.7 Hz)
- Embedded sync guesses differed substantially: approximately −905 to +644 ms, resulting in poor stabilization
- Reconstructed external GCSV, with **both** continuous sample timing and validated axis remapping, gave consistent offsets of approximately −7 to −11 ms and successful stabilization

## GPMF timeline observation

One packet with `STMP=68,713 µs` contains 26 gyro samples. The following packet advertises `STMP=102,225 µs`, an advance of ~33.5 ms, while 26 samples at the inferred 417.7 Hz rate occupy ~62 ms. Sequential sensor values remain continuous. This is inconsistent with interpreting each `STMP` as the first sample timestamp for each non-overlapping sample batch.

## Unknowns

- Whether the MOV muxer emits inconsistent GPMF STMP timestamps, or Gyroflow's parser interprets MotionCam's metadata differently than intended
- Whether the timestamp issue **alone** is sufficient to break stabilization (the working reconstruction also remaps axes)
- Whether this happens on other devices, builds, or video modes

## Useful attachments for a bug report

- Original short MOV retaining its `gpmd` data track, with nonprivate subject matter
- Exact MotionCam build number, Gyroflow version, lens/crop settings
- Matching corrected `.gcsv` and before/after sync-point screenshots
- FFprobe stream/packet details and sample-count discrepancy

Never attach private footage, locations, or identifying information without inspecting it first.

Reference: [GoPro GPMF common header](https://github.com/gopro/gpmf-parser/blob/main/GPMF_common.h).
