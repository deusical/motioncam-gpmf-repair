#!/usr/bin/env python3
"""Correct padded HEVC display height from a source checkout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from motioncam_gpmf_repair.video import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
