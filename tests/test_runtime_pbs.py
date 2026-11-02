"""Преобразование только проверенного синтетического архива, без исполнения кода."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from shared.runtime_block import RuntimeBlockError, verify_runtime_block
from shared.runtime_pbs import prepare_pbs_archive


def archive_at(path, extra=()):
    files = [('python/bin/python3.14', b'not a real executable'),
             ('python/lib/python3.14/LICENSE.txt', b'example license'),
             ('python/share/terminfo/e/eterm', b'terminfo'),
             ('python/share/terminfo/E/Eterm', b'case collision excluded')]
    with tarfile.open(path, 'w:gz') as archive:
        for name, body in files:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo('python/bin/python')
        link.type = tarfile.SYMTYPE
        link.linkname = 'python3.14'
        archive.addfile(link)
        for info, body in extra:
            archive.addfile(info, io.BytesIO(body) if body is not None else None)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def call(archive, destination, digest):
    return prepare_pbs_archive(archive, destination, expected_sha256=digest,
        origin='https://example.org/cpython.tar.gz', python_version='3.14.10')


def test_materializes_links_and_preserves_license_without_execution(tmp_path):
    archive = tmp_path / 'candidate.tar.gz'
    digest = archive_at(archive)
    block = tmp_path / 'runtime-linux'
    result = call(archive, block, digest)
    assert result['materialized_links'] == 1 and result['runtime_executed'] is False
    assert result['license_review_complete'] is False and result['terminfo_included'] is False
    assert not (block / 'bin/python').is_symlink()
    assert (block / 'bin/python').read_bytes() == b'not a real executable'
    assert not (block / 'share/terminfo').exists()
    lock = verify_runtime_block(block)
    assert lock['artifact_sha256'] == digest
    assert lock['license'] == 'LicenseRef-Composite-Unreviewed'
    assert lock['license_files'] == ['licenses/upstream/lib/python3.14/LICENSE.txt']
    assert (block / lock['license_files'][0]).read_bytes() == b'example license'


def test_wrong_hash_refuses_before_destination_created(tmp_path):
    archive = tmp_path / 'candidate.tar.gz'
    archive_at(archive)
    block = tmp_path / 'runtime-linux'
    with pytest.raises(RuntimeBlockError):
        call(archive, block, '0' * 64)
    assert not block.exists()


@pytest.mark.parametrize('kind', ['traversal', 'absolute_link', 'escaping_link', 'cycle', 'case_conflict', 'hardlink', 'extended_header'])
def test_unsafe_archive_rejected_without_destination(tmp_path, kind):
    archive = tmp_path / 'candidate.tar.gz'
    if kind == 'traversal':
        item = tarfile.TarInfo('python/../../escape')
        item.size = 1
        extra = [(item, b'x')]
    elif kind == 'case_conflict':
        item = tarfile.TarInfo('python/BIN/python3.14')
        item.size = 1
        extra = [(item, b'x')]
    elif kind == 'extended_header':
        item = tarfile.TarInfo('python/lib/' + 'a' * 150)
        item.size = 1
        extra = [(item, b'x')]
    else:
        item = tarfile.TarInfo('python/bin/unsafe')
        item.type = tarfile.LNKTYPE if kind == 'hardlink' else tarfile.SYMTYPE
        item.linkname = {'absolute_link': '/etc/passwd', 'escaping_link': '../../../outside',
                         'cycle': 'unsafe', 'hardlink': 'python/bin/python3.14'}[kind]
        extra = [(item, None)]
    digest = archive_at(archive, extra)
    block = tmp_path / 'runtime-linux'
    with pytest.raises(RuntimeBlockError):
        call(archive, block, digest)
    assert not block.exists()


@pytest.mark.skipif(os.environ.get('HOMENSAI_PBS_ISOLATED') != '1', reason='Нужен отдельный контейнер без сети и реальный архив')
def test_real_pbs_signed_package_and_prepared_interpreter(tmp_path):
    """Исполняет только кандидата внутри явно выделенной тестовой среды."""
    if os.getuid() == 0 or not Path('/run/.containerenv').is_file():
        pytest.skip('Не выполнены защитные условия контейнера')
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    from shared.fresh_prepare import prepare_fresh_native
    from shared.manifest import load_manifest
    from shared.package import build_package
    from shared.package_signing import sign_package
    from shared.preflight import NAMES
    from shared.runtime_native import prepare_signed_linux_runtime, verify_signed_linux_runtime

    expected = '4d3c16c59ed0802a3d1d34d8bce504b15ceeaff39d62dadecc6a9fa803412aed'
    origin = ('https://github.com/astral-sh/python-build-standalone/releases/download/20261026/'
              'cpython-3.14.10%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz')
    archive = Path(os.environ['HOMENSAI_PBS_ARCHIVE'])
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'examples/rent.manifest.json')
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [{'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}]}
    payload = tmp_path / 'payload'
    payload.mkdir()
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        body = ('synthetic dependency ' + str(index)).encode()
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        artifact['sha256'] = hashlib.sha256(body).hexdigest()
    block = payload / 'runtime-linux'
    conversion = prepare_pbs_archive(archive, block, expected_sha256=expected, origin=origin, python_version='3.14.10')
    assert conversion['runtime_executed'] is False and conversion['license_review_complete'] is False
    assert conversion['license_declared'] == 'LicenseRef-Composite-Unreviewed'
    assert conversion['materialized_links'] == 10 and not (block / 'share/terminfo').exists()
    package = tmp_path / 'test-package.zip'
    build_package(manifest, '1', payload, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    install = tmp_path / 'installation'
    prepare_fresh_native(package, envelope, install, **options, required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    prepared = prepare_signed_linux_runtime(package, envelope, str(install), **options,
                                             execute=True, operation_id='a' * 32, reserve_bytes=0)
    assert prepared['runtime_executed'] is False and prepared['python_compatibility_verified'] is False
    assert verify_signed_linux_runtime(package, envelope, str(install), **options,
                                       operation_id='a' * 32, reserve_bytes=0)['state'] == 'verified'
    script = ('import sys, ssl, sqlite3, bz2, lzma; '
              'assert sys.version_info[:3] == (3, 14, 10); '
              'print(sys.version.split()[0], ssl.OPENSSL_VERSION, sqlite3.sqlite_version)')
    result = subprocess.run([str(install / 'runtime/bin/python'), '-I', '-B', '-c', script],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=True,
        env={'PATH': '/usr/bin:/bin'})
    assert result.stdout.startswith('3.14.10 OpenSSL ')
