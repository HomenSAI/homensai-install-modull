"""Regression настоящего JS с injected DOM; отдельно от browser acceptance."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_wizard_allows_supported_maintenance_without_install():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node недоступен: JS response contract не выполнен")
    script = Path(__file__).parent / "js/wizard_capabilities.cjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "PASS", "cases": 6,
                                         "scope": "wizard JavaScript with injected DOM/HTTP"}
