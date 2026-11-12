# One-command MotionCam preparation

The `prepare` command combines inspection, the existing experimentally
validated GPMF-to-GCSV repair, and optional explicit lossless HEVC-height
normalization. It does not change the sensor clock model, axis order, or
camera-to-IMU mapping.

## Typical workflow

Run these commands from the checked-out repository:

~~~powershell
python .\repair_motioncam_gpmf.py prepare "C:\path\clip.mov" --dry-run
python .\repair_motioncam_gpmf.py prepare "C:\path\clip.mov"
~~~

Or, after installing the Python package:

~~~powershell
motioncam-gpmf-prepare "C:\path\clip.mov"
~~~

This creates `clip_prepared` **beside** the original video. The prepared
directory contains:

- `clip_retimed.gcsv`: motion data with reconstructed timing, original axes;
- `inspection.json`: sanitized aggregate sensor, packet, and quality evidence;
- `manifest.json`: package filenames, chosen options, and quick source/project
  fingerprints (file size and modification time).

The **original source video remains at its original location** and is not
copied, changed, or removed. Do not delete it: Gyroflow still needs the video.

A quality WARNING without an existing Gyroflow project is expected. The
pipeline can prepare the motion file before optical sync is performed;
it must not label the recording optically verified just because conversion
succeeded.

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
and the previous options. It does not revalidate the recording, calculate a
cryptographic checksum, or check whether the saved output is otherwise corrupt.

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
- No support for external motion log files when the video lacks embedded
  GPMF; this is a separate planned enhancement.
- No automatic compensation for a fixed video/IMU offset, suspicious
  `STMP` origins, frame duplication or physical camera jerks.
- No direct editing of the MOV/MP4 source, and no guarantee that a recording
  passing the automated checks will stabilize correctly.

Always inspect actual stabilized footage and use Gyroflow's optical sync
before final export. The inspector only evaluates the evidence available.
