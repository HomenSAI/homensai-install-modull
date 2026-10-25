"""Scaffold создаёт реальные hash-пины, но не утверждает готовность synthetic product."""

import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from homensai_install import __version__
from homensai_install.api.model import read_manifest, target_model
from homensai_install.errors import InstallerError
from homensai_install.scaffold import scaffold_product
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest


def wheel(path, name, version):
    """Настоящий маленький wheel-файл fixture; не production installer distribution."""
    normalized = name.replace('-', '_')
    prefix = normalized + '-' + version + '.dist-info/'
    files = {normalized + '/__init__.py': b'VALUE = 1\n',
             prefix + 'METADATA': f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n'.encode(),
             prefix + 'WHEEL': b'Wheel-Version: 1.0\nGenerator: synthetic-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    records = []
    for filename, content in files.items():
        hashed = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip('=')
        records.append(f'{filename},sha256={hashed},{len(content)}')
    records.append(prefix + 'RECORD,,')
    files[prefix + 'RECORD'] = ('\n'.join(records) + '\n').encode()
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)


@pytest.fixture
def complete_spec(tmp_path):
    payload = tmp_path / 'source-payload'
    for name in ('runtime','locks','vendor','legal'):
        (payload / name).mkdir(parents=True, exist_ok=True)
    runtime = Path('/usr/bin/python3')
    if not runtime.exists():
        pytest.skip('Linux synthetic fixture needs system Python binary; no production runtime acceptance')
    shutil.copy2(runtime.resolve(), payload / 'runtime/python')
    (payload / 'runtime/python').chmod(0o700)
    (payload / 'product_app.py').write_text('raise RuntimeError("Product fixture must not execute in scaffold")\n')
    product_wheel = payload / 'vendor/product_fixture-1.0-py3-none-any.whl'
    wheel(product_wheel, 'product-fixture', '1.0')
    actual_hash = hashlib.sha256(product_wheel.read_bytes()).hexdigest()
    for name in ('windows','linux'):
        (payload / 'locks' / (name + '.lock')).write_text('product-fixture==1.0 --hash=sha256:' + actual_hash + '\n')
    for locale in ('ru','en','de'):
        (payload / 'legal' / (locale + '.txt')).write_text('Synthetic test terms: ' + locale + '\n')
    installer = tmp_path / ('homensai_install_module-' + __version__ + '-py3-none-any.whl')
    wheel(installer, 'homensai-install-module', __version__)
    return {'product_id': '7-product', 'product_name': 'Synthetic product', 'release': '1.0',
            'target': target_model(), 'payload_dir': str(payload), 'runtime_executable': 'runtime/python',
            'installer_wheel': str(installer),
            'runtime': {'python': '3.14', 'lock_files': {name: 'locks/' + name + '.lock' for name in ('windows','linux')},
                        'vendor_wheels': ['vendor/' + product_wheel.name]},
            'commands': {name: ['python','-B','product_app.py',name] for name in ('web','migrate','backup','restore','health')},
            'web': {'host': '127.0.0.1', 'port': 8809, 'healthz': '/healthz'}}


def test_missing_inputs_produce_invalid_production_draft_and_no_invented_hashes(tmp_path):
    destination = tmp_path / 'draft'
    result = scaffold_product(destination)
    assert result['status'] == 'BLOCKED' and result['readiness'] == 'DRAFT'
    assert result['production_ready'] is False
    assert {'TARGET_REQUIRED','PAYLOAD_REQUIRED','INSTALLER_WHEEL_REQUIRED'} <= set(result['input_blockers'])
    assert not (destination / 'product.manifest.json').exists()
    assert not (destination / 'payload.inventory.unsigned.json').exists()
    value = json.loads((destination / 'product.manifest.draft.json').read_text())
    with pytest.raises(InstallerError): read_manifest(value)
    assert value['manifest_template']['runtime']['lock_files']['linux']['sha256'] is None
    assert not (destination / 'payload').exists()


