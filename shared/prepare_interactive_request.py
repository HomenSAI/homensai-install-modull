"""Build a persistent API request for the experimental interactive installer."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile

from shared import api
from shared.datahome import checked_path
from shared.i18n import LanguageError, available_languages, translate
from shared.interactive_install import MAX_CONFIG_BYTES, _Parser, read_config
from shared.package_signing import MAX_ENVELOPE
from shared.preflight import MAX_BYTES, NAMES

_FIELDS = {'package_path', 'expected_product_id', 'expected_version', 'install_root', 'unit_dir',
           'work_dir', 'modes', 'required_bytes', 'reserve_bytes'}
_NAMES = ('release', 'runtime', 'publish', 'load', 'start')


def _read_public_file(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('invalid public file')
        data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError('invalid public file')
    return data


def prepare(template, envelope, public_key, output, *, language='en', handle=api.handle):
    """Inspect signed inputs, then write a new mode-0600 request without overwriting."""
    if (type(template) is not dict or set(template) != _FIELDS or type(envelope) is not bytes
            or not 0 < len(envelope) <= MAX_ENVELOPE or type(public_key) is not bytes or len(public_key) != 32):
        raise ValueError('invalid request input')
    path = checked_path(output)
    if not path.is_absolute() or path.exists() or not path.parent.is_dir():
        raise ValueError('request destination unavailable')
    identifiers = set()
    while len(identifiers) < len(_NAMES) + 1:
        identifiers.add(secrets.token_hex(16))
    values = sorted(identifiers)
    params = {**template, 'envelope': base64.b64encode(envelope).decode('ascii'),
              'trusted_public_key': base64.b64encode(public_key).decode('ascii'),
              'workflow_key': values[0], 'keys': dict(zip(_NAMES, values[1:]))}
    response = handle({'api_major': 1, 'request_id': 'prepare-interactive-request',
                       'operation': 'inspect-portable-linux-install', 'language': language, 'params': params})
    if (type(response) is not dict or response.get('ok') is not True
            or type(response.get('result')) is not dict or response['result'].get('state') != 'no_checkpoint'):
        raise ValueError('request inspection failed')
    data = (json.dumps(params, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('ascii')
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError('request too large')
    descriptor, temporary = tempfile.mkstemp(prefix='.homensai-request-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and refuses an existing destination.
        # A failure after link may leave the complete request at the final path.
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def wizard(language, *, ask, write):
    """Collect explicit product inputs without creating or running the product."""
    def prompt(key):
        return ask(translate(language, 'interactive.' + key) + ': ').strip()
    write(translate(language, 'interactive.wizard_intro'))
    template = {'package_path': prompt('package'), 'expected_product_id': prompt('product'),
                'expected_version': prompt('version'), 'install_root': prompt('root'),
                'unit_dir': prompt('units'), 'work_dir': prompt('work')}
    template['modes'] = [item.strip() for item in prompt('wizard_modes').split(',') if item.strip()]
    write(translate(language, 'interactive.wizard_space_hint'))
    sizes = {}
    for name in NAMES:
        size = int(ask(translate(language, 'interactive.wizard_space', name=name) + ': '))
        if not 0 <= size <= MAX_BYTES:
            raise ValueError('invalid space')
        sizes[name] = size
    reserve = int(prompt('wizard_reserve'))
    if not 0 <= reserve <= MAX_BYTES:
        raise ValueError('invalid reserve')
    template['required_bytes'], template['reserve_bytes'] = sizes, reserve
    envelope_file, key_file, output = prompt('wizard_envelope'), prompt('wizard_key'), prompt('wizard_output')
    prepare(template, _read_public_file(envelope_file, MAX_ENVELOPE), _read_public_file(key_file, 32),
            output, language=language)


def main(argv=None):
    parser = _Parser(description='Prepare an experimental Linux installation request', allow_abbrev=False)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--template', help='JSON parameters without keys or signatures')
    source.add_argument('--wizard', action='store_true', help='Prompt for inputs in a terminal')
    parser.add_argument('--envelope-file', help='Raw signed envelope file')
    parser.add_argument('--public-key-file', help='Independently trusted raw 32-byte Ed25519 key')
    parser.add_argument('--output', help='New absolute request file')
    parser.add_argument('--language', default='en', help='Installed language code')
    language = 'en'
    try:
        args = parser.parse_args(argv)
        if args.language not in available_languages():
            raise ValueError('unknown language')
        language = args.language
        if args.wizard:
            if not sys.stdin.isatty() or any((args.envelope_file, args.public_key_file, args.output)):
                raise ValueError('terminal required')
            wizard(language, ask=input, write=print)
        else:
            if not all((args.envelope_file, args.public_key_file, args.output)):
                raise ValueError('missing input')
            prepare(read_config(args.template), _read_public_file(args.envelope_file, MAX_ENVELOPE),
                    _read_public_file(args.public_key_file, 32), args.output, language=language)
        print(translate(language, 'interactive.request_saved'))
        return 0
    except (OSError, ValueError, UnicodeError, RecursionError, LanguageError, EOFError, KeyboardInterrupt):
        try:
            message = translate(language, 'interactive.request_failed')
        except LanguageError:
            message = 'Request not confirmed; inspect the output before retrying.'
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
