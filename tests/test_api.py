"""Контракт раннего API v1: только согласование и чистая валидация."""

from copy import deepcopy
import base64
import json
from pathlib import Path
import subprocess
import sys

import pytest

from shared.api import MAX_BYTES, handle

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "examples/jobpilot.manifest.json").read_text(encoding="utf-8"))


def request(operation="capabilities", params=None, **overrides):
    result = {"api_major": 1, "request_id": "test-1", "operation": operation,
              "params": {} if params is None else params}
    result.update(overrides)
    return result


def cli(data):
    return subprocess.run([sys.executable, "-B", "-m", "shared.api"], cwd=ROOT,
                          input=data, capture_output=True, timeout=15)


def test_capabilities_only_advertises_read_only_operations(monkeypatch):
    monkeypatch.setattr("shared.api._signature_available", lambda: False)
    monkeypatch.setattr("shared.api._archive_import_available", lambda: False)
    response = handle(request())
    assert response["ok"] is True
    expected = ["capabilities", "validate", "preflight-fresh", "check-target", "update-plan-status", "plan-update", "inspect-update-recovery", "check-local-health"]
    if sys.platform.startswith('linux') or sys.platform == 'win32':
        expected.append('check-docker-engine')
    assert response["result"]["operations"] == expected
    assert response["result"]["execution_modes"] == []
    assert response["result"]["manifest_versions"] == [1, 2]


def test_archive_import_capability_requires_crypto(monkeypatch, tmp_path):
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': [], 'work_dir': str(tmp_path / 'work'),
              'archive': str(tmp_path / 'archive'), 'expected_sha256': 'a' * 64, 'password_env': 'PASSWORD'}
    monkeypatch.setattr('shared.api._archive_import_available', lambda: False)
    caps = handle(request())['result']
    assert 'import-update-archive' not in caps['operations']
    assert handle(request('import-update-archive', params))['error']['code'] == 'unsupported_operation'
    assert not (tmp_path / 'install').exists() and not (tmp_path / 'work').exists()
    monkeypatch.setattr('shared.api._archive_import_available', lambda: True)
    caps = handle(request())['result']
    assert caps['api_minor'] >= 27 and 'import-update-archive' in caps['mutating_operations']


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_archive_import_schema_and_localized_failure(tmp_path, monkeypatch, language):
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text(encoding='utf-8'))
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': [], 'work_dir': str(tmp_path / 'work'),
              'archive': str(tmp_path / 'archive'), 'expected_sha256': 'a' * 64, 'password_env': 'TEST_SECRET'}
    query = request('import-update-archive', params, language=language)
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid(query)
    monkeypatch.setattr('shared.api._archive_import_available', lambda: True)
    monkeypatch.setenv('TEST_SECRET', 'SECRET-SENTINEL')
    response = handle(query)
    assert response['error']['code'] == 'update_archive_import_failed'
    assert 'SECRET-SENTINEL' not in json.dumps(response) and str(tmp_path) not in json.dumps(response)
    assert not (tmp_path / 'install').exists() and not (tmp_path / 'work').exists()
    query['params']['expected_sha256'] = 'A' * 64
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'


