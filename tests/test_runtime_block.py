"""Синтетический runtime-блок, доверие к заявленному источнику не предполагается."""
import hashlib
import base64
import json
from pathlib import Path
import zipfile

import pytest
from synthetic_wheel import structural_wheel

from shared.runtime_block import RuntimeBlockError, build_runtime_block, verify_runtime_block

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def candidate(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    files = {'bin/python': b'#!/bin/sh\nexit 91\n', 'lib/data.txt': b'synthetic data',
             'licenses/LICENSE': b'synthetic license notice'}
    for name, body in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    lock = {'format': 1, 'target': {'os': 'linux', 'architecture': 'x86_64'},
            'python_version': '3.14.10', 'origin': 'https://example.org/runtime/1', 'license': 'PSF-2.0',
            'license_files': ['licenses/LICENSE'], 'executables': ['bin/python'],
            'files': [{'path': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                      for name, body in sorted(files.items())]}
    lock_path = tmp_path / 'runtime-lock.json'
    lock_path.write_text(json.dumps(lock))
    return source, lock_path, lock


def test_build_and_verify_offline_without_running_runtime(candidate, tmp_path):
    source, lock_path, lock = candidate
    block = tmp_path / 'runtime-linux'
    result = build_runtime_block(source, lock_path, block)
    assert result['file_count'] == 3 and result['runtime_executed'] is False and result['origin_verified'] is False
    assert verify_runtime_block(block) == lock
    assert json.loads((block / 'runtime-v1.json').read_text())['executables'] == ['bin/python']
    assert (block / 'licenses/LICENSE').read_text() == 'synthetic license notice'
    with pytest.raises(RuntimeBlockError):
        build_runtime_block(source, lock_path, block)


@pytest.mark.parametrize('mutation', ['file', 'extra', 'missing', 'license', 'recipe', 'marker', 'symlink'])
def test_modified_block_is_rejected(candidate, tmp_path, mutation):
    source, lock_path, _ = candidate
    block = tmp_path / 'runtime-linux'
    build_runtime_block(source, lock_path, block)
    path = block / 'lib/data.txt'
    if mutation == 'file':
        path.write_text('modified')
    elif mutation == 'extra':
        (block / 'extra').write_text('unexpected')
    elif mutation == 'missing':
        path.unlink()
    elif mutation == 'license':
        (block / 'licenses/LICENSE').write_text('changed')
    elif mutation == 'recipe':
        (block / 'runtime-v1.json').write_text('{}')
    elif mutation == 'marker':
        (block / '.homensai-runtime-incomplete').write_text('incomplete\n')
    else:
        path.unlink()
        path.symlink_to(block / 'bin/python')
    with pytest.raises(RuntimeBlockError):
        verify_runtime_block(block)


@pytest.mark.parametrize('mutation', ['hash', 'artifact_hash', 'evidence_hash', 'origin', 'origin_query', 'empty_license', 'license_executable', 'unknown_field', 'missing_python'])
def test_invalid_lock_rejected_before_destination_created(candidate, tmp_path, mutation):
    source, lock_path, lock = candidate
    if mutation == 'hash':
        lock['files'][0]['sha256'] = '0' * 64
    elif mutation == 'artifact_hash':
        lock['artifact_sha256'] = 'invalid'
    elif mutation == 'evidence_hash':
        lock['evidence_sha256'] = 'invalid'
    elif mutation == 'origin':
        lock['origin'] = 'http://example.org/not-authenticated'
    elif mutation == 'origin_query':
        lock['origin'] = 'https://example.org/archive?token=secret'
    elif mutation == 'empty_license':
        lock['license_files'] = []
    elif mutation == 'license_executable':
        lock['license_files'] = ['bin/python']
    elif mutation == 'unknown_field':
        lock['unexpected'] = True
    else:
        lock['executables'] = ['lib/data.txt']
    lock_path.write_text(json.dumps(lock))
    block = tmp_path / 'runtime-linux'
    with pytest.raises(RuntimeBlockError):
        build_runtime_block(source, lock_path, block)
    assert not block.exists()


def test_failed_copy_retains_marker_and_blocks_package(candidate, tmp_path, monkeypatch):
    from shared import runtime_block
    from shared.package import PackageError, _files

    source, lock_path, _ = candidate
    original = runtime_block._write
    def fail(path, data):
        if path.name == runtime_block.RECIPE:
            raise OSError('simulated interruption')
        original(path, data)
    monkeypatch.setattr(runtime_block, '_write', fail)
    block = tmp_path / 'runtime-linux'
    with pytest.raises(RuntimeBlockError):
        build_runtime_block(source, lock_path, block)
    assert (block / '.homensai-runtime-incomplete').exists()
    with pytest.raises(PackageError):
        _files(tmp_path)


def test_package_inventory_rechecks_runtime_block(candidate, tmp_path):
    from shared.package import PackageError, _files

    source, lock_path, _ = candidate
    payload = tmp_path / 'payload'
    payload.mkdir()
    block = payload / 'runtime-linux'
    build_runtime_block(source, lock_path, block)
    inventory, _ = _files(payload)
    assert 'runtime-linux/runtime-provenance-v1.json' in {item['path'] for item in inventory}
    (block / 'licenses/LICENSE').write_text('tampered')
    with pytest.raises(PackageError, match='runtime'):
        _files(payload)


@pytest.mark.parametrize('mutation', ['license', 'missing', 'extra', 'recipe', 'bad_provenance'])
def test_zip_rejects_inconsistent_runtime_even_when_signed(candidate, tmp_path, mutation):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    from shared.archive import _info
    from shared.manifest import load_manifest
    from shared.package import PackageError, verify_package
    from shared.package_signing import _canonical, verify_signed_package

    source, lock_path, _ = candidate
    block = tmp_path / 'runtime-linux'
    build_runtime_block(source, lock_path, block)
    members = {'runtime-linux/' + p.relative_to(block).as_posix(): p.read_bytes()
               for p in block.rglob('*') if p.is_file()}
    if mutation == 'license':
        members['runtime-linux/licenses/LICENSE'] = b'changed but rehashed'
    elif mutation == 'missing':
        del members['runtime-linux/licenses/LICENSE']
    elif mutation == 'extra':
        members['runtime-linux/unlisted'] = b'extra but rehashed'
    elif mutation == 'recipe':
        members['runtime-linux/runtime-v1.json'] = b'{}'
    else:
        members['runtime-linux/runtime-provenance-v1.json'] = b'{}'
    product = load_manifest(ROOT / 'examples/jobpilot.manifest.json')
    metadata = {'format': 1, 'product_id': product['product_id'], 'version': '1', 'manifest': product,
                'files': [{'path': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                          for name, body in sorted(members.items())]}
    package = tmp_path / 'forged.zip'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr(_info('package.json'), json.dumps(metadata).encode())
        for name, body in sorted(members.items()):
            archive.writestr(_info('payload/' + name), body)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    with pytest.raises(PackageError, match='runtime'):
        verify_package(package, expected_sha256=digest)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    claims = {'format': 1, 'algorithm': 'Ed25519', 'product_id': product['product_id'],
              'version': '1', 'sha256': digest}
    signature = base64.b64encode(private.sign(_canonical(claims))).decode('ascii')
    envelope = json.dumps({**claims, 'signature': signature}).encode()
    with pytest.raises(PackageError, match='runtime'):
        verify_signed_package(package, envelope, trusted_public_key=public,
                              expected_product_id=product['product_id'], expected_version='1')


def test_built_block_can_be_signed_and_prepared(candidate, tmp_path, monkeypatch):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    from shared.fresh_prepare import prepare_fresh_native
    from shared.manifest import load_manifest
    from shared.package import build_package
    from shared.package_signing import sign_package
    from shared.preflight import NAMES
    from shared.runtime_native import prepare_signed_linux_runtime, verify_signed_linux_runtime

    target = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}
    monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: dict(target))
    monkeypatch.setattr('shared.package_prepare.native_target', lambda: dict(target))
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [target]}
    payload = tmp_path / 'payload'
    payload.mkdir()
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        body = (structural_wheel() if artifact['file'].endswith('.whl')
                else ('synthetic dependency ' + str(index)).encode())
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        artifact['sha256'] = hashlib.sha256(body).hexdigest()
    source, lock_path, _ = candidate
    build_runtime_block(source, lock_path, payload / 'runtime-linux')
    package = tmp_path / 'signed.zip'
    build_package(manifest, '1', payload, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    root = tmp_path / 'install'
    prepare_fresh_native(package, envelope, root, **options, required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    prepared = prepare_signed_linux_runtime(package, envelope, str(root), **options,
                                             execute=True, operation_id='a' * 32, reserve_bytes=0)
    assert prepared['state'] == 'prepared' and prepared['runtime_executed'] is False
    assert verify_signed_linux_runtime(package, envelope, str(root), **options,
                                       operation_id='a' * 32, reserve_bytes=0)['state'] == 'verified'
    assert (root / 'runtime/licenses/LICENSE').read_text() == 'synthetic license notice'
