"""Remove repository documentation properties before sending a Search schema."""

import json
import sys
from pathlib import Path


def api_payload(value):
    if isinstance(value, dict):
        return {key: api_payload(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [api_payload(item) for item in value]
    return value


if __name__ == "__main__":
    print(json.dumps(api_payload(json.loads(Path(sys.argv[1]).read_text()))))
