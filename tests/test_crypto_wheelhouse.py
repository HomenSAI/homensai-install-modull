import hashlib
import io
import json
import stat
import zipfile

import pytest

from shared import crypto_wheelhouse as wh


@pytest.fixture
def sample(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    lock = tmp_path / 'requirements.txt'
    version = '1.2'
    names = ('LICENSE', 'LICENSE.BSD')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        for name in names:
            archive.writestr(f'demo-{version}.dist-info/licenses/{name}', f'text {name}')
        archive.writestr('demo/__init__.py', '# synthetic')
    wheel = data.getvalue()
    filename = 'demo-1.2-py3-none-any.whl'
    (source / filename).write_bytes(wheel)
    digest = hashlib.sha256(wheel).hexdigest()
    lock.write_text('--require-hashes\n--only-binary=:all:\n'
                    f'demo=={version} \\\n    --hash=sha256:{digest}\n')
    monkeypatch.setattr(wh, '_LOCK', lock)
    monkeypatch.setattr(wh, 'LOCKED_VERSIONS', {'demo': version})
    monkeypatch.setattr(wh, '_ARTIFACTS', {filename: ('demo', digest, names)})
    return source, filename, lock


def test_build_is_reproducible_and_keeps_only_selected_licenses(sample, tmp_path):
    source, filename, _ = sample
    first, second = tmp_path / 'a', tmp_path / 'b'
    result = wh.build_wheelhouse(source, first)
    assert result == wh.build_wheelhouse(source, second)
    assert (first / 'vendor' / filename).read_bytes() == (source / filename).read_bytes()
    assert (first / 'licenses' / filename / 'LICENSE').read_text() == 'text LICENSE'
    assert not (tmp_path / 'outside').exists()
    assert not (first / '.homensai-crypto-incomplete').exists()
    assert json.loads((first / 'crypto-wheelhouse.json').read_text()) == result
    assert (first / 'crypto-wheelhouse.json').read_bytes() == (second / 'crypto-wheelhouse.json').read_bytes()


def test_wrong_hash_rejected_before_creating_output(sample, tmp_path):
    source, filename, _ = sample
    (source / filename).write_bytes(b'tampered')
    output = tmp_path / 'output'
    with pytest.raises(wh.WheelhouseError):
        wh.build_wheelhouse(source, output)
    assert not output.exists()


@pytest.mark.parametrize('mutation', ['traversal', 'absolute', 'backslash', 'duplicate', 'case_collision', 'symlink', 'file_parent'])
def test_unsafe_wheel_is_rejected_even_with_matching_digest(sample, tmp_path, monkeypatch, mutation):
    source, filename, lock = sample
    wheel_path = source / filename
    with zipfile.ZipFile(wheel_path, 'a') as archive:
        if mutation == 'duplicate':
            with pytest.warns(UserWarning):
                archive.writestr('demo/__init__.py', 'duplicate')
        elif mutation == 'case_collision':
            archive.writestr('DEMO/__INIT__.PY', 'ambiguous')
        elif mutation == 'symlink':
            entry = zipfile.ZipInfo('demo/link')
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, 'elsewhere')
        elif mutation == 'file_parent':
            archive.writestr('demo/file', 'file')
            archive.writestr('demo/file/child', 'conflict')
        else:
            name = {'traversal': '../../outside', 'absolute': '/absolute', 'backslash': 'demo\\escape'}[mutation]
            archive.writestr(name, 'unsafe')
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    lock.write_text('--require-hashes\n--only-binary=:all:\n'
                    f'demo==1.2 \\\n    --hash=sha256:{digest}\n')
    monkeypatch.setattr(wh, '_ARTIFACTS', {filename: ('demo', digest, ('LICENSE', 'LICENSE.BSD'))})
    with pytest.raises(wh.WheelhouseError):
        wh.build_wheelhouse(source, tmp_path / 'output')
    assert not (tmp_path / 'output').exists() and not (tmp_path / 'outside').exists()


