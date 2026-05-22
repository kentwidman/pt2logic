#!/usr/bin/env python3
"""
Usage: python convert.py <session.ptf> <output.aaf> [--verbose]

Converts a Pro Tools session file (.ptf for PT7-8, .ptx for PT10+) to an AAF
file that can be imported into Logic Pro X via File → Import → AAF.

Preserves: track names, clip timeline positions, audio file references.
Does not transfer: effects, automation, pan, volume.
"""

import sys
import os
import argparse

from ptx_parser import parse, PTXError, UnsupportedVersionError, ParseError
import aaf_writer


def main():
    parser = argparse.ArgumentParser(
        description="Convert Pro Tools session to AAF for Logic Pro import"
    )
    parser.add_argument("input", help="Pro Tools session file (.ptf for PT7-8, .ptx for PT10+)")
    parser.add_argument("output", help="Output AAF file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all warnings")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    warnings = []

    try:
        print(f"Parsing {args.input} ...")
        session = parse(args.input, warnings=warnings)
    except UnsupportedVersionError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Supported versions: Pro Tools 5-12 (.ptf and .ptx)", file=sys.stderr)
        sys.exit(1)
    except ParseError as e:
        print(f"Error parsing session: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  PT version : {session.version}")
    print(f"  Sample rate: {session.sample_rate} Hz")
    print(f"  Audio files: {len(session.audio_files)}")
    print(f"  Regions    : {len(session.regions)}")
    print(f"  Placements : {len(session.placements)}")

    tracks = {}
    for p in session.placements:
        tracks.setdefault(p.track_name, 0)
        tracks[p.track_name] += 1
    if tracks:
        print(f"  Tracks     : {len(tracks)}")
        for name, count in sorted(tracks.items(), key=lambda x: x[0]):
            print(f"    {name} ({count} clip{'s' if count != 1 else ''})")

    try:
        print(f"\nWriting {args.output} ...")
        aaf_writer.write(session, args.output, warnings=warnings)
    except Exception as e:
        print(f"Error writing AAF: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    if warnings:
        if args.verbose:
            print(f"\n{len(warnings)} warning(s):")
            for w in warnings:
                print(f"  ! {w}")
        else:
            print(f"\n{len(warnings)} warning(s) — use --verbose to see details")

    print(f"\nDone. Import into Logic Pro X:")
    print(f"  File → Import → AAF → {args.output}")


if __name__ == "__main__":
    main()
