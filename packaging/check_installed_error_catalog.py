"""Офлайн-приёмка установленного SDK/CLI и подписанной справки без исполнения продукта.

Вне рабочей копии собирается ровно один wheel SDK и ставятся только существующие
криптографические зависимости с точными хешами из lock-файла. Изолированные
клиенты сохраняют строгие запреты; прежние проверки минимального SDK и
эталонного продукта не меняются.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from check_installed_sdk import canonical, sha, package_files, check_wheel
from check_installed_prerequisites import security_wheels, check_installed_dependency


ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def fixture(work, source):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    directory = work / 'signed-fixture'
    directory.mkdir(mode=0o700)
    baseline = json.loads((source / 'homensai_install/catalog/errors.v1.json').read_text())
    assert len(baseline['errors']) == 43
    semantics = deepcopy(baseline)
    semantics.pop('catalog_version')
    for value in semantics['errors'].values():
        value.pop('translations')
    semantic_hash = sha(canonical(semantics))
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda value: value.strftime('%Y-%m-%dT%H:%M:%SZ')
    policy = {'schema_version': 1, 'policy_id': 'installed-dev-catalog', 'policy_epoch': 1,
        'module_id': 'homensai-install', 'catalog_id': 'homensai-installer-errors',
        'baseline_semantics_sha256': semantic_hash, 'min_sequence': 1,
        'valid_from': stamp(now - timedelta(days=1)), 'valid_until': stamp(now + timedelta(days=60)),
        'max_lifetime_seconds': 7776000, 'keys': {'catalog-dev': base64.b64encode(public).decode()},
        'revoked_key_ids': [], 'revoked_artifact_sha256': []}
    def save(name, value):
        path = directory / name
        path.write_bytes(canonical(value) + b'\n')
        path.chmod(0o600)
        return str(path)
    def artifact(sequence, version, *, purpose='installer-error-catalog'):
        data = deepcopy(baseline)
        data['catalog_version'] = version
        for locale in ('ru', 'en', 'de'):
            data['errors']['DISK_SPACE_LOW']['translations'][locale]['title'] = 'InstalledCatalog ' + locale
        raw = canonical(data)
        metadata = {'schema_version': 1, 'kind': 'error_catalog_update', 'purpose': purpose,
            'module_id': 'homensai-install', 'catalog_id': 'homensai-installer-errors',
            'catalog_schema_version': 1, 'api_major': 1, 'baseline_semantics_sha256': semantic_hash,
            'catalog_version': version, 'sequence': sequence, 'issued_at': stamp(now - timedelta(minutes=1)),
            'expires_at': stamp(now + timedelta(days=2)), 'development': True,
            'catalog_sha256': sha(raw), 'catalog_bytes': len(raw)}
        return {'metadata': metadata, 'catalog': data, 'signature': {'algorithm': 'ed25519',
            'key_id': 'catalog-dev', 'signature': base64.b64encode(private.sign(canonical(metadata))).decode()}}
    first = artifact(1, '1.0.1')
    bad = deepcopy(first)
    bad['signature']['signature'] = 'A' * 88
    revoked = {**policy, 'revoked_key_ids': ['catalog-dev']}
    return {'policy': save('trusted-policy.json', policy), 'revoked_policy': save('revoked-policy.json', revoked),
        'artifact': save('catalog-1.json', first), 'second_artifact': save('catalog-2.json', artifact(2, '1.0.2')),
        'bad_signature': save('bad-signature.json', bad),
        'bad_role': save('wrong-role.json', artifact(1, '1.0.1', purpose='product-release')),
        'builtin_version': baseline['catalog_version'],
        'trusted_test_time_after_expiry': stamp(now + timedelta(days=3)),
        'private_key_saved': False, 'baseline_semantics_sha256': semantic_hash}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--sdk-revision')
    parser.add_argument('--source-git-dir', type=Path)
    args = parser.parse_args()
    work = args.work_dir.absolute()
    if work.resolve().is_relative_to(ROOT) or ROOT.is_relative_to(work.resolve()):
        parser.error('work-dir must be outside checkout')
    if work.exists() or args.receipt.exists():
        parser.error('fresh work-dir and receipt required; preserve previous evidence')
    if sys.platform != 'linux' or platform.machine() not in {'x86_64', 'amd64'}:
        parser.error('This installed profile requires Linux x64')
    if args.sdk_revision is not None and re.fullmatch('[0-9a-f]{40}', args.sdk_revision) is None:
        parser.error('sdk-revision must be an exact full lowercase Git SHA')
    work.mkdir(parents=True, mode=0o700)
    logs = work / 'command-logs'
    logs.mkdir(mode=0o700)
    reports = work / 'reports'
    reports.mkdir(mode=0o700)
    environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    commands, clients = [], []
    receipt = {'schema_version': 1, 'status': 'FAIL', 'profile': 'catalog-updates',
        'scope': 'Installed SDK/CLI local signed data-only catalog, Linux DEV',
        'production_ready': False, 'gate_closed': False, 'clients': clients, 'commands': commands,
        'environment': {'python': sys.version, 'platform': platform.platform(), 'uid': os.getuid(),
            'external_work_directory': str(work), 'venv_system_site_packages': False}}
    def run(argv, *, expected=0):
        index = len(commands) + 1
        result = subprocess.run([str(value) for value in argv], cwd=work, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        out, err = logs / (str(index) + '.stdout'), logs / (str(index) + '.stderr')
        out.write_bytes(result.stdout); err.write_bytes(result.stderr)
        commands.append({'index': index, 'returncode': result.returncode, 'expected_returncode': expected,
            'stdout_sha256': sha(result.stdout), 'stderr_sha256': sha(result.stderr)})
        if result.returncode != expected:
            raise RuntimeError('installed_catalog_command_failed')
        return result.stdout.decode('utf-8')
    git_prefix = ['git', '--git-dir', str(args.source_git_dir)] if args.source_git_dir else ['git', '-C', str(ROOT)]
    def git(*argv):
        return subprocess.check_output([*git_prefix, *argv], env=environment)
    try:
        revision = args.sdk_revision or git('rev-parse', 'HEAD').decode().strip()
        assert re.fullmatch('[0-9a-f]{40}', revision)
        assert git('rev-parse', revision + '^{commit}').decode().strip() == revision
        receipt.update(source_baseline=revision, source_tree=git('rev-parse', revision + '^{tree}').decode().strip())
        environment['SOURCE_DATE_EPOCH'] = git('show', '-s', '--format=%ct', revision).decode().strip()
        receipt['environment']['source_date_epoch'] = environment['SOURCE_DATE_EPOCH']
        receipt['environment']['build_versions'] = {name: importlib.metadata.version(name)
            for name in ('pip', 'setuptools', 'packaging', 'cryptography', 'cffi', 'pycparser')}
        expected = package_files(ROOT)
        assert all(sha(git('show', revision + ':' + name)) == value for name, value in expected.items())
        pinned_names = ['packaging/check_installed_error_catalog.py', 'packaging/installed_error_catalog_client.py',
            'packaging/check_installed_sdk.py', 'packaging/check_installed_prerequisites.py',
            'requirements-build.lock', 'pyproject.toml', 'INSTALLER_API.md']
        pins = {name: sha((ROOT / name).read_bytes()) for name in pinned_names}
        assert all(sha(git('show', revision + ':' + name)) == value for name, value in pins.items())
        receipt.update(source_package_files=expected, source_package_digest=sha(canonical(expected)), source_harness_files=pins)
        snapshot = work / 'build-source'
        snapshot.mkdir(mode=0o700)
        for name in ('homensai_install', 'shared'):
            shutil.copytree(ROOT / name, snapshot / name,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', 'build', 'dist'))
        for name in ('pyproject.toml', 'INSTALLER_API.md'):
            shutil.copy2(ROOT / name, snapshot / name)
        assert package_files(snapshot) == expected
        wheels = work / 'wheels'; wheels.mkdir()
        run([sys.executable, '-I', '-B', '-m', 'pip', 'wheel', '--no-index', '--no-deps', '--no-cache-dir',
             '--no-build-isolation', '--wheel-dir', wheels, snapshot])
        wheel, = wheels.glob('homensai_install_module-*.whl')
        check_wheel(wheel, expected)
        receipt['wheel'] = {'name': wheel.name, 'sha256': sha(wheel.read_bytes()), 'exact_files_checked': len(expected)}
        dependencies, security_lock = security_wheels(args.wheelhouse.resolve(), work / 'security-wheels')
        receipt['dependencies'] = dependencies
        receipt['requirements_build_lock_sha256'] = pins['requirements-build.lock']
        receipt['security_install_lock_sha256'] = sha(security_lock.read_bytes())
        venv = work / 'environment'
        run([sys.executable, '-I', '-B', '-m', 'venv', venv])
        python = venv / 'bin/python'
        assert 'include-system-site-packages = false' in (venv / 'pyvenv.cfg').read_text().lower()
        run([python, '-I', '-B', '-c', "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('homensai_install','shared','cryptography','cffi','pycparser')); print('no checkout or dependency fallback')"])
        receipt['missing_packages_no_fallback'] = True
        run([python, '-I', '-B', '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile', '--no-cache-dir',
             '--require-hashes', '--only-binary=:all:', '--find-links', security_lock.parent, '-r', security_lock])
        run([python, '-I', '-B', '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-compile', '--no-cache-dir', wheel])
        run([python, '-I', '-B', '-m', 'pip', 'check'])
        site = Path(run([python, '-I', '-B', '-c', "import sysconfig; print(sysconfig.get_path('purelib'))"]).strip())
        def verify_installed():
            check_wheel(wheel, expected, site)
            actual = {p.relative_to(site).as_posix() for name in ('homensai_install', 'shared')
                      for p in (site / name).rglob('*') if p.is_file()}
            assert actual == set(expected), 'Installed SDK namespace has missing or extra files'
            assert all(not (site / name).is_symlink() for name in actual)
            for item in dependencies:
                checked = check_installed_dependency(security_lock.parent / item['filename'], site)
                if 'installed_files' in item:
                    assert checked == item['installed_files']
                else:
                    item['installed_files'] = checked
        verify_installed()
        client = work / 'external_client.py'
        shutil.copy2(ROOT / 'packaging/installed_error_catalog_client.py', client)
        config = fixture(work, snapshot)
        fixture_before = {p.name: sha(p.read_bytes()) for p in (work / 'signed-fixture').iterdir()}
        config.update(checkout=str(ROOT), site_packages=str(site), venv=str(venv), sdk_state=str(work / 'catalog-sdk-state'),
            isolated_state=str(work / 'unconfigured-sdk-state'), history=str(work / 'historical-diagnostics.sqlite3'),
            entrypoint=str(venv / 'bin/hinstall'))
        config_path = work / 'client-config.json'
        def client_run(label, phase, argv=(), expected_exit=0):
            path = reports / (label + '.json')
            config['client_report'] = str(path)
            write_json(config_path, config)
            output = run([python, '-I', '-B', client, config_path, phase, *argv], expected=expected_exit)
            report = json.loads(path.read_text())
            assert report['status'] == 'PASS' and report['denied_attempts'] == []
            clients.append({**report, 'label': label, 'report_sha256': sha(path.read_bytes())})
            return output
        client_run('sdk-apply', 'sdk-apply')
        client_run('sdk-reopen', 'sdk-reopen')
        common = ['--json', '--state-dir', str(work / 'catalog-cli-state'), '--allow-development',
                  '--error-catalog-policy-config', config['policy']]
        def cli(label, args, *, expected_exit=0):
            return json.loads(client_run(label, 'cli', [*common, *args], expected_exit))
        assert cli('cli-status-before', ['errors-catalog', 'status'])['result']['effective_source'] == 'builtin'
        preview = cli('cli-preview', ['errors-catalog', 'preview', '--artifact', config['artifact']])['result']
        apply_args = ['errors-catalog', 'apply', '--artifact', config['artifact'], '--expected-review-digest', preview['review_digest']]
        denied = cli('cli-consent-refusal', apply_args, expected_exit=2)
        assert denied['error']['code'] == 'CONSENT_REQUIRED'
        assert not (work / 'catalog-cli-state').exists()
        assert cli('cli-apply', [*apply_args, '--confirm'])['result']['status'] == 'APPLIED'
        assert cli('cli-status-reopen', ['errors-catalog', 'status'])['result']['effective_source'] == 'updated'
        assert cli('cli-idempotent', [*apply_args, '--confirm'])['result']['status'] == 'UNCHANGED'
        for locale in ('ru', 'en', 'de'):
            described = cli('cli-errors-' + locale, ['--locale', locale, 'errors', '--code', 'DISK_SPACE_LOW'])['result']
            assert described['title'] == 'InstalledCatalog ' + locale and described['catalog_version'] == '1.0.1'
        assert len({report['pid'] for report in clients}) == len(clients), 'Client processes were not distinct'
        assert {p.name: sha(p.read_bytes()) for p in (work / 'signed-fixture').iterdir()} == fixture_before
        assert package_files(ROOT) == package_files(snapshot) == expected
        assert {name: sha((ROOT / name).read_bytes()) for name in pinned_names} == pins
        verify_installed()
        receipt.update(status='PASS', fixture={'development': True, 'files': fixture_before,
            'unchanged': True, 'private_key_saved': False, 'baseline_semantics_sha256': config['baseline_semantics_sha256']},
            no_product_effects=True, network_or_child_attempts_in_clients=0,
            source_and_installed_bytes_unchanged=True, distinct_client_processes=len(clients))
    except Exception as error:
        receipt['error'] = {'reason': 'installed_catalog_check_failed',
            'type': type(error).__name__ if type(error).__name__ in {'AssertionError', 'RuntimeError', 'OSError', 'ValueError', 'MissingInput'} else 'Exception'}
    finally:
        receipt['checked_at_utc'] = datetime.now(timezone.utc).isoformat()
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.receipt, receipt)
    print(json.dumps({'status': receipt['status'], 'receipt': str(args.receipt),
        'wheel_sha256': receipt.get('wheel', {}).get('sha256'),
        'exact_files_checked': receipt.get('wheel', {}).get('exact_files_checked')}, ensure_ascii=False))
    return 0 if receipt['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
