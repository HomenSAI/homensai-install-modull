"""Явно запускаемый E2E с настоящими wheels; входные файлы задаёт оператор."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from shared.crypto_wheelhouse import build_wheelhouse
from shared.manifest import load_manifest
from shared.package import PackageError, build_package
from shared.package_prepare import prepare_signed_package
from shared.package_signing import sign_package, verify_signed_package


@pytest.mark.skipif(not os.environ.get('HOMENSAI_CRYPTO_WHEELS'),
                    reason='Для офлайн E2E нужен заранее проверяемый каталог wheels')
def test_signed_bundle_prepares_and_installs_from_extracted_wheels(tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    source = Path(os.environ['HOMENSAI_CRYPTO_WHEELS'])
    payload = tmp_path / 'payload'
    fragment = build_wheelhouse(source, payload)
    (payload / 'demo.txt').write_text('synthetic product', encoding='utf-8')
    root = Path(__file__).resolve().parents[1]
    manifest = load_manifest(root / 'examples/rent.manifest.json')
    manifest['runtime']['lock_files'] = fragment['lock_files']
    manifest['runtime']['vendor_wheels'] = fragment['vendor_wheels']
    package = tmp_path / 'bundle.zip'
    receipt = build_package(manifest, '1.0', payload, package)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    trusted = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'],
               'expected_version': '1.0'}
    envelope = sign_package(package, private_key=key,
                            expected_product_id=manifest['product_id'], expected_version='1.0')
    metadata = verify_signed_package(package, envelope, **trusted)
    assert len(fragment['vendor_wheels']) == 5
    assert len(fragment['license_files']) == 9
    assert receipt['sha256'] and metadata['manifest'] == manifest
    extracted = tmp_path / 'prepared'
    prepare_signed_package(package, envelope, extracted, **trusted)
    assert (extracted / 'demo.txt').read_text() == 'synthetic product'
    for entry in fragment['license_files']:
        assert (extracted / entry['file']).read_bytes() == (payload / entry['file']).read_bytes()
    # Этот вызов ставит только в временный каталог и не обращается к индексу.
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps',
                    '--find-links', str(extracted / 'vendor'), '--target', str(tmp_path / 'site'),
                    '-r', str(extracted / 'locks/linux.lock')], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    subprocess.run([sys.executable, '-S', '-c',
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; "
                    "k=Ed25519PrivateKey.generate(); k.public_key().verify(k.sign(b'demo'),b'demo')",
                    str(tmp_path / 'site')], check=True, timeout=30)
    with package.open('ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(PackageError):
        verify_signed_package(package, envelope, **trusted)
    with pytest.raises(PackageError):
        prepare_signed_package(package, envelope, tmp_path / 'tampered', **trusted)
    assert not (tmp_path / 'tampered').exists()
