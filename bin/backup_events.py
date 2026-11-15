#!/usr/bin/env python3
"""Emit one JSON event per cycle with backup-system health (restic/borg/timers)."""
import sys

from backup_common import discover, emit


def main():
    try:
        emit(discover())
    except Exception:
        emit({"status": "alarm", "note": "collection error"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
