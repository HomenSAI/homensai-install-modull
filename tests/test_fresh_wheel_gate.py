"""Низкоуровневая подготовка не публикует неподтверждённый wheel."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from shared.api import handle
from shared.fresh_prepare import FreshPrepareError, prepare_fresh_native, resume_fresh_native, status_fresh_native
from shared.package import build_package
from shared.package_signing import sign_package
from test_api_signed_preflight import signed_request


@pytest.fixture
def unsafe_release(signed_request, tmp_path):
    ed = pytest.importorskip('cryptography.hazmat.primitives.asymmetric.ed25519')
    ser = pytest.importorskip('cryptography.hazmat.primitives.serialization')
    query, _, original = signed_request
    manifest = deepcopy(original)
    payload = tmp_path / 'payload'
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('../../escape', 'synthetic')
    artifact = manifest['runtime']['vendor_wheels'][0]
    (payload / artifact['file']).write_bytes(wheel.getvalue())
    artifact['sha256'] = hashlib.sha256(wheel.getvalue()).hexdigest()
    package = tmp_path / 'unsafe.zip'
    build_package(manifest, '1', payload, package)
    private = ed.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=manifest['product_id'], expected_version='1')
    root = Path(query['params']['install_root'])
    options = dict(trusted_public_key=public, expected_product_id=manifest['product_id'], expected_version='1')
    return package, envelope, root, options, query['params']['required_bytes']


def test_fresh_prepare_and_api_reject_unsafe_signed_wheel_without_writes(unsafe_release):
    package, envelope, root, options, required = unsafe_release
    with pytest.raises(FreshPrepareError):
        prepare_fresh_native(package, envelope, root, **options, required_bytes=required, reserve_bytes=0)
    request = {'api_major': 1, 'request_id': 'unsafe-fresh', 'operation': 'prepare-native-release',
               'language': 'de', 'params': {'package_path': str(package),
                   'envelope': base64.b64encode(envelope).decode(),
                   'trusted_public_key': base64.b64encode(options['trusted_public_key']).decode(),
                   'expected_product_id': options['expected_product_id'], 'expected_version': '1',
                   'install_root': str(root), 'required_bytes': required, 'reserve_bytes': 0,
                   'idempotency_key': 'a' * 32}}
    result = handle(request)
    assert result['error']['code'] == 'fresh_prepare_failed' and str(root) not in json.dumps(result)
    assert not root.exists() and not (root.parent / 'escape').exists()


def test_previously_interrupted_unsafe_package_remains_inspectable_but_cannot_resume(unsafe_release, monkeypatch):
    package, envelope, root, options, required = unsafe_release
    def interrupt(*args, **kwargs):
        raise OSError('synthetic interruption')
    with monkeypatch.context() as patch:
        # Создаём состояние старой версии до введения правила; пользовательские
        # файлы не затрагиваются, и ни один wheel не извлекается.
        patch.setattr('shared.wheel_audit.audit_signed_wheels', lambda *args, **kwargs: {})
        patch.setattr('shared.fresh_prepare.prepare_native_signed_package', interrupt)
        with pytest.raises(FreshPrepareError):
            prepare_fresh_native(package, envelope, root, **options, required_bytes=required, reserve_bytes=0)
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob('*'))
    status = status_fresh_native(package, envelope, root, **options)
    with pytest.raises(FreshPrepareError):
        resume_fresh_native(package, envelope, root, **options, reserve_bytes=0)
    assert status_fresh_native(package, envelope, root, **options) == status
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob('*')) == before
