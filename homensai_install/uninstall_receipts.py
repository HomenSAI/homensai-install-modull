"""Reviewed legacy uninstall authority and original-operation deletion intents.

Linux user scope only. The private installation namespace and lifecycle lock are
required; descriptor/name rechecks narrow races but are not an atomic POSIX
compare-and-delete against a hostile process with the same UID.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat

from .migration_snapshot import _root_handle
from .errors import InstallerError


def _fail(code='INTEGRITY_FAILED'):
    from .engine import EngineError
    raise EngineError(code, 'Uninstall authority or retained recovery is not verified')


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid]


def _signature(info):
    return [*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _safe(info, directory=False):
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.geteuid() or info.st_mode & 0o022
            or not directory and info.st_nlink != 1):
        _fail()


@contextmanager
def directory_handle(path):
    try:
        with _root_handle(Path(path)) as descriptor:
            _safe(os.fstat(descriptor), True)
            yield descriptor
    except InstallerError:
        _fail()
    except OSError:
        _fail('RECOVERY_REQUIRED')


def snapshot(path):
    """Full no-follow, owned tree; directories keep stable identities, not times."""
    directories, files = {}, {}
    def visit(descriptor, relative):
        before = os.fstat(descriptor); _safe(before, True)
        directories[relative or '.'] = _identity(before)
        names = sorted(os.listdir(descriptor))
        if len({name.casefold() for name in names}) != len(names):
            _fail()
        for name in names:
            from shared.archive import _safe_name
            key = relative + '/' + name if relative else name
            try:
                _safe_name(key)
            except ValueError:
                _fail()
            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            directory = stat.S_ISDIR(named.st_mode); _safe(named, directory)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            child = os.open(name, flags | (os.O_DIRECTORY if directory else 0), dir_fd=descriptor)
            try:
                captured = os.fstat(child)
                if _signature(named) != _signature(captured): _fail()
                if directory:
                    visit(child, key)
                else:
                    with os.fdopen(os.dup(child), 'rb') as stream:
                        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    if _signature(os.fstat(child)) != _signature(captured): _fail()
                    files[key] = {'sha256': digest, 'identity': _signature(captured)}
                if _signature(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) != _signature(captured): _fail()
            finally: os.close(child)
        if names != sorted(os.listdir(descriptor)) or _signature(before) != _signature(os.fstat(descriptor)):
            _fail()
    with directory_handle(path) as descriptor: visit(descriptor, '')
    return {'directories': directories, 'files': files}


def unlink_at(parent_fd, name):
    """Single unlink seam; intent is persisted by the caller before this effect."""
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _read_receipt(path):
    from .api.model import parse_json
    with directory_handle(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(descriptor); _safe(before)
            if before.st_mode & 0o077 or before.st_size > 64 * 1024: _fail()
            with os.fdopen(os.dup(descriptor), 'rb') as stream:
                record = parse_json(stream.read(64 * 1024 + 1), error_code='INTEGRITY_FAILED')
            if (_signature(before) != _signature(os.fstat(descriptor))
                    or _signature(before) != _signature(os.stat(path.name, dir_fd=parent, follow_symlinks=False))): _fail()
            return record
        finally: os.close(descriptor)


class UninstallProtocol:
    def __init__(self, engine, ctx, reviewed, state=None):
        from .engine import _digest
        self.engine, self.ctx, self.reviewed, self.state = engine, ctx, reviewed, state
        self.review_digest = _digest(reviewed)

    @staticmethod
    def review(engine, ctx):
        from .engine import _file_digest
        folder = ctx['control'] / 'releases'
        names = sorted(p.name for p in folder.iterdir())
        releases = []
        for name in names:
            if name.endswith(('.inventory.json', '.signature.json')): continue
            if not name.endswith('.json') or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name[:-5]) is None:
                _fail()
            release = name[:-5]
            meta = engine._installed(ctx, release)
            tree = snapshot(engine._release(ctx, release))
            if {key: value['sha256'] for key, value in tree['files'].items()} != meta['inventory']:
                _fail()
            releases.append({'release': release, **tree, 'metadata': {
                suffix: _file_digest(folder / (release + suffix))
                for suffix in ('.json', '.inventory.json', '.signature.json')}})
        expected = sorted(item['release'] + suffix for item in releases
                          for suffix in ('.json', '.inventory.json', '.signature.json'))
        if names != expected or not releases: _fail()
        return {'schema_version': 1, 'installation_id': engine._owner(ctx, required=True)['installation_id'],
                'active_release': engine._pointer(ctx), 'metadata_names': names,
                'release_entries': sorted(p.name for p in ctx['home'].releases.iterdir()), 'releases': releases}

    def save(self):
        from .engine import _write
        _write(self.ctx['control'] / 'operations' / (self.ctx['operation_token'] + '.json'), self.state)

    def _retained(self):
        from .engine import _read, _digest
        active = self.reviewed['active_release']; destination = self.ctx['home'].roots['runtime'] / 'recovery' / active
        meta = self.engine._metadata(self.ctx, active)
        owner = _read(destination / 'installation.json')
        live_owner = self.engine._owner(self.ctx, required=True)
        stable = ('schema_version', 'product_id', 'installation_id', 'owner', 'mode')
        if (type(owner) is not dict
                or _digest({key: owner.get(key) for key in stable}) != _digest({key: live_owner.get(key) for key in stable})
                or owner.get('installation_id') != self.reviewed['installation_id']
                or _read(destination / 'release.json') != meta): _fail()
        files = self.engine._verify(meta['manifest'], meta['target'], {
            'payload_dir': str(destination / 'payload'), 'release': active,
            'runtime_executable': meta['runtime_executable'],
            'inventory_file': str(destination / 'release.inventory.json'),
            'signature_file': str(destination / 'release.signature.json')})
        if files != meta['inventory']: _fail()
        if {name: value['sha256'] for name, value in snapshot(destination / 'payload')['files'].items()} != files:
            _fail()
        return _digest(files)

    def _body(self):
        return {'schema_version': 1, 'kind': 'retained_uninstall_recovery',
                'installation_id': self.reviewed['installation_id'],
                'operation_id': 'op_' + self.ctx['operation_token'],
                'review_sha256': self.review_digest, 'active_release': self.reviewed['active_release'],
                'retained_payload_sha256': self._retained(),
                'verification_scope': 'signed_active_payload_and_original_removal_set'}

    def receipt_path(self):
        return self.ctx['control'] / 'uninstall-receipts' / (self.ctx['operation_token'] + '.json')

    def preserve(self):
        from .engine import _write, _digest
        self.engine._recovery_copy(self.ctx, self.reviewed['active_release'])
        body = self._body(); record = {'receipt_id': _digest(body), **body}; path = self.receipt_path()
        if path.exists():
            if _digest(_read_receipt(path)) != _digest(record): _fail()
        else: _write(path, record)
        self.state['uninstall_review_sha256'] = self.review_digest
        self.state['uninstall_removal'] = self.reviewed
        return record

    def verify_receipt(self):
        from .engine import _digest
        if (self.state is None or self.state.get('uninstall_removal') != self.reviewed
                or self.state.get('uninstall_review_sha256') != self.review_digest): _fail('RECOVERY_REQUIRED')
        body = self._body(); expected = {'receipt_id': _digest(body), **body}
        if (_digest(_read_receipt(self.receipt_path())) != _digest(expected)
                or _digest(self.state.get('step_results', {}).get('preserve_recovery')) != _digest(expected)): _fail()
        return expected

    def preflight(self):
        """Validate the entire remaining set before the first unlink of an attempt."""
        from .engine import _file_digest
        self.engine._check_maintenance_binding(self.ctx)
        folder = self.ctx['control'] / 'releases'
        if sorted(p.name for p in folder.iterdir()) != self.reviewed['metadata_names']: _fail('PLAN_STALE')
        intents = self.state.get('uninstall_intents', {})
        allowed = {item['release'] + '/' + name: record for item in self.reviewed['releases']
                   for name, record in item['files'].items()}
        if type(intents) is not dict or set(intents) - set(allowed): _fail()
        for key, intent in intents.items():
            if intent != {'review_sha256': self.review_digest, **allowed[key]}: _fail()
        gone_dirs = self.state.get('uninstall_directory_intents', {})
        valid_dirs = {item['release'] + '/' + name: identity for item in self.reviewed['releases']
                      for name, identity in item['directories'].items()}
        if type(gone_dirs) is not dict or any(valid_dirs.get(key) != value for key, value in gone_dirs.items()): _fail()
        entries = set(self.reviewed['release_entries'])
        current_entries = {p.name for p in self.ctx['home'].releases.iterdir()}
        absent = {'app-' + item['release'] for item in self.reviewed['releases'] if item['release'] + '/.' in gone_dirs}
        if current_entries - entries or entries - current_entries - absent: _fail('PLAN_STALE')
        for item in self.reviewed['releases']:
            release = item['release']; path = self.engine._release(self.ctx, release)
            for suffix, digest in item['metadata'].items():
                if _file_digest(folder / (release + suffix)) != digest: _fail('PLAN_STALE')
            if not path.exists():
                if release + '/.' not in gone_dirs: _fail()
                if any(release + '/' + name not in intents for name in item['files']): _fail()
                if any(release + '/' + name not in gone_dirs for name in item['directories']): _fail()
                continue
            current = snapshot(path)
            if set(current['files']) - set(item['files']) or set(current['directories']) - set(item['directories']): _fail()
            for name, identity in item['directories'].items():
                if name not in current['directories']:
                    if release + '/' + name not in gone_dirs: _fail()
                elif current['directories'][name] != identity: _fail()
            for name, original in item['files'].items():
                if name not in current['files']:
                    if release + '/' + name not in intents: _fail()
                elif current['files'][name] != original: _fail()
        self.verify_receipt()

    def remove(self):
        from .engine import _write
        self.preflight()
        # Every attempt reaches this point with the complete remaining set checked.
        for item in self.reviewed['releases']:
            release = item['release']; root = self.engine._release(self.ctx, release)
            for name, original in sorted(item['files'].items()):
                key = release + '/' + name; target = root / name
                try:
                    target.lstat()
                except FileNotFoundError:
                    if self.state.get('uninstall_intents', {}).get(key) != {'review_sha256': self.review_digest, **original}:
                        _fail()
                    continue
                with directory_handle(target.parent) as parent:
                    if _identity(os.fstat(parent)) != item['directories'][str(Path(name).parent).replace(os.sep, '/')]: _fail()
                    descriptor = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    try:
                        info = os.fstat(descriptor); _safe(info)
                        with os.fdopen(os.dup(descriptor), 'rb') as stream: actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                        if original != {'sha256': actual, 'identity': _signature(info)} or _signature(os.fstat(descriptor)) != _signature(info): _fail()
                        self.state.setdefault('uninstall_intents', {})[key] = {'review_sha256': self.review_digest, **original}
                        self.save()
                        if _signature(os.stat(target.name, dir_fd=parent, follow_symlinks=False)) != _signature(info): _fail()
                        unlink_at(parent, target.name)
                    finally: os.close(descriptor)
            for name in sorted(item['directories'], key=lambda value: (value.count('/'), len(value)), reverse=True):
                target = root if name == '.' else root / name
                try:
                    target.lstat()
                except FileNotFoundError:
                    if self.state.get('uninstall_directory_intents', {}).get(release + '/' + name) != item['directories'][name]:
                        _fail()
                    continue
                with directory_handle(target.parent) as parent:
                    info = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
                    if _identity(info) != item['directories'][name]: _fail()
                    self.state.setdefault('uninstall_directory_intents', {})[release + '/' + name] = _identity(info)
                    self.save()
                    os.rmdir(target.name, dir_fd=parent); os.fsync(parent)
        self.state['uninstall_removal_complete'] = True; self.save()
        owner = self.engine._owner(self.ctx, required=True)
        _write(self.ctx['control'] / 'installation.json', {**owner, 'installed': False})
        return {'data_preserved': True, 'recovery_available': True, 'retained_recovery': self.verify_receipt()}
