"""CI failure diagnostics expose fixed classes, never private command output."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def harness(monkeypatch):
    folder = Path(__file__).parents[1] / 'packaging'
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location('reference_ci_diagnostic', folder / 'check_installed_reference_lifecycle.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('code,log,reason', [
    (127, 'python: error while loading shared libraries: libpython3.14.so.1.0: cannot open shared object file', 'python_shared_library_missing'),
    (127, 'error while loading shared libraries: libother.so: cannot open shared object file', 'shared_library_missing'),
    (1, "ModuleNotFoundError: No module named 'encodings'", 'python_stdlib_missing'),
    (1, "ModuleNotFoundError: No module named 'private-module'", 'python_module_missing'),
    (1, 'Permission denied: /private/path', 'execution_permission_denied'),
    (1, 'Exec format error', 'executable_format_invalid'),
    (1, 'AssertionError: private diagnostic', 'python_assertion_failed'),
    (-9, 'private output', 'process_signal'),
    (3, 'unrecognized private output', 'command_failed'),
])
def test_known_command_failures_get_fixed_classes(harness, code, log, reason):
    assert harness._command_failure_reason(code, log) == reason


@pytest.mark.parametrize('error,reason,error_class', [
    (AssertionError('Неожиданная ссылка product runtime'), 'unexpected_runtime_symlink', 'validation_assertion'),
    (AssertionError('/private/assertion'), 'validation_assertion_failed', 'validation_assertion'),
    (subprocess.TimeoutExpired(['/private/python'], 180), 'command_timeout', 'command_timeout'),
    (ModuleNotFoundError('/private/module'), 'python_module_missing', 'python_import'),
    (OSError('/private/error'), 'filesystem_operation_failed', 'filesystem_error'),
    (RuntimeError('/private/error'), 'harness_operation_failed', 'unexpected_error'),
])
def test_noncommand_exceptions_never_echo_error_messages(harness, error, reason, error_class):
    result = harness._safe_failure_summary(error, [{'exit_code': 0}], 'product_fixture')
    assert result == {'stage': 'product_fixture', 'command_index': 1, 'exit_code': 0,
                      'reason_class': reason, 'error_class': error_class}
    assert '/private/' not in json.dumps(result)


def test_untrusted_reason_and_stage_do_not_escape_fixed_vocabulary(harness):
    result = harness._safe_failure_summary(RuntimeError('private-literal'),
        [{'exit_code': 1, 'failure_reason_class': '/private/literal'}], '/private/stage')
    assert result == {'stage': 'setup', 'command_index': 1, 'exit_code': 1,
                      'reason_class': 'command_failed', 'error_class': 'command_failed'}


@pytest.mark.skipif(not hasattr(os, 'getuid'), reason='Linux installed harness entrypoint requires POSIX uid')
def test_failure_stdout_has_stage_index_exit_class_but_no_receipt_path_or_stderr(harness, tmp_path, monkeypatch, capsys):
    private = '/private/runner-path/private-literal-do-not-echo'
    def failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 127, '',
            'error while loading shared libraries: libpython3.14.so.1.0: cannot open shared object file ' + private)
    monkeypatch.setattr(harness.subprocess, 'run', failed)
    receipt = tmp_path / 'receipt.json'
    assert harness.main(['--work-dir', str(tmp_path / 'work'), '--receipt', str(receipt),
        '--wheelhouse', str(tmp_path / 'wheels'), '--sdk-revision', 'a' * 40, '--profile', 'fresh']) == 1
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary == {'status': 'FAIL', 'profile': 'fresh', 'failure': {
        'stage': 'source_snapshot', 'command_index': 1, 'exit_code': 127,
        'reason_class': 'python_shared_library_missing', 'error_class': 'command_failed'}}
    assert private not in output and str(tmp_path) not in output and 'libpython3.14' not in output
    # Private evidence still retains the exact original failure locally.
    assert private in json.loads(receipt.read_text())['error']['message']


@pytest.mark.parametrize('log,reason', [
    ("ImportError: /private/_ssl.so: version `OPENSSL_PRIVATE' not found", 'python_ssl_import_failed'),
    ("ImportError: /private/_sqlite3.so: undefined symbol: private_symbol", 'python_sqlite_import_failed'),
    ("ModuleNotFoundError: No module named '_ssl'", 'python_ssl_import_failed'),
])
def test_product_extension_import_failures_have_fixed_classes(harness, log, reason):
    assert harness._command_failure_reason(1, log) == reason
    assert '/private/' not in json.dumps(harness._safe_failure_summary(RuntimeError(log),
        [{'exit_code': 1, 'failure_reason_class': reason}], 'product_import_probe'))


def test_safe_client_summary_uses_catalog_codes_and_last_known_action(harness):
    report = {'status': 'FAIL', 'error': {'code': 'ADAPTER_EXECUTION_FAILED', 'type': 'InstallerError',
        'message': '/private/secret'}, 'observations': [{'status': 'PASS'}, {'status': 'FAIL'}, {'status': 'PASS'}],
        'actions': [{'action': 'install', 'status': 'SUCCEEDED', 'error_code': None},
                    {'action': 'update', 'status': 'RECOVERY_REQUIRED', 'error_code': 'INTEGRITY_FAILED'},
                    {'action': '/private/secret', 'status': '/private/secret', 'error_code': '/private/secret'}]}
    result = harness._safe_client_summary(report)
    assert result == {'available': True, 'status': 'FAIL', 'error_code': 'ADAPTER_EXECUTION_FAILED',
        'error_type': 'InstallerError', 'own_source_lines': [], 'passed_cases': 2,
        'last_safe_action': {'action': 'update', 'status': 'RECOVERY_REQUIRED', 'error_code': 'INTEGRITY_FAILED'}}
    assert '/private/' not in json.dumps(result)


@pytest.mark.parametrize('value', ['/private/secret', {'secret': 'value'}, ['SECRET'], 123, None])
def test_arbitrary_client_error_values_are_not_printed(harness, value):
    report = {'status': value, 'error': {'code': value, 'type': value},
        'observations': [{'status': value}], 'actions': [{'action': 'install', 'status': value, 'error_code': value}]}
    result = harness._safe_client_summary(report)
    assert result['error_code'] is None and result['error_type'] is None and result['status'] is None
    assert result['last_safe_action'] == {'action': 'install', 'status': None, 'error_code': None}
    assert result['passed_cases'] == 0 and 'secret' not in json.dumps(result).lower()


def test_only_codes_from_actual_sdk_catalog_are_exposed(harness):
    assert 'ADAPTER_EXECUTION_FAILED' in harness._CATALOG_ERROR_CODES
    result = harness._safe_client_summary({'error': {'code': 'HARNESS_CHECK_FAILED', 'type': 'AssertionError'}})
    assert result['error_code'] is None and result['error_type'] == 'AssertionError'


@pytest.mark.parametrize('content', [b'{', b'[]', b'\xff', b'x' * (1024 * 1024 + 1)])
def test_invalid_or_oversize_client_report_is_unavailable(harness, tmp_path, content):
    path = tmp_path / 'client-report.json';path.write_bytes(content)
    assert harness._safe_client_summary(harness._read_client_report(path)) == {'available': False}


def test_missing_client_report_and_bounded_counters(harness, tmp_path):
    assert harness._read_client_report(tmp_path / 'absent') is None
    assert harness._safe_client_summary({'observations': [{'status': 'PASS'}] * 2000})['passed_cases'] == 1024


def test_product_import_probe_uses_exact_product_python_and_no_loader_env(harness, tmp_path):
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs));return 'product_imports_ok\n'
    assert harness._probe_product_imports(tmp_path, command) == 'product_imports_ok\n'
    argv, kwargs = calls[0]
    assert argv[:4] == [tmp_path / 'payload/runtime/bin/python', '-I', '-B', '-c']
    assert argv[4] == "import ssl,sqlite3;import homensai_reference.adapter;print('product_imports_ok')"
    assert kwargs == {'cwd': tmp_path / 'payload'}


def test_installed_client_failure_whitelists_report_for_public_summary(harness):
    report = {'status': 'FAIL', 'error': {'code': 'INTEGRITY_FAILED', 'type': 'InstallerError',
        'message': '/private/secret'}, 'observations': [], 'actions': []}
    failure = harness._safe_failure_summary(RuntimeError('/private/secret'),
        [{'exit_code': 1, 'failure_reason_class': 'command_failed'}], 'installed_client', client_report=report)
    assert failure['client']['error_code'] == 'INTEGRITY_FAILED'
    assert failure['client']['error_type'] == 'InstallerError'
    assert failure['client']['passed_cases'] == 0 and failure['client']['last_safe_action'] is None
    assert '/private/' not in json.dumps(failure) and 'message' not in failure
    missing = harness._safe_failure_summary(RuntimeError('private'), [], 'installed_client')
    assert missing['client'] == {'available': False}


@pytest.fixture
def client_module():
    path = Path(__file__).parents[1] / 'packaging/installed_reference_lifecycle_client.py'
    spec = importlib.util.spec_from_file_location('reference_client_diagnostic', path)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_own_source_lines_ignore_external_frames_and_private_error_values(client_module, tmp_path):
    try:
        client_module.notes(tmp_path / 'private-do-not-echo.sqlite')
    except Exception as error:
        result = client_module._own_source_lines(error)
    else:
        pytest.fail('expected nonexistent readonly database error')
    assert result and all(type(number) is int and 1 <= number <= 10000 for number in result)
    assert len(result) <= 16 and str(tmp_path) not in json.dumps(result)
    try:
        raise ValueError('private-do-not-echo')
    except Exception as error:
        assert client_module._own_source_lines(error) == []


def test_own_source_lines_are_bounded_even_for_recursive_own_frames(client_module):
    namespace = {}
    exec(compile('def recur(n):\n if n: return recur(n-1)\n raise ValueError("private")\n',
                 client_module.__file__, 'exec'), namespace)
    try:
        namespace['recur'](40)
    except Exception as error:
        result = client_module._own_source_lines(error)
    assert len(result) == 16 and all(type(value) is int and 1 <= value <= 10000 for value in result)


@pytest.mark.parametrize('lines,expected', [
    ([1, 10000, 0, -1, 10001, True, 'private', {}, None], [1, 10000]),
    (['private'] * 16 + [999], []),
    ([42] * 30, [42] * 16),
    ('/private/path', []),
    ({'private': 7}, []),
    (None, []),
])
def test_public_summary_exposes_only_bounded_integer_lines(harness, lines, expected):
    result = harness._safe_client_summary({'error': {'own_source_lines': lines}})
    assert result['own_source_lines'] == expected and 'private' not in json.dumps(result)


@pytest.mark.parametrize('primary', [None, {'action': 'backup', 'status': 'SUCCEEDED', 'error_code': None}])
def test_primary_action_is_preserved_before_cleanup_appends_stop(harness, primary):
    report = {'primary_last_action': primary, 'actions': [
        {'action': 'backup', 'status': 'SUCCEEDED'}, {'action': 'stop', 'status': 'SUCCEEDED'}]}
    assert harness._safe_client_summary(report)['last_safe_action'] == primary


def test_malformed_primary_action_does_not_fall_back_to_cleanup(harness):
    result = harness._safe_client_summary({'primary_last_action': {'action': '/private/path'},
        'actions': [{'action': 'stop', 'status': 'SUCCEEDED'}]})
    assert result['last_safe_action'] is None and 'private' not in json.dumps(result)
