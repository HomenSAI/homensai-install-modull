"""Read-only сведения о Python executable, используемом runner fixture."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def main() -> int:
    path = Path(sys.executable).resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    result = {
        "schema_version": 1,
        "python_version": sys.version.split()[0],
        "executable_name": path.name,
        "regular_file": stat.S_ISREG(before.st_mode),
        "mode_octal": oct(stat.S_IMODE(before.st_mode)),
        "group_or_world_writable": bool(before.st_mode & 0o022),
        "hardlink_count": before.st_nlink,
        "size_bytes": before.st_size,
        "sha256": digest.hexdigest(),
        "unchanged_during_read": (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["unchanged_during_read"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