def test_complete_inputs_use_actual_files_hashes_and_remain_unsigned(complete_spec, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Scaffold must not execute untrusted runtime/product hooks')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    destination = tmp_path / 'integration'
    result = scaffold_product(destination, complete_spec)
    assert result['status'] == 'SCAFFOLDED' and result['readiness'] == 'REVIEW_REQUIRED'
    assert not result['production_ready'] and not result['product_hooks_executed']
    manifest = read_manifest(destination / 'product.manifest.json')
    for artifact in [*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]:
        assert artifact['sha256'] == hashlib.sha256((destination / 'payload' / artifact['file']).read_bytes()).hexdigest()
    inventory = json.loads((destination / 'payload.inventory.unsigned.json').read_text())
    assert inventory['manifest_sha256'] == hash_manifest(manifest)
    assert inventory['development'] is True
    assert inventory['files']['runtime/python'] == hashlib.sha256((destination / 'payload/runtime/python').read_bytes()).hexdigest()
    installer_hash = hashlib.sha256(Path(complete_spec['installer_wheel']).read_bytes()).hexdigest()
    assert installer_hash in (destination / 'installer-wheel.pin.txt').read_text()
    assert 'PUBLISHER_SIGNATURE_REQUIRED' in result['release_blockers']
    assert 'TARGET_INSTALL_UPDATE_RESTORE_ROLLBACK_NOT_TESTED' in result['release_blockers']
    assert not (destination / 'payload.signature.json').exists()
    assert not (destination / 'trust-config.json').exists()


def test_copied_payload_inventory_can_be_cryptographically_bound_after_explicit_signing(complete_spec, tmp_path):
    destination = tmp_path / 'integration'
    scaffold_product(destination, complete_spec)
    manifest = read_manifest(destination / 'product.manifest.json')
    inventory_file = destination / 'payload.inventory.unsigned.json'
    inventory = json.loads(inventory_file.read_text())
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signature = {'algorithm': 'ed25519', 'key_id': 'synthetic',
                 'signature': base64.b64encode(private.sign(canonical_bytes(inventory))).decode()}
    signature_file = tmp_path / 'ephemeral-signature.json'
    signature_file.write_bytes(canonical_bytes(signature))
    verified = SignedPayloadVerifier({'synthetic': public}, allow_test_keys=True)(manifest, complete_spec['target'],
        {'payload_dir': str(destination / 'payload'), 'inventory_file': str(inventory_file),
         'signature_file': str(signature_file), 'release': '1.0', 'runtime_executable': 'runtime/python'})
    assert verified == inventory['files']


def test_existing_destination_and_symlink_are_not_overwritten(tmp_path):
    destination = tmp_path / 'existing'
    destination.mkdir()
    sentinel = destination / 'foreign.txt'
    sentinel.write_text('preserve')
    with pytest.raises(InstallerError) as caught:
        scaffold_product(destination)
    assert caught.value.code == 'OPERATION_CONFLICT' and sentinel.read_text() == 'preserve'
    linked = tmp_path / 'linked'
    linked.symlink_to(destination, target_is_directory=True)
    with pytest.raises(InstallerError): scaffold_product(linked)


@pytest.mark.parametrize('missing', ['runtime/python', 'locks/linux.lock', 'vendor/product_fixture-1.0-py3-none-any.whl', 'legal/de.txt'])
def test_missing_real_artifact_stays_draft(complete_spec, tmp_path, missing):
    (Path(complete_spec['payload_dir']) / missing).unlink()
    destination = tmp_path / 'draft'
    result = scaffold_product(destination, complete_spec)
    assert result['status'] == 'BLOCKED'
    assert not (destination / 'product.manifest.json').exists()
    assert not (destination / 'payload').exists()


def test_claimed_hashes_are_recomputed_from_real_artifacts(complete_spec, tmp_path):
    first = tmp_path / 'first'
    scaffold_product(first, complete_spec)
    manifest = json.loads((first / 'product.manifest.json').read_text())
    for artifact in [*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]:
        artifact['sha256'] = '0' * 64
    value = {key: item for key, item in complete_spec.items() if key not in ('runtime','commands','web')}
    value['manifest'] = manifest
    second = tmp_path / 'second'
    assert scaffold_product(second, value)['status'] == 'SCAFFOLDED'
    fixed = read_manifest(second / 'product.manifest.json')
    assert fixed['runtime']['lock_files']['linux']['sha256'] != '0' * 64


def test_invalid_wheel_and_runtime_format_cannot_become_valid_release(complete_spec, tmp_path):
    payload = Path(complete_spec['payload_dir'])
    (payload / 'vendor/product_fixture-1.0-py3-none-any.whl').write_bytes(b'not a wheel')
    (payload / 'runtime/python').write_text('#!/bin/sh\nexit 0\n')
    result = scaffold_product(tmp_path / 'blocked', complete_spec)
    assert {'INVALID_PRODUCT_WHEEL','NATIVE_RUNTIME_FORMAT_REQUIRED'} <= set(result['input_blockers'])


def test_wrong_installer_wheel_version_is_blocked(complete_spec, tmp_path):
    wrong = tmp_path / 'homensai_install_module-99.0-py3-none-any.whl'
    wheel(wrong, 'homensai-install-module', '99.0')
    complete_spec['installer_wheel'] = str(wrong)
    result = scaffold_product(tmp_path / 'blocked', complete_spec)
    assert 'INSTALLER_WHEEL_VERSION_MISMATCH' in result['input_blockers']


def test_secret_configuration_and_symlink_payload_are_not_copied(complete_spec, tmp_path):
    payload = Path(complete_spec['payload_dir'])
    (payload / '.env').write_text('PRIVATE_VALUE=synthetic-only\n')
    result = scaffold_product(tmp_path / 'blocked', complete_spec)
    assert 'PAYLOAD_SECRET_CONFIGURATION_REJECTED' in result['input_blockers']
    assert not (tmp_path / 'blocked/payload/.env').exists()
    (payload / '.env').unlink()
    (payload / 'outside-link').symlink_to(tmp_path / 'elsewhere')
    result = scaffold_product(tmp_path / 'blocked-link', complete_spec)
    assert result['status'] == 'BLOCKED'


def test_escape_paths_unknown_options_and_payload_overlap_fail_before_writes(complete_spec, tmp_path):
    for value in ({'verified': True}, [], {'runtime': {'unknown': True}},
                  {**complete_spec, 'runtime_executable': '../outside'}):
        with pytest.raises(InstallerError): scaffold_product(tmp_path / 'not-written', value)
        assert not (tmp_path / 'not-written').exists()
    with pytest.raises(InstallerError):
        scaffold_product(Path(complete_spec['payload_dir']) / 'nested-output', complete_spec)


def test_generated_sdk_client_is_pinned_and_has_no_import_effects(complete_spec, tmp_path):
    destination = tmp_path / 'integration'
    scaffold_product(destination, complete_spec)
    client = destination / 'integration-client.py'
    compile(client.read_text(), str(client), 'exec')
    spec = importlib.util.spec_from_file_location('generated_product_client', client)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PINNED_VERSION == __version__
    assert callable(module.prepare_install) and callable(module.submit_reviewed_plan)
    assert not (destination / 'local-state').exists()
    assert json.loads((destination / 'installer-discovery.json').read_text())['api_version'] == '1.0'
    assert (destination / 'docs/INTEGRATION_GUIDE.md').exists()
    assert (destination / 'api/openapi.yaml').exists()
    assert (destination / 'api/schemas/manifest.schema.json').exists()
    assert 'NativeEngine' in client.read_text() and 'JobPilot' not in client.read_text()
    draft = destination / 'product-adapter.draft.py'
    compile(draft.read_text(), str(draft), 'exec')
    assert 'PRODUCT_ADAPTER_NOT_IMPLEMENTED' in draft.read_text()


def test_cli_scaffold_draft_has_explicit_blocked_exit_code(tmp_path, capsys):
    from homensai_install.cli import main
    destination = tmp_path / 'draft'
    assert main(['init-adapter','--output-dir',str(destination),'--json']) == 2
    result = json.loads(capsys.readouterr().out)['result']
    assert result['status'] == 'BLOCKED' and not result['production_ready']
