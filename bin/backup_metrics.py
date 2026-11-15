#!/usr/bin/env python3
"""Emit flat numeric metrics for backup collection (one JSON line per cycle)."""
import sys

from backup_common import discover, emit, metrics_payload


def main():
    try:
        emit(metrics_payload(discover()))
    except Exception:
        emit({"status": "alarm"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
