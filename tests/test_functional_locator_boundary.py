"""Public API locator/privacy contracts; fake backend, no native/Docker/Windows execution."""
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from homensai_install.api.model import digest
from homensai_install.api.v1 import InstallerAPI


ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 64
SOURCE_SHA = 'b' * 64


class ArtifactBackend:
    """Only structured result transport; never invokes commands or filesystem actions."""
    def __init__(self, produce):
        self.produce = produce
        self.calls = 0

    def capabilities(self, target):
        return {'actions': {name: {'supported': True} for name in ('backup', 'restore', 'rollback', 'install', 'uninstall', 'start')}}

    def discover(self, manifest, target, options):
        return {'fingerprint_data': {'fixture': 'stable'}}

    def plan(self, action, manifest, target, inventory, options):
        return {'steps': [], 'required_consents': ['action:' + action],
                'resources': [options['install_root'], options['work_dir']],
                'recovery_source_sha256': SOURCE_SHA, 'provisioning_spec': {'contract_sha256': 'c' * 64}}

    def execute(self, plan, emit, canceled):
        self.calls += 1
        emit({'type': 'step.progress', 'phase': 'fixture', 'error': {
            'code': 'UNKNOWN_ERROR', 'details': {'path': '/home/synthetic-user/private', 'api_key': 'synthetic-secret'}}})
        return self.produce(plan)

    def resume(self, plan, previous, emit, canceled):
        return self.execute(plan, emit, canceled)


def setup_api(tmp_path, produce, *, action='backup', mode='native', target_os='linux', options=None):
    backend = ArtifactBackend(produce)
    api = InstallerAPI(tmp_path / 'api-state', engine=backend)
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text())
    chosen = {'install_root': str(tmp_path / 'install'), 'work_dir': str(tmp_path / 'work'), 'release': 'release-2'}
    chosen.update(options or {})
    plan = api.plan(action, manifest, {'os': target_os, 'arch': 'x64', 'mode': mode, 'scope': 'user'}, options=chosen)
    return api, backend, plan


def submit(api, plan):
    return api.submit(plan['plan_id'], plan['plan_digest'], [*plan['required_consents'], plan['review_consent_ref']],
                      idempotency_key='artifact-roundtrip', background=False)


def native_archive(plan):
    token = digest([plan['plan_id'], plan['plan_digest']])[:32]
    path = plan['options'].get('archive_path', str(PurePosixPath(plan['options']['work_dir']) / (token + '.hsac')))
    return {'status': 'SUCCEEDED', 'archive': {'path': path, 'sha256': SHA, 'encrypted': True,
            'release': plan['options']['release'], 'includes_recovery_payload': True}}


def docker_archive(plan):
    token = hashlib.sha256(plan['plan_id'].encode()).hexdigest()
    path = plan['options'].get('archive_path', str(PurePosixPath(plan['options']['work_dir']) / ('data-' + token + '.haes')))
    return {'status': 'SUCCEEDED', 'backup': {'archive_path': path, 'archive_sha256': SHA, 'encrypted': True,
            'release': plan['options']['release'], 'contains_images': False}}


def safety_archive(plan):
    token = digest([plan['plan_id'], plan['plan_digest']])[:32]
    path = str(PurePosixPath(plan['options']['work_dir']) / (token + '-before-' + plan['action'] + '.hsac'))
    proof = {'schema_version': 1, 'kind': 'before_recovery_backup',
        'product_id': plan['source_manifest']['product_id'], 'installation_id': '1' * 32,
        'operation_id': 'op_' + token, 'from_release': plan['options']['release'],
        'from_manifest_sha256': 'c' * 64, 'from_inventory_sha256': 'd' * 64, 'from_metadata_sha256': 'e' * 64,
        'recovery_action': plan['action'], 'source_archive_sha256': SOURCE_SHA,
        'archive_sha256': SHA, 'archive_manifest_sha256': 'f' * 64,
        'encrypted': True, 'includes_recovery_payload': True, 'migration_eligible': False,
        'verification_scope': 'full_snapshot_and_signed_recovery_payload_restored',
        'consistency_scope': 'owned_components_stopped_and_adapter_quiesced'}
    proof = {'receipt_id': digest(proof), **proof}
    return {'status': 'SUCCEEDED', 'before_recovery': proof,
            'before_recovery_archive': {'path': path, 'sha256': SHA, 'release': plan['options']['release'], 'encrypted': True},
            'external_actions_on_hold': True}


