"""Backward-compatible entry point for the packaged leakage demonstration."""
from __future__ import annotations

import sys
from pathlib import Path

# Keep direct execution from a source checkout working without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from noalpha.demos.leakage import main


if __name__ == "__main__":
    raise SystemExit(main())