@pytest.mark.parametrize('guarded', [False, True])
def test_update_status_reads_matching_state_without_effects(tmp_path, monkeypatch, guarded):
    from shared.update_plan import UpdatePlan
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': ['manifest'], 'guard_effects': guarded}
    plan = UpdatePlan(manifest, Path(params['install_root']), object(), release='new',
                      modes=('autonomous',), guard_effects=guarded)
    monkeypatch.setattr(UpdatePlan, 'run', lambda *args, **kwargs: pytest.fail('status ran update'))
    assert handle(request('update-plan-status', params))['result'] == {'state': None}
    assert not Path(params['install_root']).exists()
    state = {'version': 1, 'transaction': 'a' * 32, 'plan': plan.digest, 'old_release': 'old',
             'release': 'new', 'direction': 'forward', 'index': 0, 'failed_phase': None,
             'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    plan.state_path.parent.mkdir(parents=True)
    plan.state_path.write_text(json.dumps(state), encoding='utf-8')
    before = plan.state_path.read_bytes()
    query = request('update-plan-status', params)
    assert handle(query)['result'] == {'state': state}
    result = cli(json.dumps(query).encode())
    assert result.returncode == 0 and json.loads(result.stdout)['result'] == {'state': state}
    assert plan.state_path.read_bytes() == before and not plan.lock_path.exists()
    params['guard_effects'] = not guarded
    response = handle(request('update-plan-status', params, language='en'))
    assert response['error']['code'] == 'update_status_failed'
    assert response['error']['message'] == 'Could not read update state'
    assert str(tmp_path) not in json.dumps(response)


@pytest.mark.parametrize('field,value', [('modes', []), ('modes', [True]),
    ('guard_effects', 1), ('migration_ids', [None]), ('install_root', '')])
def test_update_status_rejects_bad_parameter_types(field, value, tmp_path):
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': [], 'guard_effects': True}
    params[field] = value
    assert handle(request('update-plan-status', params))['error']['code'] == 'invalid_request'
    assert not (tmp_path / 'install').exists()


def test_update_status_request_schema_accepts_and_rejects_guard_type(tmp_path):
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator.check_schema(schema)
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': [], 'guard_effects': True}
    validator = jsonschema.Draft202012Validator(schema)
    query = request('update-plan-status', params)
    assert validator.is_valid(query) and handle(query)['ok'] is True
    query['params']['guard_effects'] = 1
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'


def test_update_preview_exposes_order_without_reading_state_or_executing(tmp_path, monkeypatch):
    from shared.update_plan import UpdatePlan
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': ['one', 'two'], 'guard_effects': True}
    plan = UpdatePlan(manifest, Path(params['install_root']), object(), release='new',
                      modes=('autonomous',), migration_ids=('one', 'two'), guard_effects=True)
    monkeypatch.setattr(UpdatePlan, 'run', lambda *args, **kwargs: pytest.fail('preview ran update'))
    monkeypatch.setattr(UpdatePlan, 'status', lambda *args, **kwargs: pytest.fail('preview read state'))
    response = handle(request('plan-update', params))
    assert response['ok'] is True
    result = response['result']
    assert result == {'plan_sha256': plan.digest, 'components': list(plan.components),
                      'forward': list(plan.forward), 'rollback': list(plan.rollback),
                      'guard_effects': True, 'execution_supported': False, 'readiness_verified': False}
    assert result['forward'] == ['stop', 'archive', 'stage', 'backup:0', 'migrate:0', 'backup:1',
                                 'migrate:1', 'switch', 'start', 'health', 'post_update']
    assert not Path(params['install_root']).exists()
    assert 'plan-update' in handle(request())['result']['operations']
    assert 'plan-update' not in handle(request())['result']['mutating_operations']


@pytest.mark.parametrize('language', ['ru', 'en', 'de'])
def test_update_preview_rejects_invalid_plan_with_localized_error(tmp_path, language):
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['invalid'], 'migration_ids': [], 'guard_effects': True}
    result = handle(request('plan-update', params, language=language))
    assert result['ok'] is False and result['error']['code'] == 'update_plan_failed'
    assert str(tmp_path) not in json.dumps(result)
    assert not Path(params['install_root']).exists()


def test_update_preview_request_schema_and_cli(tmp_path):
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text(encoding='utf-8'))
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': [], 'guard_effects': True}
    query = request('plan-update', params)
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid(query)
    output = cli(json.dumps(query).encode())
    assert output.returncode == 0 and json.loads(output.stdout) == handle(query)
    query['params']['guard_effects'] = 1
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'


