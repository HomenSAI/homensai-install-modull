"""Signed pointer recipe planning; no service or product code is executed."""
import base64
import json
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
