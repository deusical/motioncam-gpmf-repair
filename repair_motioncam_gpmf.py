#!/usr/bin/env python3
"""Run from a source checkout without installing the Python package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from motioncam_gpmf_repair.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
