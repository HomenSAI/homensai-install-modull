"""Полная цепочка до запуска синтетического продукта в отдельном user-systemd."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

import pytest

from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.preflight import NAMES
from shared.services import render_fixed_user_services
from shared.systemd_user_load import signed_user_group
from examples.demo_product.installer_client import InstallerClient, InstallerClientError

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.environ.get('HOMENSAI_TEST_ISOLATED_SYSTEMD') != '1' or os.name != 'posix',
                    reason='Нужен специально подготовленный изолированный менеджер')
def test_signed_demo_prepared_published_loaded_and_started(tmp_path):
    import pwd
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    identity = pwd.getpwuid(os.getuid())
    if (os.getuid() == 0 or identity.pw_name != 'homenstest'
            or identity.pw_dir != '/home/homenstest' or not Path('/run/.containerenv').is_file()):
        pytest.skip('Защитные условия изолированной тестовой среды не выполнены')
    product = 'homensai-test-' + uuid.uuid4().hex[:16]
    manifest = load_manifest(ROOT / 'examples/rent.manifest.json')
    manifest['product_id'] = product
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [{'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}]}
    manifest['components'] = [item for item in manifest['components'] if item['name'] != 'scheduler']
    manifest['modes'][0]['components'] = ['web', 'bot']
    manifest['secrets'] = []
    payload = tmp_path / 'payload'
    payload.mkdir()
    for index, artifact in enumerate([*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]):
        data = ('synthetic runtime ' + str(index)).encode()
        path = payload / artifact['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        artifact['sha256'] = hashlib.sha256(data).hexdigest()
    source = (ROOT / 'examples/demo_product/main.py').read_text()
    assert source.count('homensai-test-demo') == 1
    demo = payload / 'demo'
    demo.mkdir()
    (demo / 'main.py').write_text(source.replace('homensai-test-demo', product))
    (demo / 'release.json').write_bytes((ROOT / 'examples/demo_product/releases/1/release.json').read_bytes())
    install = tmp_path / 'install with spaces'
    data_dir = install / 'data'
    program = install / 'releases/app-1/demo/main.py'
    probe = subprocess.run([sys.executable, '-B', '-c',
        'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'],
        capture_output=True, text=True, timeout=10, check=True)
    port = int(probe.stdout.strip())
    manifest['web']['port'] = port
    for component in manifest['components']:
        if component['name'] == 'web':
            component['cmd'] = [sys.executable, '-B', str(program), 'serve', '--data-dir', str(data_dir), '--port', str(port)]
        elif component['name'] == 'bot':
            component['cmd'] = [sys.executable, '-B', str(program), 'worker', '--data-dir', str(data_dir)]
    units = {name: text.encode() for name, text in render_fixed_user_services(
        manifest, modes=('autonomous',), install_root=str(install)).items()}
    for name, body in units.items():
        path = payload / 'systemd-user' / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(body)
        units[name] = body
    package = tmp_path / 'package.zip'
    build_package(manifest, '1', payload, package)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private, expected_product_id=product, expected_version='1')
    common = {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode(),
              'trusted_public_key': base64.b64encode(public).decode(), 'expected_product_id': product,
              'expected_version': '1', 'install_root': str(install)}
    destination = Path(identity.pw_dir) / '.config/systemd/user'
    expected = [destination / name for name in units]
    marker = destination / ('.' + product + '.homensai-units.pending')
    assert destination.is_dir() and not marker.exists() and all(not p.exists() for p in expected)
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat',
           'XDG_RUNTIME_DIR': '/run/user/' + str(os.getuid()),
           'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/' + str(os.getuid()) + '/bus'}
    client = InstallerClient(timeout=90)
    try:
        other = tmp_path / 'other install'
        other_common = {**common, 'install_root': str(other)}
        client.call('prepare-native-release', {**other_common, 'required_bytes': {name: 0 for name in NAMES},
                    'reserve_bytes': 0, 'idempotency_key': '4' * 32}, min_minor=8)
        with pytest.raises(InstallerClientError) as failure:
            client.call('publish-systemd-user-units', {**other_common, 'modes': ['autonomous'],
                        'unit_dir': str(destination), 'idempotency_key': '5' * 32,
                        'verify_generated_units': True}, min_minor=16)
        assert failure.value.code == 'systemd_publish_failed'
        assert all(not p.exists() for p in expected) and not marker.exists()
        assert not (other / '.homensai-control/user-systemd-publication.json').exists()
        prepared = client.call('prepare-native-release', {**common, 'required_bytes': {name: 0 for name in NAMES},
                              'reserve_bytes': 0, 'idempotency_key': '1' * 32}, min_minor=8)
        assert prepared['status'] == 'prepared' and prepared['installation_complete'] is False
        service_args = {**common, 'modes': ['autonomous'], 'unit_dir': str(destination)}
        publication = {**service_args, 'idempotency_key': '2' * 32, 'verify_generated_units': True}
        published = client.call('publish-systemd-user-units', publication, min_minor=16)
        assert published['state'] == 'published' and published['services_started'] is False
        assert client.call('publish-systemd-user-units', publication, min_minor=16) == published
        with pytest.raises(InstallerClientError) as conflict:
            client.call('publish-systemd-user-units', {**publication, 'verify_generated_units': False}, min_minor=16)
        assert conflict.value.code == 'systemd_publish_failed'
        loaded = client.call('load-systemd-user-units', {**service_args, 'idempotency_key': '3' * 32}, min_minor=15)
        assert loaded['state'] == 'loaded' and loaded['services_started'] is False
        group = signed_user_group(package, envelope, str(install), trusted_public_key=public,
                                  expected_product_id=product, expected_version='1', modes=('autonomous',),
                                  unit_dir=str(destination), operation_id='3' * 32)
        assert all(item['active_state'] == 'active' for item in group.command('start', execute=True))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                heartbeat = json.loads((data_dir / 'worker-heartbeat.json').read_text())
                result = client.call('check-local-health', {'port': port, 'product_id': product,
                                      'version': '1', 'path': '/health', 'timeout_seconds': 2}, min_minor=12)
                if result['health_response_matches'] and heartbeat['release'] == '1':
                    break
            except (OSError, ValueError, IndexError, KeyError, InstallerClientError):
                pass
            time.sleep(0.1)
        else:
            pytest.fail('Тестовый продукт не подтвердил ответ и heartbeat')
        assert result['process_identity_verified'] is False
        assert all(item['active_state'] == 'inactive' for item in group.command('stop', execute=True))
    finally:
        subprocess.run(['/usr/bin/systemctl', '--user', 'stop', '--', *[p.name for p in expected]],
                       env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
        for path in expected:
            if path.exists():
                info = path.lstat()
                assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                assert path.read_bytes() == units[path.name]
                path.unlink()
        if marker.exists():
            info = marker.lstat()
            assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
            body = json.loads(marker.read_text())
            assert body['product_id'] == product and body['version'] == '1'
            assert {item['name']: item['sha256'] for item in body['units']} == {
                name: hashlib.sha256(value).hexdigest() for name, value in units.items()}
            marker.unlink()
        subprocess.run(['/usr/bin/systemctl', '--user', 'daemon-reload'], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
