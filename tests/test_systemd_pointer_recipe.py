"""Experimental pointer-based unit rendering and standalone execution."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from shared.manifest import ManifestError, load_manifest
from shared.services import render_pointer_user_services_v2
from shared.update_plan import _atomic_write

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def manifest():
    value = load_manifest(ROOT / 'examples/rent.manifest.json')
    value['paths'] = {name: name for name in value['paths']}
    value['secrets'] = []
    value['components'] = [value['components'][0]]
    value['components'][0]['cmd'] = ['@release/run.py', '@data', 'literal argument']
    value['modes'] = [{'name': 'autonomous', 'components': ['web']}]
    value['mutual_exclusion'] = []
    return value


def test_generator_does_not_pin_version_or_mutate_manifest(manifest):
    before = deepcopy(manifest)
    units = render_pointer_user_services_v2(manifest, modes=('autonomous',), install_root='/home/demo/root')
    text = next(iter(units.values()))
    assert '/home/demo/root/runtime/lib/homensai/portable_launcher.py' in text
    assert '"@release/run.py"' in text and 'app-1' not in text and 'app-2' not in text
    assert '"services/active.json"' in text and manifest == before


def test_generator_escapes_systemd_specifiers_and_environment(manifest):
    manifest['components'][0]['cmd'].append('literal%$value')
    units = render_pointer_user_services_v2(manifest, modes=('autonomous',), install_root='/home/demo/root % $')
    text = next(iter(units.values()))
    assert 'literal%%$$value' in text and '/home/demo/root %% $$' in text


@pytest.mark.skipif(not sys.platform.startswith('linux') or not Path('/usr/bin/python3').exists(),
                    reason='Требуется Linux с /usr/bin/python3')
def test_rendered_command_runs_current_release_and_rollback(manifest, tmp_path):
    root = tmp_path / 'root with spaces'
    for name in ('app', 'data', 'runtime/bin', 'runtime/lib/homensai', 'releases', 'services'):
        (root / name).mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.copy2(Path('/usr/bin/python3').resolve(), root / 'runtime/bin/python')
    shutil.copy2(ROOT / 'shared/portable_launcher.py', root / 'runtime/lib/homensai/portable_launcher.py')
    for version in ('1', '2'):
        release = root / 'releases' / ('app-' + version)
        release.mkdir(mode=0o700)
        script = release / 'run.py'
        script.write_text('#!/usr/bin/python3\nimport json, sys\nprint(json.dumps(sys.argv))\n')
        script.chmod(0o700)
    pointer = root / 'services/active.json'
    units = render_pointer_user_services_v2(manifest, modes=('autonomous',), install_root=str(root))
    text = next(iter(units.values()))
    command = next(line[len('ExecStart='):] for line in text.splitlines() if line.startswith('ExecStart='))
    args = shlex.split(command)
    for version in ('1', '2', '1'):
        _atomic_write(pointer, {'release': version})
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [str(root / 'releases' / ('app-' + version) / 'run.py'),
                                             str(root / 'data'), 'literal argument']


@pytest.mark.parametrize('change', ['unsupported-secret', 'invalid-release', 'unsafe-services', 'external-executable'])
def test_generator_rejects_unsupported_manifest_inputs(manifest, change):
    if change == 'unsupported-secret':
        manifest['secrets'] = [{'name': 'TOKEN', 'required': True, 'contexts': ['service'], 'same_value': False}]
    elif change == 'invalid-release':
        manifest['components'][0]['cmd'] = ['@release/../escape']
    elif change == 'unsafe-services':
        manifest['paths']['services'] = '/outside'
    else:
        manifest['components'][0]['cmd'] = ['/usr/bin/python3']
    with pytest.raises(ManifestError):
        render_pointer_user_services_v2(manifest, modes=('autonomous',), install_root='/home/demo/root')


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Нужен одноразовый контейнер с user-systemd')
def test_pointer_units_control_real_systemd_in_isolated_container(manifest, tmp_path):
    import pwd
    user = pwd.getpwuid(os.getuid())
    if (os.getuid() == 0 or user.pw_name != 'homenstest' or user.pw_dir != '/home/homenstest'
            or not Path('/run/.containerenv').is_file() or not tmp_path.is_relative_to(Path(user.pw_dir))):
        pytest.skip('Защитные условия изолированного теста не выполнены')
    root = tmp_path / 'pointer installation'
    for name in ('app', 'data', 'runtime/bin', 'runtime/lib/homensai', 'releases', 'services'):
        (root / name).mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.copy2(Path(sys.executable).resolve(), root / 'runtime/bin/python')
    shutil.copy2(ROOT / 'shared/portable_launcher.py', root / 'runtime/lib/homensai/portable_launcher.py')
    # The container Python may depend on its original shared library layout;
    # use a symlink-free interpreter supplied by the test image.
    manifest['product_id'] = 'homensai-test-ptr-' + uuid.uuid4().hex[:12]
    manifest['components'][0]['cmd'] = ['@release/run.py', '@data/result.txt']
    for version in ('1', '2'):
        release = root / 'releases' / ('app-' + version)
        release.mkdir(mode=0o700)
        script = release / 'run.py'
        script.write_text('#!/usr/local/bin/python\nimport pathlib, sys\npathlib.Path(sys.argv[1]).write_text(' + repr(version) + ')\n')
        script.chmod(0o700)
    units = render_pointer_user_services_v2(manifest, modes=('autonomous',), install_root=str(root))
    name, text = next(iter(units.items()))
    unit_dir = Path(user.pw_dir) / '.config/systemd/user'
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / name
    assert not unit.exists()
    unit.write_text(text)
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    try:
        subprocess.run(base + ['daemon-reload'], check=True, capture_output=True, timeout=30)
        pointer, marker = root / 'services/active.json', root / 'data/result.txt'
        for version in ('1', '2', '1'):
            marker.unlink(missing_ok=True)
            _atomic_write(pointer, {'release': version})
            subprocess.run(base + ['start', name], check=True, capture_output=True, timeout=30)
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.read_text() == version
            subprocess.run(base + ['stop', name], check=True, capture_output=True, timeout=30)
    finally:
        subprocess.run(base + ['stop', name], check=False, capture_output=True, timeout=30)
        unit.unlink(missing_ok=True)
        subprocess.run(base + ['daemon-reload'], check=False, capture_output=True, timeout=30)
