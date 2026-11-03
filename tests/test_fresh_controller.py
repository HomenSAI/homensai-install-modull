"""Контроллер сохраняет порядок и не повторяет подтверждённые шаги."""
import json
from pathlib import Path

import pytest

from test_api_signed_preflight import signed_request
from test_update_assembly import assembled
from test_systemd_pointer_signed import signed_pointer
from shared import api
from shared.fresh_controller import FreshControllerError, run_portable_linux_install
from shared.preflight import NAMES
from shared.update_plan import operation_lock


@pytest.fixture
def workflow(assembled, tmp_path, monkeypatch):
    from shared.datahome import checked_path
    monkeypatch.setattr('shared.fresh_controller._unit_directory', checked_path)
    options, _ = assembled
    work = tmp_path / 'controller-work'
    work.mkdir(mode=0o700)
    directory = tmp_path / 'units'
    directory.mkdir(mode=0o700)
    return dict(package_path=options['new_package'], envelope=options['new_envelope'],
                trusted_public_key=options['new_key'], product_id=options['product_id'], version='2',
                install_root=options['install_root'], unit_dir=str(directory), work_dir=str(work),
                modes=options['modes'], required_bytes={name: 0 for name in NAMES}, reserve_bytes=0,
                workflow_key='f' * 32, keys=dict(zip(('release', 'runtime', 'publish', 'load', 'start'),
                                                     (ch * 32 for ch in '12345'))))


def test_failed_phase_is_resumed_with_same_key_without_replaying_completed_steps(workflow, monkeypatch):
    calls, fail = [], [True]
    results = {'prepare-native-release': {'status': 'prepared'}, 'prepare-linux-runtime': {'state': 'prepared'},
               'publish-portable-systemd-user-units': {'state': 'published'},
               'load-portable-systemd-user-units': {'state': 'loaded'},
               'start-portable-linux': {'state': 'started'}, 'check-local-health': {'health_response_matches': True}}
    def handle(request):
        calls.append((request['operation'], request['params'].get('idempotency_key')))
        if request['operation'] == 'load-portable-systemd-user-units' and fail.pop():
            return {'ok': False, 'error': {'code': 'systemd_load_failed'}}
        return {'ok': True, 'result': results[request['operation']]}
    monkeypatch.setattr(api, 'handle', handle)
    with pytest.raises(FreshControllerError) as error:
        run_portable_linux_install(**workflow)
    assert error.value.code == 'systemd_load_failed'
    marker = Path(workflow['work_dir']) / ('install-' + workflow['workflow_key'] + '.json')
    assert json.loads(marker.read_text())['index'] == 3
    fail.append(False)
    result = run_portable_linux_install(**workflow, language='de')
    assert result['state'] == 'recorded_complete' and result['installation_complete'] is False
    assert result['current_health_verified'] is False and result['process_identity_verified'] is False
    assert [name for name, _ in calls] == ['prepare-native-release', 'prepare-linux-runtime',
        'publish-portable-systemd-user-units', 'load-portable-systemd-user-units',
        'load-portable-systemd-user-units', 'start-portable-linux', 'check-local-health']
    assert calls[3][1] == calls[4][1] == workflow['keys']['load']
    assert run_portable_linux_install(**workflow, language='ru') == result and len(calls) == 7


def _pointer_workflow(signed_pointer, tmp_path, monkeypatch):
    from shared.datahome import checked_path
    monkeypatch.setattr('shared.fresh_controller._unit_directory', checked_path)
    _, options, build = signed_pointer
    package, envelope, _ = build('controller-source')
    work, directory = tmp_path / 'pointer-work', tmp_path / 'pointer-units'
    work.mkdir(mode=0o700)
    directory.mkdir(mode=0o700)
    return dict(package_path=str(package), envelope=envelope, trusted_public_key=options['trusted_public_key'],
                product_id=options['expected_product_id'], version='1', install_root=str(tmp_path / 'new-install'),
                unit_dir=str(directory), work_dir=str(work), modes=('autonomous',),
                required_bytes={name: 0 for name in NAMES}, reserve_bytes=0, workflow_key='f' * 32,
                keys=dict(zip(('release', 'runtime', 'publish', 'load', 'start'), (ch * 32 for ch in '12345'))))


def test_pointer_controller_resumes_without_replaying_completed_steps(signed_pointer, tmp_path, monkeypatch):
    workflow = _pointer_workflow(signed_pointer, tmp_path, monkeypatch)
    calls, fail = [], [True]
    results = {'prepare-native-release': {'status': 'prepared'}, 'prepare-linux-runtime': {'state': 'prepared'},
               'publish-pointer-systemd-user-units': {'state': 'published'},
               'load-pointer-systemd-user-units': {'state': 'loaded'},
               'start-pointer-linux': {'state': 'started'}, 'check-local-health': {'health_response_matches': True}}
    def handle(request):
        calls.append(request)
        if request['operation'] == 'load-pointer-systemd-user-units' and fail.pop():
            return {'ok': False, 'error': {'code': 'systemd_load_failed'}}
        return {'ok': True, 'result': results[request['operation']]}
    monkeypatch.setattr(api, 'handle', handle)
    with pytest.raises(FreshControllerError) as error:
        run_portable_linux_install(**workflow)
    assert error.value.code == 'systemd_load_failed'
    observed = run_portable_linux_install(**workflow, inspect_only=True)
    assert observed['current_step'] == 'load-pointer-systemd-user-units' and observed['completed_steps'] == 3
    fail.append(False)
    result = run_portable_linux_install(**workflow, language='de')
    assert result['state'] == 'recorded_complete' and not result['installation_complete']
    assert [call['operation'] for call in calls] == ['prepare-native-release', 'prepare-linux-runtime',
        'publish-pointer-systemd-user-units', 'load-pointer-systemd-user-units',
        'load-pointer-systemd-user-units', 'start-pointer-linux', 'check-local-health']
    for call in calls[2:5]:
        assert call['params']['runtime_key'] == workflow['keys']['runtime'] and call['params']['reserve_bytes'] == 0
    assert calls[3]['params']['idempotency_key'] == calls[4]['params']['idempotency_key'] == workflow['keys']['load']


