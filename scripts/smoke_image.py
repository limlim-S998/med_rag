#!/usr/bin/env python3
"""Check that a built service starts and exposes honest process probes."""
from __future__ import annotations

import argparse
import subprocess
import time
import uuid


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--backend", choices=("local", "azure"), default="local")
    args = parser.parse_args()
    name = "medw-smoke-" + uuid.uuid4().hex[:12]
    # Local adapters avoid any Azure calls. Reranker's held-back real path is
    # selected independently and must remain unready.
    subprocess.run(["docker", "run", "--detach", "--name", name,
                    "--env", f"MEDW_BACKEND={args.backend}", "--env", "MEDW_ENV=local",
                    "--env", f"MEDW_SERVICE_NAME={args.service}",
                    args.image], check=True, capture_output=True)
    probe = """import urllib.request, urllib.error, sys
try:
    r=urllib.request.urlopen('http://127.0.0.1:8000'+sys.argv[1], timeout=2)
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
"""
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = subprocess.run(["docker", "exec", name, "python", "-c", probe, "/healthz"],
                                    capture_output=True, text=True, check=False)
            if result.returncode == 0 and result.stdout.strip() == "200":
                break
            running = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", name],
                                     capture_output=True, text=True, check=True)
            if running.stdout.strip() != "true":
                raise RuntimeError("service exited before becoming live")
            time.sleep(0.5)
        else:
            raise RuntimeError("liveness deadline exceeded")
        result = subprocess.run(["docker", "exec", name, "python", "-c", probe, "/readyz"],
                                capture_output=True, text=True, check=True)
        expected = "503" if args.service in ("gateway", "retrieval") else "200"
        if args.backend == "azure":
            expected = "503"
        allowed = {expected}
        if result.stdout.strip() not in allowed:
            raise RuntimeError(f"unexpected readiness: {result.stdout.strip()}")
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=True)
        if "dependency initialization failed" in logs.stdout + logs.stderr:
            raise RuntimeError("dependency initialization failed inside a live process")
        subprocess.run(["docker", "exec", name, "pip", "check"], check=True)
        print(f"{args.service}: health=200 ready={result.stdout.strip()}")
    except Exception:
        subprocess.run(["docker", "logs", "--tail", "50", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)


if __name__ == "__main__":
    main()
