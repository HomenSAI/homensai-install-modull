"""Signed pointer recipe planning; no service or product code is executed."""
import base64
import json
import os
from pathlib import Path
import sys

import pytest

from test_api_signed_preflight import signed_request
from shared.fresh_prepare import prepare_fresh_native
from shared.package import build_package
from shared.package_signing import sign_package
from shared.systemd_pointer import (LAUNCHER_PATH, LAUNCHER_SHA256, RECIPE_PATH, RECIPE_V2,
                                    plan_pointer_signed_user_units)
from shared.systemd_portable import PortableUnitError

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Требуется Linux')


@pytest.fixture
def signed_pointer(signed_request, tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    ser = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    query, _, manifest = signed_request
    manifest['secrets'] = []
    for component in manifest['components']:
        component['cmd'] = ['@runtime/bin/python', '-I', '-B', '@release/main.py']
    payload = tmp_path / 'payload'
    recipe = payload / RECIPE_PATH
    recipe.parent.mkdir(parents=True)
    recipe.write_bytes(RECIPE_V2)
    launcher = payload / LAUNCHER_PATH
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes((ROOT / 'shared/portable_launcher.py').read_bytes())
    binary = payload / 'runtime-linux/bin/python'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'synthetic interpreter, never executed')
    (payload / 'runtime-linux/runtime-v1.json').write_text(json.dumps({
        'format': 1, 'target': {'os': 'linux', 'architecture': 'x86_64'},
        'python_version': '3.14.10', 'executables': ['bin/python']}))
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    options = dict(trusted_public_key=public, expected_product_id=manifest['product_id'], expected_version='1')
    def build(name):
        package = tmp_path / (name + '.zip')
        build_package(manifest, '1', payload, package)
        envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'],
                                expected_version='1')
        root = tmp_path / (name + '-installation')
        prepare_fresh_native(package, envelope, root, **options,
            required_bytes=query['params']['required_bytes'], reserve_bytes=0)
        return package, envelope, root
    return payload, options, build


def test_signed_pointer_recipe_plans_without_effects(signed_pointer):
    payload, options, build = signed_pointer
    package, envelope, root = build('valid')
    result = plan_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))
    assert result['state'] == 'planned' and result['signed_package_verified']
    assert result['launcher_sha256'] == LAUNCHER_SHA256
    assert not result['prepared_runtime_verified'] and not result['publisher_trust_verified']
    assert not result['services_started'] and not result['publication_readiness_verified']
    for text in result['unit_texts'].values():
        assert '"@release/main.py"' in text and 'app-1' not in text
    assert not (root / '.homensai-control/user-systemd-publication.json').exists()
    assert not (root / 'runtime/lib/homensai/portable_launcher.py').exists()
    assert (payload / LAUNCHER_PATH).is_file()
    from shared.systemd_portable import plan_portable_signed_user_units
    with pytest.raises(PortableUnitError):
        plan_portable_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))


@pytest.mark.parametrize('change', ['wrong-launcher', 'wrong-recipe', 'conflicting-recipe'])
def test_signed_but_unrecognized_recipe_or_launcher_is_refused(signed_pointer, change):
    payload, options, build = signed_pointer
    if change == 'wrong-launcher':
        (payload / LAUNCHER_PATH).write_text('untrusted launcher')
    elif change == 'wrong-recipe':
        (payload / RECIPE_PATH).write_bytes(RECIPE_V2 + b'changed')
    else:
        (payload / 'systemd-user/portable-v1.json').write_text('conflicting recipe')
    package, envelope, root = build(change)
    with pytest.raises(PortableUnitError):
        plan_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))
    assert not (root / '.homensai-control/user-systemd-publication.json').exists()


def test_wrong_key_or_modified_prepared_launcher_is_refused(signed_pointer):
    _, options, build = signed_pointer
    package, envelope, root = build('tamper')
    with pytest.raises(PortableUnitError):
        plan_pointer_signed_user_units(package, envelope, str(root),
                                       **{**options, 'trusted_public_key': b'\0' * 32}, modes=('autonomous',))
    (root / 'releases/app-1' / LAUNCHER_PATH).write_text('modified after signature')
    with pytest.raises(PortableUnitError):
        plan_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))


