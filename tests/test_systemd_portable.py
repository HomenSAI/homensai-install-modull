"""Проверка переносимого рецепта: только синтетические подписанные выпуски."""
import hashlib
import os
from pathlib import Path

import pytest

from shared.fresh_prepare import prepare_fresh_native
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package, verify_signed_package
from shared.preflight import NAMES
from shared.systemd_portable import (RECIPE_PATH, RECIPE_V1, PortableUnitError,
                                      plan_portable_signed_user_units)

ROOT = Path(__file__).resolve().parents[1]
TARGET = {'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}


@pytest.fixture
def portable_release(tmp_path, monkeypatch):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    serialization = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    if os.environ.get('HOMENSAI_TEST_REAL_SYSTEMD') != '1':
        monkeypatch.setattr('shared.fresh_prepare.native_target', lambda: dict(TARGET))
        monkeypatch.setattr('shared.package_prepare.native_target', lambda: dict(TARGET))
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [dict(TARGET)]}
    manifest['secrets'] = []
    for component in manifest['components']:
        component['cmd'] = ['@runtime/bin/python', '-B', '@release/demo/main.py', '--data-dir', '@data']
    payload = tmp_path / 'payload'
    payload.mkdir()
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        data = ('synthetic runtime ' + str(index)).encode()
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        artifact['sha256'] = hashlib.sha256(data).hexdigest()
    recipe = payload / RECIPE_PATH
    recipe.parent.mkdir()
    recipe.write_bytes(RECIPE_V1)
    (payload / 'demo').mkdir()
    (payload / 'demo/main.py').write_text('# synthetic signed application\n')
    package = tmp_path / 'portable.zip'
    build_package(manifest, '1', payload, package)
    key = ed.Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=key, expected_product_id=manifest['product_id'], expected_version='1')
    options = {'trusted_public_key': public, 'expected_product_id': manifest['product_id'], 'expected_version': '1'}
    roots = [tmp_path / 'first root', tmp_path / 'второй % root']
    for root in roots:
        prepare_fresh_native(package, envelope, root, **options,
                             required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    return package, envelope, options, roots


def test_one_signed_recipe_derives_distinct_units_for_two_roots(portable_release):
    package, envelope, options, roots = portable_release
    original_package = hashlib.sha256(package.read_bytes()).hexdigest()
    plans = [plan_portable_signed_user_units(package, envelope, str(root), **options, modes=('autonomous',))
             for root in roots]
    name = options['expected_product_id'] + '-web.service'
    assert all(plan['state'] == 'planned' and plan['services_started'] is False
               and plan['publication_readiness_verified'] is False for plan in plans)
    assert all(plan['generator'] == 'homensai-systemd-user-portable-v1' for plan in plans)
    assert str(roots[0]) in plans[0]['unit_texts'][name]
    assert str(roots[1]).replace('%', '%%') in plans[1]['unit_texts'][name]
    assert plans[0]['units'][0]['sha256'] != plans[1]['units'][0]['sha256']
    assert hashlib.sha256(package.read_bytes()).hexdigest() == original_package
    for root in roots:
        assert not (root / '.homensai-control/user-systemd-publication.json').exists()
        assert not (root / '.homensai-control/user-systemd-reload.json').exists()


def test_portable_plan_refuses_wrong_key_and_modified_recipe(portable_release):
    package, envelope, options, roots = portable_release
    with pytest.raises(PortableUnitError):
        plan_portable_signed_user_units(package, envelope, str(roots[0]),
            **{**options, 'trusted_public_key': b'x' * 32}, modes=('autonomous',))
    recipe = roots[0] / 'releases/app-1' / RECIPE_PATH
    recipe.write_bytes(RECIPE_V1 + b'changed')
    with pytest.raises(PortableUnitError):
        plan_portable_signed_user_units(package, envelope, str(roots[0]), **options, modes=('autonomous',))
    assert recipe.read_bytes().endswith(b'changed')
    assert plan_portable_signed_user_units(package, envelope, str(roots[1]),
                                           **options, modes=('autonomous',))['state'] == 'planned'


@pytest.mark.parametrize('change', ['unknown_recipe', 'ambiguous_units'])
def test_portable_plan_rejects_signed_unsupported_or_ambiguous_recipe(portable_release, tmp_path, change):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    package, envelope, options, _ = portable_release
    metadata = verify_signed_package(package, envelope, **options)
    payload = tmp_path / 'payload'
    if change == 'unknown_recipe':
        (payload / RECIPE_PATH).write_bytes(RECIPE_V1.replace(b'portable-v1', b'portable-v2'))
    else:
        (payload / 'systemd-user/other.service').write_text('[Service]\nExecStart=/bin/false\n')
    other_package = tmp_path / 'other.zip'
    build_package(metadata['manifest'], '1', payload, other_package)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    other_options = {**options, 'trusted_public_key': public}
    other_envelope = sign_package(other_package, private_key=private,
        expected_product_id=options['expected_product_id'], expected_version='1')
    root = tmp_path / 'other-root'
    prepare_fresh_native(other_package, other_envelope, root, **other_options,
                         required_bytes={name: 0 for name in NAMES}, reserve_bytes=0)
    with pytest.raises(PortableUnitError):
        plan_portable_signed_user_units(other_package, other_envelope, str(root),
                                        **other_options, modes=('autonomous',))
    assert not (root / '.homensai-control/user-systemd-publication.json').exists()
