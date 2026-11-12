#!/usr/bin/env python3
"""Dump gyro + accelerometer samples from a MOV without installing the package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from motioncam_gpmf_repair.diagnostics import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
