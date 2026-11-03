"""Тестовый продукт вызывает публичный CLI установщика, без реальных установок."""
import json
import os
from pathlib import Path
import subprocess
import time
import tomllib
from types import SimpleNamespace

import pytest

from examples.demo_product.installer_client import InstallerClient, InstallerClientError
import homensai_installer_client

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_client_and_legacy_import_share_one_versioned_implementation():
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert homensai_installer_client.__version__ == config['project']['version']
    assert homensai_installer_client.InstallerClient is InstallerClient
    assert homensai_installer_client.InstallerClientError is InstallerClientError


def test_real_client_negotiates_and_validates_product_manifest():
    client = InstallerClient(timeout=30)
    capabilities = client.capabilities()
    assert capabilities['api_major'] == 1 and capabilities['api_minor'] >= 10
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text())
    result = client.call('validate', {'manifest': manifest}, min_minor=0, language='de')
    assert result == {'product_id': manifest['product_id'], 'schema_version': 2}
    with pytest.raises(InstallerClientError) as exc:
        client.call('validate', {'manifest': {}}, language='en')
    assert exc.value.code == 'invalid_request'
    assert str(exc.value) == 'Manifest does not meet the contract'


def test_client_uses_explicit_installer_root_from_independent_product(tmp_path, monkeypatch):
    # A product may contain its own package named shared; it must not shadow the installer.
    impostor = tmp_path / 'shared'
    impostor.mkdir()
    (impostor / '__init__.py').write_text('', encoding='utf-8')
    marker = tmp_path / 'wrong-api-executed'
    (impostor / 'api.py').write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("bad")\n',
                                     encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    client = InstallerClient(api_root=str(ROOT), timeout=30)
    assert client.capabilities()['api_major'] == 1
    # The source-tree default must also avoid the product's current directory.
    assert InstallerClient(timeout=30).capabilities()['api_major'] == 1
    assert not marker.exists()


def test_client_rejects_invalid_installer_root(tmp_path):
    for invalid in ('relative/path', str(tmp_path / 'missing'), 42):
        with pytest.raises(ValueError):
            InstallerClient(api_root=invalid)


def test_real_client_can_preview_update_without_creating_installation(tmp_path):
    client = InstallerClient(timeout=30)
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text())
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'installation'), 'release': '2',
              'modes': ['autonomous'], 'migration_ids': ['schema-2'], 'guard_effects': True}
    result = client.call('plan-update', params, min_minor=25, language='de')
    assert result['forward'][:5] == ['stop', 'archive', 'stage', 'backup:0', 'migrate:0']
    assert result['rollback'][0] == 'rollback_stop'
    assert result['execution_supported'] is False and result['readiness_verified'] is False
    assert not Path(params['install_root']).exists()


