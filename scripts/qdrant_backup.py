#!/usr/bin/env python3
"""Run the same backup implementation mounted by the Qdrant Helm chart."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] /
                      "deploy/charts/qdrant/files/qdrant_backup.py"), run_name="__main__")
