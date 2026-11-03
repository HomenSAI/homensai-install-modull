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


def run(params, language, *, ask, write, handle=api.handle):
    """Inspect once, ask once, and make at most one modifying API call."""
    def message(key):
        return translate(language, 'interactive.' + key)
    def call(operation):
        return handle({'api_major': 1, 'request_id': 'interactive-install',
                       'operation': operation, 'params': params, 'language': language})
    if type(params) is not dict or language not in available_languages():
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
    write(message('trust_warning'))
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
    parser = argparse.ArgumentParser(description='Interactive experimental Linux installation')
    parser.add_argument('--request-file', required=True, help='JSON parameters for install-portable-linux')
    parser.add_argument('--language', default='en', help='Installed language code')
    args = parser.parse_args(argv)
    try:
        if not sys.stdin.isatty():
            raise ValueError('interactive terminal required')
        params = read_config(Path(args.request_file))
        return run(params, args.language, ask=input, write=print)
    except (OSError, ValueError, UnicodeError, RecursionError, LanguageError):
        print('Unable to read request or language / Не удалось прочитать запрос или язык', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
