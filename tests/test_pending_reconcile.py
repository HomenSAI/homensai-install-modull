"""Продолжение pending подготовки runtime, публикации и загрузки units (аудит A-03).

Сбой вносится после долговечной записи pending и после частичной/полной записи
файлов. Обычный повтор с тем же ключом завершает операцию; посторонние или
изменённые байты, другой ключ и drop-in по-прежнему приводят к отказу без
изменений. Код продукта и настоящий systemd не используются.
"""
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from test_api_signed_preflight import signed_request  # noqa: F401 — фикстура
from test_runtime_native import runtime_release  # noqa: F401 — фикстура
from test_systemd_pointer_signed import _pointer_publisher, signed_pointer  # noqa: F401 — фикстура
from shared import runtime_native, systemd_pointer, systemd_user_publish
from shared.runtime_native import NativeRuntimeError, prepare_signed_linux_runtime
from shared.systemd_portable import PortableUnitError

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Требуется Linux')
KEY = 'a' * 32


class Crash(OSError):
    """Синтетическое прерывание процесса."""


def runtime_call(data, key=KEY):
    package, envelope, options, root, _ = data
    return prepare_signed_linux_runtime(package, envelope, str(root), **options, reserve_bytes=0,
                                        execute=True, operation_id=key)


def runtime_receipt(root):
    return json.loads((root / '.homensai-control/linux-runtime.json').read_text())


def assert_runtime_complete(root, files):
    for name, body in files.items():
        path = root / 'runtime' / name
        assert path.read_bytes() == body
        assert stat.S_IMODE(path.lstat().st_mode) == (0o700 if name == 'bin/python' else 0o600)
    assert runtime_receipt(root)['status'] == 'done'


def _interrupt_copy(monkeypatch, *, at, write):
    """Прервать копирование файла номер at, оставив write(source, destination)."""
    original = runtime_native._copy_runtime_file
    count = [0]

    def copy(source, destination, item, executable):
        count[0] += 1
        if count[0] == at:
            write(Path(source).read_bytes(), destination)
            raise Crash('synthetic crash during copy')
        return original(source, destination, item, executable)
    monkeypatch.setattr(runtime_native, '_copy_runtime_file', copy)
    return original


def _exclusive(destination, data, mode=0o600):
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


@pytest.mark.parametrize('fault', ['after-pending', 'empty-file', 'partial-file', 'before-chmod',
                                   'before-done'])
