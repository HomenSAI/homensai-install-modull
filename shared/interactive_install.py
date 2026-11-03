"""Interactive entry point for the experimental signed Linux controller."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from shared import api
from shared.i18n import LanguageError, available_languages, translate

MAX_CONFIG_BYTES = 1024 * 1024


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo arbitrary arguments, which may accidentally contain secrets.
        raise ValueError('invalid arguments')


class _EventLogFailure(RuntimeError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def read_bounded_file(path, limit):
    """Read a stable bounded regular file without following its final symlink."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        def signature(info):
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('invalid file')
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
        before = signature(os.fstat(stream.fileno()))
        if signature(os.lstat(path)) != before:
            raise ValueError('invalid file')
        raw = stream.read(limit + 1)
        if signature(os.fstat(stream.fileno())) != before or signature(os.lstat(path)) != before:
            raise ValueError('file changed')
    if len(raw) > limit:
        raise ValueError('invalid file')
    return raw


def read_config(path):
    """Read a bounded, stable JSON object without duplicate keys."""
    raw = read_bounded_file(path, MAX_CONFIG_BYTES)
    result = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    if type(result) is not dict:
        raise ValueError('invalid config')
    return result


def run(params, language, *, ask, write, handle=api.handle, inspect_only=False, emit=None):
    """Inspect once, ask once, and make at most one modifying API call."""
    def message(key):
        return translate(language, 'interactive.' + key)
    def call(operation):
        return handle({'api_major': 1, 'request_id': 'interactive-install',
                       'operation': operation, 'params': params, 'language': language})
    if type(params) is not dict or type(inspect_only) is not bool or language not in available_languages():
        raise ValueError('invalid input')
    def event(name):
        if emit is not None:
            emit(name)
    event('inspection_started')
    inspection = call('inspect-portable-linux-install')
    if type(inspection) is not dict or inspection.get('ok') is not True:
        event('inspection_failed')
        write(message('inspection_failed'))
        return 1
    result = inspection.get('result')
    if (type(result) is not dict or result.get('state') not in ('no_checkpoint', 'pending', 'recorded_complete')
            or any(type(params.get(key)) is not str for key in
                   ('expected_product_id', 'expected_version', 'install_root', 'package_path',
                    'unit_dir', 'work_dir', 'trusted_public_key'))
            or type(params.get('modes')) is not list or not params['modes']
            or any(type(mode) is not str for mode in params['modes'])):
        event('inspection_failed')
        write(message('inspection_failed'))
        return 1
    try:
        key = base64.b64decode(params['trusted_public_key'], validate=True)
        if len(key) != 32:
            raise ValueError('invalid public key')
    except (ValueError, TypeError):
        event('inspection_failed')
        write(message('inspection_failed'))
        return 1
    event('inspection_succeeded')
    write(message('summary'))
    for label, name in (('product', 'expected_product_id'), ('version', 'expected_version'),
                        ('package', 'package_path'), ('root', 'install_root'),
                        ('units', 'unit_dir'), ('work', 'work_dir'), ('modes', 'modes')):
        # Escape control characters and terminal sequences from untrusted paths.
        write(message(label) + ': ' + json.dumps(params[name], ensure_ascii=True))
    write(message('key_fingerprint') + ': ' + hashlib.sha256(key).hexdigest())
    if 'runtime_evidence' in params:
        policy = params['runtime_evidence']
        if (type(policy) is not dict or set(policy) != {'lock_path', 'evidence_path', 'evidence_sha256', 'max_age_seconds'}
                or any(type(policy[name]) is not str for name in ('lock_path', 'evidence_path', 'evidence_sha256'))
                or type(policy['max_age_seconds']) is not int):
            write(message('inspection_failed'))
            return 1
        write(message('evidence_policy'))
        for label, name in (('evidence_lock', 'lock_path'), ('evidence_report', 'evidence_path'),
                            ('evidence_digest', 'evidence_sha256'), ('evidence_age', 'max_age_seconds')):
            write(message(label) + ': ' + json.dumps(policy[name], ensure_ascii=True))
    write(message('state_' + result['state']))
    completed, total, current = result.get('completed_steps'), result.get('total_steps'), result.get('current_step')
    if (type(completed) is int and type(total) is int and 0 <= completed <= total <= 1000):
        write(message('progress') + ': ' + str(completed) + '/' + str(total))
    if type(current) is str and 0 < len(current) <= 128:
        write(message('current_step') + ': ' + json.dumps(current, ensure_ascii=True))
    write(message('trust_warning'))
    if inspect_only:
        write(message('inspection_only'))
        return 0
    try:
        answer = ask(message('confirm'))
    except (EOFError, KeyboardInterrupt):
        event('install_cancelled')
        write(message('cancelled'))
        return 2
    if answer != 'INSTALL':
        event('install_cancelled')
        write(message('cancelled'))
        return 2
    event('install_started')
    response = call('install-portable-linux')
    if (type(response) is not dict or response.get('ok') is not True
            or type(response.get('result')) is not dict
            or response['result'].get('state') != 'recorded_complete'):
        event('install_unconfirmed')
        write(message('install_failed'))
        return 1
    event('install_recorded')
    write(message('recorded'))
    return 0


def main(argv=None):
    parser = _Parser(description='Interactive experimental Linux installation', allow_abbrev=False)
    parser.add_argument('--request-file', required=True, help='JSON parameters for install-portable-linux')
    parser.add_argument('--language', default='en', help='Installed language code')
    parser.add_argument('--inspect', action='store_true', help='Inspect checkpoint without installing')
    parser.add_argument('--event-log', help='Absolute path in a private directory for fixed events')
    language = 'en'
    try:
        args = parser.parse_args(argv)
        if args.language not in available_languages():
            raise ValueError('unknown language')
        language = args.language
        if not args.inspect and not sys.stdin.isatty():
            raise ValueError('interactive terminal required')
        params = read_config(Path(args.request_file))
        emit = None
        if args.event_log is not None:
            from shared.technical_log import TechnicalLogError, record
            def emit(event):
                try:
                    record(args.event_log, event)
                except TechnicalLogError:
                    raise _EventLogFailure() from None
        return run(params, language, ask=input, write=print, inspect_only=args.inspect, emit=emit)
    except _EventLogFailure:
        try:
            message = translate(language, 'interactive.log_failed')
        except LanguageError:
            message = 'Event log failed; inspect the installation state before retrying.'
        print(message, file=sys.stderr)
        return 1
    except (OSError, ValueError, UnicodeError, RecursionError, LanguageError):
        try:
            message = translate(language, 'interactive.input_failed')
        except LanguageError:
            message = 'Unable to read request or language.'
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
