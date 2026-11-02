"""Подписанные синтетические пакеты и выполнение из временного выпуска."""
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import sys

import pytest

from shared.host_target import native_target
from shared.manifest import load_manifest
from shared.native_migration import run_signed_native_migration
from shared.package import build_package
from shared.package_prepare import prepare_native_signed_package
from shared.package_signing import sign_package
from shared.update_plan import UpdateError

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX исполнитель')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def signed(tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = deepcopy(load_manifest(ROOT / 'examples/rent.manifest.json'))
    manifest['compatibility'] = {'targets': [native_target()]}
    manifest['product_id'] = 'homensai-test-signed-migration'
    marker = tmp_path / 'migration-result'
    manifest['migrations']['cmd'] = ['python', 'main.py', str(marker)]
    source = tmp_path / 'source'
    source.mkdir()
    files = {'main.py': b'import pathlib, sys\npathlib.Path(sys.argv[1]).write_text("ok")\n',
             'locks/windows.lock': b'fake windows lock', 'locks/linux.lock': b'fake linux lock',
             'vendor/demo-0-py3-none-any.whl': b'fake wheel'}
    for name, data in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest['runtime']['lock_files'] = {
        name: {'file': 'locks/' + name + '.lock', 'sha256': sha256(files['locks/' + name + '.lock']).hexdigest()}
        for name in ('windows', 'linux')}
    wheel = 'vendor/demo-0-py3-none-any.whl'
    manifest['runtime']['vendor_wheels'] = [{'file': wheel, 'sha256': sha256(files[wheel]).hexdigest()}]
    package = tmp_path / 'release.zip'
    build_package(manifest, '2', source, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='2')
    release = tmp_path / 'release'
    prepare_native_signed_package(package, envelope, release, trusted_public_key=public,
                                  expected_product_id=manifest['product_id'], expected_version='2')
    interpreter = Path(sys.executable).resolve()
    params = {'package_path': str(package), 'envelope': envelope, 'trusted_public_key': public,
              'expected_product_id': manifest['product_id'], 'expected_version': '2',
              'release_root': str(release), 'interpreter': str(interpreter),
              'interpreter_sha256': sha256(interpreter.read_bytes()).hexdigest(), 'timeout': 5}
    return params, marker


def test_signed_command_is_verified_and_run(signed):
    params, marker = signed
    run_signed_native_migration(**params)
    assert not marker.exists()
    run_signed_native_migration(**params, execute=True)
    assert marker.read_text() == 'ok'


@pytest.mark.parametrize('tampering', ['wrong-key', 'wrong-product', 'wrong-version', 'script', 'extra', 'missing'])
def test_tampering_prevents_execution(signed, tampering):
    params, marker = signed
    root = Path(params['release_root'])
    if tampering == 'wrong-key':
        params['trusted_public_key'] = b'\0' * 32
    elif tampering == 'wrong-product':
        params['expected_product_id'] = 'wrong-product'
    elif tampering == 'wrong-version':
        params['expected_version'] = '3'
    elif tampering == 'script':
        (root / 'main.py').write_text('pass\n')
    elif tampering == 'extra':
        (root / 'extra').write_text('extra')
    else:
        (root / 'locks/linux.lock').unlink()
    with pytest.raises(UpdateError):
        run_signed_native_migration(**params, execute=True)
    assert not marker.exists()


def test_signed_package_must_support_current_native_target(signed, monkeypatch):
    params, marker = signed
    monkeypatch.setattr('shared.native_migration.native_target',
                        lambda: {'os': 'windows', 'architecture': 'x86_64', 'execution': 'native'})
    with pytest.raises(UpdateError):
        run_signed_native_migration(**params, execute=True)
    assert not marker.exists()


def test_symlinked_release_directory_is_rejected(signed):
    params, marker = signed
    root = Path(params['release_root'])
    displaced = root.parent / 'moved-locks'
    (root / 'locks').rename(displaced)
    (root / 'locks').symlink_to(displaced, target_is_directory=True)
    with pytest.raises(UpdateError):
        run_signed_native_migration(**params, execute=True)
    assert not marker.exists()


def test_signed_command_must_point_to_an_included_python_file(signed, tmp_path):
    from shared.package_signing import verify_signed_package
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    params, marker = signed
    manifest = verify_signed_package(params['package_path'], params['envelope'],
                                     trusted_public_key=params['trusted_public_key'],
                                     expected_product_id=params['expected_product_id'],
                                     expected_version=params['expected_version'])['manifest']
    manifest['migrations']['cmd'] = ['python', 'not-in-package.py']
    package = tmp_path / 'other.zip'
    build_package(manifest, '2', tmp_path / 'source', package)
    private = ed.Ed25519PrivateKey.generate()
    params['package_path'] = str(package)
    params['trusted_public_key'] = private.public_key().public_bytes(serialization.Encoding.Raw,
                                                                     serialization.PublicFormat.Raw)
    params['envelope'] = sign_package(package, private_key=private,
                                      expected_product_id=params['expected_product_id'], expected_version='2')
    with pytest.raises(UpdateError):
        run_signed_native_migration(**params, execute=True)
    assert not marker.exists()
