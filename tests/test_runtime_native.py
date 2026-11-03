"""Подготовка синтетического подписанного runtime без исполнения при установке."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import uuid

import pytest
from synthetic_wheel import structural_wheel

from shared.fresh_prepare import prepare_fresh_native
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.preflight import NAMES
from shared.runtime_native import NativeRuntimeError, RECIPE_PATH, prepare_signed_linux_runtime, verify_signed_linux_runtime

ROOT = Path(__file__).resolve().parents[1]
TARGET = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
_SOCKET = socket.socket


@pytest.fixture
def runtime_release(tmp_path, monkeypatch):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    if os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1':
        monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: dict(TARGET))
        monkeypatch.setattr('shared.package_prepare.native_target', lambda: dict(TARGET))
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-' + uuid.uuid4().hex[:16]
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [dict(TARGET)]}
    payload = tmp_path / 'payload'
    payload.mkdir()
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        body = (structural_wheel() if artifact['file'].endswith('.whl')
                else ('synthetic dependency ' + str(index)).encode())
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        artifact['sha256'] = hashlib.sha256(body).hexdigest()
    files = {'bin/python': b'#!/bin/sh\nexit 91\n', 'lib/data.txt': b'synthetic runtime data\n'}
    for name, body in files.items():
        path = payload / 'runtime-linux' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    recipe = {'format': 1, 'target': {'os': 'linux', 'architecture': 'x86_64'},
              'python_version': '3.14.10', 'executables': ['bin/python']}
    (payload / RECIPE_PATH).write_text(json.dumps(recipe))
    package = tmp_path / 'runtime.zip'
    build_package(manifest, '1', payload, package)
    key = ed.Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=key, expected_product_id=manifest['product_id'], expected_version='1')
    options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    root = tmp_path / 'install root'
    prepare_fresh_native(package, envelope, root, **options,
                         required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    return package, envelope, options, root, files


def call(data, **kwargs):
    package, envelope, options, root, _ = data
    return prepare_signed_linux_runtime(package, envelope, str(root), **options, reserve_bytes=0, **kwargs)


def test_runtime_preparation_checks_and_repeats_without_execution(runtime_release):
    root, files = runtime_release[-2:]
    plan = call(runtime_release)
    assert plan['state'] == 'planned' and plan['runtime_executed'] is False
    assert plan['python_compatibility_verified'] is False and not list((root / 'runtime').iterdir())
    result = call(runtime_release, execute=True, operation_id='a' * 32)
    assert result['state'] == 'prepared' and result['runtime_executed'] is False
    for name, body in files.items():
        path = root / 'runtime' / name
        assert path.read_bytes() == body
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if name == 'bin/python' else 0o600)
    before = {name: ((root / 'runtime' / name).stat().st_ino, (root / 'runtime' / name).stat().st_mtime_ns)
              for name in files}
    assert call(runtime_release, execute=True, operation_id='a' * 32) == result
    assert before == {name: ((root / 'runtime' / name).stat().st_ino, (root / 'runtime' / name).stat().st_mtime_ns)
                      for name in files}
    with pytest.raises(NativeRuntimeError):
        call(runtime_release, execute=True, operation_id='b' * 32)


@pytest.mark.parametrize('change', ['bytes', 'mode', 'extra', 'symlink', 'hardlink'])
def test_runtime_repeat_refuses_modified_tree(runtime_release, change):
    root = runtime_release[-2]
    call(runtime_release, execute=True, operation_id='a' * 32)
    path = root / 'runtime/lib/data.txt'
    if change == 'bytes':
        path.write_bytes(b'tampered')
    elif change == 'mode':
        path.chmod(0o666)
    elif change == 'extra':
        (root / 'runtime/foreign').write_text('preserve')
    elif change == 'symlink':
        path.unlink()
        path.symlink_to(root / 'runtime/bin/python')
    else:
        os.link(path, root / 'runtime/linked')
    with pytest.raises(NativeRuntimeError):
        call(runtime_release, execute=True, operation_id='a' * 32)
    assert json.loads((root / '.homensai-control/linux-runtime.json').read_text())['status'] == 'done'


def test_runtime_preserves_pending_after_interruption(runtime_release, monkeypatch):
    import shared.runtime_native as runtime
    original = runtime._inspect_runtime
    def interrupted(*args, **kwargs):
        raise OSError('synthetic interruption')
    monkeypatch.setattr(runtime, '_inspect_runtime', interrupted)
    root = runtime_release[-2]
    with pytest.raises(NativeRuntimeError):
        call(runtime_release, execute=True, operation_id='a' * 32)
    receipt = root / '.homensai-control/linux-runtime.json'
    before = receipt.read_bytes()
    assert json.loads(before)['status'] == 'pending'
    monkeypatch.setattr(runtime, '_inspect_runtime', original)
    with pytest.raises(NativeRuntimeError):
        call(runtime_release, execute=True, operation_id='a' * 32)
    assert receipt.read_bytes() == before


def test_runtime_refuses_existing_foreign_files_without_writing(runtime_release):
    root = runtime_release[-2]
    existing = root / 'runtime/foreign'
    existing.write_text('preserve')
    with pytest.raises(NativeRuntimeError):
        call(runtime_release, execute=True, operation_id='a' * 32)
    assert existing.read_text() == 'preserve'
    assert not (root / '.homensai-control/linux-runtime.json').exists()


def test_runtime_refuses_untrusted_key(runtime_release):
    package, envelope, options, root, _ = runtime_release
    with pytest.raises(NativeRuntimeError):
        prepare_signed_linux_runtime(package, envelope, str(root),
            **{**options, 'trusted_public_key': b'x' * 32}, reserve_bytes=0, execute=True, operation_id='a' * 32)
    assert not list((root / 'runtime').iterdir())


def test_verify_runtime_requires_done_and_never_prepares(runtime_release):
    package, envelope, options, root, files = runtime_release
    args = {'operation_id': 'a' * 32, 'reserve_bytes': 0}
    with pytest.raises(NativeRuntimeError):
        verify_signed_linux_runtime(package, envelope, str(root), **options, **args)
    assert not list((root / 'runtime').iterdir())
    assert not (root / '.homensai-control/linux-runtime.json').exists()
    call(runtime_release, execute=True, operation_id='a' * 32)
    before = {name: ((root / 'runtime' / name).stat().st_ino, (root / 'runtime' / name).stat().st_mtime_ns)
              for name in files}
    verified = verify_signed_linux_runtime(package, envelope, str(root), **options, **args)
    assert verified['state'] == 'verified' and verified['runtime_executed'] is False
    assert before == {name: ((root / 'runtime' / name).stat().st_ino, (root / 'runtime' / name).stat().st_mtime_ns)
                      for name in files}
    (root / 'runtime/lib/data.txt').write_bytes(b'tampered')
    with pytest.raises(NativeRuntimeError):
        verify_signed_linux_runtime(package, envelope, str(root), **options, **args)


def test_runtime_api_plans_prepares_and_verifies_without_reexecution(runtime_release, monkeypatch):
    import subprocess
    import sys
    from shared.api import handle

    package, envelope, options, root, _ = runtime_release
    monkeypatch.setattr('shared.api._systemd_plan_available', lambda: True)
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
              'expected_product_id': options['expected_product_id'], 'expected_version': '1',
              'install_root': str(root), 'reserve_bytes': 0}
    query = {'api_major': 1, 'request_id': 'test', 'operation': 'plan-linux-runtime', 'params': params}
    validator = pytest.importorskip('jsonschema').Draft202012Validator(
        json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text()))
    caps = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 18 and query['operation'] in caps['operations']
    assert query['operation'] not in caps['mutating_operations'] and validator.is_valid(query)
    assert handle(query)['result']['state'] == 'planned' and not list((root / 'runtime').iterdir())
    query['operation'] = 'verify-linux-runtime'
    query['params']['idempotency_key'] = 'a' * 32
    assert validator.is_valid(query) and handle(query)['error']['code'] == 'linux_runtime_failed'
    assert not (root / '.homensai-control/linux-runtime.json').exists()
    query['operation'] = 'prepare-linux-runtime'
    assert validator.is_valid(query) and query['operation'] in caps['mutating_operations']
    prepared = handle(query)
    assert prepared['ok'] and prepared['result']['state'] == 'prepared'
    assert prepared['result']['runtime_executed'] is False and prepared['result']['installation_complete'] is False
    assert handle(query) == prepared
    query['operation'] = 'verify-linux-runtime'
    verified = handle(query)
    assert verified['ok'] and verified['result']['state'] == 'verified'
    assert query['operation'] not in caps['mutating_operations']
    if os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') == '1':
        cli = subprocess.run([sys.executable, '-B', '-m', 'shared.api'], cwd=ROOT,
                             input=json.dumps(query).encode(), capture_output=True, timeout=60, check=True)
        assert json.loads(cli.stdout) == verified
    query['params']['idempotency_key'] = 'b' * 32
    query['language'] = 'de'
    error = handle(query)
    assert error['error']['code'] == 'linux_runtime_failed'
    assert error['error']['message'] == 'Die Linux-Laufzeitumgebung konnte nicht bestätigt werden'
    query['params']['reserve_bytes'] = True
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Нужен одноразовый тестовый контейнер Python 3.14')
def test_prepared_runtime_runs_real_container_cpython(tmp_path, monkeypatch, controller=False, pointer=False):
    import base64
    import pwd
    import shutil
    import subprocess
    import sys
    import sysconfig
    import time
    import zipfile
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from examples.demo_product.installer_client import InstallerClient, InstallerClientError
    from shared.systemd_portable import RECIPE_PATH as SERVICE_RECIPE_PATH, RECIPE_V1 as SERVICE_RECIPE_V1
    from shared.systemd_user_load import portable_signed_runtime_user_group
    from shared.systemd_group import SystemdGroupError

    identity = pwd.getpwuid(os.getuid())
    if (os.getuid() == 0 or identity.pw_name != 'homenstest'
            or identity.pw_dir != '/home/homenstest' or not Path('/run/.containerenv').is_file()
            or sys.version_info[:2] != (3, 14)):
        pytest.skip('Защитные условия тестовой среды не выполнены')
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = 'homensai-test-' + uuid.uuid4().hex[:16]
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [dict(TARGET)]}
    manifest['secrets'] = []
    manifest['components'] = [item for item in manifest['components'] if item['name'] != 'scheduler']
    manifest['modes'][0]['components'] = ['web', 'bot']
    probe = subprocess.run([sys.executable, '-B', '-c',
        'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'],
        capture_output=True, text=True, timeout=10, check=True)
    port = int(probe.stdout.strip())
    manifest['web']['port'] = port
    if controller:
        manifest['web']['healthz'] = '/health'
    for component in manifest['components']:
        if component['name'] == 'web':
            component['cmd'] = ['@runtime/bin/python', '-I', '-B', '@release/demo/main.py', 'serve',
                                '--data-dir', '@data', '--port', str(port)]
        elif component['name'] == 'bot':
            component['cmd'] = ['@runtime/bin/python', '-I', '-B', '@release/demo/main.py', 'worker',
                                '--data-dir', '@data']
    payload = tmp_path / 'real-runtime-payload'
    payload.mkdir()
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        if artifact['file'].endswith('.whl'):
            import io
            wheel = io.BytesIO()
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('fictional_demo/__init__.py', '# synthetic, not executed')
            body = wheel.getvalue()
        else:
            body = ('synthetic dependency ' + str(index)).encode()
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        artifact['sha256'] = hashlib.sha256(body).hexdigest()
    runtime = payload / 'runtime-linux'
    (runtime / 'bin').mkdir(parents=True)
    (runtime / 'lib/python3.14/lib-dynload').mkdir(parents=True)
    shutil.copyfile(Path(sys.executable).resolve(), runtime / 'bin/python')
    library_name = sysconfig.get_config_var('INSTSONAME')
    assert library_name == 'libpython3.14.so.1.0'
    shutil.copyfile((Path(sys.base_prefix) / 'lib' / library_name).resolve(), runtime / 'lib' / library_name)
    stdlib = Path(sysconfig.get_path('stdlib'))
    dynload = Path(sysconfig.get_config_var('DESTSHARED'))
    for source in dynload.glob('*.so'):
        assert source.is_file() and not source.is_symlink()
        shutil.copyfile(source, runtime / 'lib/python3.14/lib-dynload' / source.name)
    with zipfile.ZipFile(runtime / 'lib/python314.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for source in stdlib.rglob('*.py'):
            relative = source.relative_to(stdlib)
            if any(part in ('site-packages', 'test', 'tests', '__pycache__') for part in relative.parts):
                continue
            assert not source.is_symlink()
            archive.write(source, relative.as_posix())
    recipe = {'format': 1, 'target': {'os': 'linux', 'architecture': 'x86_64'},
              'python_version': f'3.14.{sys.version_info.micro}', 'executables': ['bin/python']}
    (payload / RECIPE_PATH).write_text(json.dumps(recipe))
    if pointer:
        from shared.systemd_pointer import RECIPE_PATH as POINTER_RECIPE_PATH, RECIPE_V2
        launcher = runtime / 'lib/homensai/portable_launcher.py'
        launcher.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'shared/portable_launcher.py', launcher)
    service_recipe = payload / (POINTER_RECIPE_PATH if pointer else SERVICE_RECIPE_PATH)
    service_recipe.parent.mkdir()
    service_recipe.write_bytes(RECIPE_V2 if pointer else SERVICE_RECIPE_V1)
    demo = payload / 'demo'
    demo.mkdir()
    source = (ROOT / 'examples/demo_product/main.py').read_text()
    assert source.count('homensai-test-demo') == 1
    (demo / 'main.py').write_text(source.replace('homensai-test-demo', manifest['product_id']))
    (demo / 'release.json').write_bytes((ROOT / 'examples/demo_product/releases/1/release.json').read_bytes())
    package = tmp_path / 'real-runtime.zip'
    build_package(manifest, '1', payload, package)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    install = tmp_path / 'real runtime with spaces'
    if controller:
        from shared.fresh_controller import run_portable_linux_install
        from shared.systemd_portable import plan_portable_signed_user_units
        from shared.systemd_pointer import plan_pointer_signed_user_units
        def loopback_only(address, timeout, source_address=None):
            if address != ('127.0.0.1', port) or source_address is not None:
                raise AssertionError('Разрешён только loopback тестового продукта')
            connection = _SOCKET(socket.AF_INET, socket.SOCK_STREAM)
            try:
                connection.settimeout(timeout)
                connection.connect(address)
                return connection
            except BaseException:
                connection.close()
                raise
        monkeypatch.setattr(socket, 'create_connection', loopback_only)
        destination = Path(identity.pw_dir) / '.config/systemd/user'
        names = [manifest['product_id'] + '-' + component + '.service' for component in ('web', 'bot')]
        paths = [destination / name for name in names]
        work = tmp_path / 'workflow'
        work.mkdir(mode=0o700)
        env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
               'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}
        args = dict(package_path=str(package), envelope=envelope, trusted_public_key=public,
                    product_id=manifest['product_id'], version='1', install_root=str(install),
                    unit_dir=str(destination), work_dir=str(work), modes=('autonomous',),
                    required_bytes={name: 0 for name in NAMES}, reserve_bytes=0, workflow_key='f' * 32,
                    keys=dict(zip(('release', 'runtime', 'publish', 'load', 'start'),
                                  (key * 32 for key in '1abcd'))))
        try:
            import shared.api as api_module
            class Interrupted(BaseException):
                pass
            original_handle = api_module.handle
            def interrupt_after_runtime(request):
                result = original_handle(request)
                if request['operation'] == 'prepare-linux-runtime' and result.get('ok'):
                    raise Interrupted()
                return result
            with monkeypatch.context() as patch:
                patch.setattr(api_module, 'handle', interrupt_after_runtime)
                with pytest.raises(Interrupted):
                    run_portable_linux_install(**args)
            marker_file = work / ('install-' + args['workflow_key'] + '.json')
            assert json.loads(marker_file.read_text())['index'] == 1
            api_args = {name: value for name, value in args.items() if name not in ('product_id', 'version')}
            api_args['expected_product_id'], api_args['expected_version'] = args['product_id'], args['version']
            api_args['envelope'] = base64.b64encode(args['envelope']).decode('ascii')
            api_args['trusted_public_key'] = base64.b64encode(args['trusted_public_key']).decode('ascii')
            api_args['modes'] = list(args['modes'])
            from examples.demo_product.installer_client import InstallerClient
            client = InstallerClient(timeout=300)
            observed = client.call('inspect-portable-linux-install', api_args, min_minor=57 if pointer else 32, language='de')
            assert observed['state'] == 'pending' and observed['completed_steps'] == 1
            assert observed['current_step'] == 'prepare-linux-runtime' and observed['step_may_have_executed'] is True
            assert observed['resume_authorized'] is False and json.loads(marker_file.read_text())['index'] == 1
            result = client.call('install-portable-linux', api_args, min_minor=57 if pointer else 31, language='de')
            assert result['state'] == 'recorded_complete' and result['health_verified_at_execution'] is True
            assert result['installation_complete'] is False and result['process_identity_verified'] is False
            assert client.call('install-portable-linux', api_args, min_minor=57 if pointer else 31, language='ru') == result
            completed = client.call('inspect-portable-linux-install', api_args, min_minor=57 if pointer else 32, language='ru')
            assert completed['state'] == 'recorded_complete' and completed['completed_steps'] == 6
            assert completed['effects_verified'] is False and completed['current_health_verified'] is False
            heartbeat_path = install / 'data/worker-heartbeat.json'
            deadline = time.monotonic() + 15
            while not heartbeat_path.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            heartbeat = json.loads(heartbeat_path.read_text())
            assert heartbeat['release'] == '1'
            plan_function = plan_pointer_signed_user_units if pointer else plan_portable_signed_user_units
            plan = plan_function(package, envelope, str(install), **options, modes=('autonomous',))
            expected_units = {name: text.encode('utf-8') for name, text in plan['unit_texts'].items()}
        finally:
            subprocess.run(['/usr/bin/systemctl', '--user', 'stop', '--', *names], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
            for path in paths:
                if path.exists():
                    info = path.lstat()
                    assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                    if 'expected_units' in locals():
                        assert path.read_bytes() == expected_units[path.name]
                    path.unlink()
            subprocess.run(['/usr/bin/systemctl', '--user', 'daemon-reload'], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
        return
    prepare_fresh_native(package, envelope, install, **options,
                         required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    client = InstallerClient(timeout=300)
    runtime_args = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
                    'trusted_public_key': base64.b64encode(public).decode(), 'expected_product_id': manifest['product_id'],
                    'expected_version': '1', 'install_root': str(install), 'reserve_bytes': 0, 'idempotency_key': 'a' * 32}
    prepared = client.call('prepare-linux-runtime', runtime_args, min_minor=18)
    assert prepared['runtime_executed'] is False and prepared['python_compatibility_verified'] is False
    assert client.call('verify-linux-runtime', runtime_args, min_minor=18)['state'] == 'verified'
    interpreter = install / 'runtime/bin/python'
    check = subprocess.run([str(interpreter), '-I', '-B', '-c',
        'import http.server, sqlite3, ssl, sys; print(sys.prefix); print(sys.version_info[:3])'],
        env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=True)
    assert Path(check.stdout.splitlines()[0]).resolve() == (install / 'runtime').resolve()
    assert check.stdout.splitlines()[1] == str(tuple(sys.version_info[:3]))
    destination = Path(identity.pw_dir) / '.config/systemd/user'
    names = [manifest['product_id'] + '-' + component + '.service' for component in ('web', 'bot')]
    expected = [destination / name for name in names]
    marker = destination / ('.' + manifest['product_id'] + '.homensai-units.pending')
    assert destination.is_dir() and not marker.exists() and all(not path.exists() for path in expected)
    from shared.systemd_portable import plan_portable_signed_user_units
    plan = plan_portable_signed_user_units(package, envelope, str(install), **options, modes=('autonomous',))
    units = {name: text.encode('utf-8') for name, text in plan['unit_texts'].items()}
    common = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(public).decode(), 'expected_product_id': manifest['product_id'],
              'expected_version': '1', 'install_root': str(install), 'modes': ['autonomous'], 'unit_dir': str(destination)}
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
           'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
           'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}
    try:
        published = client.call('publish-portable-systemd-user-units',
                                {**common, 'idempotency_key': 'b' * 32}, min_minor=17)
        assert published['state'] == 'published'
        loaded = client.call('load-portable-systemd-user-units',
                             {**common, 'idempotency_key': 'c' * 32}, min_minor=17)
        assert loaded['state'] == 'loaded'
        readiness = {**common, 'load_key': 'c' * 32, 'runtime_key': 'a' * 32, 'reserve_bytes': 0}
        from shared.api import handle
        request = {'api_major': 1, 'request_id': 'test', 'operation': 'check-portable-linux-start', 'params': readiness}
        validator = pytest.importorskip('jsonschema').Draft202012Validator(
            json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text()))
        assert validator.is_valid(request)
        caps = handle({'api_major': 1, 'request_id': 'test', 'operation': 'capabilities', 'params': {}})['result']
        assert request['operation'] in caps['mutating_operations']
        checked = client.call('check-portable-linux-start', readiness, min_minor=19)
        assert checked['state'] == 'checked' and checked['units'] == names
        assert checked['services_started'] is False and checked['installation_complete'] is False
        invalid = handle({**request, 'params': {**readiness, 'runtime_key': 'c' * 32}})
        assert invalid['error']['code'] == 'invalid_request'
        group = portable_signed_runtime_user_group(package, envelope, str(install), **options,
                    modes=('autonomous',), unit_dir=str(destination), operation_id='c' * 32,
                    runtime_operation_id='a' * 32, runtime_reserve_bytes=0)
        interpreter.chmod(0o600)
        with pytest.raises(InstallerClientError):
            client.call('check-portable-linux-start', readiness, min_minor=19)
        with pytest.raises(SystemdGroupError):
            group.command('start', execute=True)
        start_request = {**readiness, 'idempotency_key': 'd' * 32}
        assert validator.is_valid({**request, 'operation': 'start-portable-linux', 'params': start_request})
        with pytest.raises(InstallerClientError):
            client.call('start-portable-linux', start_request, min_minor=20)
        assert not (install / '.homensai-control/user-systemd-start.json').exists()
        for name in names:
            state = subprocess.run(['/usr/bin/systemctl', '--user', 'is-active', name], env=env,
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            assert state.stdout.strip() == 'inactive'
        interpreter.chmod(0o700)
        import shared.systemd_fresh_start as fresh_start
        original_write = fresh_start._atomic_write
        def lose_start_receipt(path, value):
            if path.name == 'user-systemd-start.json' and value['status'] == 'done':
                raise OSError('synthetic lost response')
            return original_write(path, value)
        monkeypatch.setattr(fresh_start, '_atomic_write', lose_start_receipt)
        failed_start = handle({**request, 'operation': 'start-portable-linux', 'params': start_request})
        assert failed_start['error']['code'] == 'linux_start_failed'
        assert json.loads((install / '.homensai-control/user-systemd-start.json').read_text())['status'] == 'pending'
        monkeypatch.setattr(fresh_start, '_atomic_write', original_write)
        reconcile_start = {**request, 'operation': 'reconcile-portable-linux-start', 'params': start_request}
        assert validator.is_valid(reconcile_start)
        recovered = client.call('reconcile-portable-linux-start', start_request, min_minor=23)
        assert recovered['prior_start_command_confirmed'] is False
        started = {key: value for key, value in recovered.items() if key != 'prior_start_command_confirmed'}
        assert started['state'] == 'started' and started['health_verified'] is False
        assert client.call('start-portable-linux', start_request, min_minor=20) == started
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                heartbeat = json.loads((install / 'data/worker-heartbeat.json').read_text())
                result = client.call('check-local-health', {'port': port, 'product_id': manifest['product_id'],
                                      'version': '1', 'path': '/health', 'timeout_seconds': 2}, min_minor=12)
                if result['health_response_matches'] and heartbeat['release'] == '1':
                    break
            except (OSError, ValueError, KeyError, InstallerClientError):
                pass
            time.sleep(0.1)
        else:
            pytest.fail('Подписанный продукт не подтвердил ответ и heartbeat')
        assert result['process_identity_verified'] is False
        interpreter.chmod(0o600)  # Остановка доступна при повреждённом runtime.
        stop_request = {**readiness, 'start_key': 'd' * 32, 'idempotency_key': 'e' * 32}
        assert validator.is_valid({**request, 'operation': 'stop-portable-linux', 'params': stop_request})
        import shared.systemd_fresh_start as fresh_start
        original_write = fresh_start._atomic_write
        def lose_stop_receipt(path, value):
            if path.name == 'user-systemd-stop.json' and value['status'] == 'done':
                raise OSError('synthetic lost response')
            return original_write(path, value)
        monkeypatch.setattr(fresh_start, '_atomic_write', lose_stop_receipt)
        failed = handle({**request, 'operation': 'stop-portable-linux', 'params': stop_request})
        assert failed['error']['code'] == 'linux_stop_failed'
        assert json.loads((install / '.homensai-control/user-systemd-stop.json').read_text())['status'] == 'pending'
        monkeypatch.setattr(fresh_start, '_atomic_write', original_write)
        reconcile_request = {**request, 'operation': 'reconcile-portable-linux-stop', 'params': stop_request}
        assert validator.is_valid(reconcile_request)
        stopped = client.call('reconcile-portable-linux-stop', stop_request, min_minor=22)
        assert stopped['state'] == 'stopped' and stopped['all_writers_stopped'] is False
        assert stopped['prior_stop_command_confirmed'] is False
        assert client.call('stop-portable-linux', stop_request, min_minor=21) == {
            key: value for key, value in stopped.items() if key != 'prior_stop_command_confirmed'}
        with pytest.raises(InstallerClientError):
            client.call('start-portable-linux', start_request, min_minor=20)
        interpreter.chmod(0o700)
        cycle_request = {**readiness, 'initial_start_key': 'd' * 32, 'initial_stop_key': 'e' * 32,
                         'idempotency_key': '1' * 32, 'action': 'start', 'reconcile': False}
        assert validator.is_valid({**request, 'operation': 'cycle-portable-linux', 'params': cycle_request})
        second = client.call('cycle-portable-linux', cycle_request, min_minor=24)
        assert second['state'] == 'started' and second['health_verified'] is False
        stop_cycle = {**cycle_request, 'idempotency_key': '2' * 32, 'action': 'stop'}
        assert client.call('cycle-portable-linux', stop_cycle, min_minor=24)['state'] == 'stopped'
    finally:
        subprocess.run(['/usr/bin/systemctl', '--user', 'stop', '--', *names], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
        for path in expected:
            if path.exists():
                info = path.lstat()
                assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                assert path.read_bytes() == units[path.name]
                path.unlink()
        if marker.exists():
            info = marker.lstat()
            assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
            body = json.loads(marker.read_text())
            assert body['product_id'] == manifest['product_id'] and body['version'] == '1'
            assert {item['name']: item['sha256'] for item in body['units']} == {
                name: hashlib.sha256(value).hexdigest() for name, value in units.items()}
            marker.unlink()
        subprocess.run(['/usr/bin/systemctl', '--user', 'daemon-reload'], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Нужен одноразовый тестовый контейнер Python 3.14')
def test_fresh_controller_runs_real_portable_product(tmp_path, monkeypatch):
    test_prepared_runtime_runs_real_container_cpython(tmp_path, monkeypatch, controller=True)


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Нужен одноразовый тестовый контейнер Python 3.14')
def test_fresh_controller_runs_real_pointer_product(tmp_path, monkeypatch):
    test_prepared_runtime_runs_real_container_cpython(tmp_path, monkeypatch, controller=True, pointer=True)
