# HomenS.AI · © 2026 Serhii Khomenko
"""Веб-страница установщика комплекта: `setup.sh web` / `setup.cmd web` / `setup-web.cmd`.

Те же команды, что в командной строке (install, update, status, secrets,
uninstall), выполняет тот же код (kit_setup.execute) в рабочем потоке. Вывод
установщика и его вопросы (подтверждение, пароль архива, секреты) передаются
странице: операция ждёт ответа, как ждала бы ввода в терминале. Ответы не
записываются в журнал и не хранятся после передачи установщику.

Сервер слушает только 127.0.0.1; адрес с токеном сеанса открывается в
браузере и печатается в консоли. Сервер завершается кнопкой «Завершить», по
Ctrl+C или после 2 часов без запросов.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path

from shared import kit_setup
from shared.i18n import LanguageError, available_languages, translate
from shared.web_ui import HttpError, WebApp, flag, start, text

ASSETS = ('hsi.css', 'common.js', 'installer.js', 'lang.json', 'mark.svg')
ANSWER_TIMEOUT = 30 * 60
IDLE_LIMIT = 2 * 3600
COMMANDS = ('install', 'update', 'status', 'secrets', 'uninstall')
_CANCEL = object()


class Conversation:
    """Вывод и вопросы установщика между рабочим потоком и страницей."""

    def __init__(self):
        self.changed = threading.Condition()
        self.lines: list[dict] = []
        self.prompt: dict | None = None
        self.answer = None
        self.serial = 0
        self.command: str | None = None
        self.running = False
        self.result: dict | None = None

    def begin(self, command: str):
        with self.changed:
            if self.running:
                raise HttpError(409, 'busy')
            self.lines, self.prompt, self.answer, self.result = [], None, None, None
            self.command, self.running = command, True

    def finish(self, code: int):
        with self.changed:
            self.running, self.prompt = False, None
            self.result = {'code': code}
            self.changed.notify_all()

    def out(self, line):
        with self.changed:
            self.lines.append({'kind': 'out', 'text': str(line)})

    def error(self, line):
        with self.changed:
            self.lines.append({'kind': 'error', 'text': str(line)})

    def _question(self, kind: str, prompt: str):
        with self.changed:
            self.serial += 1
            self.prompt = {'id': self.serial, 'kind': kind, 'text': prompt.strip()}
            self.answer = None
            answered = self.changed.wait_for(lambda: self.answer is not None, timeout=ANSWER_TIMEOUT)
            answer, self.answer, self.prompt = self.answer, None, None
        if not answered or answer is _CANCEL:
            raise EOFError
        return answer

    def ask(self, prompt: str) -> str:
        return self._question('text', prompt)

    def ask_secret(self, prompt: str) -> str:
        return self._question('secret', prompt)

    def ask_confirm(self, prompt: str) -> bool:
        return self._question('confirm', prompt) == 'yes'

    def reply(self, ident, value, cancel: bool):
        with self.changed:
            if self.prompt is None or self.prompt['id'] != ident:
                raise HttpError(409, 'no_question')
            if not cancel and self.prompt['kind'] == 'confirm' and value not in ('yes', 'no'):
                raise HttpError(400, 'field:value')
            # Вопрос снимается сразу: страница не должна увидеть его повторно до пробуждения установщика.
            self.answer, self.prompt = (_CANCEL if cancel else value), None
            self.changed.notify_all()

    def snapshot(self, after: int) -> dict:
        with self.changed:
            return {'lines': self.lines[after:], 'next': len(self.lines), 'prompt': self.prompt,
                    'command': self.command, 'running': self.running, 'result': self.result}


class InstallerWeb:
    def __init__(self, *, kit: str, language: str, state_dir: str | None):
        self.kit, self.language, self.state_dir = kit, language, state_dir
        self.conversation = Conversation()
        self.stop = threading.Event()

    def _language(self, value) -> str:
        if value is None:
            return self.language
        if value not in available_languages():
            raise HttpError(400, 'field:language')
        return value

    def info(self, query, body):
        language = self._language(query.get('language'))
        path = query.get('kit') or self.kit
        if len(path) > 4096:
            raise HttpError(400, 'field:kit')
        result = {'kit_path': path, 'default_kit': self.kit, 'language': language,
                  'languages': available_languages(), 'platform': kit_setup._installer_platform()}
        try:
            kit = kit_setup.Kit(Path(path))
            meta = kit.meta
            result['kit'] = {name: meta.get(name) for name in (
                'product_id', 'product_name', 'version', 'public_key_sha256', 'modes', 'all_modes',
                'secrets', 'targets', 'components', 'migrations')}
            setup = kit_setup.backend()(kit, language=language, yes=True,
                                        state_dir=Path(self.state_dir) if self.state_dir else None)
            state = setup.state()
            result['state'] = None if state is None else {
                'status': state.get('status'), 'version': state.get('version'),
                'install_root': state.get('install_root'), 'modes': state.get('modes'),
                'interrupted': any(name in state for name in ('install', 'update', 'uninstall'))}
            result['state_dir'] = str(setup.state_dir)
            stored = setup.state_dir / 'installer'
            result['stored_installer'] = str(stored) if (stored / 'kit.json').exists() else None
        except kit_setup.SetupError as error:
            result['error'] = _message(language, error.key, error.variables)
        return result

    def run(self, query, body):
        command = body.get('command')
        if command not in COMMANDS:
            raise HttpError(400, 'field:command')
        language = self._language(body.get('language'))
        modes = body.get('modes')
        if modes is not None and (type(modes) is not list or not all(
                type(mode) is str and 0 < len(mode) <= 64 and ',' not in mode for mode in modes)):
            raise HttpError(400, 'field:modes')
        args = argparse.Namespace(
            kit=text(body, 'kit') or self.kit, state_dir=self.state_dir, yes=False, command=command,
            root=text(body, 'root'), unit_dir=None, modes=','.join(modes) if modes else None,
            expect_key_sha256=text(body, 'expect_key_sha256', limit=64),
            accept_key_sha256=text(body, 'accept_key_sha256', limit=64),
            reconcile_stop=flag(body, 'reconcile_stop'), allow_downgrade=flag(body, 'allow_downgrade'),
            purge=flag(body, 'purge'), name=text(body, 'name', limit=128), clear=flag(body, 'clear'))
        conversation = self.conversation
        conversation.begin(command)

        def work():
            code = 1
            try:
                code = kit_setup.execute(args, language, out=conversation.out, error=conversation.error,
                                         ask=conversation.ask, ask_secret=conversation.ask_secret,
                                         ask_confirm=conversation.ask_confirm)
            finally:
                conversation.finish(code)
        threading.Thread(target=work, daemon=True).start()
        return {'started': command}

    def progress(self, query, body):
        try:
            after = max(0, int(query.get('after', '0')))
        except ValueError:
            raise HttpError(400, 'field:after') from None
        return self.conversation.snapshot(after)

    def answer(self, query, body):
        ident = body.get('id')
        if type(ident) is not int:
            raise HttpError(400, 'field:id')
        cancel = flag(body, 'cancel')
        value = '' if cancel else body.get('value', '')
        if type(value) is not str or len(value) > 8192:
            raise HttpError(400, 'field:value')
        self.conversation.reply(ident, value, cancel)
        return {'accepted': True}

    def quit(self, query, body):
        if self.conversation.running:
            raise HttpError(409, 'busy')
        self.stop.set()
        return {'stopping': True}

    def app(self, token: str, hosts: set[str]) -> WebApp:
        return WebApp(page='installer.html', assets=ASSETS, token=token, hosts=hosts, routes={
            ('GET', '/api/info'): self.info, ('POST', '/api/run'): self.run,
            ('GET', '/api/progress'): self.progress, ('POST', '/api/answer'): self.answer,
            ('POST', '/api/quit'): self.quit})


def _message(language, key, variables) -> str:
    try:
        return translate(language, key, **{name: str(value) for name, value in variables.items()})
    except LanguageError:
        return key


def serve(args, language: str) -> int:
    """Запустить страницу установщика на 127.0.0.1 и ждать завершения."""
    web = InstallerWeb(kit=args.kit, language=language, state_dir=args.state_dir)
    token = secrets.token_urlsafe(32)
    app = web.app(token, set())
    try:
        server = start(app, '127.0.0.1', args.port)
    except OSError:
        print(translate(language, 'kit.web_port_busy', port=str(args.port)), file=sys.stderr)
        return 2
    port = server.server_address[1]
    # Разрешённые значения Host известны только после выбора порта; до этого запросы отклоняются.
    app.hosts.update({'127.0.0.1:' + str(port), 'localhost:' + str(port)})
    url = 'http://127.0.0.1:' + str(port) + '/#t=' + token
    print(translate(language, 'kit.web_open', url=url), flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while not web.stop.wait(1.0):
            if not web.conversation.running and time.monotonic() - app.last_request > IDLE_LIMIT:
                break
    except KeyboardInterrupt:
        pass
    server.shutdown()
    server.server_close()
    print(translate(language, 'kit.web_stopped'), flush=True)
    return 0
