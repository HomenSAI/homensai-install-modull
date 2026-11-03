"""Windows-бэкенд комплекта на Linux: WinSW, http_health и сценарии без реальных процессов и сети."""
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from shared import kit_setup, kit_windows
from shared.datahome import resolve_datahome
from shared.kit import render_manifest
from shared.kit_setup import PASSWORD_ENV, Kit, SetupError
from shared.kit_windows import WindowsEffects, WindowsServices, WindowsSetup, _expand, http_health
from shared.update_plan import EffectIndeterminate

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = 'homensai-kit-demo'
PYTHON_BYTES = b'synthetic python.exe'


def demo_manifest(extra_modes=(), components=None):
    template = json.loads((ROOT / 'examples/kit_demo/installer/manifest.json').read_text(encoding='utf-8'))
    template['modes'] += [dict(mode) for mode in extra_modes]
    if components is not None:
        template['components'] = [item for item in template['components'] if item['name'] in components]
        template['modes'] = [{'name': 'default', 'components': list(components)}]
    locks = {name: {'file': f'locks/{name}.lock', 'sha256': 'a' * 64} for name in ('linux', 'windows')}
    return render_manifest(template, {'targets': ['linux-x86_64', 'windows-x86_64']}, locks,
                           [{'file': 'vendor/x-1-py3-none-any.whl', 'sha256': 'b' * 64}])


class FakeWinSW:
    """Заменяет _run: конечный автомат служб WinSW 2.x по имени exe; icacls лишь записывается."""

    def __init__(self, stuck=()):
        self.calls, self.states, self.stuck, self.fail = [], {}, set(stuck), set()

    def __call__(self, argv, *, timeout=120, check=True):
        name, verb = Path(str(argv[0])).stem, str(argv[1])
        if verb == 'status':
            return SimpleNamespace(returncode=0, stdout='INFO - WinSW 2.12.0\n' + self.states.get(name, 'NonExistent') + '\n')
        self.calls.append((name.removeprefix(PRODUCT + '-'), verb))
        if (name, verb) in self.fail:
            self.fail.discard((name, verb))
            raise SetupError('kit.windows_command_failed', command=name, code='1')
        if name not in self.stuck:
            self.states[name] = {'install': 'Stopped', 'start': 'Started', 'stop': 'Stopped',
                                 'uninstall': 'NonExistent'}[verb]
        return SimpleNamespace(returncode=0, stdout='')

    def actions(self):
        return list(self.calls)


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(kit_windows, 'time', SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep,
                                                             time=lambda: 1_700_000_000))
    return fake


@pytest.fixture
def winsw(monkeypatch, clock):
    fake = FakeWinSW()
    monkeypatch.setattr(kit_windows, '_run', fake)
    return fake


@pytest.fixture
def services(tmp_path):
    manifest = demo_manifest()
    home = resolve_datahome(manifest, tmp_path / 'root')
    return WindowsServices(manifest, home, ['default'], PRODUCT)


def create_exes(services):
    services.directory.mkdir(parents=True, exist_ok=True)
    for component in services.components:
        services.exe(component).write_bytes(b'winsw')


# --- служебные функции ------------------------------------------------------------------

def test_is_admin_false_without_windll():
    assert kit_windows.is_admin() is False


def test_run_stringifies_argv_and_raises_on_failure(monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, len(seen) - 1, stdout='out', stderr='')
    monkeypatch.setattr(kit_windows.subprocess, 'run', fake_run)
    exe = tmp_path / 'tool.exe'
    assert kit_windows._run([exe, 'status']).stdout == 'out'
    argv, kwargs = seen[0]
    assert argv == [str(exe), 'status'] and kwargs['stdin'] is subprocess.DEVNULL
    assert kwargs['creationflags'] == kit_windows._CREATE_NO_WINDOW and kwargs['check'] is False
    assert kwargs['timeout'] == 120 and 'shell' not in kwargs
    with pytest.raises(SetupError) as failure:
        kit_windows._run([exe, 'start'])
    assert failure.value.key == 'kit.windows_command_failed'
    assert failure.value.variables == {'command': 'tool.exe', 'code': '1'}
    assert kit_windows._run([exe, 'stop'], check=False).returncode == 2