def test_archived_wheelhouse_rejects_unsafe_wheel_with_consistent_metadata(sample, tmp_path):
    import base64
    from shared.archive import _info
    from shared.package import PackageError
    from shared.package_signing import _canonical, verify_signed_package
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    source, filename, _ = sample
    block = tmp_path / 'block'
    wh.build_wheelhouse(source, block)
    wheel_path = block / 'vendor' / filename
    old_digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel_path, 'a') as wheel:
        wheel.writestr('../../outside', 'unsafe')
    new_digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    metadata_path = block / 'crypto-wheelhouse.json'
    metadata = json.loads(metadata_path.read_text())
    metadata['vendor_wheels'][0]['sha256'] = new_digest
    for system in ('linux', 'windows'):
        lock_path = block / 'locks' / (system + '.lock')
        lock_path.write_text(lock_path.read_text().replace(old_digest, new_digest))
        metadata['lock_files'][system]['sha256'] = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=True, sort_keys=True, indent=2) + '\n')
    files, bodies = [], {}
    package = tmp_path / 'outer.zip'
    with zipfile.ZipFile(package, 'w') as outer:
        for path in sorted(block.rglob('*')):
            if path.is_file():
                name = 'crypto/' + path.relative_to(block).as_posix()
                data = path.read_bytes()
                files.append({'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
                bodies[name] = data
                outer.writestr('payload/' + name, data)
    with zipfile.ZipFile(package) as outer:
        with pytest.raises(wh.WheelhouseError):
            wh.verify_archived_wheelhouse(outer, files, 'crypto/crypto-wheelhouse.json')
    manifest = json.loads((wh.Path(wh.__file__).resolve().parent.parent / 'examples/jobpilot.manifest.json').read_text())
    package_metadata = {'format': 1, 'product_id': manifest['product_id'], 'version': '1',
                        'manifest': manifest, 'files': files}
    signed = tmp_path / 'signed.zip'
    with zipfile.ZipFile(signed, 'w') as outer:
        outer.writestr(_info('package.json'), json.dumps(package_metadata).encode())
        for name, data in sorted(bodies.items()):
            outer.writestr(_info('payload/' + name), data)
    digest = hashlib.sha256(signed.read_bytes()).hexdigest()
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    claims = {'format': 1, 'algorithm': 'Ed25519', 'product_id': manifest['product_id'], 'version': '1', 'sha256': digest}
    envelope = json.dumps({**claims, 'signature': base64.b64encode(private.sign(_canonical(claims))).decode()}).encode()
    with pytest.raises(PackageError, match='Криптоблок'):
        verify_signed_package(signed, envelope, trusted_public_key=public,
                              expected_product_id=manifest['product_id'], expected_version='1')


def test_lock_changes_rejected_before_creating_output(sample, tmp_path):
    source, _, lock = sample
    lock.write_text(lock.read_text() + '--extra-index-url https://example.invalid\n')
    output = tmp_path / 'output'
    with pytest.raises(wh.WheelhouseError):
        wh.build_wheelhouse(source, output)
    assert not output.exists()


def test_existing_destination_preserved(sample, tmp_path):
    source, _, _ = sample
    output = tmp_path / 'output'
    output.mkdir()
    (output / 'user-file').write_text('keep')
    with pytest.raises(wh.WheelhouseError):
        wh.build_wheelhouse(source, output)
    assert (output / 'user-file').read_text() == 'keep'


def test_symlink_source_wheel_rejected(sample, tmp_path):
    source, filename, _ = sample
    wheel = source / filename
    saved = tmp_path / 'saved'
    wheel.rename(saved)
    try:
        wheel.symlink_to(saved)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises(wh.WheelhouseError):
        wh.build_wheelhouse(source, tmp_path / 'output')


def test_duplicate_license_rejected(sample, tmp_path, monkeypatch):
    source, filename, lock = sample
    wheel_path = source / filename
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(wheel_path, 'a') as archive:
            archive.writestr('demo-1.2.dist-info/licenses/LICENSE', 'duplicate')
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    lock.write_text('--require-hashes\n--only-binary=:all:\n'
                    f'demo==1.2 \\\n    --hash=sha256:{digest}\n')
    monkeypatch.setattr(wh, '_ARTIFACTS', {filename: ('demo', digest, ('LICENSE',))})
    with pytest.raises(wh.WheelhouseError):
        wh.build_wheelhouse(source, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_source_and_destination_must_not_overlap(sample, tmp_path):
    source, _, _ = sample
    with pytest.raises(wh.WheelhouseError):
        wh.build_wheelhouse(source, source / 'output')
    assert not (source / 'output').exists()


def test_fragment_integrates_with_package_inventory(sample, tmp_path):
    from shared.package import build_package, verify_package

    source, _, _ = sample
    payload = tmp_path / 'payload'
    fragment = wh.build_wheelhouse(source, payload)
    manifest_path = wh.Path(wh.__file__).resolve().parent.parent / 'examples' / 'rent.manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['runtime']['lock_files'] = fragment['lock_files']
    manifest['runtime']['vendor_wheels'] = fragment['vendor_wheels']
    output = tmp_path / 'package.zip'
    built = build_package(manifest, '1.0', payload, output)
    verified = verify_package(output, expected_sha256=built['sha256'])
    paths = {item['path'] for item in verified['files']}
    assert {item['file'] for item in fragment['license_files']} <= paths
    assert {item['file'] for item in fragment['vendor_wheels']} <= paths


def test_failed_write_leaves_incomplete_marker(sample, tmp_path, monkeypatch):
    source, _, _ = sample
    original_write = wh._write

    def fail_after_marker(path, data):
        if path.name != '.homensai-crypto-incomplete':
            raise OSError('simulated write failure')
        original_write(path, data)

    monkeypatch.setattr(wh, '_write', fail_after_marker)
    output = tmp_path / 'output'
    with pytest.raises(wh.WheelhouseError):
        wh.build_wheelhouse(source, output)
    assert (output / '.homensai-crypto-incomplete').exists()


@pytest.mark.parametrize('mutation', ['license', 'wheel', 'lock', 'metadata', 'extra', 'missing', 'marker'])
def test_verifier_rejects_incomplete_or_modified_block(sample, tmp_path, mutation):
    source, filename, _ = sample
    block = tmp_path / 'block'
    expected = wh.build_wheelhouse(source, block)
    assert wh.verify_wheelhouse(block) == expected
    if mutation == 'license':
        (block / 'licenses' / filename / 'LICENSE').write_text('modified')
    elif mutation == 'wheel':
        (block / 'vendor' / filename).write_bytes(b'modified')
    elif mutation == 'lock':
        (block / 'locks/linux.lock').write_text('modified')
    elif mutation == 'metadata':
        (block / 'crypto-wheelhouse.json').write_text('{}')
    elif mutation == 'extra':
        (block / 'vendor/extra.whl').write_text('extra')
    elif mutation == 'missing':
        (block / 'licenses' / filename / 'LICENSE').unlink()
    else:
        (block / '.homensai-crypto-incomplete').write_text('incomplete')
    with pytest.raises(wh.WheelhouseError):
        wh.verify_wheelhouse(block)


def test_package_builder_rejects_tampered_license_even_if_runtime_hashes_match(sample, tmp_path):
    from shared.package import PackageError, build_package

    source, filename, _ = sample
    payload = tmp_path / 'payload'
    fragment = wh.build_wheelhouse(source, payload)
    manifest_path = wh.Path(wh.__file__).resolve().parent.parent / 'examples' / 'rent.manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['runtime']['lock_files'] = fragment['lock_files']
    manifest['runtime']['vendor_wheels'] = fragment['vendor_wheels']
    (payload / 'licenses' / filename / 'LICENSE').write_text('tampered')
    with pytest.raises(PackageError, match='Криптоблок'):
        build_package(manifest, '1.0', payload, tmp_path / 'bad.zip')
    assert not (tmp_path / 'bad.zip').exists()


def test_signed_release_inventory_does_not_depend_on_current_builder_lock(sample, tmp_path):
    from shared.package import PackageError, _files

    source, _, lock = sample
    payload = tmp_path / 'payload'
    wh.build_wheelhouse(source, payload)
    expected, _ = _files(payload)
    lock.write_text('newer or unavailable builder lock')
    with pytest.raises(PackageError):
        _files(payload)
    actual, _ = _files(payload, verify_crypto_block=False)
    assert actual == expected


@pytest.mark.parametrize('mutation', ['license', 'missing', 'extra', 'lock', 'wheel'])
def test_signed_zip_rejects_inconsistent_crypto_block(sample, tmp_path, mutation):
    import base64
    from shared.archive import _info
    from shared.package import PackageError, verify_package
    from shared.package_signing import _canonical, verify_signed_package
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')

    source, filename, _ = sample
    block = tmp_path / 'block'
    wh.build_wheelhouse(source, block)
    members = {'crypto/' + p.relative_to(block).as_posix(): p.read_bytes()
               for p in block.rglob('*') if p.is_file()}
    metadata = json.loads(members['crypto/crypto-wheelhouse.json'])
    if mutation == 'license':
        path = f'crypto/licenses/{filename}/LICENSE'
        members[path] = b'changed and rehashed'
        for item in metadata['license_files']:
            if 'crypto/' + item['file'] == path:
                item['sha256'] = hashlib.sha256(members[path]).hexdigest()
    elif mutation == 'missing':
        del members[f'crypto/licenses/{filename}/LICENSE']
    elif mutation == 'extra':
        members['crypto/unlisted'] = b'extra'
    elif mutation == 'lock':
        members['crypto/locks/linux.lock'] += b'# changed\n'
        metadata['lock_files']['linux']['sha256'] = hashlib.sha256(members['crypto/locks/linux.lock']).hexdigest()
    else:
        members[f'crypto/vendor/{filename}'] = b'not a wheel'
        metadata['vendor_wheels'][0]['sha256'] = hashlib.sha256(b'not a wheel').hexdigest()
    members['crypto/crypto-wheelhouse.json'] = (json.dumps(metadata, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode()
    manifest = json.loads((wh.Path(wh.__file__).resolve().parent.parent / 'examples' / 'jobpilot.manifest.json').read_text())
    package_metadata = {'format': 1, 'product_id': manifest['product_id'], 'version': '1', 'manifest': manifest,
                        'files': [{'path': name, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                                  for name, body in sorted(members.items())]}
    package = tmp_path / 'forged.zip'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr(_info('package.json'), json.dumps(package_metadata).encode())
        for name, body in sorted(members.items()):
            archive.writestr(_info('payload/' + name), body)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    with pytest.raises(PackageError, match='Криптоблок'):
        verify_package(package, expected_sha256=digest)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    claims = {'format': 1, 'algorithm': 'Ed25519', 'product_id': manifest['product_id'], 'version': '1', 'sha256': digest}
    envelope = json.dumps({**claims, 'signature': base64.b64encode(private.sign(_canonical(claims))).decode()}).encode()
    with pytest.raises(PackageError, match='Криптоблок'):
        verify_signed_package(package, envelope, trusted_public_key=public,
                              expected_product_id=manifest['product_id'], expected_version='1')


def test_archived_block_does_not_depend_on_builder_lock(sample, tmp_path, monkeypatch):
    from shared.package import build_package, verify_package
    source, _, lock = sample
    payload = tmp_path / 'payload'
    wh.build_wheelhouse(source, payload)
    manifest = json.loads((wh.Path(wh.__file__).resolve().parent.parent / 'examples' / 'jobpilot.manifest.json').read_text())
    package = tmp_path / 'package.zip'
    built = build_package(manifest, '1', payload, package)
    lock.write_text('later changed lock')
    monkeypatch.setattr(wh, '_ARTIFACTS', {})
    monkeypatch.setattr(wh, 'LOCKED_VERSIONS', {})
    assert verify_package(package, expected_sha256=built['sha256'])['product_id'] == manifest['product_id']