def docker_recovery(plan):
    folder = PurePosixPath(plan['options']['install_root']) / '.hinstall/docker-recovery' / (plan['options']['release'] + '-' + 'c' * 16)
    return {'status': 'SUCCEEDED', 'recovery': {'metadata_path': str(folder / 'recovery.json'),
            'payload_dir': str(folder / 'payload'), 'contains_images': False, 'images_retained': True}}


def assert_invalid(operation):
    assert operation['status'] == 'RECOVERY_REQUIRED'
    assert operation['error']['code'] == 'UNEXPECTED_ERROR'
    assert operation['error']['details']['reason'] == 'invalid_artifact_locator'


def test_explicit_native_export_outside_work_roundtrips_status_reopen_and_duplicate(tmp_path):
    path = str(tmp_path / 'exports/home/synthetic-user/synthetic@example.test/token=chosen/archive.hsac')
    api, backend, plan = setup_api(tmp_path, native_archive, options={'archive_path': path})
    result = submit(api, plan)
    assert result['status'] == 'SUCCEEDED'
    assert result['result']['archive']['path'] == path
    assert api.get_plan(plan['plan_id'])['options']['archive_path'] == path
    assert api.status(result['operation_id'])['result']['archive']['path'] == path
    assert api.events(result['operation_id'])['snapshot']['result']['archive']['path'] == path
    reopened = InstallerAPI(tmp_path / 'api-state', engine=backend)
    duplicate = submit(reopened, plan)
    assert duplicate['operation_id'] == result['operation_id'] and duplicate['result']['archive']['path'] == path
    assert backend.calls == 1


@pytest.mark.parametrize('mode,produce,key,path_key', [
    ('native', native_archive, 'archive', 'path'), ('docker', docker_archive, 'backup', 'archive_path')])
def test_default_locator_uses_reviewed_work_and_original_operation(tmp_path, mode, produce, key, path_key):
    api, _, plan = setup_api(tmp_path, produce, mode=mode,
        options={'work_dir': str(tmp_path / 'home/synthetic-user/recovery')})
    expected = produce(plan)[key][path_key]
    result = submit(api, plan)
    assert result['status'] == 'SUCCEEDED'
    assert result['result'][key][path_key] == expected


@pytest.mark.parametrize('action', ['restore', 'rollback'])
def test_before_recovery_locator_keeps_verified_receipt_binding(tmp_path, action):
    api, _, plan = setup_api(tmp_path, safety_archive, action=action,
        options={'work_dir': str(tmp_path / 'home/synthetic-user/recovery'),
                 'archive_path': str(tmp_path / 'source/original.hsac'), 'acknowledge_data_loss': True})
    expected = safety_archive(plan)
    result = submit(api, plan)
    assert result['status'] == 'SUCCEEDED'
    assert result['result']['before_recovery_archive'] == expected['before_recovery_archive']
    assert result['result']['before_recovery'] == expected['before_recovery']
    assert result['result']['before_recovery_archive']['path'] != plan['options']['archive_path']


def test_unknown_nested_paths_and_diagnostic_export_remain_redacted(tmp_path):
    private = '/home/synthetic-user/private-report'
    def produce(plan):
        result = native_archive(plan)
        result.update(path=private, nested={'archive': {'path': private}, 'password': 'synthetic-secret',
            'note': 'token=synthetic-secret', 'url': 'https://user:pass@example.test/?token=synthetic-secret'})
        return result
    explicit = str(tmp_path / 'home/synthetic-user/export.hsac')
    api, _, plan = setup_api(tmp_path, produce, options={'archive_path': explicit})
    operation = submit(api, plan)
    assert operation['status'] == 'SUCCEEDED'
    assert operation['result']['archive']['path'] == explicit
    unknown = json.dumps({key: value for key, value in operation['result'].items() if key != 'archive'})
    assert private not in unknown and 'synthetic-secret' not in unknown and 'user:pass' not in unknown
    events = json.dumps(api.events(operation['operation_id'])['events'])
    assert private not in events and 'synthetic-secret' not in events
    preview = api.diagnostics_export_preview(operation['operation_id'])
    exported = json.dumps(preview['report'])
    assert private not in exported and explicit not in exported and 'synthetic-secret' not in exported


