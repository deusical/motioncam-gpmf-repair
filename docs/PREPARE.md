# One-command MotionCam preparation

The `prepare` command combines recording inspection, a selected motion-data
path, and optional explicit lossless HEVC-height normalization. Direct-log
recordings with embedded GPMF use the established GPMF-to-GCSV reconstruction.
Rendered RAW recordings use an original Gyroflow GCSV sidecar, without changing
any existing samples, timestamps, scales or axis metadata. The tool never
infers a camera-to-IMU mapping.

## Typical workflow

Run these commands from the checked-out repository:

~~~powershell
python .\repair_motioncam_gpmf.py prepare "C:\path\clip.mov" --dry-run
python .\repair_motioncam_gpmf.py prepare "C:\path\clip.mov"
# If the MOV is a RAW render with an adjacent matching GCSV, auto-discover it:
python .\repair_motioncam_gpmf.py prepare "C:\path\rendered_RAW.mov"
# Or explicitly select a GCSV:
python .\repair_motioncam_gpmf.py prepare "C:\path\rendered_RAW.mov" --motion "C:\path\original.gcsv"
~~~

Or, after installing the Python package:

~~~powershell
motioncam-gpmf-prepare "C:\path\clip.mov"
~~~

This creates `clip_prepared` **beside** the original video. The prepared
directory contains:

- `clip_retimed.gcsv` for direct-log recordings: reconstructed embedded
  motion timing, original sensor axes;
- `clip_motion.gcsv` for RAW recordings: an exact byte-for-byte copy of
  the original GCSV sidecar; **no retiming or gyro/accel interpolation**;
- `inspection.json`: sanitized aggregate video and selected motion source
  evidence, explicit validation limitations, and warnings;
- `manifest.json`: package filenames, chosen options, quick video/project
  fingerprints (size+mtime), and for RAW GCSV the sidecar's SHA-256 digest.

The manifest format is v2; existing v1 direct-log packages can still be
reused or overwritten safely.

The **original source video remains at its original location** and is not
copied, changed, or removed. Do not delete it: Gyroflow still needs the video.

A quality WARNING without an existing Gyroflow project is expected. The
pipeline can prepare the motion file before optical sync is performed;
it must not label the recording optically verified just because conversion
succeeded.

## How motion input is selected

1. If `--motion FILE.gcsv` is supplied, **that sidecar is used** even if
   the video also contains embedded GPMF; its original bytes are preserved.
2. Otherwise, if the video contains usable embedded GPMF, use the established
   reconstruction path.
3. Otherwise, if the video lacks embedded GPMF, look for one adjacent GCSV
   with the exact video stem or a stem prefix ending at an underscore.
   For example `clip_24mm.gcsv` matches the RAW render
   `clip_24mm_BT.709_BT709.mov`.
4. Multiple matching sidecars are **ambiguous**: specify `--motion`.
   Neither embedded GPMF nor a sidecar: fail with a useful error.

Auto-discovery excludes known derived `_retimed.gcsv` files. It does not
read motion from a video or reconstruct it when none exists. If you choose a
RAW GCSV, `--gyro-only` and `--imu-orientation` are refused, because they
would contradict byte-for-byte preservation. Set orientation in Gyroflow.

The RAW GCSV parser checks headers, positive numeric scales, sample columns,
finite values, strictly increasing timestamps, and large timestamp gaps.
MotionCam's trailing empty CSV column is explicitly supported. It reports
a median sample rate in units determined by `tscale`; no sample is discarded
or resampled. The original motion metadata may include device timestamps;
only aggregate measurements are put in the report, never raw sensor arrays.

A RAW sidecar may span more time than a rendered MOV (for instance if the RAW
was trimmed during rendering). Duration discrepancies are **warnings, not
instructions to stretch timestamps**; Gyroflow optical sync is still required.
The GCSV's original header is preserved, including any `videofilename` field,
which may refer to a different video name.

## Optional operations

~~~powershell
# Only when the expected display height is independently known
python .\repair_motioncam_gpmf.py prepare clip.mov --target-height 2160

# Alternative explicit target aspect
python .\repair_motioncam_gpmf.py prepare clip.mov --aspect 16:9

# Provide existing Gyroflow synchronization results
python .\repair_motioncam_gpmf.py prepare clip.mov --project clip.gyroflow

# Explicit, manually verified IMU axis mapping only
python .\repair_motioncam_gpmf.py prepare clip.mov --imu-orientation yXz

# Only when accelerometer alignment is unusable and gyro-only output is desired
python .\repair_motioncam_gpmf.py prepare clip.mov --gyro-only

# Choose a different folder
python .\repair_motioncam_gpmf.py prepare clip.mov --output-dir "D:\Processed\clip_prepared"
~~~

Any requested crop is a **new copy** produced without re-encoding HEVC,
using conformance-window metadata. No dimensions, aspect, padding position,
or lens profile are inferred. The video may contain proprietary container
metadata that FFmpeg does not preserve, so keep your source. If you use the
normalized copy in Gyroflow, select the generated GCSV manually: its
`videofilename` field still refers to the original MOV.

## Rerunning safely

By default, the command refuses an existing prepared folder. To avoid
unnecessary work, use:

~~~powershell
python .\repair_motioncam_gpmf.py prepare clip.mov --reuse
~~~

Reuse is **explicit**. It verifies the generated filenames exist and that the
original source and optional Gyroflow project match quick size/mtime fingerprints
and the previous options. When RAW GCSV is used, the sidecar also must match
its saved SHA-256 checksum. Reuse does not revalidate the motion stream,
optical synchronization or whether the prepared output has been corrupted.

To regenerate an existing package:

~~~powershell
python .\repair_motioncam_gpmf.py prepare clip.mov --overwrite
~~~

The command only overwrites an intact generated folder with a recognized
manifest and no extra user-created files. All new work is staged in a
temporary sibling folder before switching the package into place. If
conversion or normalization fails, the previous package remains untouched.
There is no option to overwrite an arbitrary folder. Use a different
`--output-dir` if the existing folder has other files.

`--dry-run` runs metadata inspection and compatibility checks without
creating or modifying a prepared folder. It does not mean optical
synchronization has been independently verified.

## What this does *not* do

- No Gyroflow automatic lens selection, IMU orientation detection, optical
  synchronization, project generation, or stabilized rendering.
- RAW GCSV sidecars are supported. Other external motion formats are not.
- No automatic compensation for a fixed video/IMU offset, suspicious
  `STMP` origins, frame duplication or physical camera jerks.
- No direct editing of the MOV/MP4 source, and no guarantee that a recording
  passing the automated checks will stabilize correctly.

Always inspect actual stabilized footage and use Gyroflow's optical sync
before final export. The inspector only evaluates the evidence available.
