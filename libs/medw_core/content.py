"""Content identities shared by packaging and runtime verification."""

import hashlib
from pathlib import Path


def prompt_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(directory.rglob("*.md"))
    if not files:
        raise ValueError(f"no prompt files found in {directory}")
    for path in files:
        name = path.relative_to(directory).as_posix().encode()
        body = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return "sha256:" + digest.hexdigest()
