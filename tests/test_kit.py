"""Сборщик комплекта и выбор/распаковка wheels на синтетических входах."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

from shared import kit as kit_module
from shared.kit import KitError, build_kit, generate_keys, init_project, load_config, render_manifest, verify_kit
from shared.kit_wheels import KitWheelError, compatible, install_wheel, read_lock, select_wheels, wheel_tags

ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip('cryptography')


def make_wheel(name, version, tag='py3-none-any', files=None, data=None):
    dist = name.replace('-', '_')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for path, body in (files or {dist + '/__init__.py': 'VALUE = %r\n' % version}).items():
            archive.writestr(path, body)
        for path, body in (data or {}).items():
            archive.writestr(f'{dist}-{version}.data/{path}', body)
        info = f'{dist}-{version}.dist-info/'
        archive.writestr(info + 'METADATA', f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nLicense: MIT\n')
        archive.writestr(info + 'WHEEL', f'Wheel-Version: 1.0\nTag: {tag}\n')
        archive.writestr(info + 'RECORD', '')
    return f'{dist}-{version}-{tag}.whl', buffer.getvalue()


def lock_text(entries):
    lines = ['--require-hashes', '--only-binary=:all:']
    for name, version, digests in entries:
        lines.append(f'{name}=={version} ' + ' '.join('--hash=sha256:' + d for d in digests))
    return '\n'.join(lines) + '\n'


def synthetic_pbs(path):
    with tarfile.open(path, 'w:gz') as tar:
        for name, body in (('python/bin/python', b'\x7fELF synthetic'),
                           ('python/lib/python3.14/LICENSE.txt', b'PSF synthetic license\n'),
                           ('python/lib/python3.14/os.py', b'# synthetic\n')):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / 'product'
    (base / 'src/kitdemo').mkdir(parents=True)
    (base / 'src/kitdemo/__init__.py').write_text('')
    (base / 'src/kitdemo/hello.py').write_text(
        'import json, os, sys\nimport demo_dep\n'
        'print(json.dumps({"argv": sys.argv[1:], "dep": demo_dep.VALUE, '
        '"release": os.environ["HOMENSAI_RELEASE"], "product": os.environ["HOMENSAI_PRODUCT_ID"]}))\n')
    (base / 'src/kitdemo/__pycache__').mkdir()
    (base / 'src/kitdemo/__pycache__/junk.pyc').write_bytes(b'junk')
    wheelhouse = base / 'wheels'
    wheelhouse.mkdir()
    linux, windows = [], []
    for name, version, tag in (('demo-dep', '1.0', 'py3-none-any'),):
        filename, data = make_wheel(name, version, tag)
        (wheelhouse / filename).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        linux.append((name, version, [digest]))
        windows.append((name, version, [digest]))
    crypto = []
    for name in ('crypto-fake',):
        filename, data = make_wheel(name, '1.0', 'cp314-cp314-manylinux_2_17_x86_64')
        (wheelhouse / filename).write_bytes(data)
        crypto.append((name, '1.0', [hashlib.sha256(data).hexdigest()]))
    crypto_lock = tmp_path / 'crypto.lock'
    crypto_lock.write_text(lock_text(crypto))
    monkeypatch.setattr(kit_module, 'CRYPTO_LOCK', crypto_lock)
    (base / 'installer').mkdir()
    (base / 'installer/linux.lock').write_text(lock_text(linux))
    (base / 'installer/windows.lock').write_text(lock_text(windows))
    archive = base / 'installer/python.tar.gz'
    digest = synthetic_pbs(archive)
    template = json.loads((ROOT / 'examples/kit_demo/installer/manifest.json').read_text())
    (base / 'installer/manifest.json').write_text(json.dumps(template))
    config = {'format': 1, 'manifest': 'installer/manifest.json', 'sources': [{'from': 'src', 'to': 'src'}],
              'python_paths': ['src'], 'locks': {'linux': 'installer/linux.lock', 'windows': 'installer/windows.lock'},
              'wheelhouse': 'wheels', 'migrations': ['notes-1'],
              'runtime': {'linux': {'archive': 'installer/python.tar.gz', 'sha256': digest,
                                    'origin': 'https://example.invalid/python.tar.gz', 'python_version': '3.14.10'}}}
    (base / 'homensai-kit.json').write_text(json.dumps(config))
    keys = generate_keys(tmp_path / 'keys')
    return base, keys


def test_wheel_tags_and_compatibility():
    assert wheel_tags('cffi-2.1.1-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl')['name'] == 'cffi'
    assert compatible('a-1-py3-none-any.whl', 'linux-x86_64') == 1
    assert compatible('a-1-cp311-abi3-manylinux_2_28_x86_64.whl', 'linux-x86_64') == 2
    assert compatible('a-1-cp311-abi3-manylinux_2_34_x86_64.whl', 'linux-x86_64') == 0
    assert compatible('a-1-cp311-abi3-manylinux_2_34_x86_64.whl', 'linux-x86_64', glibc=(2, 34)) == 2
    assert compatible('a-1-cp314-cp314-win_amd64.whl', 'windows-x86_64') == 2
    assert compatible('a-1-cp314-cp314-win_amd64.whl', 'linux-x86_64') == 0
    assert compatible('a-1-cp313-cp313-manylinux_2_17_x86_64.whl', 'linux-x86_64') == 0
    assert compatible('a-1-cp314-cp314t-manylinux_2_17_x86_64.whl', 'linux-x86_64') == 0
    with pytest.raises(KitWheelError):
        wheel_tags('not-a-wheel.zip')


def test_select_requires_hash_and_compatible_tag(tmp_path):
    universal_name, universal = make_wheel('dep', '1.0')
    binary_name, binary = make_wheel('dep', '1.0', 'cp314-cp314-manylinux_2_17_x86_64')
    (tmp_path / universal_name).write_bytes(universal)
    (tmp_path / binary_name).write_bytes(binary)
    digests = [hashlib.sha256(universal).hexdigest(), hashlib.sha256(binary).hexdigest()]
    lock = [{'name': 'dep', 'version': '1.0', 'sha256': sorted(digests)}]
    assert [p.name for p in select_wheels(lock, tmp_path, 'linux-x86_64')] == [binary_name]
    assert [p.name for p in select_wheels(lock, tmp_path, 'windows-x86_64')] == [universal_name]
    with pytest.raises(KitWheelError):
        select_wheels([{'name': 'dep', 'version': '1.0', 'sha256': ['0' * 64]}], tmp_path, 'linux-x86_64')
    with pytest.raises(KitWheelError):
        select_wheels([{'name': 'other', 'version': '1.0', 'sha256': digests}], tmp_path, 'linux-x86_64')


def test_read_lock_rejects_unpinned(tmp_path):
    path = tmp_path / 'lock'
    path.write_text('requests>=2\n')
    with pytest.raises(KitWheelError):
        read_lock(path)
    path.write_text(lock_text([('Demo_Dep', '1.0', ['a' * 64])]))
    assert read_lock(path) == [{'name': 'demo-dep', 'version': '1.0', 'sha256': ['a' * 64]}]


def test_install_wheel_layout_and_conflicts(tmp_path):
    _, data = make_wheel('dep', '1.0', data={'purelib/extra.py': 'X = 1\n', 'scripts/tool': '#!/bin/sh\n'})
    owned = {}
    result = install_wheel(data, tmp_path, owned)
    assert result == {'files': 5, 'skipped': 1}
    assert (tmp_path / 'extra.py').read_text() == 'X = 1\n'
    assert (tmp_path / 'dep-1.0.dist-info/METADATA').is_file() and not (tmp_path / 'tool').exists()
    _, other = make_wheel('other', '2.0', files={'dep/__init__.py': 'clash'})
    with pytest.raises(KitWheelError):
        install_wheel(other, tmp_path, owned)
    _, manpage = make_wheel('third', '1.0', data={'data/share/man/man1/third.1': 'x'})
    assert install_wheel(manpage, tmp_path / 'x', {})['skipped'] == 1 and not (tmp_path / 'x/share').exists()
    _, unknown = make_wheel('fourth', '1.0', data={'other/file': 'x'})
    with pytest.raises(KitWheelError):
        install_wheel(unknown, tmp_path / 'z', {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('../evil.py', 'x')
    with pytest.raises(KitWheelError):
        install_wheel(buffer.getvalue(), tmp_path / 'y', {})


def test_render_manifest_expands_module_commands(project):
    base, _ = project
    config = load_config(base / 'homensai-kit.json')
    template = json.loads((base / 'installer/manifest.json').read_text())
    locks = {name: {'file': f'locks/{name}.lock', 'sha256': 'a' * 64} for name in ('linux', 'windows')}
    manifest = render_manifest(template, config, locks, [{'file': 'vendor/x-1-py3-none-any.whl', 'sha256': 'b' * 64}])
    web = next(item for item in manifest['components'] if item['name'] == 'web')
    assert web['cmd'][:4] == ['@runtime/bin/python', '-I', '-B', '@release/homensai_entry.py']
    assert manifest['migrations']['cmd'][:3] == ['python', 'homensai_entry.py', 'kitdemo.migrate']
    assert manifest['compatibility'] == {'targets': [{'os': 'linux', 'architecture': 'x86_64', 'execution': 'native'}]}
    with pytest.raises(KitError):
        render_manifest({**template, 'runtime': {}}, config, locks, [])


def test_config_rejects_unsafe_values(project):
    base, _ = project
    path = base / 'homensai-kit.json'
    original = json.loads(path.read_text())
    for change in ({'sources': [{'from': '../outside', 'to': 'src'}]}, {'sources': [{'from': 'src', 'to': 'pydeps'}]},
                   {'targets': ['windows-x86_64']}, {'unknown': 1}, {'migrations': ['a', 'a']},
                   {'runtime': {'linux': {**original['runtime']['linux'], 'origin': 'http://x'}}}):
        path.write_text(json.dumps({**original, **change}))
        with pytest.raises(KitError):
            load_config(path)


def test_build_and_verify_kit(project, tmp_path):
    base, keys = project
    output = tmp_path / 'dist'
    result = build_kit(base / 'homensai-kit.json', version='1.2.0', signing_key=keys['private_key'], output=output)
    kit = Path(result['kit'])
    assert result['dependencies'] == {'linux-x86_64': ['demo_dep-1.0-py3-none-any.whl']}
    meta = json.loads((kit / 'kit.json').read_text())
    assert meta['product_id'] == 'homensai-kit-demo' and meta['version'] == '1.2.0'
    assert meta['modes'] == ['default'] and meta['migrations'] == ['notes-1']
    assert meta['public_key_sha256'] == keys['public_key_sha256']
    assert os.access(kit / 'setup.sh', os.X_OK)
    assert os.access(kit / 'installer/linux-x86_64/python/bin/python', os.X_OK)
    site = kit / 'installer/linux-x86_64/python/lib/python3.14/site-packages'
    assert (site / 'crypto_fake/__init__.py').is_file()
    assert (kit / 'installer/app/shared/kit_setup.py').is_file()
    assert not list(kit.rglob('__pycache__'))
    verified = verify_kit(kit, expected_key_sha256=keys['public_key_sha256'])
    assert verified['signature_verified'] and verified['publisher_trust_verified']
    with pytest.raises(KitError):
        verify_kit(kit, expected_key_sha256='0' * 64)
    with tarfile.open(result['archive']) as tar:
        members = {member.name: member for member in tar.getmembers()}
    assert members['homensai-kit-demo-1.2.0/setup.sh'].mode & 0o111
    with zipfile.ZipFile(kit / meta['package']) as archive:
        names = set(archive.namelist())
    for name in ('payload/homensai_entry.py', 'payload/homensai-release.json', 'payload/src/kitdemo/hello.py',
                 'payload/pydeps/linux-x86_64/demo_dep/__init__.py', 'payload/vendor/demo_dep-1.0-py3-none-any.whl',
                 'payload/runtime-linux/lib/homensai/portable_launcher.py', 'payload/systemd-user/pointer-v2.json',
                 'payload/locks/linux.lock', 'payload/runtime-linux/runtime-provenance-v1.json'):
        assert name in names
    assert not any('__pycache__' in name for name in names)
    with pytest.raises(KitError):
        build_kit(base / 'homensai-kit.json', version='1.2.0', signing_key=keys['private_key'], output=output)
    (kit / 'README.txt').write_text('changed')
    with pytest.raises(KitError):
        verify_kit(kit)


def test_entry_shim_runs_module_with_dependencies(project, tmp_path):
    base, keys = project
    result = build_kit(base / 'homensai-kit.json', version='7', signing_key=keys['private_key'],
                       output=tmp_path / 'dist', archive=False)
    kit = Path(result['kit'])
    release = tmp_path / 'release'
    with zipfile.ZipFile(kit / 'package/homensai-kit-demo-7.zip') as archive:
        for name in archive.namelist():
            if name.startswith('payload/') and not name.startswith('payload/runtime-linux/'):
                target = release / name.removeprefix('payload/')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    completed = subprocess.run([sys.executable, '-I', '-B', str(release / 'homensai_entry.py'), 'kitdemo.hello',
                                '--flag', 'x'], capture_output=True, text=True, timeout=30, check=True,
                               env={'PATH': '/usr/bin:/bin'})
    assert json.loads(completed.stdout) == {'argv': ['--flag', 'x'], 'dep': '1.0', 'release': '7',
                                            'product': 'homensai-kit-demo'}


def test_private_key_permissions_and_init(project, tmp_path):
    base, keys = project
    os.chmod(keys['private_key'], 0o644)
    with pytest.raises(KitError):
        build_kit(base / 'homensai-kit.json', version='1', signing_key=keys['private_key'], output=tmp_path / 'd')
    with pytest.raises(KitError):
        generate_keys(tmp_path / 'keys')
    created = init_project(tmp_path / 'new', product_id='acme-app', product_name='Acme')
    assert len(created) == 2
    manifest = json.loads((tmp_path / 'new/installer/manifest.json').read_text())
    assert manifest['components'][0]['cmd'][:2] == ['@module', 'acme_app.web']
    with pytest.raises(KitError):
        init_project(tmp_path / 'new', product_id='acme-app', product_name='Acme')
    with pytest.raises(KitError):
        init_project(tmp_path / 'bad', product_id='Bad Id', product_name='x')


def synthetic_windows_pbs(path):
    with tarfile.open(path, 'w:gz') as tar:
        for name, body in (('python/python.exe', b'MZ synthetic'), ('python/LICENSE.txt', b'PSF synthetic\n'),
                           ('python/Lib/os.py', b'# synthetic\n'), ('python/DLLs/tcl90.dll', b'excluded'),
                           ('python/Lib/site-packages/pip/__init__.py', b'excluded')):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_build_kit_with_windows_target(project, tmp_path, monkeypatch):
    base, keys = project
    config = json.loads((base / 'homensai-kit.json').read_text())
    archive = base / 'installer/python-win.tar.gz'
    winsw = base / 'installer/WinSW-x64.exe'
    winsw.write_bytes(b'MZ synthetic winsw')
    (base / 'installer/WinSW-LICENSE.txt').write_text('MIT synthetic')
    config['targets'] = ['linux-x86_64', 'windows-x86_64']
    config['runtime']['windows'] = {'archive': 'installer/python-win.tar.gz', 'sha256': synthetic_windows_pbs(archive),
                                    'origin': 'https://example.invalid/win.tar.gz', 'python_version': '3.14.10'}
    config['winsw'] = {'exe': 'installer/WinSW-x64.exe', 'license': 'installer/WinSW-LICENSE.txt',
                       'sha256': hashlib.sha256(winsw.read_bytes()).hexdigest()}
    path = base / 'homensai-kit.json'
    path.write_text(json.dumps(config))
    crypto_lock = kit_module.CRYPTO_LOCK
    filename, data = make_wheel('crypto-fake', '1.0', 'cp314-cp314-win_amd64')
    (base / 'wheels' / filename).write_bytes(data)
    entries = read_lock(crypto_lock)
    crypto_lock.write_text(lock_text([(item['name'], item['version'], item['sha256'] + [hashlib.sha256(data).hexdigest()])
                                      for item in entries]))
    result = build_kit(path, version='2', signing_key=keys['private_key'], output=tmp_path / 'dist', archive=False)
    kit = Path(result['kit'])
    assert set(result['dependencies']) == {'linux-x86_64', 'windows-x86_64'}
    assert (kit / 'setup.cmd').read_bytes().count(b'\r\n') >= 4 and (kit / 'setup.sh').is_file()
    python = kit / 'installer/windows-x86_64/python'
    assert (python / 'python.exe').is_file() and (python / 'Lib/site-packages/crypto_fake/__init__.py').is_file()
    assert not (python / 'DLLs/tcl90.dll').exists()
    with zipfile.ZipFile(kit / 'package/homensai-kit-demo-2.zip') as package:
        names = set(package.namelist())
        manifest = json.loads(package.read('package.json'))['manifest']
    assert {'payload/windows/WinSW-x64.exe', 'payload/runtime-windows/python.exe',
            'payload/runtime-windows/homensai-runtime.json', 'payload/pydeps/windows-x86_64/demo_dep/__init__.py'} <= names
    assert 'payload/runtime-windows/Lib/site-packages/pip/__init__.py' not in names
    assert manifest['winsw'] == {'version': '2.12.0', 'sha256': config['winsw']['sha256']}
    assert {'os': 'windows', 'architecture': 'x86_64', 'execution': 'native'} in manifest['compatibility']['targets']
    config['winsw']['sha256'] = '0' * 64
    path.write_text(json.dumps(config))
    with pytest.raises(KitError):
        build_kit(path, version='3', signing_key=keys['private_key'], output=tmp_path / 'dist', archive=False)


def test_build_rejects_hooks_for_windows_target(project, tmp_path):
    """A-04: подписанный комплект с хуками для Windows раньше собирался, а установка их пропускала."""
    base, keys = project
    path = base / 'homensai-kit.json'
    config = json.loads(path.read_text())
    winsw = base / 'installer/WinSW-x64.exe'
    winsw.write_bytes(b'MZ synthetic winsw')
    (base / 'installer/WinSW-LICENSE.txt').write_text('MIT synthetic')
    linux_only = dict(config)
    config['targets'] = ['linux-x86_64', 'windows-x86_64']
    config['runtime']['windows'] = {'archive': 'installer/python-win.tar.gz',
                                    'sha256': synthetic_windows_pbs(base / 'installer/python-win.tar.gz'),
                                    'origin': 'https://example.invalid/win.tar.gz', 'python_version': '3.14.10'}
    config['winsw'] = {'exe': 'installer/WinSW-x64.exe', 'license': 'installer/WinSW-LICENSE.txt',
                       'sha256': hashlib.sha256(winsw.read_bytes()).hexdigest()}
    path.write_text(json.dumps(config))
    manifest_path = base / 'installer/manifest.json'
    template = json.loads(manifest_path.read_text())
    template['hooks'] = {'pre_start': [['python', 'src/kitdemo/hello.py']],
                         'health_checks': [['python', 'src/kitdemo/hello.py']], 'post_update': []}
    manifest_path.write_text(json.dumps(template))
    with pytest.raises(KitError, match='pre_start, health_checks не поддерживаются для цели windows-x86_64'):
        build_kit(path, version='2', signing_key=keys['private_key'], output=tmp_path / 'dist', archive=False)
    assert sorted(item.name for item in (tmp_path / 'dist').iterdir()) == []
    # Для одной цели Linux хуки по-прежнему допустимы.
    linux_only['runtime'] = {'linux': config['runtime']['linux']}
    path.write_text(json.dumps(linux_only))
    result = build_kit(path, version='2', signing_key=keys['private_key'], output=tmp_path / 'dist', archive=False)
    with zipfile.ZipFile(Path(result['kit']) / 'package/homensai-kit-demo-2.zip') as package:
        assert json.loads(package.read('package.json'))['manifest']['hooks'] == template['hooks']


def test_windows_service_xml_rendering(tmp_path):
    from shared.datahome import resolve_datahome
    from shared.kit_windows import WindowsServices
    manifest = json.loads((ROOT / 'examples/kit_demo/installer/manifest.json').read_text())
    locks = {name: {'file': f'locks/{name}.lock', 'sha256': 'a' * 64} for name in ('linux', 'windows')}
    config = {'targets': ['linux-x86_64', 'windows-x86_64']}
    manifest = render_manifest(manifest, config, locks, [{'file': 'vendor/x-1-py3-none-any.whl', 'sha256': 'b' * 64}])
    home = resolve_datahome(manifest, tmp_path / 'root & каталог')
    services = WindowsServices(manifest, home, ['default'], 'homensai-kit-demo')
    assert list(services.components) == ['web', 'worker']
    text = services.xml('worker', '3')
    assert '<id>homensai.homensai-kit-demo.worker</id>' in text and '<depend>homensai.homensai-kit-demo.web</depend>' in text
    assert str(home.roots['runtime'] / 'python.exe').replace('&', '&amp;') in text
    assert 'root &amp; каталог' in text and 'root & ' not in text
    assert 'app-3' in text and 'kitdemo.worker' in text and '<startmode>Automatic</startmode>' in text


@pytest.mark.parametrize('tag, expected', [
    ('cp313-none-any', False), ('cp313-none-win_amd64', False), ('cp313-cp313-manylinux_2_17_x86_64', False),
    ('cp315-abi3-manylinux_2_17_x86_64', False), ('py399-none-any', False), ('cp314-cp314t-manylinux_2_17_x86_64', False),
    ('cp313-abi3-manylinux_2_17_x86_64', True), ('cp36-abi3-manylinux2014_x86_64', True),
    ('cp314-cp314-manylinux_2_17_x86_64', True), ('py3-none-any', True), ('py2.py3-none-any', True),
    ('py314-none-any', True), ('cp314-none-any', True),
])
def test_wheel_tag_matrix_matches_cpython_314(tag, expected):
    assert bool(compatible('x-1-' + tag + '.whl', 'linux-x86_64')) is expected