def test_interrupted_runtime_preparation_completes_on_plain_retry(runtime_release, monkeypatch, fault):
    root, files = runtime_release[-2:]
    if fault == 'after-pending':
        original = runtime_native._new_publication_receipt

        def crash(path, value):
            original(path, value)
            raise Crash('synthetic crash after pending')
        monkeypatch.setattr(runtime_native, '_new_publication_receipt', crash)
    elif fault == 'empty-file':
        _interrupt_copy(monkeypatch, at=1, write=lambda data, path: _exclusive(path, b''))
    elif fault == 'partial-file':
        # Первый файл (bin/python) готов, второй записан наполовину.
        _interrupt_copy(monkeypatch, at=2, write=lambda data, path: _exclusive(path, data[:len(data) // 2]))
    elif fault == 'before-chmod':
        _interrupt_copy(monkeypatch, at=1, write=lambda data, path: _exclusive(path, data))
    else:
        original = runtime_native._inspect_runtime
        calls = []

        def crash(*args):
            calls.append(args)
            original(*args)
            raise Crash('synthetic crash before done')
        monkeypatch.setattr(runtime_native, '_inspect_runtime', crash)
    with pytest.raises(NativeRuntimeError):
        runtime_call(runtime_release)
    assert runtime_receipt(root)['status'] == 'pending'
    monkeypatch.undo()
    # Другой ключ не продолжает чужой pending и ничего не меняет.
    before = {path: path.read_bytes() for path in (root / 'runtime').rglob('*') if path.is_file()}
    with pytest.raises(NativeRuntimeError):
        runtime_call(runtime_release, key='b' * 32)
    assert {path: path.read_bytes() for path in (root / 'runtime').rglob('*') if path.is_file()} == before
    result = runtime_call(runtime_release)
    assert result['state'] == 'prepared' and result['runtime_executed'] is False
    assert_runtime_complete(root, files)
    inodes = {name: (root / 'runtime' / name).stat().st_ino for name in files}
    assert runtime_call(runtime_release) == result
    assert inodes == {name: (root / 'runtime' / name).stat().st_ino for name in files}


@pytest.mark.parametrize('change', ['foreign-bytes', 'longer', 'extra-file', 'extra-dir', 'symlink',
                                    'hardlink', 'group-mode', 'fifo'])
def test_pending_runtime_with_foreign_content_is_refused_unchanged(runtime_release, monkeypatch, change):
    root, files = runtime_release[-2:]
    _interrupt_copy(monkeypatch, at=2, write=lambda data, path: _exclusive(path, data[:5]))
    with pytest.raises(NativeRuntimeError):
        runtime_call(runtime_release)
    monkeypatch.undo()
    runtime = root / 'runtime'
    partial = runtime / 'lib/data.txt'
    if change == 'foreign-bytes':
        partial.write_bytes(b'XXXXX')
    elif change == 'longer':
        partial.write_bytes(files['lib/data.txt'] + b'appended')
    elif change == 'extra-file':
        (runtime / 'lib/foreign.txt').write_bytes(b'preserve')
    elif change == 'extra-dir':
        (runtime / 'foreign').mkdir(mode=0o700)
    elif change == 'symlink':
        partial.unlink()
        partial.symlink_to(runtime / 'bin/python')
    elif change == 'hardlink':
        os.link(partial, root / 'outside-link')
    elif change == 'group-mode':
        partial.chmod(0o640)
    else:
        partial.unlink()
        os.mkfifo(partial, 0o600)
    receipt = (root / '.homensai-control/linux-runtime.json').read_bytes()
    snapshot = {path: (path.lstat().st_ino, path.lstat().st_mode, path.read_bytes() if path.is_file()
                       and not path.is_symlink() else None)
                for path in runtime.rglob('*')}
    for _ in range(2):
        with pytest.raises(NativeRuntimeError):
            runtime_call(runtime_release)
    assert (root / '.homensai-control/linux-runtime.json').read_bytes() == receipt
    assert {path: (path.lstat().st_ino, path.lstat().st_mode, path.read_bytes() if path.is_file()
                   and not path.is_symlink() else None)
            for path in runtime.rglob('*')} == snapshot


def test_pending_runtime_with_other_binding_is_refused(runtime_release, monkeypatch):
    root = runtime_release[-2]
    original = runtime_native._new_publication_receipt

    def crash(path, value):
        original(path, value)
        raise Crash('synthetic crash after pending')
    monkeypatch.setattr(runtime_native, '_new_publication_receipt', crash)
    with pytest.raises(NativeRuntimeError):
        runtime_call(runtime_release)
    monkeypatch.undo()
    package, envelope, options, _, _ = runtime_release
    with pytest.raises(NativeRuntimeError):
        prepare_signed_linux_runtime(package, envelope, str(root), **options, reserve_bytes=1,
                                     execute=True, operation_id=KEY)
    assert runtime_receipt(root)['status'] == 'pending' and not list((root / 'runtime').iterdir())


# --- публикация pointer-units -------------------------------------------------------

def _publisher(signed_pointer, tmp_path):
    package, envelope, root, directory, options, publish, calls = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id=KEY, execute=True)
    plan = systemd_pointer.plan_pointer_signed_user_units(package, envelope, str(root), **options,
                                                          modes=('autonomous',))
    expected = {name: text.encode('utf-8') for name, text in plan['unit_texts'].items()}
    publish.source = (package, envelope)
    return root, directory, options, publish, calls, expected


def publication_receipt(root):
    return json.loads((root / '.homensai-control/user-systemd-publication.json').read_text())


def _write_faults(monkeypatch, *, at, partial):
    """Сбой на записи номер at (1 — маркер): partial — сколько байтов успело записаться."""
    original = systemd_user_publish._write_exclusive
    count = [0]

    def write(directory_fd, name, data):
        count[0] += 1
        if count[0] == at:
            original(directory_fd, name, data[:partial(len(data))])
            raise Crash('synthetic crash during unit write')
        return original(directory_fd, name, data)
    monkeypatch.setattr(systemd_user_publish, '_write_exclusive', write)


@pytest.mark.parametrize('fault', ['after-pending', 'partial-marker', 'partial-unit', 'all-units-marker-left',
                                   'all-units-marker-removed'])
def test_interrupted_pointer_publication_completes_on_plain_retry(signed_pointer, tmp_path, monkeypatch, fault):
    root, directory, _, publish, calls, expected = _publisher(signed_pointer, tmp_path)
    names = sorted(expected)
    if fault == 'after-pending':
        def crash(*args, **kwargs):
            raise Crash('synthetic crash before writing units')
        monkeypatch.setattr(systemd_pointer, '_publish_files', crash)
    elif fault == 'partial-marker':
        _write_faults(monkeypatch, at=1, partial=lambda size: size // 2)
    elif fault == 'partial-unit':
        _write_faults(monkeypatch, at=3, partial=lambda size: size // 3)
    elif fault == 'all-units-marker-left':
        original = systemd_user_publish._trusted_file
        checked = []

        def crash(path, digest):
            original(path, digest)
            checked.append(path)
            if len(checked) == len(names):
                raise Crash('synthetic crash before removing marker')
        monkeypatch.setattr(systemd_user_publish, '_trusted_file', crash)
    else:
        original = systemd_pointer._publish_files

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise Crash('synthetic crash before done')
        monkeypatch.setattr(systemd_pointer, '_publish_files', crash)
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    assert publication_receipt(root)['status'] == 'pending'
    if fault == 'all-units-marker-left':
        assert list(directory.glob('.*.pending'))
    monkeypatch.undo()
    result = publish(execute=True)
    assert result['state'] == 'published' and result['services_started'] is False
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == expected
    assert publication_receipt(root)['status'] == 'done'
    assert all('daemon-reload' not in call and 'start' not in call for call in calls)
    inodes = {path.name: path.stat().st_ino for path in directory.iterdir()}
    assert publish(execute=True) == result
    assert {path.name: path.stat().st_ino for path in directory.iterdir()} == inodes


@pytest.mark.parametrize('change', ['foreign-unit', 'longer-unit', 'drop-in', 'unit-without-marker',
                                    'foreign-marker', 'symlink'])
def test_pending_publication_with_foreign_content_is_refused_unchanged(signed_pointer, tmp_path, monkeypatch,
                                                                       change):
    root, directory, _, publish, _, expected = _publisher(signed_pointer, tmp_path)
    names = sorted(expected)
    _write_faults(monkeypatch, at=3, partial=lambda size: size // 3)
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    monkeypatch.undo()
    marker = next(directory.glob('.*.pending'))
    written = [name for name in names if (directory / name).exists()]
    assert len(written) == 2
    if change == 'foreign-unit':
        (directory / written[0]).write_bytes(b'[Service]\nExecStart=/bin/foreign\n')
    elif change == 'longer-unit':
        (directory / written[0]).write_bytes(expected[written[0]] + b'\n# foreign\n')
    elif change == 'drop-in':
        (directory / (names[-1] + '.d')).mkdir(mode=0o700)
    elif change == 'unit-without-marker':
        marker.unlink()
    elif change == 'foreign-marker':
        marker.write_bytes(b'{"product_id":"other"}\n')
    else:
        missing = [name for name in names if name not in written][0]
        (directory / missing).symlink_to(tmp_path / 'elsewhere.service')
    receipt = (root / '.homensai-control/user-systemd-publication.json').read_bytes()
    snapshot = {path.name: (path.lstat().st_ino, path.read_bytes() if path.is_file() and not path.is_symlink()
                            else None) for path in directory.iterdir()}
    for _ in range(2):
        with pytest.raises(PortableUnitError):
            publish(execute=True)
    assert (root / '.homensai-control/user-systemd-publication.json').read_bytes() == receipt
    assert {path.name: (path.lstat().st_ino, path.read_bytes() if path.is_file() and not path.is_symlink()
                        else None) for path in directory.iterdir()} == snapshot


def test_pending_publication_refuses_other_key_and_active_service(signed_pointer, tmp_path, monkeypatch):
    root, directory, options, publish, _, expected = _publisher(signed_pointer, tmp_path)
    original = systemd_pointer._publish_files

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash('synthetic crash before done')
    monkeypatch.setattr(systemd_pointer, '_publish_files', crash)
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    monkeypatch.undo()
    from test_systemd_portable import fake_manager
    with pytest.raises(PortableUnitError):
        systemd_pointer.publish_pointer_signed_user_units(
            *publish.source, str(root), **options, modes=('autonomous',), unit_dir=str(directory),
            runtime_operation_id=KEY, operation_id='c' * 32, reserve_bytes=0,
            runner=fake_manager(directory)[0], execute=True)
    # Менеджер сообщает о работающей службе с этим именем — продолжать нельзя.
    def active(argv, *, timeout, env):
        if '--value' in argv:
            return str(directory) + '\n'
        name = argv[-1]
        return (f'Id={name}\nLoadState=loaded\nActiveState=active\nSubState=running\n'
                f'FragmentPath={directory / name}\nDropInPaths=\nNeedDaemonReload=no\n')
    with pytest.raises(PortableUnitError):
        systemd_pointer.publish_pointer_signed_user_units(
            *publish.source, str(root), **options, modes=('autonomous',), unit_dir=str(directory),
            runtime_operation_id=KEY, operation_id='b' * 32, reserve_bytes=0, runner=active, execute=True)
    assert publication_receipt(root)['status'] == 'pending'
    # Загруженный из нашего файла неактивный unit (show загрузил его) допустим.
    def loaded(argv, *, timeout, env):
        if '--value' in argv:
            return str(directory) + '\n'
        name = argv[-1]
        return (f'Id={name}\nLoadState=loaded\nActiveState=inactive\nSubState=dead\n'
                f'FragmentPath={directory / name}\nDropInPaths=\nNeedDaemonReload=no\n')
    result = systemd_pointer.publish_pointer_signed_user_units(
        *publish.source, str(root), **options, modes=('autonomous',), unit_dir=str(directory),
        runtime_operation_id=KEY, operation_id='b' * 32, reserve_bytes=0, runner=loaded, execute=True)
    assert result['state'] == 'published' and publication_receipt(root)['status'] == 'done'
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == expected


# --- загрузка pointer-units ---------------------------------------------------------

def test_pending_pointer_load_resumes_with_same_key(signed_pointer, tmp_path):
    from shared.systemd_user_load import UnitLoadError, load_pointer_signed_user_units
    from test_systemd_portable import FakeReloadManager
    root, directory, options, publish, _, expected = _publisher(signed_pointer, tmp_path)
    publish(execute=True)
    package, envelope = publish.source
    args = dict(**options, modes=('autonomous',), unit_dir=str(directory), runtime_operation_id=KEY,
                reserve_bytes=0, operation_id='c' * 32)
    lost = FakeReloadManager(directory, lose_response=True)
    with pytest.raises(UnitLoadError):
        load_pointer_signed_user_units(package, envelope, str(root), **args, runner=lost)
    receipt = root / '.homensai-control/user-systemd-reload.json'
    assert json.loads(receipt.read_text())['status'] == 'pending'
    # Изменённый unit не перезагружается повторно.
    name = sorted(expected)[0]
    (directory / name).write_bytes(expected[name] + b'# changed\n')
    manager = FakeReloadManager(directory)
    with pytest.raises(UnitLoadError):
        load_pointer_signed_user_units(package, envelope, str(root), **args, runner=manager)
    assert manager.calls.count(['daemon-reload']) == 0
    (directory / name).write_bytes(expected[name])
    # Другой ключ не продолжает чужую загрузку.
    with pytest.raises(UnitLoadError):
        load_pointer_signed_user_units(package, envelope, str(root),
                                       **{**args, 'operation_id': 'd' * 32}, runner=manager)
    assert manager.calls.count(['daemon-reload']) == 0
    result = load_pointer_signed_user_units(package, envelope, str(root), **args, runner=manager)
    assert result['state'] == 'loaded' and result['services_started'] is False
    assert json.loads(receipt.read_text())['status'] == 'done'
    assert load_pointer_signed_user_units(package, envelope, str(root), **args,
                                          runner=manager) == result
    assert manager.calls.count(['daemon-reload']) == 1
    assert all('start' not in call for call in manager.calls + lost.calls)


# --- контроллер первой установки с настоящими портами ------------------------------

def test_controller_plain_retry_completes_after_crashes_in_real_ports(signed_pointer, tmp_path, monkeypatch):
    """Сбои после pending в runtime, публикации и загрузке не блокируют повтор того же вызова."""
    from shared import api
    from shared.fresh_controller import FreshControllerError, run_portable_linux_install
    from test_fresh_controller import _pointer_workflow
    from test_systemd_portable import FakeReloadManager, fake_manager
    workflow = _pointer_workflow(signed_pointer, tmp_path, monkeypatch)
    directory = Path(workflow['unit_dir'])
    publisher, _ = fake_manager(directory)
    reload_manager = FakeReloadManager(directory, lose_response=True)
    monkeypatch.setattr('shared.systemd_pointer._run', publisher)
    monkeypatch.setattr('shared.systemd_user_load._run', reload_manager)
    real_handle = api.handle
    operations = []

    def handle(request):
        operations.append(request['operation'])
        # Запуск служб и HTTP-проверка здесь не исполняются: кода продукта нет.
        if request['operation'] == 'start-pointer-linux':
            return {'ok': True, 'result': {'state': 'started'}}
        if request['operation'] == 'check-local-health':
            return {'ok': True, 'result': {'health_response_matches': True}}
        return real_handle(request)
    monkeypatch.setattr(api, 'handle', handle)
    crashes = {'runtime': 1, 'publish': 1}
    original_receipt = runtime_native._new_publication_receipt
    original_publish = systemd_pointer._publish_files

    def runtime_crash(path, value):
        original_receipt(path, value)
        if crashes['runtime']:
            crashes['runtime'] -= 1
            raise Crash('synthetic crash after runtime pending')

    def publish_crash(*args, **kwargs):
        original_publish(*args, **kwargs)
        if crashes['publish']:
            crashes['publish'] -= 1
            raise Crash('synthetic crash after all unit files')
    monkeypatch.setattr(runtime_native, '_new_publication_receipt', runtime_crash)
    monkeypatch.setattr(systemd_pointer, '_publish_files', publish_crash)
    for code in ('linux_runtime_failed', 'systemd_publish_failed', 'systemd_load_failed'):
        with pytest.raises(FreshControllerError) as failure:
            run_portable_linux_install(**workflow)
        assert failure.value.code == code
    reload_manager.lose_response = False
    result = run_portable_linux_install(**workflow)
    assert result['state'] == 'recorded_complete'
    control = Path(workflow['install_root']) / '.homensai-control'
    for name in ('linux-runtime.json', 'user-systemd-publication.json', 'user-systemd-reload.json'):
        assert json.loads((control / name).read_text())['status'] == 'done'
    assert operations == ['prepare-native-release', 'prepare-linux-runtime', 'prepare-linux-runtime',
                          'publish-pointer-systemd-user-units', 'publish-pointer-systemd-user-units',
                          'load-pointer-systemd-user-units', 'load-pointer-systemd-user-units',
                          'start-pointer-linux', 'check-local-health']
    assert reload_manager.calls.count(['daemon-reload']) == 2
