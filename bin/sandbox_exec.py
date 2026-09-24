#!/usr/bin/python3
"""Apply Landlock write roots and exec the fixed worker command."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from ai_router.landlock import LandlockUnavailable, restrict_writes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--write-root', action='append', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command or not os.path.isabs(command[0]):
        print('Sandbox requires an absolute worker executable.', file=sys.stderr)
        return 3
    try:
        restrict_writes([Path(p) for p in args.write_root])
    except LandlockUnavailable as exc:
        print(f'Filesystem write confinement unavailable: {exc}', file=sys.stderr)
        return 3
    os.execvpe(command[0], command, os.environ)
    return 127


if __name__ == '__main__':
    raise SystemExit(main())
