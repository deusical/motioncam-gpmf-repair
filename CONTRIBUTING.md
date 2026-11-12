# Contributing

Contributions and reproducible reports are welcome. This is an experimental MotionCam/Gyroflow interoperability tool; contributions should avoid assuming universal device compatibility.

## Development

1. Use Python 3.10 or newer.
2. `python -m pip install -e . ruff`
3. Run `python -m ruff check .` and `python -m unittest discover -s tests -v`.
4. Add synthetic unit tests for changes to GPMF parsing, timestamp reconstruction, and axis mapping. Do not commit real-world recordings or sensor logs.

## Reporting failures

Please include device model, camera/lens, MotionCam version, recording container/codec, gyro metadata presence, FFmpeg version, Gyroflow version, and the error or diagnostic summary. If you have different IMU orientations or sync offsets, describe them, but do not publish footage with personal information.

For suspected malformed GPMF timestamps, include *synthetic or redacted* packet summaries (`STMP`, number of gyro samples, inferred sample rate), not a raw video unless you have reviewed the footage and consent to making it public.

If a security or privacy issue is involved, follow [SECURITY.md](SECURITY.md) rather than attaching private recordings to a public issue.