"""Ensure repository root is on sys.path for both pytest.exe and python -m pytest."""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[3])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