def test_signed_launcher_runtime_copy_is_verified_and_tampering_detected(signed_pointer):
    from shared.runtime_native import NativeRuntimeError, prepare_signed_linux_runtime, verify_signed_linux_runtime
    _, options, build = signed_pointer
    package, envelope, root = build('runtime')
    plan_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))
    key = 'a' * 32
    prepared = prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                            reserve_bytes=0, operation_id=key, execute=True)
    assert prepared['runtime_executed'] is False
    installed = root / 'runtime/lib/homensai/portable_launcher.py'
    assert installed.read_bytes() == (ROOT / 'shared/portable_launcher.py').read_bytes()
    assert verify_signed_linux_runtime(package, envelope, str(root), **options,
                                       reserve_bytes=0, operation_id=key)['state'] == 'verified'
    installed.write_text('tampered launcher')
    with pytest.raises(NativeRuntimeError):
        verify_signed_linux_runtime(package, envelope, str(root), **options,
                                    reserve_bytes=0, operation_id=key)


def test_public_api_and_independent_client_plan_pointer_units(signed_pointer):
    from homensai_installer_client import InstallerClient, contract_schema
    from shared.api import handle
    _, options, build = signed_pointer
    package, envelope, root = build('api')
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
              'expected_product_id': options['expected_product_id'], 'expected_version': '1',
              'install_root': str(root), 'modes': ['autonomous']}
    request = {'api_major': 1, 'request_id': 'pointer-plan', 'operation': 'plan-pointer-systemd-user-units',
               'params': params}
    jsonschema = pytest.importorskip('jsonschema')
    assert jsonschema.Draft202012Validator(contract_schema('request')).is_valid(request)
    caps = handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 53 and request['operation'] in caps['operations']
    assert request['operation'] not in caps['mutating_operations']
    result = handle(request)['result']
    assert result['launcher_sha256'] == LAUNCHER_SHA256 and not result['manager_checked']
    assert InstallerClient(timeout=90).call(request['operation'], params, min_minor=53) == result
    assert not (root / '.homensai-control/user-systemd-publication.json').exists()


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_pointer_plan_rejects_unknown_publisher_without_exposing_key(signed_pointer, language):
    from shared.api import handle
    _, options, build = signed_pointer
    package, envelope, root = build('api-' + language)
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(b'\0' * 32).decode(),
              'expected_product_id': options['expected_product_id'], 'expected_version': '1',
              'install_root': str(root), 'modes': ['autonomous']}
    response = handle({'api_major': 1, 'request_id': 'pointer-plan', 'operation': 'plan-pointer-systemd-user-units',
                       'params': params, 'language': language})
    assert not response['ok'] and response['error']['code'] == 'systemd_plan_failed'
    assert params['trusted_public_key'] not in json.dumps(response)


def _pointer_publisher(signed_pointer, tmp_path):
    from shared.systemd_pointer import publish_pointer_signed_user_units
    from test_systemd_portable import fake_manager
    _, options, build = signed_pointer
    package, envelope, root = build('publish')
    directory = tmp_path / 'units'
    directory.mkdir(mode=0o700)
    runner, calls = fake_manager(directory)
    def call(**kwargs):
        return publish_pointer_signed_user_units(package, envelope, str(root), **options,
            modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
            operation_id='b' * 32, reserve_bytes=0, runner=runner, **kwargs)
    return package, envelope, root, directory, options, call, calls


