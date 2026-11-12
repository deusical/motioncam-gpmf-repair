# Contributing

Contributions and reproducible reports are welcome. MotionCam/Gyroflow interoperability varies by device; avoid assuming universal compatibility.

## Branches and pull requests

- Keep `main` as the stable integration branch. Do not push feature or fix commits directly to `main`.
- Start each change on a short-lived branch from the latest `main`, for example `fix/imu-boundaries` or `feat/video-normalization`.
- Open a pull request for review and CI. Prefer **one logical change per PR**.
- Merge using **Squash and merge** (one well-described commit on `main`). Do not use merge commits or rebase merges. Delete the feature branch after merging.
- Do not rewrite published history unless performing an explicitly agreed repository-maintenance operation.

## Development and tests

Use Python 3.10 or newer. For quick checks, run:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

On Windows PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

Optional: `python -m pip install -e . ruff` and `python -m ruff check .`.

Add synthetic unit tests for changes to GPMF parsing, timestamp reconstruction, sample alignment, or axis handling. Never commit private recordings or raw sensor logs.

## Reporting failures

Include device and lens model, MotionCam/Gyroflow/FFmpeg versions, container/codec, gyro metadata presence, the command used, and redacted diagnostics. If sharing `STMP` summaries or synthetic packets, ensure they do not expose sensitive motion/location details.

For security or privacy issues, follow [SECURITY.md](SECURITY.md).
