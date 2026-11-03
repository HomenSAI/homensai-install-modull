"""Bounded, local, allowlisted events for the experimental Linux installer."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import stat
import sys

from shared.datahome import checked_path

MAX_BYTES = 1024 * 1024
EVENTS = frozenset({'inspection_started', 'inspection_failed', 'inspection_succeeded',
                    'install_cancelled', 'install_started', 'install_unconfirmed', 'install_recorded',
                    'api_started', 'api_succeeded', 'api_failed', 'api_unconfirmed'})
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00\Z')


class TechnicalLogError(RuntimeError):
    """The event could not be durably recorded; it may already be present."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TechnicalLogError('Invalid log entry')
        result[key] = value
    return result


def _check_previous(descriptor, info):
    """Reject unexpected content, not just a missing newline at the end."""
    if info.st_size > MAX_BYTES:
        raise TechnicalLogError('Log needs inspection')
    data = os.pread(descriptor, info.st_size + 1, 0)
    after = os.fstat(descriptor)
    if (len(data) != info.st_size or (data and not data.endswith(b'\n'))
            or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise TechnicalLogError('Log needs inspection')
    try:
        for line in data.splitlines():
            row = json.loads(line.decode('ascii'), object_pairs_hook=_unique)
            if (type(row) is not dict or set(row) != {'time_utc', 'event'}
                    or type(row['event']) is not str or row['event'] not in EVENTS
                    or type(row['time_utc']) is not str or _TIMESTAMP.fullmatch(row['time_utc']) is None):
                raise TechnicalLogError('Invalid log entry')
            datetime.fromisoformat(row['time_utc'])
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise TechnicalLogError('Invalid log entry') from None


def record(path, event):
    """Append exactly one fixed event and UTC time; never accept arbitrary fields."""
    if (type(event) is not str or event not in EVENTS or not sys.platform.startswith('linux')
            or not all(hasattr(os, name) for name in ('getuid', 'O_NOFOLLOW', 'O_DIRECTORY', 'pread'))):
        raise TechnicalLogError('Unsupported event')
    import fcntl
    descriptor = None
    try:
        destination = checked_path(path)
        parent = destination.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise TechnicalLogError('Untrusted log directory')
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        descriptor = os.open(destination, flags, 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise TechnicalLogError('Untrusted log file')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = destination.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise TechnicalLogError('Log file changed')
        info = os.fstat(descriptor)
        _check_previous(descriptor, info)
        body = (json.dumps({'time_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                            'event': event}, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
        if info.st_size + len(body) > MAX_BYTES:
            raise TechnicalLogError('Log needs inspection')
        if os.write(descriptor, body) != len(body):
            raise TechnicalLogError('Partial event')
        os.fsync(descriptor)
        current = destination.lstat()
        if (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink) != (
                current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_nlink):
            raise TechnicalLogError('Log file changed')
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except TechnicalLogError:
        raise
    except (OSError, ValueError, OverflowError):
        raise TechnicalLogError('Event not confirmed') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