def test_recovery_inspection_is_read_only_and_does_not_authorize_resume(tmp_path, monkeypatch):
    from shared.update_plan import UpdatePlan
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': ['migration'], 'guard_effects': True}
    plan = UpdatePlan(manifest, Path(params['install_root']), object(), release='new',
                      modes=('autonomous',), migration_ids=('migration',), guard_effects=True)
    query = request('inspect-update-recovery', params)
    caps = handle(request())['result']
    assert caps['api_minor'] >= 26 and query['operation'] in caps['operations']
    assert query['operation'] not in caps['mutating_operations']
    monkeypatch.setattr(UpdatePlan, 'run', lambda *args, **kwargs: pytest.fail('inspection executed plan'))
    result = handle(query)['result']
    assert result == {'plan_sha256': plan.digest, 'outcome': None, 'direction': None, 'phase': None,
                      'operation_key': None, 'indeterminate': False, 'archive_binding_review_candidate': False,
                      'effects_verified': False, 'resume_authorized': False}
    assert not Path(params['install_root']).exists()
    plan.state_path.parent.mkdir(parents=True)
    state = {'version': 1, 'transaction': 'a' * 32, 'plan': plan.digest, 'old_release': 'old',
             'release': 'new', 'direction': 'forward', 'index': 1, 'failed_phase': None,
             'archive_ready': False, 'outcome': 'running', 'indeterminate': True}
    for direction, phase, uncertain, candidate in [
        ('forward', 'archive', True, True), ('forward', 'backup:0', True, True),
        ('forward', 'stage', True, False), ('forward', 'archive', False, False),
        ('rollback', 'before_rollback', True, True), ('rollback', 'restore', True, False),
    ]:
        phases = plan.forward if direction == 'forward' else plan.rollback
        state.update(direction=direction, index=phases.index(phase),
                     failed_phase=None if direction == 'forward' else 'health',
                     archive_ready=(phase != 'archive') if direction == 'forward' else True)
        if uncertain:
            state['indeterminate'] = True
        else:
            state.pop('indeterminate', None)
        plan._save(state)
        before = plan.state_path.read_bytes()
        result = handle(query)['result']
        assert result['phase'] == phase and result['operation_key'] == 'a' * 32 + '/' + phase
        assert result['indeterminate'] is uncertain and result['archive_binding_review_candidate'] is candidate
        assert result['effects_verified'] is False and result['resume_authorized'] is False
        assert plan.state_path.read_bytes() == before and not plan.lock_path.exists()
    state.update(direction='forward', index=len(plan.forward), failed_phase=None,
                 archive_ready=True, outcome='updated')
    state.pop('indeterminate', None)
    plan._save(state)
    result = handle(query)['result']
    assert result['outcome'] == 'updated' and result['phase'] is None and result['operation_key'] is None
    assert result['archive_binding_review_candidate'] is False and result['resume_authorized'] is False
    unguarded = UpdatePlan(manifest, Path(params['install_root']), object(), release='new',
                           modes=('autonomous',), migration_ids=('migration',), guard_effects=False)
    state.update(plan=unguarded.digest, direction='forward', index=unguarded.forward.index('archive'),
                 archive_ready=False, outcome='running', indeterminate=True)
    unguarded._save(state)
    params['guard_effects'] = False
    result = handle(query)['result']
    assert result['indeterminate'] is True and result['archive_binding_review_candidate'] is False


def test_recovery_inspection_schema_cli_and_invalid_checkpoint(tmp_path):
    from shared.update_plan import UpdatePlan
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((ROOT / 'shared/api-v1-request.schema.json').read_text(encoding='utf-8'))
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': [], 'guard_effects': True}
    query = request('inspect-update-recovery', params, language='de')
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid(query)
    output = cli(json.dumps(query).encode())
    assert output.returncode == 0 and json.loads(output.stdout) == handle(query)
    plan = UpdatePlan(manifest, Path(params['install_root']), object(), release='new',
                      modes=('autonomous',), migration_ids=(), guard_effects=True)
    plan.state_path.parent.mkdir(parents=True)
    plan.state_path.write_text('SECRET-SENTINEL', encoding='utf-8')
    response = handle(query)
    assert response['error']['code'] == 'update_status_failed'
    assert 'SECRET-SENTINEL' not in json.dumps(response) and str(tmp_path) not in json.dumps(response)
    query['params']['guard_effects'] = 1
    assert not validator.is_valid(query) and handle(query)['error']['code'] == 'invalid_request'


