"""Веб-страница установщика: вопросы установщика, команды через API и защита HTTP."""
import http.client
import json
import socket
import threading
import time

import pytest

from shared import kit_setup, kit_web
from shared.kit_setup import Kit, Setup
from shared.web_ui import HttpError, TOKEN_HEADER, start
from tests.test_kit_setup import env, make_kit  # noqa: F401 — фикстура env

TOKEN = 'T' * 43
STATE = 'home/.local/state/homensai/demo'
_SOCKET = socket.socket


@pytest.fixture
def loopback(monkeypatch):
    """Сеть в тестах запрещена (conftest); здесь допускается только 127.0.0.1."""
    monkeypatch.setattr(socket, 'socket', _SOCKET)

    def loopback_only(address, timeout=None, source_address=None):
        if address[0] != '127.0.0.1' or source_address is not None:
            raise AssertionError('Тест допускает только 127.0.0.1')
        connection = _SOCKET(socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect(address)
        return connection
    monkeypatch.setattr(socket, 'create_connection', loopback_only)


def test_conversation_question_answer_and_cancel():
    conversation = kit_web.Conversation()
    conversation.begin('install')
    answers = {}

    def work():
        answers['confirm'] = conversation.ask_confirm('Install?')
        answers['secret'] = conversation.ask_secret('Password: ')
        try:
            conversation.ask('Product id: ')
        except EOFError:
            answers['cancelled'] = True
    worker = threading.Thread(target=work)
    worker.start()

    def wait_prompt(kind):
        for _ in range(200):
            prompt = conversation.snapshot(0)['prompt']
            if prompt and prompt['kind'] == kind:
                return prompt
            time.sleep(0.01)
        raise AssertionError('нет вопроса ' + kind)
    prompt = wait_prompt('confirm')
    assert prompt['text'] == 'Install?'
    with pytest.raises(HttpError) as wrong:
        conversation.reply(prompt['id'], 'maybe', False)
    assert wrong.value.code == 'field:value'
    with pytest.raises(HttpError) as stale:
        conversation.reply(prompt['id'] + 1, 'yes', False)
    assert stale.value.status == 409
    conversation.reply(prompt['id'], 'yes', False)
    conversation.reply(wait_prompt('secret')['id'], 'synthetic-password', False)
    conversation.reply(wait_prompt('text')['id'], '', True)
    worker.join(5)
    assert answers == {'confirm': True, 'secret': 'synthetic-password', 'cancelled': True}
    with pytest.raises(HttpError) as busy:
        conversation.begin('status')
    assert busy.value.code == 'busy'
    conversation.finish(0)
    snapshot = conversation.snapshot(0)
    assert not snapshot['running'] and snapshot['result'] == {'code': 0}
    assert 'synthetic-password' not in json.dumps(snapshot)


@pytest.fixture
def web(env, monkeypatch):
    base, client, _ = env

    class WebSetup(Setup):
        def __init__(self, kit, **options):
            super().__init__(kit, client=client, **options)
            self.manifest = lambda package, envelope, version, key=None: {
                'manifest': {}, 'files': [{'path': 'runtime-linux/bin/python', 'sha256': 'a' * 64}]}
            self.units = lambda manifest, modes: ['demo-web.service']
    monkeypatch.setattr(kit_setup, 'backend', lambda: WebSetup)
    monkeypatch.setattr(kit_setup, '_unit_state', lambda unit: 'active')
    kit = make_kit(base)
    return base, client, kit_web.InstallerWeb(kit=str(kit), language='en', state_dir=None)


def finish(web, answers=()):
    """Дождаться конца операции, отвечая на вопросы по порядку; вернуть снимок."""
    answers = list(answers)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        snapshot = web.progress({}, None)
        if not snapshot['running']:
            return snapshot
        prompt = snapshot['prompt']
        if prompt is not None:
            web.answer({}, {'id': prompt['id'], 'value': answers.pop(0)})
        time.sleep(0.01)
    raise AssertionError('операция не завершилась')


def test_install_update_status_uninstall_through_web(web):
    base, client, installer = web
    info = installer.info({}, None)
    assert info['kit']['product_id'] == 'demo' and info['state'] is None
    installer.run({}, {'command': 'install'})
    snapshot = finish(installer, ['yes'])
    assert snapshot['result'] == {'code': 0}, snapshot['lines']
    assert any('Installed' in line['text'] or 'installed' in line['text'] for line in snapshot['lines'])
    info = installer.info({}, None)
    assert info['state']['status'] == 'installed' and info['state']['version'] == '1'
    assert info['stored_installer'].endswith('installer')

    client.results['run-pointer-linux-update'] = {'state': {'outcome': 'updated', 'transaction': 't'}}
    kit2 = make_kit(base, '2', ('m-1', 'm-2'))
    installer.run({}, {'command': 'update', 'kit': str(kit2)})
    snapshot = finish(installer, ['yes', 'synthetic-password-1', 'synthetic-password-1'])
    assert snapshot['result'] == {'code': 0}, snapshot['lines']
    assert 'synthetic-password-1' not in json.dumps(snapshot)
    state = json.loads((base / STATE / 'state.json').read_text())
    assert state['version'] == '2'

    installer.run({}, {'command': 'status'})
    assert finish(installer)['result'] == {'code': 0}
    installer.run({}, {'command': 'uninstall', 'kit': str(kit2)})
    assert finish(installer, ['yes'])['result'] == {'code': 0}
    assert json.loads((base / STATE / 'state.json').read_text())['status'] == 'uninstalled'


def test_declined_confirmation_cancels_without_changes(web):
    base, client, installer = web
    installer.run({}, {'command': 'install'})
    snapshot = finish(installer, ['no'])
    assert snapshot['result'] == {'code': 3}
    assert snapshot['lines'][-1]['kind'] == 'error'
    assert not any(call[0] == 'install-portable-linux' for call in client.calls)


def test_short_archive_password_is_refused(web):
    base, client, installer = web
    installer.run({}, {'command': 'install'})
    finish(installer, ['yes'])
    installer.run({}, {'command': 'update', 'kit': str(make_kit(base, '2', ('m-1', 'm-2')))})
    snapshot = finish(installer, ['yes', 'short', 'short'])
    assert snapshot['result']['code'] != 0
    assert json.loads((base / STATE / 'state.json').read_text())['version'] == '1'


@pytest.mark.parametrize('body,code', [
    ({'command': 'rm'}, 'field:command'),
    ({'command': 'install', 'modes': 'default'}, 'field:modes'),
    ({'command': 'install', 'modes': ['a,b']}, 'field:modes'),
    ({'command': 'install', 'root': 7}, 'field:root'),
    ({'command': 'update', 'allow_downgrade': 'yes'}, 'field:allow_downgrade'),
    ({'command': 'install', 'language': 'xx'}, 'field:language'),
])
def test_run_rejects_invalid_fields(web, body, code):
    with pytest.raises(HttpError) as refused:
        web[2].run({}, body)
    assert refused.value.code == code
    assert not web[2].conversation.running


def test_quit_refused_while_running(web):
    installer = web[2]
    installer.run({}, {'command': 'install'})
    with pytest.raises(HttpError) as busy:
        installer.quit({}, {})
    assert busy.value.code == 'busy'
    finish(installer, ['no'])
    assert installer.quit({}, {}) == {'stopping': True} and installer.stop.is_set()


def test_info_reports_unreadable_kit(web, tmp_path):
    info = web[2].info({'kit': str(tmp_path / 'missing')}, None)
    assert 'kit' not in info and info['error']


# --- HTTP --------------------------------------------------------------------------------
@pytest.fixture
def server(web, loopback):
    app = web[2].app(TOKEN, set())
    httpd = start(app, '127.0.0.1', 0)
    port = httpd.server_address[1]
    app.hosts.update({'127.0.0.1:' + str(port)})
    yield web, port
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, *, body=None, headers=None, token=TOKEN, raw=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    sent = {'Host': '127.0.0.1:' + str(port)}
    if token is not None:
        sent[TOKEN_HEADER] = token
    if body is not None or raw is not None:
        sent['Content-Type'] = 'application/json'
    sent.update(headers or {})
    connection.request(method, path, body=raw if raw is not None else (json.dumps(body) if body is not None else None),
                       headers=sent)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


def test_page_and_assets_are_served_with_strict_headers(server):
    _, port = server
    response, data = request(port, 'GET', '/', token=None)
    assert response.status == 200 and b'installer.js' in data
    policy = response.getheader('Content-Security-Policy')
    assert "script-src 'self'" in policy and 'unsafe-inline' not in policy and "frame-ancestors 'none'" in policy
    assert response.getheader('Cache-Control') == 'no-store'
    assert response.getheader('X-Content-Type-Options') == 'nosniff'
    for path in ('/hsi.css', '/common.js', '/installer.js', '/lang.json', '/mark.svg'):
        assert request(port, 'GET', path, token=None)[0].status == 200
    for path in ('/builder.js', '/../kit_web.py', '/kit_web.py', '/web/hsi.css', '/%2e%2e/kit.py'):
        assert request(port, 'GET', path, token=None)[0].status == 404


@pytest.mark.parametrize('case', ['no_token', 'wrong_token', 'host', 'origin', 'cross_site', 'content_type',
                                  'too_large', 'not_object', 'not_json'])
def test_api_requests_are_refused(server, case):
    _, port = server
    path, method, body, headers, token, raw = '/api/run', 'POST', {'command': 'status'}, {}, TOKEN, None
    if case == 'no_token':
        token = None
    elif case == 'wrong_token':
        token = 'U' * 43
    elif case == 'host':
        headers = {'Host': 'attacker.example:' + str(port)}
    elif case == 'origin':
        headers = {'Origin': 'http://attacker.example'}
    elif case == 'cross_site':
        headers = {'Sec-Fetch-Site': 'cross-site'}
    elif case == 'content_type':
        headers = {'Content-Type': 'text/plain'}
    elif case == 'too_large':
        raw = json.dumps({'command': 'status', 'pad': 'x' * 70000})
    elif case == 'not_object':
        raw = '[]'
    else:
        raw = '{'
    response, data = request(port, method, path, body=body if raw is None else None, raw=raw, headers=headers,
                             token=token)
    expected = {'no_token': 403, 'wrong_token': 403, 'host': 421, 'origin': 403, 'cross_site': 403,
                'content_type': 415, 'too_large': 413, 'not_object': 400, 'not_json': 400}[case]
    assert response.status == expected
    assert not server[0][2].conversation.running


def test_full_install_over_http(server):
    (base, client, installer), port = server
    origin = {'Origin': 'http://127.0.0.1:' + str(port), 'Sec-Fetch-Site': 'same-origin'}
    response, data = request(port, 'GET', '/api/info?language=ru', headers=origin)
    assert response.status == 200 and json.loads(data)['kit']['version'] == '1'
    assert request(port, 'POST', '/api/run', body={'command': 'install', 'language': 'ru'}, headers=origin)[0].status == 200
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        snapshot = json.loads(request(port, 'GET', '/api/progress?after=0')[1])
        if snapshot['prompt']:
            assert snapshot['prompt']['kind'] == 'confirm'
            assert request(port, 'POST', '/api/answer', body={'id': snapshot['prompt']['id'], 'value': 'yes'},
                           headers=origin)[0].status == 200
        if not snapshot['running'] and snapshot['result']:
            break
        time.sleep(0.02)
    assert snapshot['result'] == {'code': 0}
    assert any('Установлен' in line['text'] or 'установлен' in line['text'] for line in snapshot['lines'])
    assert json.loads((base / STATE / 'state.json').read_text())['status'] == 'installed'


def test_cli_web_command_is_parsed():
    args = kit_setup.parser().parse_args(['--kit', 'k', 'web', '--port', '8123', '--no-browser'])
    assert (args.command, args.port, args.no_browser) == ('web', 8123, True)


def test_web_serves_until_quit(monkeypatch, tmp_path, capsys, loopback):
    import argparse
    monkeypatch.setattr(kit_web, 'IDLE_LIMIT', 0.5)
    args = argparse.Namespace(kit=str(tmp_path), state_dir=None, port=0, no_browser=True)
    assert kit_web.serve(args, 'en') == 0
    out = capsys.readouterr().out
    assert 'http://127.0.0.1:' in out and '/#t=' in out and 'stopped' in out


def test_web_assets_are_signed_installer_files(tmp_path):
    from shared.kit import _copy_installer_code, installer_files
    _copy_installer_code(tmp_path / 'installer/app')
    files = installer_files(tmp_path)
    for name in kit_web.ASSETS:
        assert 'installer/app/shared/web/' + name in files
    assert 'installer/app/shared/kit_web.py' in files and 'installer/app/shared/web_ui.py' in files


def test_language_files_have_the_same_keys():
    from shared.web_ui import ASSETS
    catalog = json.loads((ASSETS / 'lang.json').read_text(encoding='utf-8'))
    assert set(catalog) == {'ru', 'en', 'de'}
    keys = set(catalog['ru'])
    assert all(set(table) == keys for table in catalog.values())
    used = set()
    import re
    for name in ('installer.html', 'builder.html', 'installer.js', 'builder.js', 'common.js'):
        text = (ASSETS / name).read_text(encoding='utf-8')
        used |= set(re.findall(r'data-i18n(?:-label)?="([a-z_.]+)"', text))
        used |= set(re.findall(r'HSI\.t\("([a-z_.]+)"', text))
        used |= set(re.findall(r'\bt\("([a-z_.]+)"', text))
    assert {key for key in used if not key.endswith('.')} - keys == set()


def test_pages_have_no_inline_code_or_external_addresses():
    import re
    from shared.web_ui import ASSETS
    for path in ASSETS.iterdir():
        text = path.read_text(encoding='utf-8')
        assert 'http://' not in text.replace('http://www.w3.org/2000/svg', '') and 'https://' not in text, path.name
        if path.suffix == '.html':
            assert re.search(r'<script(?![^>]*\bsrc=)[^>]*>', text) is None, path.name
            assert re.search(r'\son[a-z]+=', text) is None and ' style=' not in text and '<style' not in text, path.name


def test_kit_e2e_runs_kit_python_without_writing_bytecode():
    """E2E-сценарий запускает Python установщика из комплекта K1 (веб-драйвер, запросы к службе).

    Без -B он записывал __pycache__ в installer/linux-x86_64/python комплекта, и следующий
    запуск setup.sh из K1 (веб-установка) отказывал с kit.installer_mismatch. Байт-код
    не исключается из сверки намеренно: Python загрузил бы подложенный .pyc.
    """
    import re
    from tests import run_kit_e2e
    calls = re.findall(r'"\$PY"[^\n]*', run_kit_e2e.SESSION)
    assert len(calls) >= 5
    for call in calls:
        assert call.startswith('"$PY" -I -B '), call
    pyc = 'installer/' + kit_setup._installer_platform() + '/python/lib/__pycache__/os.cpython-314.pyc'
    assert pyc in kit_setup.installer_files({pyc: '0' * 64})
