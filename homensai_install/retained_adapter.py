"""Read-only authoritative quiesce witnesses and explicit retained execution.

No constructor here creates an invocation ledger or runs a product hook. The
caller first verifies the complete signed source and selects the existing ledger.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
from contextlib import closing

from .api.model import digest, parse_json
from .uninstall_receipts import directory_handle, _safe, _signature, _fail


def quiesce_witness(adapter, *, installation_id, operation_id, data_home):
    """Read exactly the completed original call without a writable ledger API."""
    from .engine import EngineError
    if not operation_id.startswith('op_') or len(operation_id) != 35:
        _fail()
    token = operation_id[3:]
    key = 'quiesce_' + token
    request = {'operation_id': operation_id, 'idempotency_key': key, 'phase': 'execute',
               'data': {'installation_id': installation_id, 'release': adapter.options['release'],
                        'data_home': str(data_home)}}
    _, request_digest = adapter._request('quiesce', request)
    call_id = adapter._identity(key)
    path = adapter.state_dir / 'adapter-invocations.sqlite3'
    try:
        with directory_handle(adapter.state_dir) as parent:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(descriptor); _safe(before)
                if before.st_mode & 0o077:
                    _fail()
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
                    connection.execute('PRAGMA query_only=ON')
                    connection.execute('BEGIN')
                    row = connection.execute('SELECT request_digest,operation_id,hook,state,result FROM calls WHERE call_id=?',
                                             (call_id,)).fetchone()
                if (_signature(before) != _signature(os.fstat(descriptor))
                        or _signature(before) != _signature(os.stat(path.name, dir_fd=parent, follow_symlinks=False))):
                    _fail('RECOVERY_REQUIRED')
                if (row is None or row[:4] != (request_digest, operation_id, 'quiesce', 'SUCCEEDED')
                        or type(row[4]) is not str or len(row[4]) > 4096):
                    _fail('RECOVERY_REQUIRED')
                result = parse_json(row[4], error_code='RECOVERY_REQUIRED')
                if digest(result) != digest({'quiesced': True, 'active_writers': 0}):
                    _fail('RECOVERY_REQUIRED')
                return {'call_id': call_id, 'idempotency_key': key, 'request_digest': request_digest,
                        'operation_id': operation_id,
                        'ledger_identity': [before.st_dev, before.st_ino, before.st_uid], 'result': result}
            finally:
                os.close(descriptor)
    except (OSError, sqlite3.Error):
        raise EngineError('RECOVERY_REQUIRED', 'Authoritative quiesce witness is unavailable') from None


def prepare_execution(adapter):
    """Grant execute only to an owned fd after complete signed-source verification."""
    from .engine import EngineError
    # capabilities re-verifies the complete signed closure, never invokes hooks.
    adapter.capabilities()
    relative = adapter._descriptor['command'][0]
    expected = adapter._inventory[relative]
    executable = Path(adapter.options['payload_dir']) / relative
    try:
        with directory_handle(executable.parent) as parent:
            descriptor = os.open(executable.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(descriptor); _safe(before)
                with os.fdopen(os.dup(descriptor), 'rb') as stream:
                    header = stream.read(4); stream.seek(0)
                    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                if (header != b'\x7fELF' or actual != expected
                        or _signature(before) != _signature(os.fstat(descriptor))
                        or _signature(before) != _signature(os.stat(executable.name, dir_fd=parent, follow_symlinks=False))):
                    _fail()
                os.fchmod(descriptor, 0o700)
                after = os.fstat(descriptor)
                if ((after.st_dev, after.st_ino, after.st_uid) != (before.st_dev, before.st_ino, before.st_uid)
                        or _signature(after) != _signature(os.stat(executable.name, dir_fd=parent, follow_symlinks=False))):
                    _fail()
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        adapter.capabilities()
    except OSError:
        raise EngineError('RECOVERY_REQUIRED', 'Verified retained runtime is unavailable') from None


def adapter_from_retained(engine, ctx, release, directory):
    """Static signed construction only; the caller supplies verified authority."""
    meta = engine._metadata(ctx, release)
    options = {**ctx['options'], 'release': release, 'payload_dir': str(directory / 'payload'),
               'runtime_executable': meta['runtime_executable'],
               'inventory_file': str(directory / 'release.inventory.json'),
               'signature_file': str(directory / 'release.signature.json')}
    return engine._product_adapter({**ctx, 'manifest': meta['manifest'], 'target': meta['target'],
                                    'release': release, 'options': options})


def source_reference(receipt):
    return {'operation_id': receipt['operation_id'], 'receipt_id': receipt['receipt_id'],
            'source_binding_sha256': digest(receipt['source_adapter_binding'])}


class RetainedSource:
    """One exact original uninstall receipt; never searches for a latest backup."""
    def __init__(self, engine, ctx, state, operation_id, *, directory=None, remaining=True):
        import re
        from .uninstall_receipts import UninstallProtocol
        if (type(operation_id) is not str or re.fullmatch(r'op_[0-9a-f]{32}', operation_id) is None
                or type(state) is not dict or state.get('action') != 'uninstall'
                or type(state.get('uninstall_removal')) is not dict):
            _fail('RECOVERY_REQUIRED')
        self.engine, self.ctx, self.state, self.operation_id = engine, ctx, state, operation_id
        self.release = state['uninstall_removal']['active_release']
        self.directory = directory if directory is not None else ctx['home'].roots['runtime'] / 'recovery' / self.release
        original = {**ctx, 'operation_token': operation_id[3:], 'operation_state': state,
                    'maintenance_binding': state.get('maintenance_binding')}
        protocol = UninstallProtocol(engine, original, state['uninstall_removal'], state,
                                     retained_directory=self.directory)
        if remaining:
            protocol.preflight()
        self.receipt = protocol.verify_receipt()
        if 'source_adapter_binding' not in self.receipt:
            _fail('RECOVERY_REQUIRED')
        self.adapter, self.binding = adapter_from_retained(engine, original, self.release, self.directory)
        if digest(self.binding) != digest(self.receipt['source_adapter_binding']):
            _fail()
        self.reference = source_reference(self.receipt)
        reviewed = ctx.get('reviewed_retained_source')
        if reviewed is not None and digest(reviewed) != digest(self.reference):
            _fail('PLAN_STALE')

    @classmethod
    def selected(cls, engine, ctx):
        from .engine import _read
        pending = ctx.get('uninstall_source_state')
        if ctx.get('action') == 'uninstall' and pending is not None:
            return cls(engine, ctx, pending, 'op_' + ctx['operation_token'])
        owner = engine._owner(ctx, required=True)
        marker = owner.get('retained_uninstall')
        if (ctx.get('action') not in {'restore', 'rollback'} or owner.get('installed') is not False
                or type(marker) is not dict or set(marker) != {'operation_id', 'receipt_id'}):
            _fail('RECOVERY_REQUIRED')
        import re
        operation_id = marker['operation_id']
        if type(operation_id) is not str or re.fullmatch(r'op_[0-9a-f]{32}', operation_id) is None:
            _fail('RECOVERY_REQUIRED')
        state = _read(ctx['control'] / 'operations' / (operation_id[3:] + '.json'))
        if state.get('status') != 'SUCCEEDED' or state.get('uninstall_removal_complete') is not True:
            _fail('RECOVERY_REQUIRED')
        selected = cls(engine, ctx, state, operation_id)
        if selected.receipt['receipt_id'] != marker['receipt_id']:
            _fail()
        return selected

    def checkpoint_restore_source(self, current_ctx):
        """Publish one private source closure before any replacing-root effect."""
        from .engine import _write
        from shared.adapters_fs import FilesystemPorts
        from .uninstall_receipts import snapshot
        state = current_ctx['operation_state']
        if 'retained_restore_source' in state:
            return self.from_closure(self.engine, current_ctx)
        destination = current_ctx['work'] / (current_ctx['operation_token'] + '-retained-adapter-source')
        if destination.exists() or destination.is_symlink():
            _fail('RECOVERY_REQUIRED')
        with directory_handle(current_ctx['work']) as parent:
            os.mkdir(destination.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        FilesystemPorts._reconcile(self.directory, destination, set())
        copied = RetainedSource(self.engine, current_ctx, self.state, self.operation_id,
                                directory=destination, remaining=False)
        tree = snapshot(destination)
        state['retained_restore_source'] = {'reference': copied.reference,
            'source_binding': copied.binding,
            'files_sha256': digest({name: item['sha256'] for name, item in tree['files'].items()}),
            'directory_identity': tree['directories']['.']}
        _write(current_ctx['control'] / 'operations' / (current_ctx['operation_token'] + '.json'), state)
        return self.from_closure(self.engine, current_ctx)

    @classmethod
    def from_closure(cls, engine, ctx):
        from .engine import _read
        from .uninstall_receipts import snapshot
        state = ctx.get('operation_state') or ctx.get('resume_operation_state')
        record = state.get('retained_restore_source') if type(state) is dict else None
        if (type(record) is not dict or set(record) != {'reference', 'source_binding', 'files_sha256', 'directory_identity'}
                or type(record['reference']) is not dict):
            _fail('RECOVERY_REQUIRED')
        reference = record['reference']
        import re
        operation_id = reference.get('operation_id')
        if type(operation_id) is not str or re.fullmatch(r'op_[0-9a-f]{32}', operation_id) is None:
            _fail('RECOVERY_REQUIRED')
        original = _read(ctx['control'] / 'operations' / (operation_id[3:] + '.json'))
        if original.get('status') != 'SUCCEEDED' or original.get('uninstall_removal_complete') is not True:
            _fail('RECOVERY_REQUIRED')
        destination = ctx['work'] / (ctx['operation_token'] + '-retained-adapter-source')
        tree = snapshot(destination)
        if (tree['directories']['.'] != record['directory_identity']
                or digest({name: item['sha256'] for name, item in tree['files'].items()}) != record['files_sha256']):
            _fail()
        selected = cls(engine, ctx, original, operation_id, directory=destination, remaining=False)
        if digest(selected.reference) != digest(reference) or digest(selected.binding) != digest(record['source_binding']):
            _fail()
        return selected
