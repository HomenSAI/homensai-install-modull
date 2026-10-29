"""Original-operation stage intent and read-only publication reconciliation.

Only a complete signed target with the original Ports completion receipt can
be adopted. This never copies a partial staging directory or repairs metadata.
The existing private owned namespace and lifecycle lock remain prerequisites.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat

from .api.model import digest, parse_json
from .errors import InstallerError
from .migration_receipts import _reader
from .uninstall_receipts import snapshot


def fail(code='RECOVERY_REQUIRED'):
    from .engine import EngineError
    raise EngineError(code, 'Original target stage authority is not verified')


def read_bytes(path):
    from .engine import EngineError
    try:
        with _reader(path) as (stream, info):
            if info.st_size > 16 * 1024 * 1024: fail()
            return stream.read(16 * 1024 * 1024 + 1)
    except (OSError, EngineError, InstallerError, ValueError):
        fail()


def maybe_bytes(path):
    try: path.lstat()
    except FileNotFoundError: return None
    return read_bytes(path)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identity_digest(tree, executables):
    """Publication only chmods the declared executables of an existing tree."""
    value = deepcopy(tree)
    for name in executables:
        if name not in value['files']: fail()
        item = value['files'][name]['identity']
        item[2] = stat.S_IFREG | 0o700
        item[8] = None  # The owned chmod legitimately changes only ctime.
    return digest(value)


def target_spec(engine, ctx, inventory):
    """Execution preflight, before candidate probes, stop or root replacement."""
    from .engine import EngineError
    try:
        target = engine._release(ctx, ctx['release'])
        source = snapshot(Path(ctx['options']['payload_dir']))
        files = {n: v['sha256'] for n, v in source['files'].items()}
        if files != inventory: fail('INTEGRITY_FAILED')
        executable = {ctx['options']['runtime_executable']}
        executable.update(c['cmd'][0] for c in ctx['manifest']['components'] if c['cmd'][0] != 'python')
        if not executable <= set(files): fail('INTEGRITY_FAILED')
        metadata = engine._release_metadata_value(ctx, inventory)
        pins = {'inventory': sha(read_bytes(Path(ctx['options']['inventory_file']))),
                'signature': sha(read_bytes(Path(ctx['options']['signature_file'])))}
        folder = ctx['control'] / 'releases'
        paths = {'release': engine._metadata_path(ctx, ctx['release']),
                 'inventory': folder / (ctx['release'] + '.inventory.json'),
                 'signature': folder / (ctx['release'] + '.signature.json')}
        old = {n: maybe_bytes(p) for n, p in paths.items()}
        if old['release'] is not None and parse_json(old['release'], error_code='INTEGRITY_FAILED') != metadata:
            fail('INTEGRITY_FAILED')
        if any(old[n] is not None and sha(old[n]) != pins[n] for n in pins): fail('INTEGRITY_FAILED')
        before = None
        if target.exists() or target.is_symlink():
            tree = snapshot(target)
            if (set(tree['directories']) != set(source['directories'])
                    or {n: v['sha256'] for n, v in tree['files'].items()} != files): fail('INTEGRITY_FAILED')
            before = {'root_identity': tree['directories']['.'], 'identity_sha256': digest(tree),
                      'published_identity_sha256': identity_digest(tree, executable)}
        ports_value = {'release': ctx['release'], 'inventory': sha(
            json.dumps([sorted(source['directories']), files], sort_keys=True).encode())}
        return {'target_path': str(target), 'target_preimage': before,
                'metadata_preimages': {n: sha(raw) if raw is not None else None for n, raw in old.items()},
                'expected_metadata_sha256': digest(metadata), 'expected_inventory_file_sha256': pins['inventory'],
                'expected_signature_file_sha256': pins['signature'], 'ports_value': ports_value,
                'executables': sorted(executable)}
    except (OSError, InstallerError, EngineError, ValueError):
        fail('INTEGRITY_FAILED')


class TargetStage:
    def __init__(self, protocol):
        self.protocol, self.engine, self.ctx, self.state = protocol, protocol.engine, protocol.ctx, protocol.state
        self.root = protocol.record()
        self.body = self.root['body']
        self.release = self.body['target_binding']['release']
        self.target = self.engine._release(self.ctx, self.release)
        self.path = self.ctx['control'] / 'recovery-stage-receipts' / (protocol.token + '.json')
        self.key = protocol.token + '/stage'
        self.ports_path = self.ctx['work'] / (sha(self.key.encode()) + '.json')
        folder = self.ctx['control'] / 'releases'
        self.metadata_paths = {'release': self.engine._metadata_path(self.ctx, self.release),
            'inventory': folder / (self.release + '.inventory.json'), 'signature': folder / (self.release + '.signature.json')}

    def record(self):
        from .recovery_replacement import private_read, _hash
        record = private_read(self.path)
        if (type(record) is not dict or set(record) != {'receipt_id', 'body'}
                or not _hash(record['receipt_id']) or digest(record['body']) != record['receipt_id']
                or record['receipt_id'] != self.state.get('recovery_target_stage_receipt_id')): fail()
        b = record['body']
        fields = {'schema_version', 'kind', 'operation_id', 'action', 'installation_id', 'root_receipt_id',
            'target_binding_sha256', 'candidate_tree_sha256', 'target_path', 'target_preimage', 'metadata_preimages',
            'expected_metadata_sha256', 'expected_inventory_file_sha256', 'expected_signature_file_sha256',
            'ports_receipt', 'executables'}
        if (type(b) is not dict or set(b) != fields or type(b['schema_version']) is not int or b['schema_version'] != 1
                or b['kind'] != 'recovery_target_stage_intent' or b['operation_id'] != self.protocol.operation_id
                or b['action'] != self.ctx['action'] or b['installation_id'] != self.body['installation_id']
                or b['root_receipt_id'] != self.root['receipt_id']
                or b['target_binding_sha256'] != digest(self.body['target_binding'])
                or b['candidate_tree_sha256'] != self.body['candidate_tree_sha256']
                or b['target_path'] != str(self.target)
                or any(not _hash(b[n]) for n in ('expected_metadata_sha256', 'expected_inventory_file_sha256', 'expected_signature_file_sha256'))
                or type(b['executables']) is not list or not b['executables']
                or any(type(n) is not str for n in b['executables'])
                or b['executables'] != sorted(set(b['executables']))
                or self.body['target_binding']['runtime_executable'] not in b['executables']
                or type(b['metadata_preimages']) is not dict or set(b['metadata_preimages']) != set(self.metadata_paths)
                or any(v is not None and not _hash(v) for v in b['metadata_preimages'].values())): fail()
        ports = b['ports_receipt']
        if (type(ports) is not dict or set(ports) != {'key', 'path', 'value'} or ports['key'] != self.key
                or ports['path'] != str(self.ports_path) or type(ports['value']) is not dict
                or set(ports['value']) != {'release', 'inventory'} or ports['value']['release'] != self.release
                or not _hash(ports['value']['inventory'])): fail()
        old = b['target_preimage']
        if old is not None and (type(old) is not dict
                or set(old) != {'root_identity', 'identity_sha256', 'published_identity_sha256'}
                or type(old['root_identity']) is not list or len(old['root_identity']) != 5
                or any(type(n) is not int for n in old['root_identity'])
                or not _hash(old['identity_sha256']) or not _hash(old['published_identity_sha256'])): fail()
        return record

    def prepare(self, target_ctx, inventory):
        from .engine import _write
        spec = target_spec(self.engine, target_ctx, inventory)
        if self.state.get('recovery_target_stage_receipt_id') is not None:
            b = self.record()['body']
            for key in ('expected_metadata_sha256', 'expected_inventory_file_sha256', 'expected_signature_file_sha256', 'executables'):
                if b[key] != spec[key]: fail()
            if b['ports_receipt']['value'] != spec['ports_value']: fail()
            return
        if self.state.get('recovery_target_stage_started') or self.path.exists() or self.path.is_symlink(): fail()
        if self.ports_path.exists() or self.ports_path.is_symlink(): fail()
        body = {'schema_version': 1, 'kind': 'recovery_target_stage_intent',
            'operation_id': self.protocol.operation_id, 'action': self.ctx['action'],
            'installation_id': self.body['installation_id'], 'root_receipt_id': self.root['receipt_id'],
            'target_binding_sha256': digest(self.body['target_binding']),
            'candidate_tree_sha256': self.body['candidate_tree_sha256'],
            **{k: v for k, v in spec.items() if k != 'ports_value'},
            'ports_receipt': {'key': self.key, 'path': str(self.ports_path), 'value': spec['ports_value']}}
        record = {'receipt_id': digest(body), 'body': body}
        _write(self.path, record)
        self.state['recovery_target_stage_receipt_id'] = record['receipt_id']; self.protocol.save()
        self.before_publish()

    def before_publish(self):
        from .engine import EngineError
        try: self._before_publish()
        except (OSError, EngineError, InstallerError, ValueError): fail()

    def _before_publish(self):
        b = self.record()['body']
        if self.ports_path.exists() or self.ports_path.is_symlink(): fail()
        old = b['target_preimage']
        if old is None:
            if self.target.exists() or self.target.is_symlink(): fail()
        elif digest(snapshot(self.target)) != old['identity_sha256']: fail()
        actual = {n: maybe_bytes(p) for n, p in self.metadata_paths.items()}
        if {n: sha(raw) if raw is not None else None for n, raw in actual.items()} != b['metadata_preimages']: fail()

    def published(self):
        from .engine import EngineError
        try:
            b = self.record()['body']
            ports = parse_json(read_bytes(self.ports_path), error_code='RECOVERY_REQUIRED')
            if ports != b['ports_receipt']['value']: fail()
            metadata = parse_json(read_bytes(self.metadata_paths['release']), error_code='RECOVERY_REQUIRED')
            if (digest(metadata) != b['expected_metadata_sha256']
                    or sha(read_bytes(self.metadata_paths['inventory'])) != b['expected_inventory_file_sha256']
                    or sha(read_bytes(self.metadata_paths['signature'])) != b['expected_signature_file_sha256']): fail()
            target_ctx = {**self.ctx, 'release': self.release, 'recovery_adapter_role': 'target'}
            _, binding = self.engine._release_product_adapter(target_ctx, self.release)
            if binding != self.body['target_binding']: fail()
            executable = {metadata['runtime_executable']}
            executable.update(c['cmd'][0] for c in metadata['manifest']['components'] if c['cmd'][0] != 'python')
            if sorted(executable) != b['executables']: fail()
            tree = snapshot(self.target)
            if any(n not in tree['files'] or tree['files'][n]['identity'][2] & 0o7777 != 0o700 for n in b['executables']): fail()
            old = b['target_preimage']
            if old is not None and (tree['directories']['.'] != old['root_identity']
                    or identity_digest(tree, b['executables']) != old['published_identity_sha256']): fail()
        except (OSError, EngineError, InstallerError, ValueError):
            fail()
