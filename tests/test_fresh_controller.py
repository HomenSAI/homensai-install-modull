"""Контроллер сохраняет порядок и не повторяет подтверждённые шаги."""
import json
from pathlib import Path

import pytest

from test_api_signed_preflight import signed_request
from test_update_assembly import assembled
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
    result = run_portable_linux_install(**workflow)
    assert result['state'] == 'recorded_complete' and result['installation_complete'] is False
    assert result['current_health_verified'] is False and result['process_identity_verified'] is False
    assert [name for name, _ in calls] == ['prepare-native-release', 'prepare-linux-runtime',
        'publish-portable-systemd-user-units', 'load-portable-systemd-user-units',
        'load-portable-systemd-user-units', 'start-portable-linux', 'check-local-health']
    assert calls[3][1] == calls[4][1] == workflow['keys']['load']
    assert run_portable_linux_install(**workflow) == result and len(calls) == 7


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
