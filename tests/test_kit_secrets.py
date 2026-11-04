"""Секреты продукта в комплекте: объявление, файл владельца, ввод в установщике и загрузчик выпуска."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from shared import kit, kit_secrets, kit_setup
from shared.kit_setup import Kit, Setup, SetupError
from tests.test_kit_setup import MIGRATIONS, SIGNED, FakeClient, last, make_kit

TOKEN = {'name': 'DEMO_TOKEN', 'required': True, 'generate': False, 'prompt': 'demo API token'}
SESSION = {'name': 'DEMO_SESSION', 'required': True, 'generate': True, 'prompt': ''}
OPTIONAL = {'name': 'DEMO_OPTIONAL', 'required': False, 'generate': False, 'prompt': ''}
DECLARED = {}


# --- объявление и файл ---------------------------------------------------------------
def test_declarations_are_normalized():
    # Продуктовые имена с префиксом HOMENSAI_ допустимы (ключ Rent).
    assert kit_secrets.check_declarations([{'name': 'HOMENSAI_RENT_DEV_KEY'}])[0]['name'] == 'HOMENSAI_RENT_DEV_KEY'
    assert kit_secrets.check_declarations([{'name': 'A_1'}, {'name': 'B', 'required': False, 'generate': True,
                                                             'prompt': 'text'}]) == [
        {'name': 'A_1', 'required': True, 'generate': False, 'prompt': ''},
        {'name': 'B', 'required': False, 'generate': True, 'prompt': 'text'}]


@pytest.mark.parametrize('items', [
    {'name': 'A'}, [{'name': 'a'}], [{'name': '1A'}], [{'name': 'A'}, {'name': 'A'}], [{'name': 'PATH'}],
    [{'name': 'HOMENSAI_RELEASE'}], [{'name': 'HOMENSAI_SECRET_X'}], [{'name': 'HOMENSAI_WEB_PORT'}],
    [{'name': 'HOMENSAI_ARCHIVE_PASSWORD'}], [{'name': 'PYTHONPATH'}], [{'name': 'LD_PRELOAD'}], [{'name': 'A', 'value': 'x'}],
    [{'name': 'A', 'required': 'yes'}], [{'name': 'A', 'prompt': 'two\nlines'}], [{'required': True}],
    [{'name': 'S' + str(index)} for index in range(65)]])
def test_invalid_declarations_are_rejected(items):
    with pytest.raises(kit_secrets.SecretsError):
        kit_secrets.check_declarations(items)


def test_file_round_trip_is_private_and_strict(tmp_path):
    path = tmp_path / 'secrets.env'
    kit_secrets.write(path, {'B': 'two=equals', 'A': 'пароль с пробелом '})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding='utf-8') == 'A=пароль с пробелом \nB=two=equals\n'
    assert kit_secrets.read(path) == {'A': 'пароль с пробелом ', 'B': 'two=equals'}
    assert kit_secrets.read(tmp_path / 'absent') == {}
    path.write_text('# comment\n\nA=1\n', encoding='utf-8')
    assert kit_secrets.read(path) == {'A': '1'}
    for text in ('lower=1\n', 'A\n', 'A=\n'):
        path.write_text(text, encoding='utf-8')
        with pytest.raises(kit_secrets.SecretsError):
            kit_secrets.read(path)
    with pytest.raises(kit_secrets.SecretsError):
        kit_secrets.write(path, {'A': 'line\nbreak'})
    link = tmp_path / 'link.env'
    link.symlink_to(path)
    with pytest.raises(kit_secrets.SecretsError):
        kit_secrets.read(link)
    assert not list(tmp_path.glob('.*.tmp'))


def test_posix_dir_is_tightened(tmp_path):
    directory = tmp_path / 'a/b'
    kit_secrets.prepare_posix_dir(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    directory.chmod(0o755)
    kit_secrets.prepare_posix_dir(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_locations(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    assert kit_secrets.location('demo', windows=False) == tmp_path / '.config/homensai/demo/secrets.env'
    assert kit_secrets.location('demo', windows=True, program_data=Path('PD')) == Path(
        'PD/HomenSAI/.secrets/demo/secrets.env')


# --- сборщик и загрузчик ---------------------------------------------------------------
def test_template_secrets_are_rejected_by_builder():
    template = {'secrets': [{'name': 'A', 'required': True, 'contexts': ['service'], 'same_value': False}]}
    with pytest.raises(kit.KitError, match='homensai-kit.json'):
        kit.render_manifest(template, {'targets': ['linux-x86_64']}, {}, [])


def release_dir(tmp_path, declared):
    root = tmp_path / 'root'
    release = root / 'releases/app-1'
    (release / 'src').mkdir(parents=True)
    (release / kit.ENTRY).write_text(kit.ENTRY_SOURCE, encoding='utf-8')
    (release / kit.RELEASE_INFO).write_text(json.dumps(
        {'product_id': 'demo', 'version': '1', 'python_paths': ['src'], 'migrations': [], 'secrets': declared}))
    (release / 'src/show.py').write_text(
        'import json, os\nprint(json.dumps({name: os.environ.get(name) for name in '
        '("DEMO_TOKEN", "DEMO_OPTIONAL", "OTHER")}))\n')
    return release


def run_entry(release, environment):
    completed = subprocess.run([sys.executable, '-I', '-B', str(release / kit.ENTRY), 'show'],
                               env=environment, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def test_entry_delivers_only_declared_secrets_and_file_wins(tmp_path):
    release = release_dir(tmp_path, [TOKEN, OPTIONAL])
    home = tmp_path / 'home'
    secrets_file = home / '.config/homensai/demo/secrets.env'
    secrets_file.parent.mkdir(parents=True)
    kit_secrets.write(secrets_file, {'DEMO_TOKEN': 'from-file', 'OTHER': 'not-declared'})
    environment = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'DEMO_TOKEN': 'stale', 'DEMO_OPTIONAL': 'env'}
    assert run_entry(release, environment) == {'DEMO_TOKEN': 'from-file', 'DEMO_OPTIONAL': 'env', 'OTHER': None}
    secrets_file.unlink()
    assert run_entry(release, {'HOME': str(home), 'PATH': '/usr/bin:/bin'}) == {
        'DEMO_TOKEN': None, 'DEMO_OPTIONAL': None, 'OTHER': None}
    override = tmp_path / 'override.env'
    kit_secrets.write(override, {'DEMO_OPTIONAL': 'override'})
    assert run_entry(release, {'HOME': str(home), 'HOMENSAI_SECRETS_FILE': str(override)})['DEMO_OPTIONAL'] == 'override'


def test_entry_without_declarations_ignores_file(tmp_path):
    release = release_dir(tmp_path, [])
    home = tmp_path / 'home'
    (home / '.config/homensai/demo').mkdir(parents=True)
    kit_secrets.write(home / '.config/homensai/demo/secrets.env', {'DEMO_TOKEN': 'x'})
    assert run_entry(release, {'HOME': str(home)})['DEMO_TOKEN'] is None


# --- установщик ---------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('XDG_STATE_HOME', raising=False)
    monkeypatch.delenv(kit_setup.PASSWORD_ENV, raising=False)
    for item in (TOKEN, SESSION, OPTIONAL):
        monkeypatch.delenv(item['name'], raising=False)
    (tmp_path / 'home').mkdir()
    DECLARED.clear()
    monkeypatch.setattr(kit_setup, 'signed_release_info', lambda package, envelope, key, product, version: {
        'migrations': MIGRATIONS[version], 'secrets': DECLARED.get(version, []), **SIGNED[version]})
    client = FakeClient()
    restarts = []

    def setup(kit_dir, *, yes=True, answers=(), secrets=()):
        answers, secrets = list(answers), list(secrets)
        output = []
        instance = Setup(Kit(kit_dir), language='en', state_dir=None, yes=yes, out=output.append,
                         ask=lambda prompt: answers.pop(0), ask_secret=lambda prompt: secrets.pop(0), client=client)
        instance.output = output
        instance.manifest = lambda package, envelope, version, key=None: {
            'manifest': {}, 'files': [{'path': 'runtime-linux/bin/python', 'sha256': 'a' * 64}]}
        instance.units = lambda manifest, modes: ['demo-web.service']
        instance.restart_services = lambda state: restarts.append(state['version']) or True
        return instance
    return tmp_path, client, setup, restarts


def secrets_path(base):
    return base / 'home/.config/homensai/demo/secrets.env'


def test_install_with_yes_requires_secrets_before_any_api_call(env):
    base, client, setup, _ = env
    DECLARED['1'] = [TOKEN, SESSION, OPTIONAL]
    with pytest.raises(SetupError) as failure:
        setup(make_kit(base)).install()
    assert failure.value.key == 'kit.secret_missing' and failure.value.variables['name'] == 'DEMO_TOKEN'
    assert not client.calls and not secrets_path(base).exists()


def test_install_takes_environment_generates_and_skips_optional(env, monkeypatch):
    base, client, setup, _ = env
    DECLARED['1'] = [TOKEN, SESSION, OPTIONAL]
    monkeypatch.setenv('DEMO_TOKEN', 'token-from-env')
    assert setup(make_kit(base)).install() == 0
    values = kit_secrets.read(secrets_path(base))
    assert values['DEMO_TOKEN'] == 'token-from-env' and len(values['DEMO_SESSION']) >= 32
    assert 'DEMO_OPTIONAL' not in values
    assert stat.S_IMODE(secrets_path(base).stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_path(base).parent.stat().st_mode) == 0o700
    dumped = json.dumps(client.calls) + (base / 'home/.local/state/homensai/demo/state.json').read_text()
    assert 'token-from-env' not in dumped and values['DEMO_SESSION'] not in dumped


def test_interactive_install_asks_hidden_and_keeps_existing(env):
    base, _, setup, _ = env
    DECLARED['1'] = [TOKEN, SESSION, OPTIONAL]
    secrets_path(base).parent.mkdir(parents=True)
    kit_secrets.write(secrets_path(base), {'DEMO_SESSION': 'kept'})
    instance = setup(make_kit(base), yes=False, answers=['yes'], secrets=['typed-token', ''])
    assert instance.install() == 0
    assert kit_secrets.read(secrets_path(base)) == {'DEMO_TOKEN': 'typed-token', 'DEMO_SESSION': 'kept'}
    assert not any('typed-token' in line for line in instance.output)


def test_invalid_typed_value_is_rejected(env):
    base, client, setup, _ = env
    DECLARED['1'] = [TOKEN]
    with pytest.raises(SetupError) as failure:
        setup(make_kit(base), yes=False, answers=['yes'], secrets=['x' * 5000]).install()
    assert failure.value.key == 'kit.secret_invalid' and not client.calls


def test_update_asks_only_for_new_secret_before_transaction(env, monkeypatch):
    base, client, setup, _ = env
    DECLARED['1'] = [TOKEN]
    monkeypatch.setenv('DEMO_TOKEN', 'one')
    assert setup(make_kit(base)).install() == 0
    monkeypatch.delenv('DEMO_TOKEN')
    DECLARED['2'] = [TOKEN, {**TOKEN, 'name': 'DEMO_NEW'}]
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    monkeypatch.setenv(kit_setup.PASSWORD_ENV, 'synthetic-password')
    calls = len(client.calls)
    with pytest.raises(SetupError) as failure:
        setup(kit2).update()
    assert failure.value.key == 'kit.secret_missing' and failure.value.variables['name'] == 'DEMO_NEW'
    assert not [call for call in client.calls[calls:] if call[0] == 'run-pointer-linux-update']
    monkeypatch.setenv('DEMO_NEW', 'two')
    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}
    setup(kit2).update()
    assert kit_secrets.read(secrets_path(base)) == {'DEMO_TOKEN': 'one', 'DEMO_NEW': 'two'}


def test_secrets_command_lists_sets_and_clears(env, monkeypatch):
    base, _, setup, restarts = env
    DECLARED['1'] = [TOKEN, OPTIONAL]
    monkeypatch.setenv('DEMO_TOKEN', 'one')
    kit_dir = make_kit(base)
    assert setup(kit_dir).install() == 0
    listing = setup(kit_dir)
    assert listing.manage_secrets() == 0
    assert listing.output[:2] == ['  DEMO_TOKEN: set', '  DEMO_OPTIONAL: not set (optional)']
    monkeypatch.delenv('DEMO_TOKEN')
    assert setup(kit_dir, yes=False, secrets=['two']).manage_secrets('DEMO_TOKEN') == 0
    assert kit_secrets.read(secrets_path(base))['DEMO_TOKEN'] == 'two' and restarts == ['1']
    monkeypatch.setenv('DEMO_OPTIONAL', 'opt')
    setup(kit_dir).manage_secrets('DEMO_OPTIONAL')
    setup(kit_dir).manage_secrets('DEMO_OPTIONAL', clear=True)
    assert kit_secrets.read(secrets_path(base)) == {'DEMO_TOKEN': 'two'}
    for name, clear, key in (('DEMO_TOKEN', True, 'kit.secret_missing'), ('UNKNOWN', False, 'kit.secret_unknown')):
        with pytest.raises(SetupError) as failure:
            setup(kit_dir).manage_secrets(name, clear=clear)
        assert failure.value.key == key
    secrets_path(base).unlink()
    missing = setup(kit_dir)
    assert missing.manage_secrets() == 1 and missing.output[0] == '  DEMO_TOKEN: NOT SET (required)'


def test_damaged_file_is_reported_without_content(env, monkeypatch):
    base, _, setup, _ = env
    DECLARED['1'] = [TOKEN]
    monkeypatch.setenv('DEMO_TOKEN', 'one')
    kit_dir = make_kit(base)
    setup(kit_dir).install()
    secrets_path(base).write_text('garbage-secret-value\n', encoding='utf-8')
    with pytest.raises(SetupError) as failure:
        setup(kit_dir).manage_secrets()
    assert failure.value.key == 'kit.secrets_unreadable'
    assert 'garbage' not in json.dumps(failure.value.variables, default=str)


def test_purge_removes_secrets_and_keep_preserves_them(env, monkeypatch):
    base, client, setup, _ = env
    DECLARED['1'] = [TOKEN]
    monkeypatch.setenv('DEMO_TOKEN', 'one')
    kit_dir = make_kit(base)
    setup(kit_dir).install()
    setup(kit_dir).uninstall()
    assert secrets_path(base).is_file()
    assert setup(kit_dir).install() == 0
    assert setup(kit_dir).uninstall(purge=True) == 0
    assert last(client, 'uninstall-portable-linux')[1]['data_policy'] == 'purge'
    assert not secrets_path(base).parent.exists()


def test_main_dispatches_secrets(env, monkeypatch, capsys):
    base, client, _, _ = env
    DECLARED['1'] = [TOKEN]
    monkeypatch.setenv('DEMO_TOKEN', 'one')
    kit_dir = make_kit(base)
    monkeypatch.setattr(kit_setup.Setup, 'client', property(lambda self: client))
    monkeypatch.setattr(kit_setup.Setup, 'manifest', lambda self, *args, **kwargs: {'manifest': {}, 'files': []})
    monkeypatch.setattr(kit_setup.Setup, 'units', lambda self, manifest, modes: ['demo-web.service'])
    assert kit_setup.main(['--kit', str(kit_dir), '--yes', 'install']) == 0
    capsys.readouterr()
    assert kit_setup.main(['--kit', str(kit_dir), '--language', 'en', 'secrets']) == 0
    assert 'DEMO_TOKEN: set' in capsys.readouterr().out
    assert kit_setup.main(['--kit', str(kit_dir), 'secrets', '--clear']) == 2
