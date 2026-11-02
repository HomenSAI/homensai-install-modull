import hashlib
import io
import json
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
        archive.writestr('../../outside', 'ignored')
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
