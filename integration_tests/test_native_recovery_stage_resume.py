"""Original public operation adopts only a verified completed target stage."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys

import pytest

from homensai_install.adapters import ExecutableProductAdapter
from homensai_install.api.model import digest
from homensai_install.api.v1 import InstallerAPI
from homensai_install.errors import InstallerError
from homensai_install.recovery_replacement import RootReplacement
from shared.adapters_fs import FilesystemPorts
from test_native_adapter_uninstall_boundary import case, effect_rows, interrupted_operation
from test_native_migration import migration
from test_native_recovery_root_resume_boundary import managed_state, tree_state


@pytest.mark.parametrize('action', ['restore', 'rollback'])
def test_completed_target_publication_lost_ack_resumes_without_any_republication(case, monkeypatch, action):
    case.run('stop', case.second, 'stop-before-stage-controller')
    review = case.plan(action, case.second, archive_path=case.archive['path'], acknowledge_data_loss=True)
    token = digest([review['plan_id'], review['plan_digest']])[:32]
    journal = case.fixture.root / '.hinstall/operations' / (token + '.json')
    child = os.fork()
    if child == 0:
        RootReplacement.stage_complete = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
        try: case.submit(review, 'completed-stage-cut')
        except BaseException: os._exit(86)
        os._exit(87)
    assert os.waitstatus_to_exitcode(os.waitpid(child, 0)[1]) == -signal.SIGKILL
    state = json.loads(journal.read_text())
    assert state['recovery_roots_complete'] is True and state['recovery_target_stage_started'] is True
    assert state.get('recovery_target_stage_complete') is not True
    assert 'restore' not in state['completed']
    target = case.fixture.root / 'releases/app-release-1'
    metadata = case.fixture.root / '.hinstall/releases/release-1.json'
    metadata_before = metadata.read_bytes(), metadata.stat().st_ino, metadata.stat().st_ctime_ns
    files_before = {p.relative_to(target).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino, p.stat().st_ctime_ns)
                    for p in target.rglob('*') if p.is_file()}
    data_before = {p.relative_to(case.fixture.root / 'data').as_posix(): p.stat().st_ino
                   for p in (case.fixture.root / 'data').rglob('*') if p.is_file()}
    archives = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in case.fixture.base.rglob('*.hsac')}
    source_ledger = case.fixture.root / '.hinstall/product-adapter/release-2/adapter-invocations.sqlite3'
    ledger_before = source_ledger.stat().st_dev, source_ledger.stat().st_ino
    actual_execute = ExecutableProductAdapter._execute
    def execute(adapter, hook, request, request_digest):
        assert adapter.options['release'] != 'release-2', 'No source hook/status on restored roots'
        if hook == 'restore':
            assert {p.relative_to(case.fixture.root / 'data').as_posix(): p.stat().st_ino
                    for p in (case.fixture.root / 'data').rglob('*') if p.is_file()} == data_before
        return actual_execute(adapter, hook, request, request_digest)
    api = case.new_api(); operation = interrupted_operation(api, review)
    with monkeypatch.context() as guarded:
        guarded.setattr(ExecutableProductAdapter, '_execute', execute)
        guarded.setattr(FilesystemPorts, '_reconcile', lambda *a, **k: pytest.fail('No root reconciliation after completed stage'))
        guarded.setattr(FilesystemPorts, 'stage_release', lambda *a, **k: pytest.fail('No republish after completed stage'))
        result = api.resume(operation['operation_id'], background=False)
    assert result['status'] == 'SUCCEEDED', result.get('error')
    assert result['operation_id'] == operation['operation_id']
    assert metadata_before == (metadata.read_bytes(), metadata.stat().st_ino, metadata.stat().st_ctime_ns)
    assert files_before == {p.relative_to(target).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino, p.stat().st_ctime_ns)
                            for p in target.rglob('*') if p.is_file()}
    assert (source_ledger.stat().st_dev, source_ledger.stat().st_ino) == ledger_before
    assert archives == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in case.fixture.base.rglob('*.hsac')}
    assert len(effect_rows(case.database, 'restore')) == len(effect_rows(case.database, 'post_restore')) == 1
    case.fixture.current = case.first
    case.run('stop', case.first, 'stop-after-stage-resume')


def test_surviving_stage_hint_fences_lost_root_and_native_history_before_prepare(case, monkeypatch, tmp_path):
    case.run('stop', case.second, 'stop-before-orphan-stage-controller')
    review = case.plan('restore', case.second, archive_path=case.archive['path'])
    token = digest([review['plan_id'], review['plan_digest']])[:32]
    child = os.fork()
    if child == 0:
        RootReplacement.stage_complete = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
        try: case.submit(review, 'orphan-stage-cut')
        except BaseException: os._exit(86)
        os._exit(87)
    assert os.waitstatus_to_exitcode(os.waitpid(child, 0)[1]) == -signal.SIGKILL
    root = case.fixture.root
    owner = root / '.hinstall/installation.json'
    value = json.loads(owner.read_text()); value.pop('pending_recovery')
    owner.write_text(json.dumps(value)); owner.chmod(0o600)
    journal = root / '.hinstall/operations' / (token + '.json'); journal.unlink()
    root_receipt = root / '.hinstall/recovery-root-receipts' / (token + '.json'); root_receipt.unlink()
    stage = root / '.hinstall/recovery-stage-receipts' / (token + '.json')
    assert stage.is_file()
    (root / 'config').chmod(0o755)
    before = managed_state(root), tree_state(owner), tree_state(stage)
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', lambda *a, **k: pytest.fail('Orphan authority cannot authorize hooks'))
    api = InstallerAPI.from_bootstrap(tmp_path / 'orphan-legacy-state', trust_config=tmp_path / 'trust.json',
        allow_development=True, development_runtime=sys.executable, secret_provider=case.fixture.engine.secret_provider)
    with pytest.raises(InstallerError) as caught:
        api.plan('restore', case.second['manifest'], case.fixture.target,
            options={**case.second['options'], 'archive_path': case.archive['path']})
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert (managed_state(root), tree_state(owner), tree_state(stage)) == before
    assert not journal.exists() and not root_receipt.exists()
