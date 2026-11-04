"""Доверие к комплекту вне подписанного пакета: код установщика, kit.json, порядок выпусков."""
import json

import pytest

from shared import kit_setup
from shared.kit_setup import SetupError
from tests.test_kit_setup import env, make_kit  # noqa: F401 — фикстура env

STATE = 'home/.local/state/homensai/demo'


def installed(base, client, setup, monkeypatch):
    setup(make_kit(base)).install()
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}


def test_tampered_installer_code_is_refused_before_changes(env, monkeypatch):
    base, client, setup = env
    installed(base, client, setup, monkeypatch)
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    (kit2 / 'installer/app/shared').mkdir(parents=True)
    (kit2 / 'installer/app/shared/kit_setup.py').write_text('# подмена')
    calls = len(client.calls)
    with pytest.raises(SetupError) as refused:
        setup(kit2).update()
    assert refused.value.key == 'kit.installer_mismatch'
    assert len(client.calls) == calls
    state = json.loads((base / STATE / 'state.json').read_text())
    assert state['version'] == '1' and 'update' not in state


def test_installer_replaced_after_check_is_not_stored(env, monkeypatch):
    base, client, setup = env
    installed(base, client, setup, monkeypatch)
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    original = kit_setup.Setup.store_installer

    def swap_then_store(self, state):
        # Файл комплекта меняется после проверки signed_kit, но до копирования.
        (self.kit.root / 'setup.sh').write_text('подмена')
        return original(self, state)
    monkeypatch.setattr(kit_setup.Setup, 'store_installer', swap_then_store)
    with pytest.raises(SetupError) as refused:
        setup(kit2).update()
    assert refused.value.key == 'kit.installer_mismatch'
    stored = base / STATE / 'installer'
    assert (stored / 'setup.sh').read_text() == 'synthetic'
    assert json.loads((stored / 'kit.json').read_text())['version'] == '1'
    assert not (base / STATE / '.installer.tmp').exists()
    state = json.loads((base / STATE / 'state.json').read_text())
    assert state['version'] == '2' and state['pending_installer'] == '2'


def test_unsigned_kit_json_values_are_refused(env):
    base, client, setup = env
    kit = make_kit(base)
    meta = json.loads((kit / 'kit.json').read_text())
    meta['required_bytes']['data'] = 1
    (kit / 'kit.json').write_text(json.dumps(meta))
    with pytest.raises(SetupError) as refused:
        setup(kit).install()
    assert refused.value.key == 'kit.kit_unreadable'
    assert not client.calls


def test_stored_installer_matches_signed_files(env):
    base, client, setup = env
    setup(make_kit(base)).install()
    stored = base / STATE / 'installer'
    assert (stored / 'setup.sh').read_text() == 'synthetic'
    assert not (stored / 'README.txt').exists()
    assert (stored / 'package/publisher-ed25519.pub').read_bytes() == b'k' * 32


def test_downgrade_is_refused_unless_allowed(env, monkeypatch):
    base, client, setup = env
    installed(base, client, setup, monkeypatch)
    assert setup(make_kit(base, '3', ('m-1',))).update() == 0
    older = make_kit(base / 'old', '2', ('m-1',))
    calls = len(client.calls)
    with pytest.raises(SetupError) as refused:
        setup(older).update()
    assert refused.value.key == 'kit.downgrade'
    assert len(client.calls) == calls
    assert setup(older).update(allow_downgrade=True) == 0
    state = json.loads((base / STATE / 'state.json').read_text())
    assert state['version'] == '2'


def test_secret_from_environment_needs_confirmation(env, monkeypatch):
    base, client, setup = env
    instance = setup(make_kit(base), yes=False, answers=['нет'], secrets=['typed-value'])
    monkeypatch.setenv('GITHUB_TOKEN', 'from-shell')
    item = {'name': 'GITHUB_TOKEN', 'required': True, 'generate': False, 'prompt': ''}
    assert instance.ask_value(item) == 'typed-value'
    instance = setup(make_kit(base / 'b'), yes=False, answers=['да'])
    assert instance.ask_value(item) == 'from-shell'


def test_hidden_input_never_falls_back_to_echo(monkeypatch):
    import getpass
    import warnings

    def fallback(prompt=''):
        warnings.warn('echo', getpass.GetPassWarning)
        return 'echoed'
    monkeypatch.setattr(getpass, 'getpass', fallback)
    with pytest.raises(SetupError) as refused:
        kit_setup._hidden_input('?')
    assert refused.value.key == 'kit.no_terminal'


def test_release_sequence_rules():
    from shared.kit import KitError, release_sequence
    assert release_sequence('1.10.0') == [1, 10, 0]
    assert release_sequence('1.10') > release_sequence('1.9.9')
    for bad in ('', '1.x', '1..2', '-1', '1.2.3.4.5.6.7', 1):
        with pytest.raises(KitError):
            release_sequence(bad)
