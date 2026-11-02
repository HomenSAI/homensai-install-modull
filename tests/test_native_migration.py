"""Исполнение только синтетических файлов в tmp_path, без shell и сети."""
from hashlib import sha256
import os
from pathlib import Path
import sys
import time

import pytest

from shared.native_migration import run_python_migration
from shared.update_plan import EffectIndeterminate, UpdateError


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Ранний адаптер только для POSIX')


def options(tmp_path, code, args=()):
    root = tmp_path / 'release'
    root.mkdir()
    script = root / 'migrate.py'
    script.write_text(code, encoding='utf-8')
    interpreter = Path(sys.executable).resolve()
    return {'interpreter': str(interpreter), 'interpreter_sha256': sha256(interpreter.read_bytes()).hexdigest(),
            'release_root': str(root), 'script': str(script), 'script_sha256': sha256(script.read_bytes()).hexdigest(),
            'args': args, 'timeout': 5}


def test_executes_pinned_script_without_shell_or_inherited_environment(tmp_path, monkeypatch):
    output = tmp_path / 'result'
    arg = '$(touch should-not-exist)'
    settings = options(tmp_path, 'import os, pathlib, sys\npathlib.Path(sys.argv[1]).write_text('
                       'sys.argv[2] + "|" + str(os.environ.get("SYNTHETIC_SECRET")))\n', (str(output), arg))
    monkeypatch.setenv('SYNTHETIC_SECRET', 'do-not-inherit')
    run_python_migration(**settings)  # Проверка без исполнения.
    assert not output.exists()
    run_python_migration(**settings, execute=True)
    assert output.read_text() == arg + '|None'
    assert not (Path(settings['release_root']) / 'should-not-exist').exists()


@pytest.mark.parametrize('field', ['interpreter_sha256', 'script_sha256'])
def test_wrong_hash_stops_before_script(tmp_path, field):
    output = tmp_path / 'result'
    settings = options(tmp_path, 'import pathlib, sys\npathlib.Path(sys.argv[1]).touch()\n', (str(output),))
    settings[field] = '0' * 64
    with pytest.raises(UpdateError):
        run_python_migration(**settings, execute=True)
    assert not output.exists()


def test_script_outside_or_wrong_case_release_is_rejected(tmp_path):
    settings = options(tmp_path, 'pass\n')
    other = tmp_path / 'Release'
    other.mkdir()
    script = other / 'migrate.py'
    script.write_text('pass\n')
    settings.update(script=str(script), script_sha256=sha256(script.read_bytes()).hexdigest())
    with pytest.raises(UpdateError):
        run_python_migration(**settings, execute=True)


def test_nonzero_and_timeout_are_indeterminate(tmp_path):
    settings = options(tmp_path, 'raise SystemExit(7)\n')
    with pytest.raises(EffectIndeterminate):
        run_python_migration(**settings, execute=True)
    script = Path(settings['script'])
    script.write_text('import time\ntime.sleep(10)\n')
    settings.update(script_sha256=sha256(script.read_bytes()).hexdigest(), timeout=0.2)
    with pytest.raises(EffectIndeterminate):
        run_python_migration(**settings, execute=True)


def test_lingering_child_makes_result_indeterminate(tmp_path):
    settings = options(tmp_path, 'import subprocess, sys\n'
                       'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n')
    with pytest.raises(EffectIndeterminate):
        run_python_migration(**settings, execute=True)


def test_timeout_stops_same_group_child_writer(tmp_path):
    ready, written = tmp_path / 'ready', tmp_path / 'written'
    code = ('import pathlib, subprocess, sys, time\n'
            'child = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); "'
            '"time.sleep(2); pathlib.Path(sys.argv[2]).touch()"\n'
            'subprocess.Popen([sys.executable, "-c", child, sys.argv[1], sys.argv[2]])\n'
            'while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)\n'
            'time.sleep(10)\n')
    settings = options(tmp_path, code, (str(ready), str(written)))
    settings['timeout'] = 1
    with pytest.raises(EffectIndeterminate):
        run_python_migration(**settings, execute=True)
    assert ready.exists()
    time.sleep(2.2)
    assert not written.exists()


def test_symlink_script_rejected(tmp_path):
    settings = options(tmp_path, 'pass\n')
    link = Path(settings['release_root']) / 'link.py'
    link.symlink_to(settings['script'])
    settings['script'] = str(link)
    with pytest.raises(UpdateError):
        run_python_migration(**settings, execute=True)
