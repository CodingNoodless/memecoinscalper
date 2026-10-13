"""Filesystem anchors. Runtime state, logs and .env live in the project root,
one level above this package, so moving code never moves the data."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