def test_secure_directory_argv(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(kit_windows, '_run', lambda argv, **kwargs: seen.append(argv))
    kit_windows.secure_directory(tmp_path)
    assert seen == [['icacls', str(tmp_path), '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F',
                     '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '/Q']]


def test_expand_templates(tmp_path):
    roots = {name: tmp_path / name for name in ('runtime', 'data', 'app', 'release')}
    assert _expand('@runtime/bin/python', roots, executable=True) == str(tmp_path / 'runtime' / 'python.exe')
    assert _expand('@runtime/bin/python3', roots, executable=True) == str(tmp_path / 'runtime' / 'python.exe')
    assert _expand('@runtime/bin/python', roots, executable=False) == str(tmp_path / 'runtime' / 'bin' / 'python')
    assert _expand('@data', roots, executable=False) == str(tmp_path / 'data')
    assert _expand('@release/homensai_entry.py', roots, executable=False) == str(
        tmp_path / 'release' / 'homensai_entry.py')
    assert _expand('@release/a/b.py', roots, executable=False) == str(tmp_path / 'release' / 'a' / 'b.py')
    assert _expand('--port', roots, executable=False) == '--port'
    assert _expand('kitdemo.web', roots, executable=True) == 'kitdemo.web'
    for invalid in ('@unknown', '@unknown/x', '@data/'):
        with pytest.raises(SetupError) as failure:
            _expand(invalid, roots, executable=False)
        assert failure.value.key == 'kit.windows_command_failed' and failure.value.variables['code'] == 'template'


# --- WindowsServices --------------------------------------------------------------------

def test_service_ids_and_paths(services):
    assert services.components == ('web', 'worker')
    assert services.service_id('web') == PRODUCT + '-web'
    assert services.exe('worker') == services.home.app / 'services' / (PRODUCT + '-worker.exe')
    assert services.logs == services.home.app / 'logs'


def test_xml_maps_service_command_to_windows_runtime(services):
    text = services.xml('web', '7')
    home = services.home
    assert f'<executable>{home.roots["runtime"] / "python.exe"}</executable>' in text
    release = home.releases / 'app-7'
    arguments = subprocess.list2cmdline(['-I', '-B', str(release / 'homensai_entry.py'), 'kitdemo.web',
                                         '--data-dir', str(home.roots['data']), '--port', '18431'])
    assert f'<arguments>{arguments}</arguments>' in text
    assert f'<workingdirectory>{release}</workingdirectory>' in text
    assert '<depend>' not in text and '<onfailure action="restart" delay="5 sec"/>' in text
    assert '<resetfailure>600 sec</resetfailure>' in text


def test_mode_selection_limits_components(tmp_path):
    manifest = demo_manifest(extra_modes=[{'name': 'webonly', 'components': ['web']}])
    home = resolve_datahome(manifest, tmp_path / 'root')
    assert WindowsServices(manifest, home, ['webonly'], PRODUCT).components == ('web',)
    assert WindowsServices(manifest, home, ['default', 'webonly'], PRODUCT).components == ('web', 'worker')


def test_write_copies_winsw_only_when_bytes_differ(services, tmp_path, monkeypatch):
    source = tmp_path / 'WinSW-x64.exe'
    source.write_bytes(b'winsw v1')
    copies = []
    real_copy = kit_windows.shutil.copyfile
    monkeypatch.setattr(kit_windows.shutil, 'copyfile', lambda src, dst: (copies.append(Path(dst).name),
                                                                          real_copy(src, dst)))
    services.write('1', source)
    assert copies == [PRODUCT + '-web.exe', PRODUCT + '-worker.exe']
    assert services.logs.is_dir()
    for component in ('web', 'worker'):
        assert services.exe(component).read_bytes() == b'winsw v1'
        xml = services.exe(component).with_suffix('.xml').read_text(encoding='utf-8')
        assert f'<id>{PRODUCT}-{component}</id>' in xml and 'app-1' in xml
    copies.clear()
    services.write('2', source)
    assert copies == []
    assert 'app-2' in services.exe('web').with_suffix('.xml').read_text(encoding='utf-8')
    source.write_bytes(b'winsw v2')
    services.write('2', source)
    assert copies == [PRODUCT + '-web.exe', PRODUCT + '-worker.exe']
    assert services.exe('worker').read_bytes() == b'winsw v2'


def test_write_only_selected_components(tmp_path):
    manifest = demo_manifest(extra_modes=[{'name': 'webonly', 'components': ['web']}])
    services = WindowsServices(manifest, resolve_datahome(manifest, tmp_path / 'root'), ['webonly'], PRODUCT)
    source = tmp_path / 'WinSW-x64.exe'
    source.write_bytes(b'winsw')
    services.write('1', source)
    assert sorted(path.name for path in services.directory.iterdir()) == [PRODUCT + '-web.exe', PRODUCT + '-web.xml']


def test_state_parsing(services, monkeypatch):
    outputs = []
    monkeypatch.setattr(kit_windows, '_run', lambda argv, **kwargs: (
        outputs.append((argv, kwargs)), SimpleNamespace(stdout=next(answers)))[1])
    answers = iter(['INFO - starting\n  Started  \n', '', 'Stopped', '\n\n'])
    assert services.state('web') == 'NonExistent' and outputs == []
    create_exes(services)
    assert services.state('web') == 'Started'
    assert outputs[0][0] == [services.exe('web'), 'status'] and outputs[0][1] == {'check': False}
    assert services.state('web') == 'Unknown'
    assert services.state('worker') == 'Stopped'
    assert services.state('worker') == 'Unknown'


def test_install_only_nonexistent(services, winsw):
    create_exes(services)
    winsw.states[PRODUCT + '-worker'] = 'Stopped'
    services.install()
    assert winsw.actions() == [('web', 'install')]


def test_start_in_dependency_order_and_skip_started(services, winsw, clock):
    create_exes(services)
    services.install()
    winsw.calls.clear()
    services.start()
    assert winsw.actions() == [('web', 'start'), ('worker', 'start')]
    winsw.calls.clear()
    services.start()
    assert winsw.actions() == []
    winsw.states[PRODUCT + '-worker'] = 'Stopped'
    services.start(('worker',))
    assert winsw.actions() == [('worker', 'start')]


def test_stop_in_reverse_order_and_skip_stopped(services, winsw):
    create_exes(services)
    winsw.states.update({PRODUCT + '-web': 'Started', PRODUCT + '-worker': 'Started'})
    services.stop()
    assert winsw.actions() == [('worker', 'stop'), ('web', 'stop')]
    winsw.calls.clear()
    services.stop()
    assert winsw.actions() == []


def test_uninstall_stops_then_removes_in_reverse_order(services, winsw):
    create_exes(services)
    winsw.states.update({PRODUCT + '-web': 'Started', PRODUCT + '-worker': 'Started'})
    services.uninstall()
    assert winsw.actions() == [('worker', 'stop'), ('web', 'stop'), ('worker', 'uninstall'), ('web', 'uninstall')]
    winsw.calls.clear()
    services.uninstall()
    assert winsw.actions() == []


def test_wait_times_out_with_service_state_error(services, monkeypatch, clock):
    create_exes(services)
    fake = FakeWinSW(stuck={PRODUCT + '-web'})
    fake.states[PRODUCT + '-web'] = 'Stopped'
    monkeypatch.setattr(kit_windows, '_run', fake)
    with pytest.raises(SetupError) as failure:
        services.start()
    assert failure.value.key == 'kit.windows_service_state'
    assert failure.value.variables == {'service': PRODUCT + '-web', 'state': 'Started'}
    assert clock.now > 60 and fake.actions() == [('web', 'start')]


# --- http_health ------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit):
        return self.body[:limit]


def fake_urlopen(monkeypatch, responses):
    seen = []

    def urlopen(url, timeout):
        seen.append((url, timeout))
        item = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    return seen


def good_body(release='3'):
    return json.dumps({'product': PRODUCT, 'release': release, 'healthy': True}).encode()


def test_http_health_success(monkeypatch, clock):
    seen = fake_urlopen(monkeypatch, [FakeResponse(good_body())])
    assert http_health(demo_manifest(), PRODUCT, '3', 30) is True
    assert seen == [('http://127.0.0.1:18431/healthz', 3)] and clock.sleeps == 0


def test_http_health_retries_until_exact_answer(monkeypatch, clock):
    import urllib.error
    seen = fake_urlopen(monkeypatch, [urllib.error.URLError('refused'), FakeResponse(b'not json'),
                                      FakeResponse(good_body(), status=500), FakeResponse(good_body('2')),
                                      FakeResponse(good_body())])
    assert http_health(demo_manifest(), PRODUCT, '3', 30) is True
    assert len(seen) == 5 and clock.sleeps == 4


@pytest.mark.parametrize('body', [
    {'product': PRODUCT, 'release': '3', 'healthy': False},
    {'product': PRODUCT, 'release': '3', 'healthy': 'true'},
    {'product': PRODUCT, 'release': '3', 'healthy': True, 'extra': 1},
    {'product': 'other', 'release': '3', 'healthy': True},
    {'product': PRODUCT, 'release': 3, 'healthy': True},
    [PRODUCT, '3', True],
])
def test_http_health_rejects_inexact_answer_until_timeout(monkeypatch, clock, body):
    seen = fake_urlopen(monkeypatch, [FakeResponse(json.dumps(body).encode())])
    assert http_health(demo_manifest(), PRODUCT, '3', 2) is False
    assert clock.now >= 2 and len(seen) == clock.sleeps + 1


def test_http_health_timeout_on_connection_errors(monkeypatch, clock):
    seen = fake_urlopen(monkeypatch, [TimeoutError('timed out')])
    assert http_health(demo_manifest(), PRODUCT, '3', 0) is False
    assert len(seen) == 1 and clock.sleeps == 0


# --- WindowsEffects ---------------------------------------------------------------------

class FakePorts:
    def __init__(self, release):
        self.release = release

    def current_release(self):
        return self.release


def make_effects(services, tmp_path, release='1'):
    source = tmp_path / 'WinSW-x64.exe'
    source.write_bytes(b'winsw')
    ports = FakePorts(release)
    return WindowsEffects(services, lambda: ports, product_id=PRODUCT, target_release='2', winsw=source), ports


def test_migrate_argv_and_environment(services, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv('HOMENSAI_SECRET_ARCHIVE_PASSWORD', 'synthetic-secret')

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(kit_windows.subprocess, 'run', fake_run)
    effects, _ = make_effects(services, tmp_path)
    command = tuple(services.manifest['migrations']['cmd'])
    assert command[:2] == ('python', 'homensai_entry.py')
    effects.migrate('key', 'm-2', command)
    home = services.home
    release = home.releases / 'app-2'
    argv, kwargs = seen[0]
    assert argv == [str(home.roots['runtime'] / 'python.exe'), '-I', '-B', str(release / 'homensai_entry.py'),
                    'kitdemo.migrate', '--data-dir', str(home.roots['data']), '--migration', 'm-2']
    assert kwargs['cwd'] == str(release) and kwargs['stdin'] is subprocess.DEVNULL
    assert kwargs['timeout'] == 600 and 'shell' not in kwargs
    assert set(kwargs['env']) == {'SystemRoot', 'PATH'} and 'synthetic-secret' not in json.dumps(kwargs['env'])
    effects.migrate('key', 'm-3', ('python', 'tools/migrate.py', '@MIGRATION_ID@'))
    assert seen[1][0][3:] == [str(release / 'tools' / 'migrate.py'), 'm-3']


def test_migrate_failures(services, tmp_path, monkeypatch):
    effects, _ = make_effects(services, tmp_path)
    results = []

    def fake_run(argv, **kwargs):
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return subprocess.CompletedProcess(argv, item)
    monkeypatch.setattr(kit_windows.subprocess, 'run', fake_run)
    results.append(1)
    with pytest.raises(RuntimeError, match='migration failed'):
        effects.migrate('key', 'm-2', ('python', 'homensai_entry.py'))
    results.append(subprocess.TimeoutExpired(['python.exe'], 600))
    with pytest.raises(EffectIndeterminate):
        effects.migrate('key', 'm-2', ('python', 'homensai_entry.py'))
    for command in (('python',), ('bash', 'x.py'), ('python', 'x.sh')):
        with pytest.raises(RuntimeError, match='unsupported'):
            effects.migrate('key', 'm-2', command)
    assert results == []


def test_effects_start_writes_current_release_and_starts(services, tmp_path, winsw):
    effects, ports = make_effects(services, tmp_path, release='1')
    effects.start('key', services.components, ())
    for component in services.components:
        assert 'app-1' in services.exe(component).with_suffix('.xml').read_text(encoding='utf-8')
    assert winsw.actions() == [('web', 'install'), ('worker', 'install'), ('web', 'start'), ('worker', 'start')]
    effects.stop('key', tuple(reversed(services.components)))
    ports.release = '2'
    effects.start('key', services.components, ())
    assert 'app-2' in services.exe('web').with_suffix('.xml').read_text(encoding='utf-8')
    assert winsw.actions()[-4:] == [('worker', 'stop'), ('web', 'stop'), ('web', 'start'), ('worker', 'start')]


def test_effects_refuse_unsupported_hooks(services, tmp_path, winsw, monkeypatch):
    effects, _ = make_effects(services, tmp_path)
    with pytest.raises(RuntimeError, match='pre_start'):
        effects.start('key', services.components, (('python', 'x.py'),))
    assert winsw.calls == [] and not services.directory.exists()
    monkeypatch.setattr(kit_windows, 'http_health', lambda *args: pytest.fail('health must not run'))
    with pytest.raises(RuntimeError, match='health_checks'):
        effects.health('key', 'http://127.0.0.1:18431/healthz', 30, (('python', 'x.py'),))
    with pytest.raises(RuntimeError, match='post_update'):
        effects.post_update('key', (('python', 'x.py'),))
    assert effects.post_update('key', ()) is None


def test_effects_health_uses_current_release(services, tmp_path, monkeypatch):
    effects, ports = make_effects(services, tmp_path, release='1')
    seen, answers = [], [True, False]
    monkeypatch.setattr(kit_windows, 'http_health', lambda *args: (seen.append(args), answers.pop(0))[1])
    effects.health('key', 'ignored', 30, ())
    assert seen == [(services.manifest, PRODUCT, '1', 30)]
    with pytest.raises(RuntimeError, match='health check failed'):
        effects.health('key', 'ignored', 30, ())


# --- WindowsSetup -----------------------------------------------------------------------

MIGRATIONS = {}


def make_kit(base: Path, version='1', migrations=('m-1',), key=b'k' * 32, targets=('windows-x86_64',)) -> Path:
    MIGRATIONS[version] = list(migrations)
    kit = base / ('kit-' + version)
    (kit / 'package').mkdir(parents=True)
    for name in ('app', 'linux-x86_64', 'windows-x86_64'):
        (kit / 'installer' / name).mkdir(parents=True)
    package = kit / 'package' / f'{PRODUCT}-{version}.zip'
    package.write_bytes(b'package ' + version.encode())
    (kit / 'package' / f'{PRODUCT}-{version}.sig').write_bytes(b'{"signature": "synthetic"}')
    (kit / 'package/publisher-ed25519.pub').write_bytes(key)
    for name in ('setup.cmd', 'setup.sh', 'README.txt'):
        (kit / name).write_text('synthetic')
    meta = {'format': 1, 'product_id': PRODUCT, 'product_name': 'Kit Demo', 'version': version,
            'package': f'package/{PRODUCT}-{version}.zip', 'envelope': f'package/{PRODUCT}-{version}.sig',
            'public_key': 'package/publisher-ed25519.pub', 'public_key_sha256': hashlib.sha256(key).hexdigest(),
            'package_sha256': hashlib.sha256(package.read_bytes()).hexdigest(), 'modes': ['default'],
            'all_modes': ['default'], 'migrations': list(migrations), 'targets': list(targets),
            'paths': {'runtime': 'runtime'}, 'web': {'host': '127.0.0.1', 'port': 18431, 'healthz': '/healthz'}}
    (kit / 'kit.json').write_text(json.dumps(meta))
    return kit


@pytest.fixture
def win(tmp_path, monkeypatch, winsw):
    monkeypatch.setenv('ProgramData', str(tmp_path / 'ProgramData'))
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    secured, staged = [], []
    monkeypatch.setattr(kit_windows, 'secure_owned', secured.append)
    for module in (kit_windows, kit_setup):
        monkeypatch.setattr(module, 'signed_release_info',
                            lambda package, envelope, key, product, version: {'migrations': MIGRATIONS[version]})
    health = {'value': True}
    monkeypatch.setattr(kit_windows, 'http_health', lambda *args: health['value'])
    files = [{'path': 'runtime-windows/python.exe', 'sha256': hashlib.sha256(PYTHON_BYTES).hexdigest()}]
    manifests = {}

    def verified(self, release):
        return {'manifest': manifests.get(release['version'], demo_manifest()), 'files': files}

    def stage(self, release, metadata, destination):
        staged.append(destination)
        (destination / 'windows').mkdir(parents=True, exist_ok=True)
        (destination / 'windows' / 'WinSW-x64.exe').write_bytes(b'winsw')
        (destination / 'runtime-windows').mkdir(exist_ok=True)
        (destination / 'runtime-windows' / 'python.exe').write_bytes(PYTHON_BYTES)
    monkeypatch.setattr(WindowsSetup, '_verified', verified)
    monkeypatch.setattr(WindowsSetup, '_stage', stage)
    lines = []

    def setup(kit, *, yes=True, answers=()):
        answers = list(answers)
        return WindowsSetup(Kit(kit), language='en', yes=yes, out=lines.append,
                            ask=lambda prompt: answers.pop(0), ask_secret=lambda prompt: pytest.fail('no secret'))
    return SimpleNamespace(base=tmp_path, setup=setup, secured=secured, staged=staged, health=health,
                           manifests=manifests, lines=lines, winsw=winsw,
                           state_dir=tmp_path / 'ProgramData' / 'HomenSAI' / '.state' / PRODUCT,
                           root=tmp_path / 'ProgramData' / 'HomenSAI' / PRODUCT)


def read_state(win):
    return json.loads((win.state_dir / 'state.json').read_text(encoding='utf-8'))


def test_setup_install_full_flow(win):
    kit = make_kit(win.base)
    assert win.setup(kit).install() == 0
    state = read_state(win)
    assert state['status'] == 'installed' and state['version'] == '1' and state['platform'] == 'windows'
    assert state['install_root'] == str(win.root) and state['applied_migrations'] == ['m-1']
    assert 'install' not in state and state['releases']['1']['version'] == '1'
    base = win.state_dir.parent.parent
    assert win.secured == [base, base / '.state', win.state_dir, win.root]
    home = resolve_datahome(demo_manifest(), win.root)
    assert json.loads((home.roots['services'] / 'active.json').read_text())['release'] == '1'
    assert (home.roots['runtime'] / 'python.exe').read_bytes() == PYTHON_BYTES
    assert win.staged == [home.releases / 'app-1']
    assert win.winsw.actions() == [('web', 'install'), ('worker', 'install'), ('web', 'start'), ('worker', 'start')]
    assert (win.state_dir / 'installer' / 'setup.cmd').is_file()
    with pytest.raises(SetupError) as again:
        win.setup(kit).install()
    assert again.value.key == 'kit.already_installed'


def test_setup_install_resumes_after_failed_step(win, monkeypatch):
    kit = make_kit(win.base)
    real = WindowsSetup._copy_runtime
    calls = []

    def flaky(self, release_dir, runtime, metadata):
        calls.append(runtime)
        if len(calls) == 1:
            raise SetupError('kit.release_mismatch', path=runtime)
        return real(self, release_dir, runtime, metadata)
    monkeypatch.setattr(WindowsSetup, '_copy_runtime', flaky)
    with pytest.raises(SetupError):
        win.setup(kit).install()
    pending = read_state(win)['install']
    assert pending['index'] == 1 and read_state(win)['status'] == 'installing'
    assert win.setup(kit).install() == 0
    assert len(win.staged) == 1 and len(calls) == 2 and read_state(win)['status'] == 'installed'


def test_setup_install_refusals(win):
    with pytest.raises(SetupError) as host:
        win.setup(make_kit(win.base / 'linux', targets=('linux-x86_64',))).install()
    assert host.value.key == 'kit.unsupported_host'
    kit = make_kit(win.base)
    with pytest.raises(SetupError) as key:
        win.setup(kit).install(expect_key='0' * 64)
    assert key.value.key == 'kit.key_mismatch'
    with pytest.raises(SetupError) as mode:
        win.setup(kit).install(modes=['other'])
    assert mode.value.key == 'kit.unknown_mode'
    win.root.mkdir(parents=True)
    with pytest.raises(SetupError) as exists:
        win.setup(kit).install()
    assert exists.value.key == 'kit.root_exists'
    assert not (win.state_dir / 'state.json').exists() and win.winsw.calls == []


def test_setup_status(win):
    kit = make_kit(win.base)
    assert win.setup(kit).status() == 1
    win.setup(kit).install()
    win.lines.clear()
    assert win.setup(kit).status() == 0
    assert f'  {PRODUCT}-web: Started' in win.lines and f'  {PRODUCT}-worker: Started' in win.lines
    win.health['value'] = False
    assert win.setup(kit).status() == 1


def test_setup_uninstall_keep_reinstall_and_purge(win):
    kit = make_kit(win.base)
    win.setup(kit).install()
    home = resolve_datahome(demo_manifest(), win.root)
    (home.roots['data'] / 'user.db').write_text('data')
    win.winsw.calls.clear()
    assert win.setup(kit).uninstall() == 0
    assert win.winsw.actions() == [('worker', 'stop'), ('web', 'stop'), ('worker', 'uninstall'), ('web', 'uninstall')]
    for path in (home.app, home.releases, home.roots['runtime'], home.roots['services']):
        assert not path.exists()
    assert (home.roots['data'] / 'user.db').read_text() == 'data'
    state = read_state(win)
    assert state['status'] == 'uninstalled' and state['data_policy'] == 'keep'
    assert state['history'][-1]['action'] == 'uninstall'
    with pytest.raises(SetupError) as wrong:
        win.setup(make_kit(win.base / 'v2', '2')).install()
    assert wrong.value.key == 'kit.reinstall_version'
    assert win.setup(kit).install() == 0
    assert (home.roots['data'] / 'user.db').is_file() and read_state(win)['status'] == 'installed'
    with pytest.raises(SetupError) as cancelled:
        win.setup(kit, yes=False, answers=['wrong']).uninstall(purge=True)
    assert cancelled.value.key == 'kit.cancelled' and read_state(win)['status'] == 'installed'
    assert win.setup(kit, yes=False, answers=[PRODUCT]).uninstall(purge=True) == 0
    assert not win.root.exists()
    state = read_state(win)
    assert state['status'] == 'uninstalled' and state['data_policy'] == 'purge'
    with pytest.raises(SetupError) as missing:
        win.setup(kit).uninstall()
    assert missing.value.key == 'kit.not_installed'


def test_setup_uninstall_resume_keeps_data_policy(win):
    kit = make_kit(win.base)
    win.setup(kit).install()
    win.winsw.fail.add((PRODUCT + '-web', 'uninstall'))
    with pytest.raises(SetupError):
        win.setup(kit).uninstall()
    state = read_state(win)
    assert state['status'] == 'uninstalling' and state['uninstall'] == {'data_policy': 'keep'}
    with pytest.raises(SetupError) as conflict:
        win.setup(kit).uninstall(purge=True)
    assert conflict.value.key == 'kit.pending_uninstall'
    assert win.setup(kit).uninstall() == 0
    assert read_state(win)['status'] == 'uninstalled' and win.root.exists()


def test_setup_update_refusals_before_any_effect(win):
    kit1 = make_kit(win.base)
    with pytest.raises(SetupError) as missing:
        win.setup(kit1).update()
    assert missing.value.key == 'kit.not_installed'
    win.setup(kit1).install()
    win.winsw.calls.clear()
    with pytest.raises(SetupError) as same:
        win.setup(kit1).update()
    assert same.value.key == 'kit.same_version'
    with pytest.raises(SetupError) as key:
        win.setup(make_kit(win.base / 'other', '2', ('m-1', 'm-2'), key=b'x' * 32)).update()
    assert key.value.key == 'kit.key_changed'
    with pytest.raises(SetupError) as diverged:
        win.setup(make_kit(win.base / 'div', '2', ('m-9',))).update()
    assert diverged.value.key == 'kit.migrations_diverged'
    win.manifests['2'] = demo_manifest(components=['web'])
    with pytest.raises(SetupError) as components:
        win.setup(make_kit(win.base / 'comp', '2', ('m-1', 'm-2'))).update()
    assert components.value.key == 'kit.components_changed'
    win.manifests.pop('2')
    # Без пароля в неинтерактивном режиме обновление останавливается до транзакции.
    with pytest.raises(SetupError) as password:
        win.setup(make_kit(win.base / 'pw', '2', ('m-1', 'm-2'))).update()
    assert password.value.key == 'kit.password_required'
    state = read_state(win)
    assert state['status'] == 'installed' and 'update' not in state and state['version'] == '1'
    assert win.winsw.calls == []
