#!/usr/bin/env python3
"""Inspect MotionCam GPMF timing from an uninstalled source checkout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from motioncam_gpmf_repair.inspect import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
