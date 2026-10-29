"""Клиент установленного каталога: без чтения исходников, сети и дочерних процессов."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import ssl
import stat
import sys


def image(root):
    if not root.exists():
        return None
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        item = {'identity': [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]}
        if stat.S_ISREG(info.st_mode):
            item['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            item['link'] = os.readlink(path)
        result[path.relative_to(root).as_posix()] = item
    return result


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    phase = sys.argv[2]
    checkout, installed = Path(config['checkout']), Path(config['site_packages'])
    denied = []
    def guard(event, arguments):
        if event == 'open' and isinstance(arguments[0], (str, bytes)):
            if Path(os.fsdecode(arguments[0])).absolute().is_relative_to(checkout):
                denied.append('checkout_read')
                raise AssertionError('Installed client attempted a checkout read')
        if event in {'socket.bind', 'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg', 'subprocess.Popen',
                'os.system', 'os.posix_spawn', 'os.exec', 'os.fork', 'os.forkpty'}:
            denied.append(event)
            raise AssertionError('Catalog client attempted network or child-process effects')
    sys.addaudithook(guard)
    assert not Path.cwd().is_relative_to(checkout)
    assert not any(Path(p or os.getcwd()).absolute().is_relative_to(checkout) for p in sys.path)
    import cffi
    import pycparser
    import _cffi_backend
    from homensai_install.api.v1 import InstallerAPI
    from homensai_install.errors import ErrorCatalog, InstallerError, error_envelope

    report = {'schema_version': 1, 'status': 'FAIL', 'phase': phase, 'pid': os.getpid(),
        'scope': 'Installed SDK/CLI local data-only catalog; no product/runtime acceptance',
        'production_ready': False}
    exit_code = 0
    code = 'DISK_SPACE_LOW'
    state = Path(config['sdk_state'])
    artifact = Path(config['artifact'])
    def api(*, root=state, policy_file=None, locale='ru'):
        return InstallerAPI.from_bootstrap(root, locale=locale, allow_development=True,
            error_catalog_policy_config=config['policy'] if policy_file is None else policy_file)
    def rejected(call, expected):
        try:
            call()
        except InstallerError as error:
            assert error.code == expected, (error.code, expected)
        else:
            raise AssertionError('Expected catalog refusal did not occur')
    try:
        if phase == 'sdk-apply':
            from homensai_install.diagnostics import DiagnosticStore
            first, before_activation = api(), api(locale='en')
            assert first.errors_catalog_status()['effective_source'] == 'builtin'
            assert not state.exists()
            preview = first.errors_catalog_preview(artifact)
            assert preview['artifact_sha256'] == hashlib.sha256(artifact.read_bytes()).hexdigest()
            assert preview['requires_confirmation'] is True and not state.exists()
            rejected(lambda: first.errors_catalog_preview(config['bad_signature']), 'INTEGRITY_FAILED')
            rejected(lambda: first.errors_catalog_preview(config['bad_role']), 'INTEGRITY_FAILED')
            rejected(lambda: first.errors_catalog_apply(config['second_artifact'],
                expected_review_digest=preview['review_digest'], confirmed=True), 'PLAN_STALE')
            rejected(lambda: first.errors_catalog_apply(artifact,
                expected_review_digest=preview['review_digest'], confirmed=False), 'CONSENT_REQUIRED')
            assert not state.exists()
            historical = DiagnosticStore(Path(config['history']))
            op = historical.start_operation('update', operation_id='synthetic-historical-error')
            historical.record_error(op, error_envelope(code, locale='de', error_id='catalog-history'))
            old = historical.get(op)
            applied = first.errors_catalog_apply(artifact, expected_review_digest=preview['review_digest'], confirmed=True)
            assert applied['status'] == 'APPLIED' and applied['changed'] is True
            assert stat.S_IMODE(state.stat().st_mode) == stat.S_IMODE((state / 'error-catalog').stat().st_mode) == 0o700
            assert api().operations_list()['operations'] == []
            for reader in (first, before_activation, api()):
                for locale in ('ru', 'en', 'de'):
                    observed = reader.errors_describe(code, locale=locale)
                    assert observed['title'] == 'InstalledCatalog ' + locale
                    assert observed['catalog_version'] == '1.0.1'
                    assert [row['code'] for row in reader.errors_search('InstalledCatalog', locale=locale)] == [code]
            assert historical.get(op) == old and old['errors'][0]['catalog_version'] == config['builtin_version']
            assert ErrorCatalog().version == config['builtin_version']
            snapshot = image(state)
            repeated = first.errors_catalog_apply(artifact, expected_review_digest=preview['review_digest'], confirmed=True)
            assert repeated['status'] == 'UNCHANGED' and repeated['changed'] is False and image(state) == snapshot
            isolated = InstallerAPI.from_bootstrap(config['isolated_state'], allow_development=True)
            assert isolated.errors_catalog_status()['effective_source'] == 'builtin'
            assert isolated.errors_describe(code)['catalog_version'] == config['builtin_version']
            assert not Path(config['isolated_state']).exists()
            report['scenarios'] = {'preview_readonly': True, 'signature_refusal': True, 'role_refusal': True, 'first_stale_readonly': True,
                'explicit_consent': True, 'applied': True, 'preexisting_instance_observes_update': True,
                'three_locales': True, 'idempotent_no_write': True, 'instance_isolation': True,
                'historical_envelope_unchanged': True, 'normal_operations_list': True}
        elif phase == 'sdk-reopen':
            from homensai_install.error_catalog_updates import CatalogUpdatePolicy
            before = image(state)
            assert api().errors_catalog_status()['status'] == 'active'
            for locale in ('ru', 'en', 'de'):
                assert api(locale=locale).errors_describe(code)['title'] == 'InstalledCatalog ' + locale
            revoked = api(policy_file=config['revoked_policy'])
            status = revoked.errors_catalog_status()
            assert status['status'] == 'revoked' and status['effective_source'] == 'builtin'
            assert status['stored_sequence'] == status['high_water_sequence'] == 1
            settings = json.loads(Path(config['policy']).read_text())
            settings.pop('schema_version')
            keys = {key: base64.b64decode(value, validate=True) for key, value in settings.pop('keys').items()}
            # Только доверенный синтетический контроллер передаёт время с
            # часовым поясом UTC. Файл каталога и JSON политики не содержат clock.
            future = datetime.strptime(config['trusted_test_time_after_expiry'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            policy = CatalogUpdatePolicy(keys, **settings, allow_development=True, clock=lambda: future)
            expired = InstallerAPI(state, error_catalog_policy=policy)
            expired_status = expired.errors_catalog_status()
            assert expired_status['status'] == 'expired' and expired_status['effective_source'] == 'builtin'
            assert expired_status['stored_sequence'] == expired_status['high_water_sequence'] == 1
            assert expired.errors_describe(code)['catalog_version'] == config['builtin_version']
            assert image(state) == before
            report['scenarios'] = {'new_process_reopen': True, 'three_locales': True,
                'revocation_readonly_fallback': True, 'expiry_readonly_fallback': True, 'high_water_preserved': True}
        elif phase == 'cli':
            sys.argv = [config['entrypoint'], *sys.argv[3:]]
            try:
                runpy.run_path(config['entrypoint'], run_name='__main__')
            except SystemExit as error:
                exit_code = error.code or 0
            report['exit_code'] = exit_code
        else:
            raise AssertionError('Unknown catalog client phase')
        origins = {}
        for name, module in list(sys.modules.items()):
            if name == 'shared' or name == '_cffi_backend' or name.startswith(('homensai_install', 'shared.', 'cryptography', 'cffi', 'pycparser')):
                filename = getattr(module, '__file__', None)
                if filename:
                    path = Path(filename).resolve()
                    assert path.is_relative_to(installed), (name, str(path))
                    origins[name] = path.relative_to(installed).as_posix()
        assert origins and not denied
        report.update(status='PASS', module_origins=origins, denied_attempts=denied, exit_code=exit_code,
            interpreter={'isolated': bool(sys.flags.isolated), 'no_user_site': bool(sys.flags.no_user_site),
                'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'base_executable': sys._base_executable})
        assert sys.flags.isolated and sys.flags.no_user_site and sys.prefix != sys.base_prefix
        assert Path(sys.prefix).resolve() == Path(config['venv']).resolve()
        assert not Path(sys.base_prefix).resolve().is_relative_to(checkout)
        assert not Path(sys._base_executable).resolve().is_relative_to(checkout)
    except Exception as error:
        safe = type(error).__name__
        report['error'] = {'type': safe if safe in {'AssertionError', 'InstallerError', 'ValueError', 'OSError', 'ImportError'} else 'Exception',
            'code': error.code if isinstance(error, InstallerError) else 'UNEXPECTED_ERROR'}
        raise
    finally:
        Path(config['client_report']).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
