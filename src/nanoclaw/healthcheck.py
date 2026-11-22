"""Exec probe for the controller: healthy while the main loop keeps touching its heartbeat file."""

import sys
import time
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/heartbeat")
    max_age = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    try:
        age = time.time() - int(path.read_text().strip())
    except (OSError, ValueError):
        return 1
    return 0 if age <= max_age else 1


if __name__ == "__main__":
    sys.exit(main())