def test_pointer_publication_requires_runtime_and_is_idempotent(signed_pointer, tmp_path):
    from shared.runtime_native import prepare_signed_linux_runtime
    package, envelope, root, directory, options, publish, calls = _pointer_publisher(signed_pointer, tmp_path)
    assert publish()['state'] == 'planned' and not calls and not list(directory.iterdir())
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    assert not calls and not list(directory.iterdir())
    assert not (root / '.homensai-control/user-systemd-publication.json').exists()
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    result = publish(execute=True)
    assert result['state'] == 'published' and result['prepared_runtime_verified']
    assert result['services_started'] is False
    contents = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert contents and all(b'portable_launcher.py' in content for content in contents.values())
    assert publish(execute=True) == result
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == contents
    assert all('start' not in call and 'daemon-reload' not in call for call in calls)
    (root / 'runtime/lib/homensai/portable_launcher.py').write_text('tampered')
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == contents


def test_pointer_publication_pending_never_repeats(signed_pointer, tmp_path, monkeypatch):
    import shared.systemd_pointer as pointer
    from shared.runtime_native import prepare_signed_linux_runtime
    package, envelope, root, directory, options, publish, calls = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    def interrupted(*args, **kwargs):
        raise OSError('synthetic interrupted write')
    monkeypatch.setattr(pointer, '_publish_files', interrupted)
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    receipt = root / '.homensai-control/user-systemd-publication.json'
    assert json.loads(receipt.read_text())['status'] == 'pending'
    previous_calls = len(calls)
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    assert len(calls) == previous_calls and not list(directory.iterdir())


def test_runtime_tampering_during_publication_leaves_pending_marker(signed_pointer, tmp_path, monkeypatch):
    import shared.systemd_pointer as pointer
    from shared.runtime_native import prepare_signed_linux_runtime
    package, envelope, root, directory, options, publish, calls = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    original = pointer._publish_files
    def race(directory, names, units, public, product_id, version, verify_again):
        def tamper_then_check():
            (root / 'runtime/lib/homensai/portable_launcher.py').write_text('tampered during publication')
            verify_again()
        return original(directory, names, units, public, product_id, version, tamper_then_check)
    monkeypatch.setattr(pointer, '_publish_files', race)
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    receipt = root / '.homensai-control/user-systemd-publication.json'
    assert json.loads(receipt.read_text())['status'] == 'pending'
    assert list(directory.glob('*.pending'))
    assert all('start' not in call and 'daemon-reload' not in call for call in calls)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(PortableUnitError):
        publish(execute=True)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.parametrize('lost_response', [False, True])
