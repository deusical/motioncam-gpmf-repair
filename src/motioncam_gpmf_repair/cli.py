"""Command-line interface for the experimental MotionCam GPMF repair."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .core import RepairError, repair


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="motioncam-gpmf-repair",
        description="Experimental: reconstruct embedded MotionCam GPMF gyro timing for Gyroflow.",
    )
    parser.add_argument("files", nargs="+", type=Path, help="MotionCam MOV video(s) containing a gpmd data track")
    parser.add_argument("-o", "--output", type=Path, help="Output path (only valid with one input)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing GCSV")
    parser.add_argument("--dry-run", action="store_true", help="Inspect metadata without generating GCSV")
    parser.add_argument("--gyro-only", action="store_true",
                        help="Explicitly discard accelerometer stream (e.g. if sample counts mismatch)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.output and len(args.files) != 1:
        parser.error("--output requires exactly one input video")

    failures = 0
    for file in args.files:
        try:
            result = repair(file, args.output, overwrite=args.overwrite, dry_run=args.dry_run,
                            gyro_only=args.gyro_only)
        except (RepairError, OSError, ValueError) as exc:
            print(f"ERROR: {file}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"\n{result.source}")
        print(f"  Gyro batches: {result.gyro_batches}; accel batches: {result.accel_batches}")
        print(f"  Samples: gyro={result.samples}; accel={result.accel_samples}; inferred rate: {result.rate_hz:.3f} Hz")
        if args.gyro_only:
            print("  WARNING: --gyro-only selected; accelerometer stream deliberately omitted.")
        print(f"  Packet timestamp deviations from uniform fit: median {result.timestamp_mismatch_median_ms:.2f} ms; max {result.timestamp_mismatch_max_ms:.2f} ms")
        if result.output:
            print(f"  Wrote: {result.output}")
        else:
            print("  Dry run: no file written")
        print("  Verify sync offsets in Gyroflow; this mapping is experimental.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
