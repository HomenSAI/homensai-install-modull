"""Реальная Native установка не получает API journal внутри своих данных."""
import hashlib

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from test_native_adapter_recovery import recovery_adapter, migration


def data_snapshot(root):
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.stat()
        result[path.relative_to(root).as_posix()] = (
            info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_mtime_ns, info.st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return result


def test_registered_restore_plan_refuses_api_journal_in_existing_data(recovery_adapter):
    fixture, first, current, archive, document = recovery_adapter
    data = fixture.root / "data"
    api = InstallerAPI(data / "private-api-state", engine=fixture.engine)
    before = data_snapshot(data)
    with pytest.raises(InstallerError) as caught:
        api.plan("restore", current["manifest"], fixture.target,
                 options={**current["options"], "archive_path": archive,
                          "acknowledge_data_loss": True})
    assert caught.value.code == "INVALID_REQUEST"
    assert caught.value.to_dict()["details"] == {
        "field_path": "state_dir", "reason": "state_directory_overlaps_managed_files"}
    assert data_snapshot(data) == before
    assert document.read_text() == "latest data"
    assert not api.state_dir.exists()
