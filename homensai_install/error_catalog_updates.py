"""Explicit local data-only catalogue updates; no network, hooks or global cache.

Private sequence state does not resist deletion/rollback of its entire history
by its owner. SQLite path identity is checked in a private namespace; this is
not an atomic defence against a hostile same-UID path swap. Only Linux writes
are accepted. Readers never create, chmod, migrate or recover a database.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from types import MappingProxyType
import uuid

from shared.datahome import checked_path
from .api.model import canonical, digest, parse_json
from .errors import ErrorCatalog, InstallerError

MAX_BYTES = 2 * 1024 * 1024
_MAX_DB_BYTES = 5 * MAX_BYTES
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z')
_SIGNATURE_FIELDS = {'algorithm', 'key_id', 'signature'}
_META_FIELDS = {'schema_version', 'kind', 'purpose', 'module_id', 'catalog_id',
    'catalog_schema_version', 'api_major', 'baseline_semantics_sha256', 'catalog_version',
    'sequence', 'issued_at', 'expires_at', 'development', 'catalog_sha256', 'catalog_bytes'}
_POLICY_FIELDS = {'schema_version', 'policy_id', 'policy_epoch', 'module_id', 'catalog_id',
    'baseline_semantics_sha256', 'min_sequence', 'valid_from', 'valid_until',
    'max_lifetime_seconds', 'keys', 'revoked_key_ids', 'revoked_artifact_sha256'}
_STATE_FIELDS = {'schema_version', 'store_id', 'sequence', 'high_water_sequence',
    'artifact_sha256', 'catalog_version', 'policy_epoch', 'policy_digest',
    'accepted_at', 'accepted_review_digest'}
_ALLOWED_FILES = {'initialized.json', 'catalog-state.sqlite3', 'catalog.lock',
                  'catalog-state.sqlite3-journal'}


def _fail(reason, code='INTEGRITY_FAILED'):
    raise InstallerError(code, phase='error_catalog', details={'reason': reason}) from None


def _exact(value, fields):
    if type(value) is not dict or set(value) != fields:
        _fail('catalog_fields')


def _integer(value, *, minimum=1):
    return type(value) is int and minimum <= value <= (1 << 63) - 1


def _time(value):
    if type(value) is not str or not _TIME.fullmatch(value):
        _fail('catalog_timestamp')
    try:
        return datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError:
        _fail('catalog_timestamp')


def _builtin():
    return deepcopy(ErrorCatalog()._data)


def catalog_semantics_digest(catalog):
    """Behaviour and code set binding; text/display version are excluded."""
    value = deepcopy(catalog)
    value.pop('catalog_version', None)
    for entry in value['errors'].values():
        entry.pop('translations', None)
    return digest(value)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_file(path, *, maximum=MAX_BYTES, private=False):
    path = checked_path(Path(path).absolute())
    before = path.lstat()
    mask = 0o077 if private else 0o022
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum
            or os.name != 'nt' and (before.st_uid != os.geteuid() or before.st_mode & mask)):
        _fail('catalog_unsafe_file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            _fail('catalog_file_changed')
        raw = stream.read(maximum + 1)
        if len(raw) > maximum or _identity(os.fstat(stream.fileno())) != _identity(before):
            _fail('catalog_file_changed')
    if _identity(path.lstat()) != _identity(before):
        _fail('catalog_file_changed')
    return raw, _identity(before)


def _directory(path, *, create=False):
    path = checked_path(path)
    if create:
        missing = []
        ancestor = path
        while not os.path.lexists(ancestor):
            missing.append(ancestor)
            ancestor = ancestor.parent
        for directory in reversed(missing):
            # Path.mkdir(parents=True) uses default0755 for intermediate API
            # state dirs. Create only absent components, never chmod parents.
            parent_fd = os.open(checked_path(directory.parent),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                try:
                    os.mkdir(directory.name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    child = os.fstat(child_fd)
                    if child.st_uid != os.geteuid() or stat.S_IMODE(child.st_mode) != 0o700:
                        _fail('catalog_private_directory_required', 'STATE_STORAGE_UNAVAILABLE')
                finally:
                    os.close(child_fd)
            finally:
                os.close(parent_fd)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or os.name != 'nt'
            and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700)):
        _fail('catalog_private_directory_required', 'STATE_STORAGE_UNAVAILABLE')
    return path


@dataclass(frozen=True)
class CatalogUpdatePolicy:
    keys: object
    policy_id: str
    valid_from: str
    valid_until: str
    policy_epoch: int = 1
    module_id: str = 'homensai-install'
    catalog_id: str = 'homensai-installer-errors'
    baseline_semantics_sha256: str | None = None
    min_sequence: int = 1
    max_lifetime_seconds: int = 7_776_000
    revoked_key_ids: object = ()
    revoked_artifact_sha256: object = ()
    allow_development: bool = False
    clock: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        for value in (self.policy_id, self.module_id, self.catalog_id):
            if type(value) is not str or not _NAME.fullmatch(value):
                _fail('catalog_policy_identifier')
        if (not _integer(self.policy_epoch) or not _integer(self.min_sequence)
                or not _integer(self.max_lifetime_seconds)
                or type(self.allow_development) is not bool
                or self.clock is not None and not callable(self.clock)):
            _fail('catalog_policy_shape')
        if _time(self.valid_from) >= _time(self.valid_until):
            _fail('catalog_policy_interval')
        if type(self.keys) is not dict or not 1 <= len(self.keys) <= 128:
            _fail('catalog_role_keys_required')
        keys = dict(self.keys)
        for key, value in keys.items():
            if (type(key) is not str or not _NAME.fullmatch(key)
                    or type(value) is not bytes or len(value) != 32):
                _fail('catalog_role_key_invalid')
        if (type(self.revoked_key_ids) not in (list, tuple)
                or type(self.revoked_artifact_sha256) not in (list, tuple)):
            _fail('catalog_revocation_policy')
        revoked = tuple(self.revoked_key_ids)
        hashes = tuple(self.revoked_artifact_sha256)
        if (len(revoked) > 128 or len(hashes) > 1024
                or any(type(k) is not str or not _NAME.fullmatch(k) for k in revoked)
                or any(type(k) is not str or not _HASH.fullmatch(k) for k in hashes)
                or len(set(revoked)) != len(revoked) or len(set(hashes)) != len(hashes)):
            _fail('catalog_revocation_policy')
        baseline = catalog_semantics_digest(_builtin())
        if self.baseline_semantics_sha256 not in (None, baseline):
            _fail('catalog_baseline_incompatible')
        object.__setattr__(self, 'keys', MappingProxyType(keys))
        object.__setattr__(self, 'revoked_key_ids', revoked)
        object.__setattr__(self, 'revoked_artifact_sha256', hashes)
        object.__setattr__(self, 'baseline_semantics_sha256', baseline)

    def _body(self):
        return {'schema_version': 1, 'policy_id': self.policy_id, 'policy_epoch': self.policy_epoch,
            'module_id': self.module_id, 'catalog_id': self.catalog_id,
            'baseline_semantics_sha256': self.baseline_semantics_sha256,
            'min_sequence': self.min_sequence, 'valid_from': self.valid_from,
            'valid_until': self.valid_until, 'max_lifetime_seconds': self.max_lifetime_seconds,
            'keys': {k: base64.b64encode(v).decode('ascii') for k, v in self.keys.items()},
            'revoked_key_ids': list(self.revoked_key_ids),
            'revoked_artifact_sha256': list(self.revoked_artifact_sha256)}

    @property
    def policy_digest(self):
        return digest({**self._body(), 'allow_development': self.allow_development})

    def _now(self):
        now = self.clock() if self.clock is not None else datetime.now(timezone.utc)
        if (not isinstance(now, datetime) or now.tzinfo is None
                or now.utcoffset() != timezone.utc.utcoffset(now)):
            _fail('catalog_clock_invalid')
        return now


def load_catalog_update_policy(path, allow_development=False):
    try:
        raw, _ = _safe_file(path, maximum=65536)
        value = parse_json(raw, error_code='INTEGRITY_FAILED')
        _exact(value, _POLICY_FIELDS)
        if type(value['schema_version']) is not int or value.pop('schema_version') != 1:
            _fail('catalog_policy_schema')
        if (type(value['keys']) is not dict or type(value['revoked_key_ids']) is not list
                or type(value['revoked_artifact_sha256']) is not list):
            _fail('catalog_policy_shape')
        value['keys'] = {k: base64.b64decode(v, validate=True) for k, v in value['keys'].items()}
        return CatalogUpdatePolicy(**value, allow_development=allow_development)
    except InstallerError:
        raise
    except (OSError, ValueError, TypeError, binascii.Error):
        _fail('catalog_policy_unavailable')


def _catalog(value, policy):
    baseline = _builtin()
    _exact(value, set(baseline))
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['catalog_version']) is not str or not _NAME.fullmatch(value['catalog_version'])
            or value['locales'] != baseline['locales'] or type(value['errors']) is not dict
            or set(value['errors']) != set(baseline['errors'])):
        _fail('catalog_schema_or_codes')
    for code, expected in baseline['errors'].items():
        entry = value['errors'][code]
        _exact(entry, set(expected))
        if (type(entry['retryable']) is not bool or type(entry['severity']) is not str
                or any(entry[k] != expected[k] for k in expected if k != 'translations')):
            _fail('catalog_behavior_changed')
        _exact(entry['translations'], {'ru', 'en', 'de'})
        for text in entry['translations'].values():
            _exact(text, {'title', 'description', 'impact', 'possible_causes', 'checks', 'remediation'})
            for key in ('title', 'description', 'impact'):
                if type(text[key]) is not str or not text[key].strip() or len(text[key]) > (256 if key == 'title' else 8192):
                    _fail('catalog_text_bounds')
            for key in ('possible_causes', 'checks', 'remediation'):
                if (type(text[key]) is not list or len(text[key]) > 32
                        or any(type(item) is not str or len(item) > 8192 for item in text[key])):
                    _fail('catalog_text_bounds')
    if catalog_semantics_digest(value) != policy.baseline_semantics_sha256:
        _fail('catalog_behavior_changed')
    return value


def _artifact(raw, policy):
    if len(raw) > MAX_BYTES:
        _fail('catalog_artifact_too_large')
    value = parse_json(raw, error_code='INTEGRITY_FAILED')
    _exact(value, {'metadata', 'catalog', 'signature'})
    meta, signature = value['metadata'], value['signature']
    _exact(meta, _META_FIELDS)
    _exact(signature, _SIGNATURE_FIELDS)
    if (any(type(meta[k]) is not int or meta[k] != 1 for k in ('schema_version', 'catalog_schema_version', 'api_major'))
            or meta['kind'] != 'error_catalog_update' or meta['purpose'] != 'installer-error-catalog'
            or meta['module_id'] != policy.module_id or meta['catalog_id'] != policy.catalog_id
            or meta['baseline_semantics_sha256'] != policy.baseline_semantics_sha256
            or not _integer(meta['sequence']) or type(meta['development']) is not bool
            or type(meta['catalog_bytes']) is not int or not 1 <= meta['catalog_bytes'] <= MAX_BYTES
            or type(meta['catalog_sha256']) is not str or not _HASH.fullmatch(meta['catalog_sha256'])):
        _fail('catalog_metadata_binding')
    data = _catalog(value['catalog'], policy)
    serialized = canonical(data).encode('utf-8')
    if (meta['catalog_bytes'] != len(serialized) or meta['catalog_sha256'] != hashlib.sha256(serialized).hexdigest()
            or meta['catalog_version'] != data['catalog_version']):
        _fail('catalog_content_digest')
    issued, expires = _time(meta['issued_at']), _time(meta['expires_at'])
    if issued >= expires or (expires - issued).total_seconds() > policy.max_lifetime_seconds:
        _fail('catalog_artifact_interval')
    key = signature['key_id']
    if (signature['algorithm'] != 'ed25519' or type(key) is not str or key not in policy.keys
            or type(signature['signature']) is not str or len(signature['signature']) > 128):
        _fail('catalog_signature_key_or_algorithm')
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        _fail('catalog_crypto_unavailable', 'PREREQUISITE_MISSING')
    try:
        sig = base64.b64decode(signature['signature'], validate=True)
        if len(sig) != 64:
            _fail('catalog_signature_invalid')
        Ed25519PublicKey.from_public_bytes(policy.keys[key]).verify(sig, canonical(meta).encode('utf-8'))
    except (ValueError, binascii.Error, InvalidSignature):
        _fail('catalog_signature_invalid')
    return value


def _authorize(value, raw_digest, policy, *, accepted_at=None, policy_epoch=None):
    now = policy._now()
    if not _time(policy.valid_from) <= now < _time(policy.valid_until):
        _fail('catalog_policy_expired')
    meta = value['metadata']
    if (value['signature']['key_id'] in policy.revoked_key_ids
            or raw_digest in policy.revoked_artifact_sha256):
        _fail('catalog_revoked')
    if meta['development'] and not policy.allow_development:
        _fail('catalog_development_forbidden')
    if not _time(meta['issued_at']) <= now:
        _fail('catalog_not_yet_valid')
    if now >= _time(meta['expires_at']):
        _fail('catalog_expired')
    if meta['sequence'] < policy.min_sequence:
        _fail('catalog_sequence_below_floor')
    if policy_epoch is not None and policy.policy_epoch < policy_epoch:
        _fail('catalog_policy_rollback')
    if accepted_at is not None and now < _time(accepted_at):
        _fail('catalog_clock_rollback')
    return now


def _commit(connection):
    """Actual transaction commit; a narrow crash-test boundary, no side effects beyond SQLite."""
    connection.execute('COMMIT')


class CatalogUpdateStore:
    def __init__(self, directory, *, policy):
        if not isinstance(policy, CatalogUpdatePolicy):
            _fail('catalog_trusted_policy_required', 'INVALID_REQUEST')
        self.directory = Path(directory).absolute()
        self.policy = policy
        self.path = self.directory / 'catalog-state.sqlite3'
        self.marker = self.directory / 'initialized.json'

    def _read(self, *, locked=False):
        """Snapshot authority only. Missing first-use namespace is not initialized state."""
        if os.path.lexists(self.directory) and not sys.platform.startswith('linux'):
            _fail('catalog_read_platform_unverified', 'STATE_STORAGE_UNAVAILABLE')
        lock = self.directory / 'catalog.lock'
        if not locked and os.path.lexists(lock):
            with self._read_lock(lock):
                return self._read(locked=True)
        if not os.path.lexists(self.directory):
            return None
        _directory(self.directory)
        names = {p.name for p in self.directory.iterdir()}
        if names - _ALLOWED_FILES or 'catalog-state.sqlite3-journal' in names:
            _fail('catalog_state_uncertain', 'STATE_STORAGE_UNAVAILABLE')
        has_marker = 'initialized.json' in names
        has_db = self.path.name in names
        if not has_marker and not has_db:
            return None
        if not has_marker or not has_db or 'catalog.lock' not in names:
            _fail('catalog_initialized_state_missing', 'STATE_STORAGE_UNAVAILABLE')
        marker_raw, marker_identity = _safe_file(self.marker, maximum=4096, private=True)
        marker = parse_json(marker_raw)
        _exact(marker, {'schema_version', 'store_id', 'module_id', 'catalog_id'})
        if (type(marker['schema_version']) is not int or marker['schema_version'] != 1
                or type(marker['store_id']) is not str or not re.fullmatch('[a-f0-9]{32}', marker['store_id'])
                or marker['module_id'] != self.policy.module_id or marker['catalog_id'] != self.policy.catalog_id):
            _fail('catalog_state_marker', 'STATE_STORAGE_UNAVAILABLE')
        # A rollback-only header and absent sidecars are prerequisites for RO SQLite.
        raw, db_identity = _safe_file(self.path, maximum=_MAX_DB_BYTES, private=True)
        if raw[:16] != b'SQLite format 3\x00' or raw[18:20] != b'\x01\x01':
            _fail('catalog_state_database_mode', 'STATE_STORAGE_UNAVAILABLE')
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=0)
            connection.execute('PRAGMA query_only=ON')
            schema = connection.execute("SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            columns = connection.execute('PRAGMA table_info(state)').fetchall()
            if (schema != [('table', 'state', 'state')]
                    or [(row[1], row[2], row[3], row[5]) for row in columns] != [
                        ('id', 'INTEGER', 0, 1), ('body', 'TEXT', 1, 0), ('artifact', 'BLOB', 1, 0)]):
                _fail('catalog_state_schema', 'STATE_STORAGE_UNAVAILABLE')
            rows = connection.execute('SELECT id,body,artifact FROM state').fetchall()
        finally:
            if connection is not None:
                connection.close()
        if (len(rows) != 1 or rows[0][0] != 1 or _identity(self.path.lstat()) != db_identity
                or _identity(self.marker.lstat()) != marker_identity
                or {p.name for p in self.directory.iterdir()} != names):
            _fail('catalog_state_changed', 'STATE_STORAGE_UNAVAILABLE')
        body = parse_json(rows[0][1], error_code='STATE_STORAGE_UNAVAILABLE')
        _exact(body, _STATE_FIELDS)
        artifact = rows[0][2]
        if (type(artifact) is not bytes or len(artifact) > MAX_BYTES
                or type(body['schema_version']) is not int or body['schema_version'] != 1
                or body['store_id'] != marker['store_id'] or not _integer(body['sequence'])
                or type(body['high_water_sequence']) is not int or body['high_water_sequence'] != body['sequence']
                or not _integer(body['policy_epoch'])
                or any(type(body[k]) is not str or not _HASH.fullmatch(body[k])
                       for k in ('artifact_sha256', 'policy_digest', 'accepted_review_digest'))
                or body['artifact_sha256'] != hashlib.sha256(artifact).hexdigest()):
            _fail('catalog_state_binding', 'STATE_STORAGE_UNAVAILABLE')
        _time(body['accepted_at'])
        return {'body': body, 'raw': artifact, 'identity': digest({'db': db_identity[:6], 'marker': marker_identity[:6]})}

    def _current(self, state):
        if state is None:
            return None
        value = _artifact(state['raw'], self.policy)
        body = state['body']
        if value['metadata']['sequence'] != body['sequence'] or value['metadata']['catalog_version'] != body['catalog_version']:
            _fail('catalog_state_binding', 'STATE_STORAGE_UNAVAILABLE')
        _authorize(value, body['artifact_sha256'], self.policy, accepted_at=body['accepted_at'], policy_epoch=body['policy_epoch'])
        return value

    def _inspect(self):
        state = None
        selection = None
        reason = None
        status = 'builtin'
        try:
            state = self._read()
            now = self.policy._now()
            if not _time(self.policy.valid_from) <= now < _time(self.policy.valid_until):
                _fail('catalog_policy_expired')
            selection = self._current(state)
            status = 'active' if selection is not None else 'builtin'
        except (InstallerError, OSError, ValueError, sqlite3.Error) as error:
            reason = error.context.get('details', {}).get('reason') if isinstance(error, InstallerError) else 'catalog_state_unavailable'
            status = {'catalog_expired': 'expired', 'catalog_revoked': 'revoked',
                      'catalog_policy_expired': 'policy_invalid'}.get(reason, 'unavailable')
        return state, selection, status, reason

    def status(self):
        state, selection, status, reason = self._inspect()
        body = state['body'] if state is not None else {}
        return {'schema_version': 1, 'configured': True, 'status': status,
            'effective_source': 'updated' if selection is not None else 'builtin',
            'effective_catalog_version': selection['catalog']['catalog_version'] if selection is not None else ErrorCatalog().version,
            'stored_sequence': body.get('sequence'), 'stored_artifact_sha256': body.get('artifact_sha256'),
            'high_water_sequence': body.get('high_water_sequence', 0 if status == 'builtin' else None),
            'policy_digest': self.policy.policy_digest, 'reason': reason}

    def resolve_catalog(self):
        _, selection, _, _ = self._inspect()
        return ErrorCatalog(selection['catalog']) if selection is not None else ErrorCatalog()

    def _load(self, artifact_path):
        raw, _ = _safe_file(artifact_path)
        value = _artifact(raw, self.policy)
        artifact_hash = hashlib.sha256(raw).hexdigest()
        _authorize(value, artifact_hash, self.policy)
        return raw, value, artifact_hash

    def _preview(self, value, artifact_hash, state):
        body = state['body'] if state is not None else {}
        if state is not None:
            # Expired/revoked old help does not erase authority; only validate its
            # signed identity here so a new valid higher-sequence update can replace it.
            existing = _artifact(state['raw'], self.policy)
            if (existing['metadata']['sequence'] != body['sequence']
                    or existing['metadata']['catalog_version'] != body['catalog_version']):
                _fail('catalog_state_binding', 'STATE_STORAGE_UNAVAILABLE')
            if self.policy.policy_epoch < body['policy_epoch'] or self.policy._now() < _time(body['accepted_at']):
                _fail('catalog_policy_or_clock_rollback')
        meta = value['metadata']
        previous = body.get('sequence', 0)
        previous_hash = body.get('artifact_sha256')
        if meta['sequence'] < previous or meta['sequence'] == previous and artifact_hash != previous_hash:
            _fail('catalog_sequence_rollback')
        summary = {'schema_version': 1, 'status': 'PREVIEW', 'artifact_sha256': artifact_hash,
            'catalog_version': meta['catalog_version'], 'sequence': meta['sequence'],
            'previous_sequence': previous, 'previous_artifact_sha256': previous_hash,
            'policy_digest': self.policy.policy_digest, 'development': meta['development'],
            'requires_confirmation': True}
        summary['review_digest'] = digest({**summary, 'state_identity': state['identity'] if state is not None else None,
                                          'store': str(self.directory)})
        return summary

    def preview(self, artifact_path):
        try:
            _, value, artifact_hash = self._load(artifact_path)
            return self._preview(value, artifact_hash, self._read())
        except InstallerError:
            raise
        except (OSError, ValueError, TypeError, sqlite3.Error):
            _fail('catalog_preview_unavailable', 'STATE_STORAGE_UNAVAILABLE')

    @contextmanager
    def _read_lock(self, lock):
        # Advisory read lock on an EXISTING inode only. No creation or marking;
        # it prevents SQLite header/name observations racing our own writer.
        import fcntl
        _directory(self.directory)
        fd = os.open(checked_path(lock), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or _identity(lock.lstat()) != _identity(info)):
                _fail('catalog_lock_unsafe', 'STATE_STORAGE_UNAVAILABLE')
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                _fail('catalog_update_busy', 'OPERATION_CONFLICT')
            yield
            if _identity(lock.lstat()) != _identity(info):
                _fail('catalog_lock_changed', 'STATE_STORAGE_UNAVAILABLE')
        finally:
            os.close(fd)

    @contextmanager
    def _lock(self):
        import fcntl
        _directory(self.directory, create=True)
        lock = self.directory / 'catalog.lock'
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or _identity(lock.lstat()) != _identity(info)):
                _fail('catalog_lock_unsafe', 'STATE_STORAGE_UNAVAILABLE')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _fail('catalog_update_busy', 'OPERATION_CONFLICT')
            yield
            if _identity(lock.lstat()) != _identity(info):
                _fail('catalog_lock_changed', 'STATE_STORAGE_UNAVAILABLE')
        finally:
            os.close(fd)

    def _write(self, raw, value, artifact_hash, review, state, now):
        previous = state['body'] if state is not None else {}
        now = _authorize(value, artifact_hash, self.policy,
            accepted_at=previous.get('accepted_at'), policy_epoch=previous.get('policy_epoch'))
        store_id = state['body']['store_id'] if state is not None else uuid.uuid4().hex
        if state is None:
            marker = {'schema_version': 1, 'store_id': store_id,
                      'module_id': self.policy.module_id, 'catalog_id': self.policy.catalog_id}
            fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(canonical(marker).encode('utf-8'))
                output.flush()
                os.fsync(output.fileno())
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            # Exclusive owned 0600 inode exists before SQLite sees the path.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        body = {'schema_version': 1, 'store_id': store_id, 'sequence': value['metadata']['sequence'],
            'high_water_sequence': value['metadata']['sequence'], 'artifact_sha256': artifact_hash,
            'catalog_version': value['metadata']['catalog_version'], 'policy_epoch': self.policy.policy_epoch,
            'policy_digest': self.policy.policy_digest, 'accepted_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'accepted_review_digest': review}
        before = self.path.lstat()
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,artifact BLOB NOT NULL)')
            connection.execute('INSERT OR REPLACE INTO state VALUES (1,?,?)', (canonical(body), raw))
            _authorize(value, artifact_hash, self.policy,
                accepted_at=body['accepted_at'], policy_epoch=body['policy_epoch'])
            if _identity(self.path.lstat())[:6] != _identity(before)[:6]:
                _fail('catalog_state_changed', 'STATE_STORAGE_UNAVAILABLE')
            _commit(connection)
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.execute('ROLLBACK')
                finally:
                    connection.close()
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _check_review(self, preview, artifact_hash, state, expected_review_digest):
        same = state is not None and state['body']['artifact_sha256'] == artifact_hash
        accepted = (same and state['body']['policy_digest'] == self.policy.policy_digest
                    and state['body']['accepted_review_digest'] == expected_review_digest)
        if expected_review_digest != preview['review_digest'] and not accepted:
            _fail('catalog_review_stale', 'PLAN_STALE')
        return same

    def apply(self, artifact_path, *, expected_review_digest, confirmed):
        if confirmed is not True:
            _fail('catalog_confirmation_required', 'CONSENT_REQUIRED')
        if type(expected_review_digest) is not str or not _HASH.fullmatch(expected_review_digest):
            _fail('catalog_review_digest_required', 'INVALID_REQUEST')
        if not sys.platform.startswith('linux'):
            _fail('catalog_apply_linux_only', 'CAPABILITY_UNSUPPORTED')
        try:
            # Validate before any mkdir/lock/marker/SQLite effects.
            raw, value, artifact_hash = self._load(artifact_path)
            previous = self._read()
            preview = self._preview(value, artifact_hash, previous)
            # A rejected initial review must not create API state or a lock.
            # The same check is repeated under the writer lock after reloading
            # the artifact, policy/time and current authority.
            self._check_review(preview, artifact_hash, previous, expected_review_digest)
            with self._lock():
                raw, value, artifact_hash = self._load(artifact_path)
                state = self._read(locked=True)
                preview = self._preview(value, artifact_hash, state)
                same = self._check_review(preview, artifact_hash, state, expected_review_digest)
                if not same:
                    now = _authorize(value, artifact_hash, self.policy)
                    self._write(raw, value, artifact_hash, expected_review_digest, state, now)
                return {'schema_version': 1, 'status': 'UNCHANGED' if same else 'APPLIED',
                    'artifact_sha256': artifact_hash, 'catalog_version': value['metadata']['catalog_version'],
                    'sequence': value['metadata']['sequence'], 'policy_digest': self.policy.policy_digest,
                    'changed': not same}
        except InstallerError:
            raise
        except OSError as error:
            _fail('catalog_space_low' if error.errno == errno.ENOSPC else 'catalog_state_unavailable',
                  'DISK_SPACE_LOW' if error.errno == errno.ENOSPC else 'STATE_STORAGE_UNAVAILABLE')
        except sqlite3.Error as error:
            space_low = getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL
            _fail('catalog_space_low' if space_low else 'catalog_state_unavailable',
                  'DISK_SPACE_LOW' if space_low else 'STATE_STORAGE_UNAVAILABLE')
        except (ValueError, TypeError):
            _fail('catalog_state_unavailable', 'STATE_STORAGE_UNAVAILABLE')