def test_update_status_rejects_oversized_checkpoint_without_echo(tmp_path):
    from shared.update_plan import MAX_STATE_BYTES, UpdatePlan
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': ['manifest'], 'guard_effects': False}
    plan = UpdatePlan(manifest, Path(params['install_root']), object(), release='new', modes=('autonomous',))
    plan.state_path.parent.mkdir(parents=True)
    plan.state_path.write_bytes(b'SECRET-SENTINEL' * (MAX_STATE_BYTES // 15 + 1))
    response = handle(request('update-plan-status', params))
    assert response['error']['code'] == 'update_status_failed'
    assert 'SECRET-SENTINEL' not in json.dumps(response)


@pytest.mark.skipif(not hasattr(__import__('os'), 'mkfifo'), reason='FIFO недоступен')
def test_update_status_refuses_fifo_without_blocking(tmp_path):
    import os
    from shared.update_plan import UpdatePlan
    manifest = json.loads((ROOT / 'examples/rent.manifest.json').read_text(encoding='utf-8'))
    params = {'manifest': manifest, 'install_root': str(tmp_path / 'install'), 'release': 'new',
              'modes': ['autonomous'], 'migration_ids': ['manifest'], 'guard_effects': False}
    plan = UpdatePlan(manifest, Path(params['install_root']), object(), release='new', modes=('autonomous',))
    plan.state_path.parent.mkdir(parents=True)
    os.mkfifo(plan.state_path)
    assert handle(request('update-plan-status', params))['error']['code'] == 'update_status_failed'


def test_missing_signature_dependency_does_not_advertise_or_fallback(monkeypatch):
    monkeypatch.setattr("shared.api._signature_available", lambda: False)
    params = {"package_path": "/nonexistent", "envelope": "e30=", "trusted_public_key": "YQ==",
              "expected_product_id": "demo", "expected_version": "1"}
    response = handle(request("verify-package", params))
    assert response["error"]["code"] == "unsupported_operation"


@pytest.mark.parametrize('operation', ['fresh-status', 'verify-native-release'])
def test_fresh_status_is_not_advertised_without_signature_support(monkeypatch, operation):
    monkeypatch.setattr("shared.api._signature_available", lambda: False)
    assert operation not in handle(request())["result"]["operations"]
    params = {"package_path": "/missing", "envelope": "e30=", "trusted_public_key": "YQ==",
              "expected_product_id": "demo", "expected_version": "1", "install_root": "/missing"}
    assert handle(request(operation, params))["error"]["code"] == "unsupported_operation"


@pytest.mark.parametrize("name,product,version", [
    ("jobpilot", "homensai-jobpilot-pro", 1),
    ("rent", "homensai-rent-demo", 2),
])
def test_validate_python_and_cli_agree(name, product, version):
    manifest = json.loads((ROOT / "examples" / (name + ".manifest.json")).read_text(encoding="utf-8"))
    value = request("validate", {"manifest": manifest})
    response = handle(value)
    assert response["result"] == {"product_id": product, "schema_version": version}
    result = cli(json.dumps(value).encode())
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout) == response


@pytest.mark.parametrize("change,code", [
    ({"api_major": 2}, "unsupported_version"),
    ({"api_major": True}, "invalid_request"),
    ({"operation": "install"}, "unsupported_operation"),
    ({"operation": "../secret-value"}, "unsupported_operation"),
    ({"params": {"unexpected": "secret-value"}}, "invalid_request"),
    ({"request_id": "invalid request secret-value"}, "invalid_request"),
    ({"unknown": "secret-value"}, "invalid_request"),
])
def test_reject_unsupported_and_malformed_requests_without_values(change, code):
    response = handle(request(**change))
    assert response["ok"] is False and response["error"]["code"] == code
    assert "secret-value" not in json.dumps(response)


def test_manifest_value_is_not_echoed_in_error():
    manifest = deepcopy(MANIFEST)
    manifest["password"] = "SECRET-SENTINEL"
    response = handle(request("validate", {"manifest": manifest}))
    assert response["error"]["code"] == "invalid_request"
    assert "SECRET-SENTINEL" not in json.dumps(response)


def test_reject_cycles_and_deep_python_requests():
    cycle = []
    cycle.append(cycle)
    assert handle(request(params={"cycle": cycle}))["error"]["code"] == "invalid_request"
    nested = None
    for _ in range(80):
        nested = [nested]
    assert handle(request(params={"nested": nested}))["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("payload", [
    b'{"api_major":1,"api_major":1}', b'{"api_major":NaN}',
    b'{"unexpected":"SECRET-SENTINEL"}', b'\xff',
    b" " * (MAX_BYTES + 1),
])
def test_cli_rejects_bad_json_and_oversize_without_echo(payload):
    result = cli(payload)
    assert result.returncode == 2 and result.stderr == b""
    response = json.loads(result.stdout)
    assert response["error"]["code"] == "invalid_request"
    assert b"SECRET-SENTINEL" not in result.stdout


def test_python_input_is_not_mutated():
    original = request("validate", {"manifest": deepcopy(MANIFEST)})
    before = deepcopy(original)
    handle(original)
    assert original == before


def test_cli_rejects_arguments_without_echo():
    result = subprocess.run([sys.executable, "-B", "-m", "shared.api", "SECRET-SENTINEL"],
                            cwd=ROOT, input=b"", capture_output=True, timeout=15)
    assert result.returncode == 2 and b"SECRET-SENTINEL" not in result.stdout + result.stderr


