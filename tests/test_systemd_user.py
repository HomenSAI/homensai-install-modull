"""Адаптер systemd: без сетевых обращений и управления живыми службами."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

import shared.systemd_user as systemd
from shared.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ROOT / 'examples/rent.manifest.json')
UNIT = 'homensai-rent-demo-web.service'
PATH = '/run/user/1000/systemd/user/' + UNIT
DIGEST = 'a' * 64


def report(*, unit=UNIT, path=PATH, active='active', sub='running', dropins=''):
    return (f'Id={unit}\nLoadState=loaded\nActiveState={active}\nSubState={sub}\n'
            f'FragmentPath={path}\nDropInPaths={dropins}\nNeedDaemonReload=no\n')


def adapter(monkeypatch, runner):
    monkeypatch.setattr(systemd, '_trusted_file', lambda path, digest: None)
    return systemd.SystemdUserAdapter(MANIFEST, modes=('autonomous',), component='web',
                                      fragment_path=PATH, fragment_sha256=DIGEST, runner=runner)


def test_dry_run_never_invokes_process_or_reads_unit(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('dry-run must not perform I/O')
    service = adapter(monkeypatch, forbidden)
    monkeypatch.setattr(systemd, '_trusted_file', forbidden)
    assert service.command('start') == {'action': 'start', 'unit': UNIT, 'scope': 'user'}
    assert service.command('stop') == {'action': 'stop', 'unit': UNIT, 'scope': 'user'}


def test_stop_checks_identity_before_and_after(monkeypatch):
    calls = []
    def runner(argv, *, timeout, env):
        calls.append((argv, timeout, env))
        if 'show' in argv:
            return report(active='active' if len(calls) == 1 else 'inactive',
                          sub='running' if len(calls) == 1 else 'dead')
        return ''
    service = adapter(monkeypatch, runner)
    assert service.command('stop', execute=True)['active_state'] == 'inactive'
    assert [next(x for x in ('show', 'start', 'stop') if x in entry[0]) for entry in calls] == ['show', 'stop', 'show']
    assert calls[1][0] == ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password', 'stop', '--', UNIT]
    assert calls[0][2]['PATH'] == '/usr/bin:/bin' and 'HOME' not in calls[0][2]


def test_already_active_does_not_start_again(monkeypatch):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return report()
    assert adapter(monkeypatch, runner).command('start', execute=True)['active_state'] == 'active'
    assert len(calls) == 2 and all('show' in call for call in calls)


def test_missing_transient_unit_was_never_verified_and_cannot_be_claimed_stopped(monkeypatch):
    transient = '/run/user/' + str(systemd.os.getuid()) + '/systemd/transient/' + UNIT
    service = systemd.SystemdUserAdapter(MANIFEST, modes=('autonomous',), component='web',
                                         fragment_path=transient, fragment_sha256=DIGEST,
                                         runner=lambda *a, **k: pytest.fail('unverified unit must not query manager'))
    def absent(*args):
        raise OSError('not found')
    monkeypatch.setattr(systemd, '_trusted_file', absent)
    with pytest.raises(systemd.SystemdUserError):
        service.confirm_stopped()


@pytest.mark.parametrize('body', [
    report(unit='other.service'), report(path='/tmp/other'), report(dropins='/tmp/override.conf'),
    report().replace('NeedDaemonReload=no', 'NeedDaemonReload=yes'),
    report(active='active\nSECRET-SENTINEL=1'), report() + 'Id=duplicate\n',
    report().replace('LoadState=loaded', 'LoadState=not-found'), report().replace('SubState=running\n', ''),
])
def test_mismatch_never_runs_effect_or_leaks_output(monkeypatch, body):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return body
    with pytest.raises(systemd.SystemdUserError) as error:
        adapter(monkeypatch, runner).command('stop', execute=True)
    assert 'SECRET-SENTINEL' not in str(error.value)
    assert len(calls) == 1 and 'show' in calls[0]


def test_timeout_and_postcondition_fail_closed(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('SECRET-SENTINEL', 1)
    with pytest.raises(systemd.SystemdUserError) as error:
        adapter(monkeypatch, timeout).command('stop', execute=True)
    assert 'SECRET-SENTINEL' not in str(error.value)
    with pytest.raises(systemd.SystemdUserError):
        adapter(monkeypatch, lambda *a, **k: report()).command('stop', execute=True)


@pytest.mark.parametrize('change', [
    {'component': 'bad'}, {'component': '../web'}, {'fragment_path': '/tmp/../' + UNIT},
    {'fragment_path': '/tmp/other.service'}, {'fragment_sha256': 'bad'}, {'timeout': True},
])
def test_invalid_constructor_does_not_run(change):
    args = {'modes': ('autonomous',), 'component': 'web', 'fragment_path': PATH,
            'fragment_sha256': DIGEST, 'runner': lambda *a, **k: pytest.fail('no process')}
    args.update(change)
    with pytest.raises(systemd.SystemdUserError):
        systemd.SystemdUserAdapter(deepcopy(MANIFEST), **args)


@pytest.mark.parametrize('action,execute', [('restart', True), ('stop', 1), ('../stop', True), ([], True)])
def test_forbidden_actions(monkeypatch, action, execute):
    service = adapter(monkeypatch, lambda *a, **k: pytest.fail('no process'))
    with pytest.raises(systemd.SystemdUserError):
        service.command(action, execute=execute)


@pytest.mark.parametrize('code', ["print('x' * 20000)", "import time; time.sleep(10)"])
def test_runner_limits_output_and_terminates_process(monkeypatch, code):
    original = systemd.subprocess.Popen
    processes = []
    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(systemd.subprocess, 'Popen', capture)
    with pytest.raises(systemd.SystemdUserError):
        systemd._run([sys.executable, '-B', '-c', code], timeout=0.5, env={'PATH': '/usr/bin:/bin'})
    assert len(processes) == 1 and processes[0].poll() is not None