def test_pointer_reload_is_bound_to_runtime_and_never_replayed(signed_pointer, tmp_path, lost_response):
    from shared.runtime_native import prepare_signed_linux_runtime
    from shared.systemd_user_load import (UnitLoadError, load_pointer_signed_user_units,
                                         inspect_pending_pointer_user_units)
    from test_systemd_portable import FakeReloadManager
    package, envelope, root, directory, options, publish, _ = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    publish(execute=True)
    manager = FakeReloadManager(directory, lose_response=lost_response)
    args = dict(**options, modes=('autonomous',), unit_dir=str(directory),
                runtime_operation_id='a' * 32, reserve_bytes=0, operation_id='c' * 32, runner=manager)
    if lost_response:
        with pytest.raises(UnitLoadError):
            load_pointer_signed_user_units(package, envelope, str(root), **args)
        with pytest.raises(UnitLoadError):
            load_pointer_signed_user_units(package, envelope, str(root), **args)
        (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged')
        observed = inspect_pending_pointer_user_units(package, envelope, str(root), **args)
        assert observed['state'] == 'pending_observed' and not observed['reload_completion_verified']
    else:
        result = load_pointer_signed_user_units(package, envelope, str(root), **args)
        assert result['state'] == 'loaded' and not result['services_started']
        assert load_pointer_signed_user_units(package, envelope, str(root), **args) == result
    assert manager.calls.count(['daemon-reload']) == 1
    assert all('start' not in call for call in manager.calls)


def test_pointer_reload_refuses_corrupt_runtime_before_manager(signed_pointer, tmp_path):
    from shared.runtime_native import prepare_signed_linux_runtime
    from shared.systemd_user_load import UnitLoadError, load_pointer_signed_user_units
    from test_systemd_portable import FakeReloadManager
    package, envelope, root, directory, options, publish, _ = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    publish(execute=True)
    (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged')
    manager = FakeReloadManager(directory)
    with pytest.raises(UnitLoadError):
        load_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',),
            unit_dir=str(directory), runtime_operation_id='a' * 32, reserve_bytes=0,
            operation_id='c' * 32, runner=manager)
    assert not manager.calls and not (root / '.homensai-control/user-systemd-reload.json').exists()


def test_pointer_reload_detects_runtime_change_during_reload(signed_pointer, tmp_path):
    from shared.runtime_native import prepare_signed_linux_runtime
    from shared.systemd_user_load import UnitLoadError, load_pointer_signed_user_units
    from test_systemd_portable import FakeReloadManager
    package, envelope, root, directory, options, publish, _ = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    publish(execute=True)
    class TamperingManager(FakeReloadManager):
        def __call__(self, argv, *, timeout, env):
            result = super().__call__(argv, timeout=timeout, env=env)
            if argv[4:] == ['daemon-reload']:
                (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged after reload')
            return result
    manager = TamperingManager(directory)
    with pytest.raises(UnitLoadError):
        load_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',),
            unit_dir=str(directory), runtime_operation_id='a' * 32, reserve_bytes=0,
            operation_id='c' * 32, runner=manager)
    receipt = root / '.homensai-control/user-systemd-reload.json'
    assert json.loads(receipt.read_text())['status'] == 'pending'
    assert manager.calls.count(['daemon-reload']) == 1 and all('start' not in x for x in manager.calls)


def test_pointer_group_checks_runtime_before_start_and_allows_stop(signed_pointer, tmp_path):
    from shared.runtime_native import prepare_signed_linux_runtime
    from shared.systemd_group import SystemdGroupError
    from shared.systemd_user_load import (load_pointer_signed_user_units, pointer_signed_runtime_user_group,
                                         _pointer_signed_user_group)
    from test_systemd_portable import FakeReloadManager
    package, envelope, root, directory, options, publish, _ = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    publish(execute=True)
    manager = FakeReloadManager(directory)
    args = dict(**options, modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
                reserve_bytes=0, operation_id='c' * 32, runner=manager)
    load_pointer_signed_user_units(package, envelope, str(root), **args)
    group = pointer_signed_runtime_user_group(package, envelope, str(root), **args)
    assert len(group.command('start')) == 3
    (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged')
    before = len(manager.calls)
    with pytest.raises(SystemdGroupError):
        group.command('start', execute=True)
    assert len(manager.calls) == before
    assert len(group.command('stop', execute=True)) == 3
    stopped = _pointer_signed_user_group(package, envelope, str(root), **args,
                                         _require_stable=True, _verify_runtime=False)
    assert len(stopped.command('stop', execute=True)) == 3
    assert all('start' not in call for call in manager.calls)


@pytest.mark.parametrize('lost_response', [False, True])
def test_api_pointer_reload_and_pending_inspection(signed_pointer, tmp_path, monkeypatch, lost_response):
    import shared.systemd_user_load as loader
    from homensai_installer_client import contract_schema
    from shared.api import handle
    from shared.runtime_native import prepare_signed_linux_runtime
    from test_systemd_portable import FakeReloadManager
    package, envelope, root, directory, options, publish, _ = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    publish(execute=True)
    manager = FakeReloadManager(directory, lose_response=lost_response)
    monkeypatch.setattr(loader, '_run', manager)
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
              'expected_product_id': options['expected_product_id'], 'expected_version': '1',
              'install_root': str(root), 'modes': ['autonomous'], 'unit_dir': str(directory),
              'runtime_key': 'a' * 32, 'idempotency_key': 'c' * 32, 'reserve_bytes': 0}
    request = {'api_major': 1, 'request_id': 'pointer-load', 'operation': 'load-pointer-systemd-user-units',
               'params': params}
    jsonschema = pytest.importorskip('jsonschema')
    validator = jsonschema.Draft202012Validator(contract_schema('request'))
    caps = handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert validator.is_valid(request) and caps['api_minor'] >= 55
    assert request['operation'] in caps['mutating_operations']
    first = handle(request)
    if lost_response:
        assert first['error']['code'] == 'systemd_load_failed'
        assert handle(request)['error']['code'] == 'systemd_load_failed'
        (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged')
        request['operation'] = 'inspect-pending-pointer-systemd-user-units'
        assert validator.is_valid(request) and request['operation'] in caps['mutating_operations']
        observed = handle(request)
        assert observed['ok'] and observed['result']['state'] == 'pending_observed'
        assert not observed['result']['reload_completion_verified']
    else:
        assert first['ok'] and first['result']['state'] == 'loaded' and not first['result']['services_started']
        assert handle(request) == first
    assert manager.calls.count(['daemon-reload']) == 1


@pytest.mark.parametrize('lost_response', ['none', 'start', 'stop'])
def test_api_pointer_start_stop_survives_runtime_damage(signed_pointer, tmp_path, monkeypatch, lost_response):
    import shared.systemd_user as user_adapter
    import shared.systemd_user_load as loader
    from homensai_installer_client import contract_schema
    from shared.api import handle
    from shared.runtime_native import prepare_signed_linux_runtime
    from test_systemd_portable import FakeReloadManager
    package, envelope, root, directory, options, publish, _ = _pointer_publisher(signed_pointer, tmp_path)
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    publish(execute=True)
    class StatefulManager(FakeReloadManager):
        def __init__(self, directory):
            super().__init__(directory)
            self.active = set()
            self.lost = False
        def __call__(self, argv, *, timeout, env):
            action, name = argv[4], argv[-1]
            if action in ('start', 'stop'):
                self.calls.append(argv[4:])
                self.active.add(name) if action == 'start' else self.active.discard(name)
                if (not self.lost and action == lost_response
                        and ((action == 'start' and len(self.active) == 3) or
                             (action == 'stop' and not self.active))):
                    self.lost = True
                    raise TimeoutError('synthetic response loss')
                return ''
            output = super().__call__(argv, timeout=timeout, env=env)
            if action == 'show' and name in self.active:
                output = output.replace('ActiveState=inactive\nSubState=dead', 'ActiveState=active\nSubState=running')
            return output
    manager = StatefulManager(directory)
    monkeypatch.setattr(loader, '_run', manager)
    monkeypatch.setattr(user_adapter, '_run', manager)
    loader.load_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',),
        unit_dir=str(directory), runtime_operation_id='a' * 32, reserve_bytes=0,
        operation_id='c' * 32, runner=manager)
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
              'expected_product_id': options['expected_product_id'], 'expected_version': '1',
              'install_root': str(root), 'modes': ['autonomous'], 'unit_dir': str(directory),
              'runtime_key': 'a' * 32, 'load_key': 'c' * 32, 'reserve_bytes': 0}
    request = {'api_major': 1, 'request_id': 'pointer-start', 'operation': 'check-pointer-linux-start', 'params': params}
    validator = pytest.importorskip('jsonschema').Draft202012Validator(contract_schema('request'))
    assert validator.is_valid(request) and handle(request)['result']['state'] == 'checked'
    request['operation'] = 'start-pointer-linux'
    params['idempotency_key'] = 'd' * 32
    assert validator.is_valid(request)
    first = handle(request)
    if lost_response == 'start':
        assert first['error']['code'] == 'linux_start_failed'
        assert handle(request)['error']['code'] == 'linux_start_failed'
        request['operation'] = 'reconcile-pointer-linux-start'
        assert validator.is_valid(request)
        first = handle(request)
        assert first['ok'] and not first['result']['prior_start_command_confirmed']
    assert first['ok'] and first['result']['services_started'] and not first['result']['health_verified']
    assert handle(request) == first
    assert len(manager.active) == 3
    (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged after start')
    request['operation'] = 'stop-pointer-linux'
    params.update(start_key='d' * 32, idempotency_key='e' * 32)
    assert validator.is_valid(request)
    stopped = handle(request)
    if lost_response == 'stop':
        assert stopped['error']['code'] == 'linux_stop_failed'
        assert handle(request)['error']['code'] == 'linux_stop_failed'
        request['operation'] = 'reconcile-pointer-linux-stop'
        assert validator.is_valid(request)
        stopped = handle(request)
        assert stopped['ok'] and not stopped['result']['prior_stop_command_confirmed']
    assert stopped['ok'] and stopped['result']['services_stopped'] and not manager.active
    assert handle(request) == stopped
    assert sum(call[0] == 'start' for call in manager.calls) == 3
    assert sum(call[0] == 'stop' for call in manager.calls) == 3


def test_api_publishes_pointer_units_only_with_verified_runtime(signed_pointer, tmp_path, monkeypatch):
    import shared.systemd_pointer as pointer
    from homensai_installer_client import contract_schema
    from shared.api import handle
    from shared.runtime_native import prepare_signed_linux_runtime
    from test_systemd_portable import fake_manager
    _, options, build = signed_pointer
    package, envelope, root = build('api-publish')
    directory = tmp_path / 'api-units'
    directory.mkdir(mode=0o700)
    runner, calls = fake_manager(directory)
    monkeypatch.setattr(pointer, '_run', runner)
    params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
              'expected_product_id': options['expected_product_id'], 'expected_version': '1',
              'install_root': str(root), 'modes': ['autonomous'], 'unit_dir': str(directory),
              'runtime_key': 'a' * 32, 'idempotency_key': 'b' * 32, 'reserve_bytes': 0}
    request = {'api_major': 1, 'request_id': 'pointer-publish',
               'operation': 'publish-pointer-systemd-user-units', 'params': params}
    jsonschema = pytest.importorskip('jsonschema')
    assert jsonschema.Draft202012Validator(contract_schema('request')).is_valid(request)
    caps = handle({'api_major': 1, 'request_id': 'caps', 'operation': 'capabilities', 'params': {}})['result']
    assert caps['api_minor'] >= 54 and request['operation'] in caps['mutating_operations']
    assert handle(request)['error']['code'] == 'systemd_publish_failed'
    assert not calls and not list(directory.iterdir())
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    response = handle(request)
    assert response['ok'] and response['result']['prepared_runtime_verified']
    assert response['result']['services_started'] is False and response['result']['installation_complete'] is False
    assert handle(request) == response
    params['runtime_key'] = params['idempotency_key']
    assert handle(request)['error']['code'] == 'invalid_request'


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Нужен одноразовый контейнер с user-systemd')
def test_pointer_publication_and_reload_with_real_isolated_systemd(signed_pointer, tmp_path):
    import pwd
    import subprocess
    from shared.runtime_native import prepare_signed_linux_runtime
    from shared.systemd_pointer import publish_pointer_signed_user_units
    from shared.systemd_user_load import load_pointer_signed_user_units
    user = pwd.getpwuid(os.getuid())
    if (os.getuid() == 0 or user.pw_name != 'homenstest' or user.pw_dir != '/home/homenstest'
            or not Path('/run/.containerenv').is_file() or not tmp_path.is_relative_to(Path(user.pw_dir))):
        pytest.skip('Защитные условия изолированного теста не выполнены')
    _, options, build = signed_pointer
    package, envelope, root = build('real-publish')
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    directory = Path(user.pw_dir) / '.config/systemd/user'
    directory.mkdir(parents=True, exist_ok=True)
    planned = plan_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))
    files = [directory / unit['name'] for unit in planned['units']]
    assert all(not path.exists() for path in files)
    try:
        result = publish_pointer_signed_user_units(package, envelope, str(root), **options,
            modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
            operation_id='b' * 32, reserve_bytes=0, execute=True)
        assert result['state'] == 'published' and result['prepared_runtime_verified']
        assert not result['services_started'] and all(path.is_file() for path in files)
        assert publish_pointer_signed_user_units(package, envelope, str(root), **options,
            modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
            operation_id='b' * 32, reserve_bytes=0, execute=True) == result
        loaded = load_pointer_signed_user_units(package, envelope, str(root), **options,
            modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
            operation_id='c' * 32, reserve_bytes=0)
        assert loaded['state'] == 'loaded' and not loaded['services_started']
        assert load_pointer_signed_user_units(package, envelope, str(root), **options,
            modes=('autonomous',), unit_dir=str(directory), runtime_operation_id='a' * 32,
            operation_id='c' * 32, reserve_bytes=0) == loaded
    finally:
        for path in files:
            path.unlink(missing_ok=True)
        subprocess.run(['/usr/bin/systemctl', '--user', 'daemon-reload'],
                       check=False, capture_output=True, timeout=30)


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Нужен одноразовый контейнер с user-systemd')
def test_real_pointer_start_and_stop_with_damaged_runtime(signed_pointer, tmp_path):
    import pwd
    import shutil
    import subprocess
    from shared.api import handle
    from shared.runtime_native import prepare_signed_linux_runtime
    from shared.systemd_pointer import publish_pointer_signed_user_units
    from shared.systemd_user_load import load_pointer_signed_user_units
    user = pwd.getpwuid(os.getuid())
    if (os.getuid() == 0 or user.pw_name != 'homenstest' or user.pw_dir != '/home/homenstest'
            or not Path('/run/.containerenv').is_file() or not tmp_path.is_relative_to(Path(user.pw_dir))):
        pytest.skip('Защитные условия изолированного теста не выполнены')
    payload, options, build = signed_pointer
    shutil.copy2(Path(sys.executable).resolve(), payload / 'runtime-linux/bin/python')
    (payload / 'main.py').write_text('import time\ntime.sleep(180)\n')
    package, envelope, root = build('real-start')
    prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                 reserve_bytes=0, operation_id='a' * 32, execute=True)
    directory = Path(user.pw_dir) / '.config/systemd/user'
    directory.mkdir(parents=True, exist_ok=True)
    plan = plan_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))
    names = [item['name'] for item in plan['units']]
    files = [directory / name for name in names]
    assert all(not path.exists() for path in files)
    base = ['/usr/bin/systemctl', '--user', '--no-pager', '--no-ask-password']
    try:
        publish_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',),
            unit_dir=str(directory), runtime_operation_id='a' * 32, operation_id='b' * 32,
            reserve_bytes=0, execute=True)
        load_pointer_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',),
            unit_dir=str(directory), runtime_operation_id='a' * 32, reserve_bytes=0, operation_id='c' * 32)
        params = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
                  'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
                  'expected_product_id': options['expected_product_id'], 'expected_version': '1',
                  'install_root': str(root), 'modes': ['autonomous'], 'unit_dir': str(directory),
                  'runtime_key': 'a' * 32, 'load_key': 'c' * 32, 'reserve_bytes': 0, 'idempotency_key': 'd' * 32}
        request = {'api_major': 1, 'request_id': 'real-pointer', 'operation': 'start-pointer-linux', 'params': params}
        started = handle(request)
        assert started['ok'] and started['result']['services_started'] and not started['result']['health_verified']
        assert all(subprocess.run(base + ['is-active', '--quiet', name], timeout=15).returncode == 0 for name in names)
        (root / 'runtime/lib/homensai/portable_launcher.py').write_text('damaged after start')
        request['operation'] = 'stop-pointer-linux'
        params.update(start_key='d' * 32, idempotency_key='e' * 32)
        stopped = handle(request)
        assert stopped['ok'] and stopped['result']['services_stopped']
        assert all(subprocess.run(base + ['is-active', '--quiet', name], timeout=15).returncode != 0 for name in names)
    finally:
        for name in names:
            subprocess.run(base + ['stop', name], check=False, capture_output=True, timeout=30)
        for path in files:
            path.unlink(missing_ok=True)
        subprocess.run(base + ['daemon-reload'], check=False, capture_output=True, timeout=30)
