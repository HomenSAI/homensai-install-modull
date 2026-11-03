"""Pointer-selected command resolution with synthetic files, no system services."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from shared.portable_launcher import LauncherError, resolve_command
from shared.update_plan import _atomic_write

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux launcher')
SCRIPT = Path(__file__).resolve().parents[1] / 'shared/portable_launcher.py'


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / 'installation with spaces'
    for name in ('app', 'data', 'runtime', 'releases', 'services'):
        (root / name).mkdir(parents=True, mode=0o700)
    for version in ('1', '2'):
        release = root / 'releases' / ('app-' + version)
        release.mkdir(mode=0o700)
        executable = release / 'run.py'
        executable.write_text('#!/usr/bin/python3\nimport json, sys\nprint(json.dumps(sys.argv))\n')
        executable.chmod(0o700)
    pointer = root / 'services/active.json'
    pointer.write_text('{"release":"1"}')
    pointer.chmod(0o600)
    params = dict(root=str(root), app='app', data='data', runtime='runtime', releases='releases',
                  pointer='services/active.json', command=('@release/run.py', '@data', 'literal $; value'))
    return root, pointer, params


def cli(params):
    arguments = []
    for name in ('root', 'app', 'data', 'runtime', 'releases', 'pointer'):
        arguments.extend(('--' + name, params[name]))
    return subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), *arguments, '--', *params['command']],
                          capture_output=True, text=True, timeout=10)


def test_real_subprocess_follows_pointer_and_preserves_arguments(installation):
    root, pointer, params = installation
    for version in ('1', '2', '1'):
        _atomic_write(pointer, {'release': version})
        expected = resolve_command(**params)
        result = cli(params)
        assert result.returncode == 0 and json.loads(result.stdout) == list(expected)
        assert expected[0] == str(root / 'releases' / ('app-' + version) / 'run.py')
        assert expected[2] == 'literal $; value'


def test_release_subdirectory_is_available_as_an_argument(installation):
    root, _, params = installation
    (root / 'releases/app-1/assets').mkdir(mode=0o700)
    params['command'] = ('@release/run.py', '@release/assets')
    assert resolve_command(**params)[1] == str(root / 'releases/app-1/assets')


@pytest.mark.parametrize('value', ['{"release":"../outside"}', '{"release":"1","release":"2"}',
                                   '{"release":true}', '{"release":"missing"}', '{}', 'x' * 4097])
def test_invalid_pointer_never_starts_product(installation, value):
    _, pointer, params = installation
    pointer.write_text(value)
    with pytest.raises((LauncherError, OSError, ValueError)):
        resolve_command(**params)
    result = cli(params)
    assert result.returncode == 2 and not result.stdout
    assert value not in result.stderr


@pytest.mark.parametrize('change', ['pointer-symlink', 'release-symlink', 'executable-symlink',
                                    'writable-pointer', 'traversal', 'external-executable', 'overlap'])
def test_unsafe_paths_are_rejected(installation, tmp_path, change):
    root, pointer, params = installation
    if change == 'pointer-symlink':
        pointer.unlink()
        pointer.symlink_to(tmp_path / 'foreign')
    elif change == 'release-symlink':
        path = root / 'releases/app-1'
        (path / 'run.py').unlink()
        path.rmdir()
        path.symlink_to(root / 'releases/app-2', target_is_directory=True)
    elif change == 'executable-symlink':
        executable = root / 'releases/app-1/run.py'
        executable.unlink()
        executable.symlink_to(root / 'releases/app-2/run.py')
    elif change == 'writable-pointer':
        pointer.chmod(0o666)
    elif change == 'traversal':
        params['command'] = ('@release/../app-2/run.py',)
    elif change == 'external-executable':
        params['command'] = ('/usr/bin/python3',)
    else:
        params['runtime'] = 'releases'
    result = cli(params)
    assert result.returncode == 2 and not result.stdout


def test_pointer_change_during_resolution_is_rejected(installation, monkeypatch):
    from shared import portable_launcher as launcher
    _, pointer, params = installation
    original = launcher._pointer
    calls = []
    def change(path):
        result = original(path)
        calls.append(result)
        if len(calls) == 1:
            pointer.write_text('{"release":"2"}')
        return result
    monkeypatch.setattr(launcher, '_pointer', change)
    with pytest.raises(LauncherError):
        resolve_command(**params)
