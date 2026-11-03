"""Подписанные синтетические пакеты и выполнение из временного выпуска."""
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import sys
import io
import zipfile

import pytest

from shared.host_target import native_target
from shared.datahome import resolve_datahome
from shared.manifest import load_manifest
from shared.native_migration import run_signed_native_migration
from shared.package import build_package
from shared.package_prepare import prepare_native_signed_package
from shared.package_signing import sign_package
from shared.update_plan import UpdateError
from synthetic_wheel import structural_wheel

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX исполнитель')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def signed(tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = deepcopy(load_manifest(ROOT / 'examples/rent.manifest.json'))
    manifest['compatibility'] = {'targets': [native_target()]}
    manifest['product_id'] = 'homensai-test-signed-migration'
    manifest['migrations']['cmd'] = ['python', 'main.py', '@DATA_DIR@', '@MIGRATION_ID@']
    source = tmp_path / 'source'
    source.mkdir()
    files = {'main.py': b'import pathlib, sys\n(pathlib.Path(sys.argv[1]) / sys.argv[2]).write_text("ok")\n',
             'locks/windows.lock': b'fake windows lock', 'locks/linux.lock': b'fake linux lock',
             'vendor/demo-0-py3-none-any.whl': structural_wheel()}
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
    installation = tmp_path / 'installation'
    home = resolve_datahome(manifest, installation)
    home.releases.mkdir(parents=True)
    home.roots['data'].mkdir(parents=True)
    marker = home.roots['data'] / 'schema-2'
    release = home.releases / 'app-2'
    prepare_native_signed_package(package, envelope, release, trusted_public_key=public,
                                  expected_product_id=manifest['product_id'], expected_version='2')
    interpreter = Path(sys.executable).resolve()
    params = {'package_path': str(package), 'envelope': envelope, 'trusted_public_key': public,
              'expected_product_id': manifest['product_id'], 'expected_version': '2',
              'expected_manifest': manifest, 'install_root': str(installation), 'migration_id': 'schema-2',
              'release_root': str(release), 'interpreter': str(interpreter),
              'interpreter_sha256': sha256(interpreter.read_bytes()).hexdigest(), 'timeout': 5}
    return params, marker


def test_signed_command_is_verified_and_run(signed):
    params, marker = signed
    run_signed_native_migration(**params)
    assert not marker.exists()
    run_signed_native_migration(**params, execute=True)
    assert marker.read_text() == 'ok'


def test_signed_unsafe_wheel_stops_direct_migration(signed, tmp_path):
    params, marker = signed
    manifest = deepcopy(params['expected_manifest'])
    wheel_path = 'vendor/demo-0-py3-none-any.whl'
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('../escape.py', 'unsafe')
    (tmp_path / 'source' / wheel_path).write_bytes(data.getvalue())
    manifest['runtime']['vendor_wheels'][0]['sha256'] = sha256(data.getvalue()).hexdigest()
    package = tmp_path / 'unsafe.zip'
    build_package(manifest, '2', tmp_path / 'source', package)
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='2')
    installation = tmp_path / 'unsafe-installation'
    home = resolve_datahome(manifest, installation)
    home.releases.mkdir(parents=True)
    home.roots['data'].mkdir(parents=True)
    release = home.releases / 'app-2'
    prepare_native_signed_package(package, envelope, release, trusted_public_key=public,
                                  expected_product_id=manifest['product_id'], expected_version='2')
    params.update(package_path=str(package), envelope=envelope, trusted_public_key=public,
                  expected_manifest=manifest, install_root=str(installation), release_root=str(release))
    with pytest.raises(UpdateError, match='Не удалось подтвердить подписанную миграцию'):
        run_signed_native_migration(**params, execute=True)
    assert not marker.exists()
    assert not (home.roots['data'] / 'schema-2').exists()


def test_same_signed_package_works_in_another_installation(signed, tmp_path):
    params, original_marker = signed
    run_signed_native_migration(**params, execute=True)
    assert original_marker.read_text() == 'ok'
    original_marker.write_text('first-installation')
    other = tmp_path / 'other-installation'
    home = resolve_datahome(params['expected_manifest'], other)
    home.releases.mkdir(parents=True)
    home.roots['data'].mkdir(parents=True)
    release = home.releases / 'app-2'
    prepare_native_signed_package(params['package_path'], params['envelope'], release,
                                  trusted_public_key=params['trusted_public_key'],
                                  expected_product_id=params['expected_product_id'], expected_version='2')
    params.update(install_root=str(other), release_root=str(release))
    run_signed_native_migration(**params, execute=True)
    assert (home.roots['data'] / 'schema-2').read_text() == 'ok'
    assert original_marker.read_text() == 'first-installation'


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


@pytest.mark.parametrize('command', [
    ['python', 'not-in-package.py'],
    ['python', 'main.py', 'prefix@DATA_DIR@', '@MIGRATION_ID@'],
    ['python', 'main.py', '@DATA_DIR@', '@DATA_DIR@', '@MIGRATION_ID@'],
])
def test_rejects_unlisted_script_or_ambiguous_templates(signed, tmp_path, command):
    from shared.package_signing import verify_signed_package
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    params, marker = signed
    manifest = verify_signed_package(params['package_path'], params['envelope'],
                                     trusted_public_key=params['trusted_public_key'],
                                     expected_product_id=params['expected_product_id'],
                                     expected_version=params['expected_version'])['manifest']
    manifest['migrations']['cmd'] = command
    package = tmp_path / 'other.zip'
    build_package(manifest, '2', tmp_path / 'source', package)
    private = ed.Ed25519PrivateKey.generate()
    params['package_path'] = str(package)
    params['trusted_public_key'] = private.public_key().public_bytes(serialization.Encoding.Raw,
                                                                     serialization.PublicFormat.Raw)
    params['envelope'] = sign_package(package, private_key=private,
                                      expected_product_id=params['expected_product_id'], expected_version='2')
    params['expected_manifest'] = manifest
    with pytest.raises(UpdateError):
        run_signed_native_migration(**params, execute=True)
    assert not marker.exists()


@pytest.mark.parametrize('case', ['different-manifest', 'different-installation', 'invalid-id', 'missing-id'])
def test_binding_rejects_wrong_plan_or_installation(signed, tmp_path, case):
    params, marker = signed
    if case == 'different-manifest':
        params['expected_manifest'] = deepcopy(params['expected_manifest'])
        params['expected_manifest']['update']['health_wait'] += 1
    elif case == 'different-installation':
        params['install_root'] = str(tmp_path / 'other')
    elif case == 'invalid-id':
        params['migration_id'] = '../outside'
    else:
        params['migration_id'] = None
    with pytest.raises(UpdateError):
        run_signed_native_migration(**params, execute=True)
    assert not marker.exists()
