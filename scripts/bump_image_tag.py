#!/usr/bin/env python3
"""Select a release: bump_image_tag.py bundle.json --environment dev.

Shared mutable image tags are no longer a supported release operation.
"""
import sys

if __package__:
    from .release import main
else:
    from release import main

if __name__ == "__main__":
    sys.argv.insert(1, "select")
    main()
