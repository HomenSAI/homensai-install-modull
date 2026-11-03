#!/usr/bin/env python3
"""Experimental standalone Linux launcher for a pointer-selected release.

Only run with trusted configuration, launcher, interpreter and product files.
It neither verifies package signatures nor isolates code. Paths can be changed
by a process with the same privileges between checks and exec.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_MAX_POINTER = 4096


class LauncherError(ValueError):
    pass


def _relative(value):
    if (type(value) is not str or not value or value.startswith('/') or '\\' in value or ':' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(not part or part in ('.', '..') or part.endswith(('.', ' ')) for part in value.split('/'))):
        raise LauncherError('invalid relative path')
    return value


def _checked(path, *, directory):
    if (not sys.platform.startswith('linux') or not hasattr(os, 'getuid')
            or not hasattr(os, 'O_NOFOLLOW') or not path.is_absolute()):
        raise LauncherError('unsupported environment')
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022):
            raise LauncherError('unsafe parent directory')
    info = path.lstat()
    if (info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022
            or (directory is True and not stat.S_ISDIR(info.st_mode))
            or (directory is False and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
            or (directory is None and not (stat.S_ISDIR(info.st_mode)
                or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)))):
        raise LauncherError('unsafe path')
    return info


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LauncherError('duplicate pointer key')
        result[key] = value
    return result


def _pointer(path):
    original = _checked(path, directory=False)
    if original.st_uid != os.getuid() or original.st_size > _MAX_POINTER:
        raise LauncherError('invalid pointer')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(_MAX_POINTER + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                              value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if (identity(original) != identity(before) or identity(before) != identity(after)
            or identity(after) != identity(current) or len(data) != before.st_size or len(data) > _MAX_POINTER):
        raise LauncherError('pointer changed')
    value = json.loads(data.decode('utf-8'), object_pairs_hook=_unique)
    if (type(value) is not dict or set(value) != {'release'} or type(value['release']) is not str
            or not _VERSION.fullmatch(value['release'])):
        raise LauncherError('invalid pointer')
    return value['release'], identity(current)


def resolve_command(*, root, app, data, runtime, releases, pointer, command):
    """Resolve trusted argv without launching; every template must be a whole argument."""
    if (type(root) is not str or not root.startswith('/') or root == '/' or '\\' in root
            or any(part in ('', '.', '..') for part in root.split('/')[1:])
            or any(ord(c) < 32 or ord(c) == 127 for c in root)
            or type(command) is not tuple or not command or len(command) > 128
            or any(type(arg) is not str or not arg or len(arg) > 8192
                   or any(ord(c) < 32 or ord(c) == 127 for c in arg) for arg in command)):
        raise LauncherError('invalid launcher configuration')
    root = Path(root)
    _checked(root, directory=True)
    roots = {name: root / _relative(value) for name, value in
             (('app', app), ('data', data), ('runtime', runtime), ('releases', releases))}
    directories = list(roots.values())
    if any(a.is_relative_to(b) or b.is_relative_to(a)
           for index, a in enumerate(directories) for b in directories[index + 1:]):
        raise LauncherError('overlapping directories')
    for directory in roots.values():
        _checked(directory, directory=True)
    pointer_path = root / _relative(pointer)
    if any(pointer_path.is_relative_to(directory) for directory in directories):
        raise LauncherError('pointer overlaps data or code')
    version, identity = _pointer(pointer_path)
    release = roots['releases'] / ('app-' + version)
    _checked(release, directory=True)
    roots['release'] = release
    output = []
    for index, argument in enumerate(command):
        if argument.startswith('@'):
            marker, sep, suffix = argument.partition('/')
            name = marker[1:]
            if name not in roots or name == 'releases' or (sep and not suffix):
                raise LauncherError('invalid template')
            if index == 0 and (name not in ('release', 'runtime') or not sep):
                raise LauncherError('invalid executable template')
            path = roots[name] / _relative(suffix) if sep else roots[name]
            if name in ('release', 'runtime') and sep:
                _checked(path, directory=False if index == 0 else None)
            output.append(str(path))
        else:
            if index == 0:
                raise LauncherError('executable template required')
            output.append(argument)
    if not os.access(output[0], os.X_OK) or _pointer(pointer_path) != (version, identity):
        raise LauncherError('executable or pointer changed')
    return tuple(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'app', 'data', 'runtime', 'releases', 'pointer'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        resolved = resolve_command(root=args.root, app=args.app, data=args.data, runtime=args.runtime,
                                   releases=args.releases, pointer=args.pointer, command=tuple(command))
        os.execv(resolved[0], resolved)
    except (OSError, ValueError, UnicodeError, RecursionError):
        print('Unable to start the selected release', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