def test_real_client_can_inspect_interrupted_update_without_resuming(tmp_path):
    from shared.update_plan import UpdatePlan
    client = InstallerClient(timeout=30)
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text())
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'installation'), 'release': '2',
              'modes': ['autonomous'], 'migration_ids': ['schema-2'], 'guard_effects': True}
    plan = UpdatePlan(manifest, Path(params['install_root']), object(), release='2',
                      modes=('autonomous',), migration_ids=('schema-2',), guard_effects=True)
    plan.state_path.parent.mkdir(parents=True)
    state = {'version': 1, 'transaction': 'b' * 32, 'plan': plan.digest, 'old_release': '1',
             'release': '2', 'direction': 'forward', 'index': plan.forward.index('archive'),
             'failed_phase': None, 'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan._save(state)
    result = client.call('inspect-update-recovery', params, min_minor=26, language='de')
    assert result['operation_key'] == 'b' * 32 + '/archive'
    assert result['archive_binding_review_candidate'] is True
    assert result['effects_verified'] is False and result['resume_authorized'] is False
    assert plan.status() == state and not plan.lock_path.exists()


def test_missing_capability_stops_before_action():
    client = InstallerClient(timeout=30)
    with pytest.raises(InstallerClientError) as exc:
        client.call('future-action', {}, min_minor=999)
    assert exc.value.code == 'incompatible_api'


def test_client_does_not_pass_secrets_or_use_shell(monkeypatch):
    monkeypatch.setenv('SYNTHETIC_SECRET', 'sensitive')
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        request = json.loads(kwargs['input'])
        result = {'api_major': 1, 'api_minor': 10, 'operations': ['validate'], 'languages': ['en']}
        if request['operation'] == 'validate':
            result = {'product_id': 'demo'}
        return SimpleNamespace(returncode=0, stdout=json.dumps({'api_major': 1, 'request_id': request['request_id'],
                                                                 'ok': True, 'result': result}).encode(), stderr=b'')
    monkeypatch.setattr('homensai_installer_client.client._run_bounded', run)
    assert InstallerClient().call('validate', {'manifest': {}}, language='en') == {'product_id': 'demo'}
    assert len(seen) == 2
    for argv, kwargs in seen:
        assert argv[1:] == ['-B', '-m', 'shared.api']
        assert 'SYNTHETIC_SECRET' not in kwargs['env']
        assert b'sensitive' not in kwargs['input']


@pytest.mark.parametrize('operation', ['import-update-archive', 'run-pointer-linux-update'])
def test_client_passes_only_explicit_archive_secret_to_operation(monkeypatch, operation):
    monkeypatch.setenv('HOMENSAI_SECRET_BACKUP', 'synthetic-password')
    monkeypatch.setenv('UNRELATED_SECRET', 'must-not-pass')
    seen = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input'])
        seen.append((request, kwargs['env']))
        result = ({'api_major': 1, 'api_minor': 60, 'operations': [operation], 'languages': ['en']}
                  if request['operation'] == 'capabilities' else {'archive_imported': True})
        body = {'api_major': 1, 'request_id': request['request_id'], 'ok': True, 'result': result}
        return SimpleNamespace(returncode=0, stdout=json.dumps(body).encode(), stderr=b'')
    monkeypatch.setattr('homensai_installer_client.client._run_bounded', run)
    client = InstallerClient()
    result = client.call(operation, {'password_env': 'HOMENSAI_SECRET_BACKUP'},
                         min_minor=27, secret_env_name='HOMENSAI_SECRET_BACKUP')
    assert result['archive_imported'] is True and len(seen) == 2
    assert 'HOMENSAI_SECRET_BACKUP' not in seen[0][1]
    assert seen[1][1]['HOMENSAI_SECRET_BACKUP'] == 'synthetic-password'
    assert all('UNRELATED_SECRET' not in env for _, env in seen)
    assert all('synthetic-password' not in json.dumps(request) for request, _ in seen)
    for invalid in ('PATH', 'PYTHONPATH', 'HOMENSAI_SECRET_'):
        with pytest.raises(ValueError):
            client.call(operation, {'password_env': invalid}, secret_env_name=invalid)
    with pytest.raises(ValueError):
        client.call(operation, {'password_env': 'HOMENSAI_SECRET_OTHER'},
                    secret_env_name='HOMENSAI_SECRET_BACKUP')
    with pytest.raises(ValueError):
        client.call('validate', {'password_env': 'HOMENSAI_SECRET_BACKUP'},
                    secret_env_name='HOMENSAI_SECRET_BACKUP')
    monkeypatch.delenv('HOMENSAI_SECRET_BACKUP')
    with pytest.raises(InstallerClientError) as error:
        client.call(operation, {'password_env': 'HOMENSAI_SECRET_BACKUP'},
                    secret_env_name='HOMENSAI_SECRET_BACKUP')
    assert error.value.code == 'secret_unavailable' and len(seen) == 3


@pytest.mark.parametrize('response', [b'{}', b'{"api_major":1,"api_major":1}', b'bad', b'x' * (1024 * 1024 + 1)])
def test_invalid_response_does_not_disclose_stderr(monkeypatch, response):
    monkeypatch.setattr('homensai_installer_client.client._run_bounded',
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=response, stderr=b'SECRET-SENTINEL'))
    with pytest.raises(InstallerClientError) as exc:
        InstallerClient().capabilities()
    assert exc.value.code == 'transport_error' and 'SECRET-SENTINEL' not in str(exc.value)


def test_timeout_does_not_retry_or_disclose_error(monkeypatch):
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise subprocess.TimeoutExpired('SECRET-SENTINEL', 1)
    monkeypatch.setattr('homensai_installer_client.client._run_bounded', fail)
    with pytest.raises(InstallerClientError) as exc:
        InstallerClient().call('prepare-native-release', {})
    assert exc.value.code == 'transport_error' and 'SECRET-SENTINEL' not in str(exc.value)
    assert len(calls) == 1


