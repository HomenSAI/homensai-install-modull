"""Read-only witness must refuse SQLite recovery/WAL modes before opening SQL."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from homensai_install.engine import EngineError
from homensai_install.retained_adapter import quiesce_witness


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux owned FD witness contract')


@pytest.fixture
def witness_database(tmp_path):
    folder = tmp_path / 'private-ledger'; folder.mkdir(mode=0o700)
    path = folder / 'adapter-invocations.sqlite3'
    operation_id = 'op_' + 'a' * 32
    adapter = SimpleNamespace(state_dir=folder, options={'release': 'release-1'},
        _request=lambda hook, request: ({}, 'b' * 64), _identity=lambda key: key)
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE calls(call_id TEXT PRIMARY KEY,request_digest TEXT,operation_id TEXT,hook TEXT,state TEXT,result TEXT)')
        for prefix in ('quiesce_', 'cleanup_quiesce_'):
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?)', (prefix + 'a' * 32, 'b' * 64, operation_id,
                'quiesce', 'SUCCEEDED', json.dumps({'quiesced': True, 'active_writers': 0})))
        db.commit()
    path.chmod(0o600)
    return adapter, operation_id, path


def read(adapter, operation_id, **extra):
    return quiesce_witness(adapter, installation_id='c' * 32, operation_id=operation_id,
        data_home=adapter.state_dir.parent / 'data', **extra)


@pytest.mark.parametrize('cleanup', [False, True])
def test_readonly_normal_or_cleanup_witness_preserves_ledger_bytes_and_namespace(witness_database, cleanup):
    adapter, operation_id, path = witness_database
    before = path.read_bytes(); names = sorted(p.name for p in path.parent.iterdir())
    extra = {'idempotency_key': 'cleanup_quiesce_' + 'a' * 32} if cleanup else {}
    result = read(adapter, operation_id, **extra)
    assert result['result'] == {'quiesced': True, 'active_writers': 0}
    assert result['idempotency_key'].startswith('cleanup_' if cleanup else 'quiesce_')
    assert path.read_bytes() == before and sorted(p.name for p in path.parent.iterdir()) == names


def test_wal_header_is_refused_without_creating_sqlite_sidecars(witness_database):
    adapter, operation_id, path = witness_database
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    assert path.read_bytes()[18:20] == b'\x02\x02'
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    with pytest.raises(EngineError) as caught: read(adapter, operation_id)
    assert caught.value.code == 'RECOVERY_REQUIRED'
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_existing_sqlite_recovery_sidecar_refuses_before_sql_open(witness_database, monkeypatch, suffix):
    adapter, operation_id, path = witness_database
    sidecar = path.with_name(path.name + suffix); sidecar.write_bytes(b'synthetic unverified sidecar')
    before = path.read_bytes()
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: pytest.fail('no SQLite open with unverified sidecar'))
    with pytest.raises(EngineError) as caught: read(adapter, operation_id)
    assert caught.value.code == 'RECOVERY_REQUIRED' and path.read_bytes() == before
    assert sidecar.read_bytes() == b'synthetic unverified sidecar'
