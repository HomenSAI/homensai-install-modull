"""Independent SDK/CLI acceptance of local data-only signed help updates.

Artifacts are signed here from packaged baseline data using only standard
canonical JSON/hash primitives and real Ed25519. No SDK verifier/digest/signing
helper supplies the expected result; no product operation is executed.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from homensai_install.api.v1 import InstallerAPI
from homensai_install.cli import main
from homensai_install.error_catalog_updates import CatalogUpdatePolicy
from homensai_install.errors import ErrorCatalog, InstallerError, error_envelope


CODE = 'DISK_SPACE_LOW'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def stamp(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def filesystem_image(root):
    if not root.exists():
        return None
    result = {}
    for p in [root, *sorted(root.rglob('*'))]:
        s = p.lstat()
        row = {'identity': (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
            s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)}
        if stat.S_ISREG(s.st_mode):
            row['sha256'] = sha(p.read_bytes())
        elif stat.S_ISLNK(s.st_mode):
            row['link'] = os.readlink(p)
        result[p.relative_to(root).as_posix()] = row
    return result


class NoProductEffects:
    def __getattr__(self, name):
        raise AssertionError('Error catalog help must not invoke product engine: ' + name)


def catalog_bundle(tmp_path):
    if os.name != 'posix':
        pytest.skip('Local signed catalog writer acceptance requires the POSIX secure state backend')
    builtin = json.loads(resources.files('homensai_install').joinpath('catalog/errors.v1.json').read_text())
    assert len(builtin['errors']) == 43
    semantics = deepcopy(builtin)
    semantics.pop('catalog_version')
    for entry in semantics['errors'].values():
        entry.pop('translations')
    semantic_hash = sha(canonical(semantics))
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = [now]
    valid_from, valid_until = stamp(now - timedelta(days=1)), stamp(now + timedelta(days=60))
    def policy(**extra):
        values = dict(policy_id='independent-dev-help', policy_epoch=1,
            module_id='homensai-install', catalog_id='homensai-installer-errors',
            baseline_semantics_sha256=semantic_hash, min_sequence=1, max_lifetime_seconds=7776000,
            valid_from=valid_from, valid_until=valid_until, allow_development=True, clock=lambda: clock[0])
        values.update(extra)
        return CatalogUpdatePolicy({'catalog-dev': public}, **values)
    state = tmp_path / 'sdk-state'
    def api(*, locale='ru', selected_policy=None):
        return InstallerAPI(state, engine=NoProductEffects(), locale=locale,
                            error_catalog_policy=policy() if selected_policy is None else selected_policy)
    def artifact(*, sequence=1, version='1.0.1', marker='CatalogHIM070', mutate=None, signer=None):
        catalog = deepcopy(builtin)
        catalog['catalog_version'] = version
        for locale in ('ru', 'en', 'de'):
            catalog['errors'][CODE]['translations'][locale]['title'] = marker + ' ' + locale
        if mutate:
            mutate(catalog)
        raw = canonical(catalog)
        metadata = {'schema_version': 1, 'kind': 'error_catalog_update', 'purpose': 'installer-error-catalog',
            'module_id': 'homensai-install', 'catalog_id': 'homensai-installer-errors',
            'catalog_schema_version': 1, 'api_major': 1, 'baseline_semantics_sha256': semantic_hash,
            'catalog_version': version, 'sequence': sequence, 'issued_at': stamp(now - timedelta(minutes=1)),
            'expires_at': stamp(now + timedelta(days=2)), 'development': True,
            'catalog_sha256': sha(raw), 'catalog_bytes': len(raw)}
        return sign(metadata, catalog, signer=signer)
    def sign(metadata, catalog, *, signer=None):
        import base64
        signature = (private if signer is None else signer).sign(canonical(metadata))
        return {'metadata': deepcopy(metadata), 'catalog': deepcopy(catalog), 'signature': {
            'algorithm': 'ed25519', 'key_id': 'catalog-dev', 'signature': base64.b64encode(signature).decode()}}
    def save(value, name='help-update.json'):
        path = tmp_path / name
        path.write_bytes(canonical(value) + b'\n')
        path.chmod(0o600)
        return path
    def config():
        import base64
        return {'schema_version': 1, 'policy_id': 'independent-dev-help', 'policy_epoch': 1,
            'module_id': 'homensai-install', 'catalog_id': 'homensai-installer-errors',
            'baseline_semantics_sha256': semantic_hash, 'min_sequence': 1,
            'valid_from': valid_from, 'valid_until': valid_until, 'max_lifetime_seconds': 7776000,
            'keys': {'catalog-dev': base64.b64encode(public).decode()},
            'revoked_key_ids': [], 'revoked_artifact_sha256': []}
    return SimpleNamespace(root=tmp_path, state=state, builtin=builtin, clock=clock, now=now,
        policy=policy, api=api, artifact=artifact, sign=sign, save=save, config=config)


@pytest.fixture
def catalog_case(tmp_path):
    return catalog_bundle(tmp_path)


def reject(call, code):
    with pytest.raises(InstallerError) as error:
        call()
    assert error.value.code == code


def activate(value, api=None, *, name='help-update.json'):
    api = value.api() if api is None else api
    artifact = value.save(value.artifact(), name)
    preview = api.errors_catalog_preview(artifact)
    applied = api.errors_catalog_apply(artifact, expected_review_digest=preview['review_digest'], confirmed=True)
    assert applied['status'] == 'APPLIED' and applied['changed'] is True
    return api, artifact, preview, applied


def test_unconfigured_help_keeps_builtin_without_creating_state(tmp_path):
    state = tmp_path / 'uncreated'
    api = InstallerAPI(state, engine=NoProductEffects())
    assert api.errors_catalog_status()['configured'] is False
    assert api.errors_catalog_status()['effective_source'] == 'builtin'
    assert api.errors_describe(CODE)['catalog_version'] == ErrorCatalog().version
    assert not state.exists()


def test_preview_is_readonly_apply_is_explicit_and_distinct_instances_reopen_locales(catalog_case):
    value = catalog_case
    first, second = value.api(), value.api(locale='en')
    artifact = value.save(value.artifact())
    before = filesystem_image(value.state)
    assert first.errors_catalog_status()['effective_source'] == 'builtin'
    preview = first.errors_catalog_preview(artifact)
    assert preview['status'] == 'PREVIEW' and preview['requires_confirmation'] is True
    assert preview['artifact_sha256'] == sha(artifact.read_bytes())
    assert filesystem_image(value.state) == before
    reject(lambda: first.errors_catalog_apply(artifact, expected_review_digest=preview['review_digest'], confirmed=False), 'CONSENT_REQUIRED')
    assert first.errors_catalog_status()['effective_source'] == 'builtin'
    assert not value.state.exists()
    result = first.errors_catalog_apply(artifact, expected_review_digest=preview['review_digest'], confirmed=True)
    assert result['status'] == 'APPLIED' and result['artifact_sha256'] == preview['artifact_sha256']
    assert stat.S_IMODE(value.state.stat().st_mode) == 0o700
    assert stat.S_IMODE((value.state / 'error-catalog').stat().st_mode) == 0o700
    before_listing = filesystem_image(value.state)
    assert value.api().operations_list()['operations'] == []
    assert filesystem_image(value.state) == before_listing
    for api in (first, second, value.api()):
        for locale in ('ru', 'en', 'de'):
            described = api.errors_describe(CODE, locale=locale)
            assert described['title'] == 'CatalogHIM070 ' + locale and described['catalog_version'] == '1.0.1'
            assert described['severity'] == value.builtin['errors'][CODE]['severity']
            assert described['permitted_actions'] == value.builtin['errors'][CODE]['permitted_actions']
            assert [r['code'] for r in api.errors_search('CatalogHIM070', locale=locale)] == [CODE]
    assert ErrorCatalog().describe(CODE)['catalog_version'] == value.builtin['catalog_version']
    assert ErrorCatalog().describe(CODE)['title'] != 'CatalogHIM070 ru'
    snapshot = filesystem_image(value.state)
    repeated = first.errors_catalog_apply(artifact, expected_review_digest=preview['review_digest'], confirmed=True)
    assert repeated['status'] == 'UNCHANGED' and repeated['changed'] is False
    assert filesystem_image(value.state) == snapshot


def test_first_apply_creates_only_missing_ancestors_private_and_preserves_existing_mode(catalog_case):
    value = catalog_case
    existing = value.root / 'existing-parent'
    existing.mkdir(mode=0o755)
    before = existing.lstat()
    state = existing / 'new-parent' / 'new-state'
    api = InstallerAPI(state, engine=NoProductEffects(), error_catalog_policy=value.policy())
    artifact = value.save(value.artifact())
    preview = api.errors_catalog_preview(artifact)
    assert not (existing / 'new-parent').exists()
    api.errors_catalog_apply(artifact, expected_review_digest=preview['review_digest'], confirmed=True)
    after = existing.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (
        before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid)
    for path in (existing / 'new-parent', state, state / 'error-catalog'):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700 and path.stat().st_uid == os.getuid()
    reopened = InstallerAPI(state, engine=NoProductEffects(), error_catalog_policy=value.policy())
    assert reopened.operations_list()['operations'] == []


@pytest.mark.parametrize('fault', ['signature', 'foreign_key', 'retryable', 'actions', 'missing_code', 'role', 'duplicate'])
def test_independent_signed_artifact_cannot_replace_authority_or_behavior(catalog_case, fault):
    value = catalog_case
    api = value.api()
    record = value.artifact()
    if fault == 'signature':
        record['signature']['signature'] = 'A' * 88
    elif fault == 'foreign_key':
        record = value.artifact(signer=Ed25519PrivateKey.generate())
    elif fault in ('retryable', 'actions', 'missing_code'):
        def mutate(catalog):
            if fault == 'retryable': catalog['errors'][CODE]['retryable'] = not catalog['errors'][CODE]['retryable']
            elif fault == 'actions': catalog['errors'][CODE]['permitted_actions'].append('execute-untrusted')
            else: catalog['errors'].pop('UNKNOWN_ERROR')
        record = value.artifact(mutate=mutate)
    elif fault == 'role':
        record['metadata']['purpose'] = 'product-release'
        record = value.sign(record['metadata'], record['catalog'])
    path = value.save(record)
    if fault == 'duplicate':
        path.write_bytes(b'{"metadata":{},' + path.read_bytes()[1:])
    before = filesystem_image(value.state)
    reject(lambda: api.errors_catalog_preview(path), 'INTEGRITY_FAILED')
    assert filesystem_image(value.state) == before
    assert api.errors_catalog_status()['effective_source'] == 'builtin'


def test_stale_review_cannot_apply_changed_valid_signed_artifact(catalog_case):
    value = catalog_case
    api = value.api()
    artifact = value.save(value.artifact())
    reviewed = api.errors_catalog_preview(artifact)
    value.save(value.artifact(sequence=2, version='1.0.2', marker='ChangedValidHelp'))
    reject(lambda: api.errors_catalog_apply(artifact, expected_review_digest=reviewed['review_digest'], confirmed=True), 'PLAN_STALE')
    assert api.errors_catalog_status()['effective_source'] == 'builtin'
    assert not value.state.exists()


@pytest.mark.parametrize('fault', ['expired', 'revoked_key', 'revoked_artifact'])
def test_readonly_fallback_preserves_antirollback_state(catalog_case, fault):
    value = catalog_case
    _, artifact, _, _ = activate(value)
    if fault == 'expired':
        value.clock[0] += timedelta(days=3)
        selected = value.policy()
    elif fault == 'revoked_key':
        selected = value.policy(revoked_key_ids=('catalog-dev',))
    else:
        selected = value.policy(revoked_artifact_sha256=(sha(artifact.read_bytes()),))
    api = value.api(selected_policy=selected)
    before = filesystem_image(value.state)
    status = api.errors_catalog_status()
    assert status['status'] == ('expired' if fault == 'expired' else 'revoked')
    assert status['effective_source'] == 'builtin' and status['stored_sequence'] == status['high_water_sequence'] == 1
    assert api.errors_describe(CODE)['catalog_version'] == value.builtin['catalog_version']
    assert filesystem_image(value.state) == before


def test_updated_help_does_not_rewrite_recorded_diagnostic_envelopes(catalog_case):
    from homensai_install.diagnostics import DiagnosticStore
    value = catalog_case
    diagnostics = DiagnosticStore(value.root / 'historical-diagnostics.sqlite3')
    operation = diagnostics.start_operation('update', operation_id='historic-help-error')
    old = error_envelope(CODE, locale='de', error_id='historical-space-error')
    diagnostics.record_error(operation, old)
    before = deepcopy(diagnostics.get(operation))
    api, _, _, _ = activate(value)
    assert api.errors_describe(CODE, locale='de')['title'] == 'CatalogHIM070 de'
    assert diagnostics.get(operation) == before
    assert old['catalog_version'] == value.builtin['catalog_version']
    assert ErrorCatalog().version == value.builtin['catalog_version']


def test_unreadable_initialized_history_falls_back_without_resetting_unknown_high_water(catalog_case):
    value = catalog_case
    _, artifact, _, _ = activate(value)
    database = value.state / 'error-catalog/catalog-state.sqlite3'
    database.write_bytes(b'SYNTHETIC_CORRUPT_CATALOG_DATABASE')
    before = filesystem_image(value.state)
    api = value.api()
    status = api.errors_catalog_status()
    assert status['status'] == 'unavailable' and status['effective_source'] == 'builtin'
    assert status['stored_sequence'] is None and status['high_water_sequence'] is None
    assert api.errors_describe(CODE)['catalog_version'] == value.builtin['catalog_version']
    reject(lambda: api.errors_catalog_preview(artifact), 'STATE_STORAGE_UNAVAILABLE')
    assert filesystem_image(value.state) == before


def invoke(capsys, *argv, api=None):
    code = main(['--json', *argv], api=api)
    captured = capsys.readouterr()
    assert captured.err == '' and len(captured.out.splitlines()) == 1
    return code, json.loads(captured.out)


def test_cli_explicit_preview_confirm_apply_reopen_and_lookup(catalog_case, capsys):
    value = catalog_case
    api = value.api()
    artifact = value.save(value.artifact())
    code, response = invoke(capsys, 'errors-catalog', 'preview', '--artifact', str(artifact), api=api)
    assert code == 0 and response['result']['status'] == 'PREVIEW'
    review = response['result']['review_digest']
    code, response = invoke(capsys, 'errors-catalog', 'apply', '--artifact', str(artifact),
        '--expected-review-digest', review, api=api)
    assert code != 0 and response['error']['code'] == 'CONSENT_REQUIRED'
    code, response = invoke(capsys, 'errors-catalog', 'apply', '--artifact', str(artifact),
        '--expected-review-digest', review, '--confirm', api=api)
    assert code == 0 and response['result']['status'] == 'APPLIED'
    code, response = invoke(capsys, 'errors-catalog', 'status', api=value.api())
    assert code == 0 and response['result']['effective_source'] == 'updated'
    code, response = invoke(capsys, '--locale', 'en', 'errors', '--code', CODE, api=value.api(locale='en'))
    assert code == 0 and response['result']['title'] == 'CatalogHIM070 en'


def test_cli_bootstrap_policy_is_explicit_and_json_cannot_supply_clock(catalog_case, capsys):
    value = catalog_case
    config = value.save(value.config(), 'trusted-catalog-policy.json')
    args = ('--state-dir', str(value.state), '--allow-development', '--error-catalog-policy-config', str(config))
    before = filesystem_image(value.state)
    code, response = invoke(capsys, *args, 'errors-catalog', 'status')
    assert code == 0 and response['result']['configured'] is True
    assert filesystem_image(value.state) == before
    bad = value.config(); bad['clock'] = '2026-10-29T00:00:00Z'
    value.save(bad, config.name)
    code, response = invoke(capsys, *args, 'errors-catalog', 'status')
    assert code != 0 and response['error']['code'] in {'INVALID_REQUEST', 'INTEGRITY_FAILED'}
    assert filesystem_image(value.state) == before


def test_actual_cli_bootstrap_policy_applies_and_reopens_without_injected_api(catalog_case, capsys):
    value = catalog_case
    config = value.save(value.config(), 'catalog-role-policy.json')
    artifact = value.save(value.artifact())
    common = ('--state-dir', str(value.state), '--allow-development', '--error-catalog-policy-config', str(config))
    code, response = invoke(capsys, *common, 'errors-catalog', 'preview', '--artifact', str(artifact))
    assert code == 0 and response['result']['status'] == 'PREVIEW'
    review = response['result']['review_digest']
    code, response = invoke(capsys, *common, 'errors-catalog', 'apply', '--artifact', str(artifact),
        '--expected-review-digest', review, '--confirm')
    assert code == 0 and response['result']['status'] == 'APPLIED'
    code, response = invoke(capsys, *common, '--locale', 'de', 'errors', '--code', CODE)
    assert code == 0 and response['result']['title'] == 'CatalogHIM070 de'


def test_product_trust_keyring_cannot_authorize_catalog_artifact(catalog_case):
    import base64
    value = catalog_case
    config = value.save(value.config(), 'catalog-policy.json')
    product_private = Ed25519PrivateKey.generate()
    product_public = product_private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    product_trust = value.save({'schema_version': 1, 'keys': {
        'catalog-dev': base64.b64encode(product_public).decode()}}, 'product-only-trust.json')
    api = InstallerAPI.from_bootstrap(value.state, trust_config=product_trust, allow_development=True,
                                     error_catalog_policy_config=config)
    artifact = value.save(value.artifact(signer=product_private))
    reject(lambda: api.errors_catalog_preview(artifact), 'INTEGRITY_FAILED')
    assert not value.state.exists()
