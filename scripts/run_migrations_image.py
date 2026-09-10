#!/usr/bin/env python3
"""Run migrations with the selected generation image's pinned ODBC stack."""
import argparse
import json
import pathlib
import re
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--bundle", type=pathlib.Path, required=True)
parser.add_argument("--registry", required=True)
args = parser.parse_args()
digest = json.loads(args.bundle.read_text())["images"]["generation"]["digest"]
if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    parser.error("invalid migration image digest")
root = pathlib.Path(__file__).resolve().parent.parent
subprocess.run(["docker", "run", "--rm", "--env", "MEDW_SQL_CONNECTION_STRING",
                "--mount", f"type=bind,src={root},dst=/workspace,readonly",
                f"{args.registry}/generation@{digest}", "python", "/workspace/scripts/migrate.py"],
               check=True)
