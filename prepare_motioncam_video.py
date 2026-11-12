#!/usr/bin/env python3
"""Prepare a MotionCam recording from a source checkout without installation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from motioncam_gpmf_repair.prepare import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
