"""Проверить test fixture при writable копии исходного Python в приватном temp."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sys
import tempfile

import pytest


def main() -> int:
    original = Path(sys.executable).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="hinstall-unsafe-source-") as temporary:
        source = Path(temporary) / "synthetic-python"
        shutil.copyfile(original, source)
        source.chmod(0o777)  # Только новая копия внутри принадлежащего нам temp.
        assert source.stat().st_nlink == 1 and source.stat().st_mode & 0o022
        assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(original.read_bytes()).digest()
        previous = sys.executable
        project_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, project_root)
        sys.executable = str(source)
        try:
            return pytest.main([
                "-q", "-p", "no:cacheprovider",
                "tests/test_prerequisites_runner.py::test_real_exact_argv_has_no_shell_or_parent_secrets",
                "tests/test_prerequisites_runner.py::test_timeout_returns_unknown_without_claiming_or_killing_system_installer",
                "tests/test_prerequisites_execution.py::test_real_fixed_runner_is_reached_only_after_signed_pins_and_consent",
                "tests/test_prerequisites_runner.py::test_unsafe_executable_is_rejected_before_use",
            ])
        finally:
            sys.executable = previous
            sys.path.remove(project_root)


if __name__ == "__main__":
    raise SystemExit(main())
