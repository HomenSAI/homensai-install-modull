"""Перечень состава подписанного выпуска на синтетических данных; сеть не используется."""
from copy import deepcopy
from hashlib import sha256
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.preflight import NAMES
from shared.release_inventory import InventoryError, release_inventory

ROOT = Path(__file__).resolve().parents[1]
LOCK = (b'--require-hashes\n--only-binary=:all:\n'
        b'Fictional_Demo==1.2.0 \\\n    --hash=sha256:' + b'a' * 64 + b'\n'
        b'other-lib==0.9 --hash=sha256:' + b'b' * 64 + b' --hash=sha256:' + b'c' * 64 + b'\n')


def _wheel():
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        archive.writestr('fictional_demo/__init__.py', '# synthetic, not executed')
        archive.writestr('fictional_demo-1.2.0.dist-info/METADATA',
                         'Metadata-Version: 2.4\nName: fictional-demo\nVersion: 1.2.0\n'
                         'License-Expression: MIT\nClassifier: License :: OSI Approved :: MIT License\n'
                         'Classifier: Programming Language :: Python\n')
        archive.writestr('fictional_demo-1.2.0.dist-info/licenses/LICENSE', 'Synthetic MIT text')
    return body.getvalue()


@pytest.fixture
def signed(tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['paths'] = {name: name for name in NAMES}
    payload = tmp_path / 'payload'
    payload.mkdir()
    lock_files = manifest['runtime']['lock_files']
    for index, artifact in enumerate([*lock_files.values(), *manifest['runtime']['vendor_wheels']]):
        if artifact['file'].endswith('.whl'):
            data = _wheel()
        elif artifact is lock_files['linux']:
            data = LOCK
        else:
            data = ('not a strict lock ' + str(index)).encode()
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        artifact['sha256'] = sha256(data).hexdigest()
    (payload / 'demo.txt').write_text('synthetic product file')
    package = tmp_path / 'bundle.zip'
    build_package(deepcopy(manifest), '1', payload, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'],
                            expected_version='1')
    return package, envelope, public, manifest


def test_inventory_lists_files_locks_and_wheel_licenses(signed):
    package, envelope, public, manifest = signed
    result = release_inventory(package, envelope, trusted_public_key=public,
                               expected_product_id=manifest['product_id'], expected_version='1')
    assert result['format'] == 'homensai-release-inventory/1' and result['package_signature_verified']
    assert result['package_sha256'] == sha256(package.read_bytes()).hexdigest()
    assert result['publisher_key_sha256'] == sha256(public).hexdigest()
    assert any(item['path'] == 'demo.txt' for item in result['files'])
    locks = {item['platform']: item for item in result['lock_files']}
    assert locks['linux']['pinned_packages'] == [
        {'name': 'fictional-demo', 'version': '1.2.0', 'sha256': ['a' * 64]},
        {'name': 'other-lib', 'version': '0.9', 'sha256': ['b' * 64, 'c' * 64]}]
    assert locks['windows']['pinned_packages'] is None
    wheel = result['wheels'][0]
    assert (wheel['name'], wheel['version'], wheel['license_expression']) == ('fictional-demo', '1.2.0', 'MIT')
    assert wheel['license_classifiers'] == ['License :: OSI Approved :: MIT License']
    assert wheel['license_files'] == [{'path': 'fictional_demo-1.2.0.dist-info/licenses/LICENSE',
                                       'sha256': sha256(b'Synthetic MIT text').hexdigest()}]
    for flag in ('dependency_completeness_verified', 'licenses_reviewed', 'vulnerabilities_checked',
                 'malicious_code_scanned', 'publisher_trust_verified'):
        assert result[flag] is False
    assert json.loads(json.dumps(result)) == result


def test_inventory_refuses_wrong_key_or_changed_package(signed):
    package, envelope, public, manifest = signed
    options = dict(expected_product_id=manifest['product_id'], expected_version='1')
    with pytest.raises(InventoryError):
        release_inventory(package, envelope, trusted_public_key=b'\0' * 32, **options)
    with pytest.raises(InventoryError):
        release_inventory(package, envelope, trusted_public_key=public,
                          expected_product_id=manifest['product_id'], expected_version='2')
    package.write_bytes(package.read_bytes() + b'tail')
    with pytest.raises(InventoryError):
        release_inventory(package, envelope, trusted_public_key=public, **options)


def test_cli_writes_new_file_only(signed, tmp_path):
    package, envelope, public, manifest = signed
    (tmp_path / 'sig.json').write_bytes(envelope)
    (tmp_path / 'key.bin').write_bytes(public)
    output = tmp_path / 'inventory.json'
    command = [sys.executable, '-B', '-m', 'shared.release_inventory', '--package', str(package),
               '--envelope-file', str(tmp_path / 'sig.json'), '--public-key-file', str(tmp_path / 'key.bin'),
               '--product-id', manifest['product_id'], '--version', '1', '--output', str(output)]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0 and json.loads(output.read_text())['product_id'] == manifest['product_id']
    before = output.read_bytes()
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert second.returncode == 1 and output.read_bytes() == before and second.stdout == ''
