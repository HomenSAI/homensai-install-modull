import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from homensai_install.errors import InstallerError
from homensai_install.prerequisite_runner import verify_regular_file

with tempfile.TemporaryDirectory(prefix="hinstall-ci-mode-") as temporary:
    fixture = Path(temporary) / "synthetic-python"
    shutil.copyfile(Path(sys.executable).resolve(), fixture)
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    fixture.chmod(0o777)
    try:
        verify_regular_file(str(fixture), digest)
    except InstallerError as error:
        assert error.code == "INTEGRITY_FAILED"
        assert error.context["details"]["reason"] == "prerequisite_executable_not_private_regular"
        unsafe = error.code
    else:
        raise AssertionError("world-writable fixture accepted")
    fixture.chmod(0o755)
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == digest
    assert os.stat(fixture).st_nlink == 1
    assert verify_regular_file(str(fixture), digest) == fixture
    print(json.dumps({"unsafe_mode": "0777", "unsafe_result": unsafe,
        "reason": "prerequisite_executable_not_private_regular", "private_mode": "0755",
        "private_result": "PASS", "same_bytes": True, "sha256": digest}))
