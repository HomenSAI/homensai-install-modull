"""Registered adapter retained-source boundaries; public closed loop follows here."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from homensai_install.engine import EngineError, _digest
from homensai_install.retained_adapter import quiesce_witness, prepare_execution
from test_native_adapter_recovery import recovery_adapter, migration


def stopped_source(recovery_adapter):
    fixture, first, current, archive, document = recovery_adapter
    plan = fixture.reviewed_plan('stop', current)
    fixture.engine.execute(plan)
    token = _digest([plan['plan_id'], plan.get('plan_digest', _digest(plan))])[:32]
    ctx = fixture.engine._context(current['manifest'], fixture.target, current['options'])
    adapter, binding = fixture.engine._release_product_adapter(ctx, 'release-2')
    return fixture, ctx, adapter, 'op_' + token


def test_existing_quiesce_witness_is_read_only_and_exactly_bound(recovery_adapter, monkeypatch):
    fixture, ctx, adapter, operation_id = stopped_source(recovery_adapter)
    path = adapter.state_dir / 'adapter-invocations.sqlite3'
    before = path.read_bytes()
    def no_hook(*args, **kwargs): pytest.fail('witness read must not invoke a product hook')
    monkeypatch.setattr(adapter, 'invoke', no_hook)
    witness = quiesce_witness(adapter, installation_id=fixture.engine._owner(ctx)['installation_id'],
                              operation_id=operation_id, data_home=fixture.root / 'data')
    assert witness['operation_id'] == operation_id and witness['idempotency_key'] == 'quiesce_' + operation_id[3:]
    assert witness['result'] == {'quiesced': True, 'active_writers': 0}
    assert witness['ledger_identity'] == [path.stat().st_dev, path.stat().st_ino, path.stat().st_uid]
    assert path.read_bytes() == before


@pytest.mark.parametrize('fault', ['missing', 'unknown', 'digest'])
def test_unavailable_authoritative_quiesce_never_creates_or_repeats(recovery_adapter, monkeypatch, fault):
    fixture, ctx, adapter, operation_id = stopped_source(recovery_adapter)
    path = adapter.state_dir / 'adapter-invocations.sqlite3'
    if fault == 'missing': path.unlink()
    else:
        with sqlite3.connect(path) as db:
            key = adapter._identity('quiesce_' + operation_id[3:])
            if fault == 'unknown': db.execute("UPDATE calls SET state='UNKNOWN' WHERE call_id=?", (key,))
            else: db.execute('UPDATE calls SET request_digest=? WHERE call_id=?', ('0' * 64, key))
    monkeypatch.setattr(adapter, 'invoke', lambda *a, **k: pytest.fail('no hook on unverified witness'))
    with pytest.raises(EngineError) as caught:
        quiesce_witness(adapter, installation_id=fixture.engine._owner(ctx)['installation_id'],
                        operation_id=operation_id, data_home=fixture.root / 'data')
    assert caught.value.code == 'RECOVERY_REQUIRED'
    if fault == 'missing': assert not path.exists()


def retained_adapter(recovery_adapter):
    fixture, ctx, adapter, operation_id = stopped_source(recovery_adapter)
    root = fixture.engine._recovery_copy(ctx, 'release-2')
    meta = fixture.engine._metadata(ctx, 'release-2')
    options = {**ctx['options'], 'release': 'release-2', 'payload_dir': str(root / 'payload'),
               'runtime_executable': meta['runtime_executable'], 'inventory_file': str(root / 'release.inventory.json'),
               'signature_file': str(root / 'release.signature.json')}
    retained, binding = fixture.engine._product_adapter({**ctx, 'release': 'release-2', 'options': options})
    return fixture, retained


def test_retained_construction_is_read_only_and_execution_permission_is_explicit(recovery_adapter, monkeypatch):
    fixture, adapter = retained_adapter(recovery_adapter)
    executable = Path(adapter.options['payload_dir']) / adapter._descriptor['command'][0]
    assert stat.S_IMODE(executable.stat().st_mode) == 0o600
    original = hashlib.sha256(executable.read_bytes()).hexdigest()
    monkeypatch.setattr(adapter, 'invoke', lambda *a, **k: pytest.fail('permission preparation must not run hooks'))
    adapter.capabilities()
    assert stat.S_IMODE(executable.stat().st_mode) == 0o600
    prepare_execution(adapter)
    assert stat.S_IMODE(executable.stat().st_mode) == 0o700
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == original


def test_runtime_swap_does_not_chmod_an_unrelated_target(recovery_adapter, monkeypatch):
    fixture, adapter = retained_adapter(recovery_adapter)
    executable = Path(adapter.options['payload_dir']) / adapter._descriptor['command'][0]
    unrelated = fixture.base / 'unrelated-runtime'; unrelated.write_bytes(b'synthetic unrelated bytes'); unrelated.chmod(0o640)
    real = os.open; fired = []
    def swapped(path, flags, *args, **kwargs):
        if path == executable.name and kwargs.get('dir_fd') is not None and not flags & os.O_DIRECTORY and not fired:
            fired.append(True)
            executable.rename(executable.with_name(executable.name + '.preserved'))
            executable.symlink_to(unrelated)
        return real(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swapped)
    with pytest.raises(EngineError): prepare_execution(adapter)
    assert fired and unrelated.read_bytes() == b'synthetic unrelated bytes'
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o640


@pytest.mark.parametrize('target', ['different', 'same'])
def test_registered_public_uninstall_then_restore_uses_retained_source_and_fresh_safety(recovery_adapter, tmp_path, monkeypatch, target):
    import base64
    from homensai_install.api.v1 import InstallerAPI
    fixture, first, second, snapshot, document = recovery_adapter
    if target == 'same':
        snapshot = fixture.engine.execute(fixture.reviewed_plan('backup', second))['archive']['path']
    expected_release = 'release-2' if target == 'same' else 'release-1'
    expected_document = document.read_text() if target == 'same' else 'snapshot data'
    trust = tmp_path / 'trust.json'
    trust.write_text(json.dumps({'schema_version': 1, 'keys': {'synthetic': base64.b64encode(fixture.public_key).decode()}}))
    def client():
        return InstallerAPI.from_bootstrap(tmp_path / 'public-state', trust_config=trust,
            allow_development=True, development_runtime=__import__('sys').executable,
            product_adapter_descriptor='installer/adapter.json', secret_provider=fixture.engine.secret_provider)
    def execute(api, action, **extra):
        plan = api.plan(action, second['manifest'], fixture.target, options={**second['options'], **extra})
        result = api.submit(plan['plan_id'], plan['plan_digest'], [plan['review_consent_ref'], *plan['required_consents']],
                            idempotency_key=action, background=False)
        assert result['status'] == 'SUCCEEDED', result.get('error')
        return plan, result
    api = client()
    _, removed = execute(api, 'uninstall')
    proof = removed['result']['retained_recovery']
    assert proof['source_adapter_binding']['release'] == 'release-2'
    assert proof['quiesce_witness']['result'] == {'quiesced': True, 'active_writers': 0}
    assert not any((fixture.root / 'releases').iterdir())
    document.write_text('post-uninstall latest data')
    import shutil
    shutil.rmtree(second['options']['payload_dir'])
    shutil.rmtree(first['options']['payload_dir'])
    from homensai_install.adapters import ExecutableProductAdapter
    actual_execute = ExecutableProductAdapter._execute
    observed = []
    def observe(adapter, hook, request, request_digest):
        if hook in {'quiesce', 'restore', 'post_restore', 'readiness'}:
            observed.append((hook, Path(adapter.options['payload_dir'])))
        return actual_execute(adapter, hook, request, request_digest)
    monkeypatch.setattr(ExecutableProductAdapter, '_execute', observe)
    plan, restored = execute(client(), 'restore', archive_path=snapshot, acknowledge_data_loss=True)
    assert plan['engine_plan']['retained_source']['receipt_id'] == proof['receipt_id']
    fixture.current = second if target == 'same' else first
    assert restored['result']['health']['release'] == expected_release
    assert restored['result']['before_recovery']['from_release'] == 'release-2'
    assert document.read_text() == expected_document
    token = _digest([plan['plan_id'], plan['plan_digest']])[:32]
    expected_source = fixture.root.parent / '.installed-recovery' / (token + '-retained-adapter-source') / 'payload'
    expected_target = fixture.root / 'releases' / ('app-' + expected_release)
    assert [path for hook, path in observed if hook == 'quiesce'] == [expected_source]
    for hook in ('restore', 'post_restore', 'readiness'):
        assert [path for observed_hook, path in observed if observed_hook == hook] == [expected_target]
    assert (fixture.root / '.hinstall/product-adapter/release-2/adapter-invocations.sqlite3').is_file()
    owner = json.loads((fixture.root / '.hinstall/installation.json').read_text())
    assert owner['installed'] is True and 'retained_uninstall' not in owner
    safety = restored['result']['before_recovery_archive']['path']
    from shared import archive_crypto
    oracle = tmp_path / 'safety-oracle'
    oracle.mkdir()
    archive_crypto.restore_archive(safety, oracle, password_provider=lambda: 'synthetic-archive-passphrase',
                                   expected_product_id=first['manifest']['product_id'])
    assert (oracle / 'roots/data/document.txt').read_text() == 'post-uninstall latest data'
    assert api.capabilities(fixture.target)['actions']['repair']['supported'] is False
    assert api.capabilities(fixture.target)['actions']['reinstall']['supported'] is False


def fresh_quiesce_request(fixture, adapter):
    return {'operation_id': 'op_' + 'f' * 32, 'idempotency_key': 'quiesce_' + 'f' * 32,
            'phase': 'execute', 'data': {'installation_id': json.loads((fixture.root / '.hinstall/installation.json').read_text())['installation_id'],
                'release': adapter.options['release'], 'data_home': str(fixture.root / 'data')}}


def test_new_quiesce_key_uses_existing_database_without_requiring_an_old_row(recovery_adapter):
    fixture, ctx, adapter, old_operation_id = stopped_source(recovery_adapter)
    path = adapter.state_dir / 'adapter-invocations.sqlite3'
    identity = (path.stat().st_dev, path.stat().st_ino)
    request = fresh_quiesce_request(fixture, adapter)
    result = adapter.invoke('quiesce', request, require_existing_ledger=True, expected_ledger_identity=identity)
    assert result['result'] == {'quiesced': True, 'active_writers': 0} and result['replayed'] is False
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT operation_id,state FROM calls WHERE call_id=?', (adapter._identity(request['idempotency_key']),)).fetchone()
    assert row == (request['operation_id'], 'SUCCEEDED')


@pytest.mark.parametrize('fault', ['missing', 'replaced'])
def test_new_quiesce_does_not_create_or_trust_a_database_lost_after_witness(recovery_adapter, monkeypatch, fault):
    from homensai_install.errors import InstallerError
    fixture, ctx, adapter, old_operation_id = stopped_source(recovery_adapter)
    witness = quiesce_witness(adapter, installation_id=fixture.engine._owner(ctx)['installation_id'],
        operation_id=old_operation_id, data_home=fixture.root / 'data')
    path = adapter.state_dir / 'adapter-invocations.sqlite3'
    saved = path.with_name('saved-original.sqlite3'); path.rename(saved)
    if fault == 'replaced': path.write_bytes(saved.read_bytes()); path.chmod(0o600)
    monkeypatch.setattr(adapter, '_execute', lambda *args, **kwargs: pytest.fail('unverified database must not authorize a hook'))
    with pytest.raises(InstallerError):
        adapter.invoke('quiesce', fresh_quiesce_request(fixture, adapter),
            require_existing_ledger=True, expected_ledger_identity=witness['ledger_identity'][:2])
    if fault == 'missing': assert not path.exists()
    else:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            assert db.execute('SELECT COUNT(*) FROM calls WHERE operation_id=?', ('op_' + 'f' * 32,)).fetchone() == (0,)