@pytest.mark.parametrize('change', ['launcher', 'recipe', 'conflicting-recipe'])
def test_pointer_controller_rejects_signed_bad_recipe_before_checkpoint(signed_pointer, tmp_path, monkeypatch, change):
    from shared.systemd_pointer import LAUNCHER_PATH, RECIPE_PATH
    payload, _, _ = signed_pointer
    name = (LAUNCHER_PATH if change == 'launcher' else RECIPE_PATH if change == 'recipe'
            else 'systemd-user/portable-v1.json')
    (payload / name).write_bytes(b'synthetic tampering')
    workflow = _pointer_workflow(signed_pointer, tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(api, 'handle', lambda request: calls.append(request))
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**workflow)
    assert not calls and not list(Path(workflow['work_dir']).iterdir())


def test_controller_rejects_changed_request_corrupt_journal_and_busy_lock(workflow, monkeypatch):
    def fail(request):
        return {'ok': False, 'error': {'code': 'fresh_prepare_failed'}}
    monkeypatch.setattr(api, 'handle', fail)
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**workflow)
    marker = Path(workflow['work_dir']) / ('install-' + workflow['workflow_key'] + '.json')
    original = marker.read_bytes()
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**{**workflow, 'reserve_bytes': 1})
    assert marker.read_bytes() == original
    malformed = json.loads(original)
    malformed['index'] = True
    marker.write_text(json.dumps(malformed))
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**workflow)
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**workflow, inspect_only=True)
    marker.write_bytes(original)
    lock = Path(workflow['work_dir']) / ('install-' + workflow['workflow_key'] + '.lock')
    with operation_lock(lock):
        with pytest.raises(FreshControllerError):
            run_portable_linux_install(**workflow)
    assert marker.read_bytes() == original


def test_controller_does_not_start_with_duplicate_keys_or_overlapping_workspace(workflow):
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**{**workflow, 'keys': {**workflow['keys'], 'start': workflow['keys']['load']}})
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**{**workflow, 'work_dir': workflow['install_root']})
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**{**workflow, 'modes': ('unknown-mode',)})
    assert not list(Path(workflow['work_dir']).iterdir())


def test_previous_version_one_journal_remains_supported(workflow, monkeypatch):
    import hashlib
    import shared.fresh_controller as module
    real_sha, captured = hashlib.sha256, []
    def sha(data=b'', *args, **kwargs):
        result = real_sha(data, *args, **kwargs)
        if isinstance(data, bytes) and b'"workflow_key"' in data and b'"language"' in data:
            captured.append(result.hexdigest())
        return result
    monkeypatch.setattr(module.hashlib, 'sha256', sha)
    monkeypatch.setattr(api, 'handle', lambda request: {'ok': False, 'error': {'code': 'fresh_prepare_failed'}})
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**workflow)
    assert captured
    marker = Path(workflow['work_dir']) / ('install-' + workflow['workflow_key'] + '.json')
    state = json.loads(marker.read_text())
    state.update(version=1, binding=captured[-1])
    marker.write_text(json.dumps(state))
    with pytest.raises(FreshControllerError) as error:
        run_portable_linux_install(**workflow)
    assert error.value.code == 'fresh_prepare_failed'
    with pytest.raises(FreshControllerError) as conflict:
        run_portable_linux_install(**workflow, language='de')
    assert conflict.value.code == 'controller_failed'


def test_inspection_reads_journal_without_executing_steps(workflow, monkeypatch):
    calls = []
    def handle(request):
        calls.append(request['operation'])
        return {'ok': False, 'error': {'code': 'fresh_prepare_failed'}}
    monkeypatch.setattr(api, 'handle', handle)
    marker = Path(workflow['work_dir']) / ('install-' + workflow['workflow_key'] + '.json')
    result = run_portable_linux_install(**workflow, inspect_only=True)
    assert result['state'] == 'no_checkpoint' and result['completed_steps'] is None
    assert result['step_may_have_executed'] is None and not marker.exists() and calls == []
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**workflow)
    before, calls_before = marker.read_bytes(), list(calls)
    result = run_portable_linux_install(**workflow, inspect_only=True, language='de')
    assert result['state'] == 'pending' and result['completed_steps'] == 0
    assert result['current_step'] == 'prepare-native-release' and result['step_may_have_executed'] is True
    assert result['resume_authorized'] is False and result['effects_verified'] is False
    assert marker.read_bytes() == before and calls == calls_before
    with pytest.raises(FreshControllerError):
        run_portable_linux_install(**{**workflow, 'reserve_bytes': 1}, inspect_only=True)
