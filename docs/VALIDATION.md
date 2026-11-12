# Recording validation: what the checks actually establish

Run \`python repair_motioncam_gpmf.py inspect clip.mov --project clip.gyroflow\`
or inspect an exported \`*_imu_diagnostic.json.gz\` without the video file.

The inspector emits a **screening result**, not a calibrated sensor-fusion
ground truth. It never shifts timestamps, guesses orientation, or edits the
video. The \`--json\` form contains only aggregate evidence, selected Gyroflow
settings and sync offsets; review it before sharing.

## Automated check: PASS / WARNING / FAIL

- **PASS** means the parsed gyro and accelerometer clocks satisfy the
  converter's established rate, count, and endpoint rules; a corresponding
  Gyroflow project has at least **three** optical sync points spanning at
  least **30 seconds**, and those offsets vary by no more than **one quarter
  of a video frame**. This is a *provisional consistency heuristic*, not an
  independently calibrated quality guarantee.
- **WARNING** means optical validation is missing/insufficient/inconsistent,
  a sensor check requires review, or metadata-packet association is
  incomplete. Many viable recordings will legitimately have this status.
- **FAIL** means parsing/timestamp checks found a blocker for the embedded
  IMU reconstruction. A missing embedded GPMF track is shown as unavailable,
  not proof that an external sensor log cannot be used.

**Even PASS requires visual inspection of stabilization**, particularly during
rapid motion. Neither the application nor a static report can prove the
camera-to-IMU axis orientation, absence of individual missing samples, or
correct lens calibration.

The 25%-of-a-frame threshold and minimum optical sample span are documented
**screening heuristics** intended for future validation against more real
recordings. They should not be interpreted as a scientific confidence level.
The tool reports raw sync offsets and a linear trend so they remain auditable.

## Three distinct kinds of timestamps

| Label | What it refers to | Safe inference |
| --- | --- | --- |
| GPMF \`STMP\` | A sensor stream's batch metadata clock | Relative timing evidence, subject to producer interpretation |
| Metadata-packet PTS | When the media container presents a GPMF packet | A packet-level schedule; **not** necessarily sensor capture time |
| Video-packet PTS | When the video frame should be presented | Frame presentation cadence; **not** a guaranteed exposure timestamp |

The inspector summarizes \`metadata packet PTS - STMP\` for each sensor,
including the median and 5th–95th percentile spread. When the actual
MOV/MP4 is provided, it separately checks **sorted video-packet PTS
spacing** and compares metadata-packet PTS to the nearest video-packet
PTS. A close packet match means the container timelines are aligned, **not
that the motion samples are synchronized with optical footage**.

Large first-batch-anchored \`STMP\` residuals can result from a systematic
difference between the first batch timestamp and the fitted intercept.
The separately fitted residual helps identify this pattern, but cannot
establish which timestamp representation is physically correct. There
is deliberately **no automatic fixed-frame correction**.

Gyroflow's optical synchronization offsets remain the primary independent
check of motion-to-image timing. Comparing offset changes across a recording
can suggest drift, but a few noisy points are not proof of drift.

## Privacy and limitations

The \`inspect\` command does not write raw sensor samples, embedded project
quaternion caches, GPS data, video frames, or absolute source paths into its
summary. It may include recording filenames and Gyroflow sync parameters.
The original diagnostic dump is more sensitive; do not post it publicly
without reviewing it. Video packet inspection requires FFprobe but never
re-encodes or decodes frames.

Without the original video, the inspector can still compare **metadata PTS
to \`STMP\`** using a previously exported IMU diagnostic. It cannot check
video-packet cadence or metadata-to-video packet PTS agreement unless the
MOV/MP4 itself is inspected.