def test_verify_signed_package_python_and_cli(tmp_path):
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    from shared.package import build_package
    from shared.package_signing import sign_package

    source = tmp_path / "source"
    source.mkdir()
    (source / "app.txt").write_bytes(b"synthetic application")
    package = tmp_path / "package.zip"
    build_package(MANIFEST, "1.0", source, package)
    private = ed25519.Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=private,
                            expected_product_id=MANIFEST["product_id"], expected_version="1.0")
    params = {"package_path": str(package), "envelope": base64.b64encode(envelope).decode(),
              "trusted_public_key": base64.b64encode(public).decode(),
              "expected_product_id": MANIFEST["product_id"], "expected_version": "1.0"}
    query = request("verify-package", params)
    assert "verify-package" in handle(request())["result"]["operations"]
    response = handle(query)
    assert response["ok"] is True and response["result"]["file_count"] == 1
    result = cli(json.dumps(query).encode())
    assert result.returncode == 0 and json.loads(result.stdout) == response
    package.write_bytes(package.read_bytes() + b"tamper")
    response = handle(query)
    assert response["error"]["code"] == "verification_failed"
    result = cli(json.dumps(query).encode())
    assert result.returncode == 1 and json.loads(result.stdout) == response
    assert str(package) not in result.stdout.decode() and result.stderr == b""


def test_verify_package_rejects_malformed_parameters_without_reading(monkeypatch):
    monkeypatch.setattr("shared.api._signature_available", lambda: True)
    params = {"package_path": "/nonexistent", "envelope": "SECRET-INVALID!",
              "trusted_public_key": "AAAA", "expected_product_id": "demo", "expected_version": "1"}
    response = handle(request("verify-package", params))
    assert response["error"]["code"] == "invalid_request"
    assert "SECRET-INVALID!" not in json.dumps(response)


def test_fresh_preflight_api_is_read_only_and_cli_agrees(tmp_path):
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text(encoding="utf-8"))
    manifest["paths"] = {name: name for name in manifest["paths"]}
    names = ("app", "releases", "data", "backup", "services", "config", "runtime")
    root = tmp_path / "new-installation"
    params = {"manifest": manifest, "install_root": str(root),
              "required_bytes": {name: 0 for name in names}, "reserve_bytes": 0}
    query = request("preflight-fresh", params)
    response = handle(query)
    assert response["ok"] and response["result"]["checks_passed"]
    assert "package_authenticity" in response["result"]["not_checked"]
    result = cli(json.dumps(query).encode())
    cli_response = json.loads(result.stdout)
    assert result.returncode == 0 and cli_response["result"]["directories"] == response["result"]["directories"]
    assert cli_response["result"]["checks_passed"]
    assert not root.exists()
    (root / "data").mkdir(parents=True)
    (root / "data/keep").write_text("preserve")
    result = cli(json.dumps(query).encode())
    assert result.returncode == 1 and json.loads(result.stdout)["error"]["code"] == "preflight_failed"
    assert (root / "data/keep").read_text() == "preserve"


@pytest.mark.parametrize('language,expected', [
    ('ru', 'Операция API не поддерживается'),
    ('en', 'API operation is not supported'),
    ('de', 'Die API-Operation wird nicht unterstützt'),
])
def test_api_localizes_errors_without_changing_code(language, expected):
    query = request('unknown', language=language)
    response = handle(query)
    assert response['error'] == {'code': 'unsupported_operation', 'message': expected, 'retryable': False}
    result = cli(json.dumps(query).encode())
    assert result.returncode == 2 and json.loads(result.stdout) == response


def test_languages_are_discoverable_and_legacy_default_is_unchanged():
    capabilities = handle(request())['result']
    assert capabilities['api_minor'] >= 4
    assert {'de', 'en', 'ru'} <= set(capabilities['languages'])
    assert handle(request('unknown'))['error']['message'] == 'Операция API не поддерживается'


@pytest.mark.parametrize('language', ['../SECRET-SENTINEL', 'xx', 'RU', None, 1, {}])
def test_unknown_or_invalid_language_rejected_without_echo(language):
    response = handle(request('unknown', language=language))
    assert response['error']['code'] == 'invalid_request'
    assert 'SECRET-SENTINEL' not in json.dumps(response)


def test_selected_language_applies_to_invalid_manifest_and_invalid_params():
    response = handle(request('validate', {'manifest': {}}, language='en'))
    assert response['error']['message'] == 'Manifest does not meet the contract'
    response = handle(request('validate', {}, language='de'))
    assert response['error']['message'] == 'Ungültige API-Anfrage'


def test_missing_or_broken_catalog_does_not_hide_failure(monkeypatch):
    import shared.api as api
    def broken(*args, **kwargs):
        raise api.LanguageError('broken')
    monkeypatch.setattr(api, 'translate', broken)
    error = api._error('test', 'verification_failed', 'Подпись или пакет не прошли проверку',
                       'de', 'api.verification_failed')
    assert error['error']['code'] == 'verification_failed'
    assert error['error']['message'] == 'Подпись или пакет не прошли проверку'
