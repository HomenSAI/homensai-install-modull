"""Actual prerequisite wizard JS against controlled DOM/HTTP; not a browser."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_prerequisite_wizard_response_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node недоступен: prerequisite JS response contract NOT RUN")
    script = Path(__file__).parent / "js/wizard_prerequisites.cjs"
    # Original CP018 also exceeded its old 20 s JS watchdog on the current host.
    # Keep a bounded outer timeout above the JS watchdog, without skipping cases.
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=150, check=False)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["status"] == "PASS"
    assert evidence["cases"] >= 39
    assert evidence["scope"] == "actual wizard JavaScript with injected DOM/HTTP; not browser acceptance"
    assert set(evidence["assets_sha256"]) == {"app.js", "index.html", "locales.json"}
    assert all(len(value) == 64 for value in evidence["assets_sha256"].values())