@pytest.mark.parametrize('field,value', [('path', '/home/other-user/unreviewed.hsac'),
    ('path', '../relative.hsac'), ('path', ['not-a-path']), ('sha256', 'broken'), ('encrypted', False)])
def test_native_mismatched_or_malformed_artifact_is_refused(tmp_path, field, value):
    def produce(plan):
        result = native_archive(plan);result['archive'][field] = value;return result
    api, _, plan = setup_api(tmp_path, produce, options={'archive_path': str(tmp_path / 'approved.hsac')})
    assert_invalid(submit(api, plan))


@pytest.mark.parametrize('field,value', [('operation_id', 'op_foreign'), ('recovery_action', 'backup'),
    ('archive_sha256', '9' * 64), ('source_archive_sha256', '8' * 64), ('from_release', 'foreign-release')])
def test_before_recovery_tampered_binding_is_refused(tmp_path, field, value):
    def produce(plan):
        result = safety_archive(plan);result['before_recovery'][field] = value;return result
    api, _, plan = setup_api(tmp_path, produce, action='restore', options={'archive_path': str(tmp_path / 'input.hsac')})
    assert_invalid(submit(api, plan))


def test_docker_explicit_locator_requires_owned_work_directory(tmp_path):
    chosen = str(tmp_path / 'work/home/synthetic-user-export.haes')
    # A nested export is outside Docker's exact owned work parent, even though
    # it is explicitly supplied; Native's outside-work allowance does not apply.
    api, _, plan = setup_api(tmp_path, docker_archive, mode='docker', options={'archive_path': chosen})
    assert_invalid(submit(api, plan))


@pytest.mark.parametrize('style', ['windows-users', 'long-posix'])
def test_valid_reviewed_lexical_locators_are_not_renamed_or_truncated(tmp_path, style):
    # Pure transport tests: no Windows process/drive or long filesystem path is accessed.
    if style == 'windows-users':
        path = str(PureWindowsPath(r'C:\Users\Synthetic User\Exports\chosen.hsac'))
        options = {'install_root': r'C:\Users\Synthetic User\Application',
                   'work_dir': r'C:\Users\Synthetic User\Recovery', 'archive_path': path}
        target_os = 'windows'
    else:
        path = '/tmp/owned/' + 'segment/' * 600 + 'archive.hsac'
        options = {'archive_path': path};target_os = 'linux'
    api, _, plan = setup_api(tmp_path, native_archive, target_os=target_os, options=options)
    result = submit(api, plan)
    assert result['status'] == 'SUCCEEDED' and result['result']['archive']['path'] == path


@pytest.mark.parametrize('action', ['install', 'uninstall'])
def test_docker_retained_recovery_resource_locators_are_reversible(tmp_path, action):
    api, _, plan = setup_api(tmp_path, docker_recovery, action=action, mode='docker',
        options={'install_root': str(tmp_path / 'home/synthetic-user/product')})
    result = submit(api, plan)
    assert result['status'] == 'SUCCEEDED'
    assert result['result']['recovery'] == docker_recovery(plan)['recovery']


def test_docker_retained_recovery_wrong_contract_directory_is_refused(tmp_path):
    def produce(plan):
        result = docker_recovery(plan)
        for field in ('metadata_path', 'payload_dir'):
            result['recovery'][field] = result['recovery'][field].replace('c' * 16, 'd' * 16)
        return result
    api, _, plan = setup_api(tmp_path, produce, action='uninstall', mode='docker')
    assert_invalid(submit(api, plan))
