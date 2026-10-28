"""CI failure diagnostics expose fixed classes, never private command output."""
import importlib.util
import json
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
