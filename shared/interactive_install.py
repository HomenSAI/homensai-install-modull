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


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def read_config(path):
    """Read a bounded regular JSON file without following its final symlink."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise ValueError('invalid file')
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError('invalid file')
    result = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    if type(result) is not dict:
        raise ValueError('invalid config')
    return result


def run(params, language, *, ask, write, handle=api.handle, inspect_only=False):
    """Inspect once, ask once, and make at most one modifying API call."""
    def message(key):
        return translate(language, 'interactive.' + key)
    def call(operation):
        return handle({'api_major': 1, 'request_id': 'interactive-install',
                       'operation': operation, 'params': params, 'language': language})
    if type(params) is not dict or type(inspect_only) is not bool or language not in available_languages():
        raise ValueError('invalid input')
    inspection = call('inspect-portable-linux-install')
    if type(inspection) is not dict or inspection.get('ok') is not True:
        write(message('inspection_failed'))
        return 1
    result = inspection.get('result')
    if (type(result) is not dict or result.get('state') not in ('no_checkpoint', 'pending', 'recorded_complete')
            or any(type(params.get(key)) is not str for key in
                   ('expected_product_id', 'expected_version', 'install_root', 'package_path',
                    'unit_dir', 'work_dir', 'trusted_public_key'))
            or type(params.get('modes')) is not list or not params['modes']
            or any(type(mode) is not str for mode in params['modes'])):
        write(message('inspection_failed'))
        return 1
    try:
        key = base64.b64decode(params['trusted_public_key'], validate=True)
        if len(key) != 32:
            raise ValueError('invalid public key')
    except (ValueError, TypeError):
        write(message('inspection_failed'))
        return 1
    write(message('summary'))
    for label, name in (('product', 'expected_product_id'), ('version', 'expected_version'),
                        ('package', 'package_path'), ('root', 'install_root'),
                        ('units', 'unit_dir'), ('work', 'work_dir'), ('modes', 'modes')):
        # Escape control characters and terminal sequences from untrusted paths.
        write(message(label) + ': ' + json.dumps(params[name], ensure_ascii=True))
    write(message('key_fingerprint') + ': ' + hashlib.sha256(key).hexdigest())
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
        write(message('cancelled'))
        return 2
    if answer != 'INSTALL':
        write(message('cancelled'))
        return 2
    response = call('install-portable-linux')
    if (type(response) is not dict or response.get('ok') is not True
            or type(response.get('result')) is not dict
            or response['result'].get('state') != 'recorded_complete'):
        write(message('install_failed'))
        return 1
    write(message('recorded'))
    return 0


def main(argv=None):
    parser = _Parser(description='Interactive experimental Linux installation', allow_abbrev=False)
    parser.add_argument('--request-file', required=True, help='JSON parameters for install-portable-linux')
    parser.add_argument('--language', default='en', help='Installed language code')
    parser.add_argument('--inspect', action='store_true', help='Inspect checkpoint without installing')
    language = 'en'
    try:
        args = parser.parse_args(argv)
        if args.language not in available_languages():
            raise ValueError('unknown language')
        language = args.language
        if not args.inspect and not sys.stdin.isatty():
            raise ValueError('interactive terminal required')
        params = read_config(Path(args.request_file))
        return run(params, language, ask=input, write=print, inspect_only=args.inspect)
    except (OSError, ValueError, UnicodeError, RecursionError, LanguageError):
        try:
            message = translate(language, 'interactive.input_failed')
        except LanguageError:
            message = 'Unable to read request or language.'
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