def test_uncertain_mutating_call_is_not_retried(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        request = json.loads(kwargs['input'])
        calls.append(request['operation'])
        if request['operation'] == 'capabilities':
            response = {'api_major': 1, 'request_id': request['request_id'], 'ok': True,
                        'result': {'api_major': 1, 'api_minor': 10,
                                   'operations': ['prepare-native-release'], 'languages': ['en']}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(response).encode(), stderr=b'')
        raise subprocess.TimeoutExpired('synthetic', 1)
    monkeypatch.setattr('homensai_installer_client.client._run_bounded', run)
    with pytest.raises(InstallerClientError) as exc:
        InstallerClient().call('prepare-native-release', {'idempotency_key': 'a' * 32}, min_minor=8)
    assert exc.value.code == 'transport_error'
    assert calls == ['capabilities', 'prepare-native-release']


def test_wrong_response_request_id_is_rejected(monkeypatch):
    response = {'api_major': 1, 'request_id': 'unrelated', 'ok': True, 'result': {}}
    monkeypatch.setattr('homensai_installer_client.client._run_bounded',
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(response).encode(), stderr=b''))
    with pytest.raises(InstallerClientError) as exc:
        InstallerClient().capabilities()
    assert exc.value.code == 'transport_error'


@pytest.mark.skipif(os.name != 'posix', reason='Проверка использует исполняемый POSIX-скрипт')
def test_client_stops_oversized_output_during_read(tmp_path):
    executable = tmp_path / 'fake-python'
    executable.write_text('#!/usr/bin/python3\nimport os, time\nos.read(0, 8192)\n'
                          'while True:\n os.write(1, b"X" * 8192)\n', encoding='utf-8')
    executable.chmod(0o700)
    started = time.monotonic()
    with pytest.raises(InstallerClientError) as error:
        InstallerClient(python_executable=str(executable), timeout=15).capabilities()
    assert error.value.code == 'transport_error' and time.monotonic() - started < 20


@pytest.mark.skipif(os.name != 'posix', reason='Проверка использует исполняемый POSIX-скрипт')
def test_client_discards_large_stderr_without_blocking(tmp_path):
    executable = tmp_path / 'fake-python'
    executable.write_text(
        '#!/usr/bin/python3\nimport json, os, sys\nrequest=json.load(sys.stdin)\n'
        'for _ in range(1024): os.write(2, b"SECRET" * 1024)\n'
        'print(json.dumps({"api_major":1,"request_id":request["request_id"],"ok":True,'
        '"result":{"api_major":1,"api_minor":1,"operations":[],"languages":[]}}))\n',
        encoding='utf-8')
    executable.chmod(0o700)
    assert InstallerClient(python_executable=str(executable), timeout=10).capabilities()['api_minor'] == 1


@pytest.mark.skipif(os.name != 'posix', reason='Группа процессов POSIX')
def test_client_stops_descendant_holding_output_after_parent_exits(tmp_path):
    executable, marker = tmp_path / 'fake-python', tmp_path / 'late-write'
    executable.write_text(
        '#!/usr/bin/python3\nimport json, os, sys, time\n'
        'request = json.load(sys.stdin)\n'
        'if os.fork() == 0:\n time.sleep(2.5)\n open(' + repr(str(marker)) + ', "w").write("late")\n os._exit(0)\n'
        'print(json.dumps({"api_major": 1, "request_id": request["request_id"], '
        '"ok": True, "result": {"api_major": 1, "api_minor": 1, "operations": [], "languages": []}}))\n',
        encoding='utf-8')
    executable.chmod(0o700)
    with pytest.raises(InstallerClientError) as error:
        InstallerClient(python_executable=str(executable), timeout=10).capabilities()
    assert error.value.code == 'transport_error'
    time.sleep(3)
    assert not marker.exists()


@pytest.mark.skipif(os.name != 'posix', reason='Группа процессов POSIX')
def test_real_timeout_stops_child_before_late_side_effect(tmp_path):
    executable, marker = tmp_path / 'fake-python', tmp_path / 'late-write'
    executable.write_text('#!/usr/bin/python3\nimport os, time\nos.read(0, 8192)\n'
                          'time.sleep(2)\nopen(' + repr(str(marker)) + ', "w").write("late")\n',
                          encoding='utf-8')
    executable.chmod(0o700)
    with pytest.raises(InstallerClientError) as error:
        InstallerClient(python_executable=str(executable), timeout=0.2).capabilities()
    assert error.value.code == 'transport_error'
    time.sleep(2.5)
    assert not marker.exists()
