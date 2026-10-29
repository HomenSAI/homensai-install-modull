"""Original-operation recovery at complete-root boundaries, never mixed files.

This protocol uses the existing in-place reconciler. A root with an interrupted
mixed tree is deliberately not repaired automatically. Private owned namespace,
the lifecycle lock, and repeated FD scans are prerequisites, not an atomic
compare-and-swap guarantee against a hostile process with the same UID.
"""
import os
from pathlib import Path
import re

from shared.adapters_fs import FilesystemPorts
from shared.datahome import PERSISTENT_ROOTS

from .api.model import digest, parse_json
from .errors import InstallerError
from .retained_adapter import quiesce_witness
from .migration_receipts import _reader
from .uninstall_receipts import snapshot, _read_receipt, directory_handle


def fail(code='RECOVERY_REQUIRED'):
    from .engine import EngineError
    raise EngineError(code, 'Original recovery root authority is not verified')


def private_read(path):
    from .engine import EngineError
    try:
        return _read_receipt(path)
    except (OSError, InstallerError, EngineError, ValueError):
        fail()


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def typed_progress(value, complete=False):
    return (type(value) is dict and set(value) == {'started', 'complete'}
        and type(value['started']) is bool and value['started'] is True
        and type(value['complete']) is bool and (not complete or value['complete'] is True))


def completed_progress(state):
    values = state.get('recovery_root_progress')
    return (state.get('recovery_roots_complete') is True and type(values) is dict
        and set(values) == set(PERSISTENT_ROOTS) and all(typed_progress(v, True) for v in values.values()))


def owned_data_bytes(path):
    """Existing services data may be 0644 below a private owned DataHome.

    This is not the private receipt reader: journal shape is bound separately
    to the original native control journal. Owner/single-link/no-follow and
    read identity checks remain mandatory; no chmod is performed.
    """
    from .engine import EngineError
    try:
        with _reader(path) as (stream, info):
            if info.st_size > 64 * 1024: fail()
            return stream.read(64 * 1024 + 1)
    except (OSError, InstallerError, EngineError, ValueError):
        fail()


def owned_data_json(path):
    return parse_json(owned_data_bytes(path), error_code='RECOVERY_REQUIRED')


def released_service_lock(path):
    # shared.update_plan.operation_lock reserves byte zero and writes its
    # exact JSON at offset one. Validate, but preserve the original bytes.
    raw = owned_data_bytes(path)
    if raw[:1] != b'\x00': fail()
    value = parse_json(raw[1:], error_code='RECOVERY_REQUIRED')
    if (type(value) is not dict or set(value) != {'version', 'status', 'pid'}
            or type(value['version']) is not int or value['version'] != 1
            or value['status'] != 'released' or type(value['pid']) is not int or value['pid'] <= 0):
        fail()


def projection(tree, *, normalized=False):
    """Stable semantic state; scan itself still checks inode/time stability."""
    dirs = {n: [0o700, os.geteuid()] if normalized else [v[2] & 0o7777, v[3]]
            for n, v in tree['directories'].items()}
    files = {n: [v['sha256'], v['identity'][6], 0o600, os.geteuid(), 1] if normalized
             else [v['sha256'], v['identity'][6], v['identity'][2] & 0o7777,
                   v['identity'][3], v['identity'][5]] for n, v in tree['files'].items()}
    return {'directories': dirs, 'files': files}


def content_digest(root):
    tree = snapshot(root)
    return digest([sorted(tree['directories']), {n: v['sha256'] for n, v in tree['files'].items()}])


def pending(engine, ctx):
    owner = engine._owner(ctx)
    marker = owner.get('pending_recovery') if owner else None
    if marker is None:
        return None
    strict = private_read(ctx['control'] / 'installation.json')
    if (strict != owner or type(marker) is not dict or set(marker) != {'operation_id', 'receipt_id'}
            or type(marker['operation_id']) is not str or re.fullmatch('op_[0-9a-f]{32}', marker['operation_id']) is None
            or not _hash(marker['receipt_id'])):
        fail()
    return marker


def terminal_proof(engine, ctx, token, state):
    """Historical proof for retired operations; no live archive/DB observations."""
    root = private_read(ctx['control'] / 'recovery-root-receipts' / (token + '.json'))
    if type(root) is not dict or set(root) != {'receipt_id', 'body'}: fail()
    b = root['body']
    if (type(b) is not dict or root['receipt_id'] != digest(b)
            or root['receipt_id'] != state.get('recovery_root_receipt_id')
            or b.get('operation_id') != 'op_' + token or b.get('action') not in {'restore', 'rollback'}
            or state.get('action') != b['action']
            or b.get('root_locators') != {n: str(p) for n, p in ctx['home'].roots.items()}
            or b.get('installation_id') != engine._owner(ctx, required=True)['installation_id']): fail()
    terminal_id = state.get('recovery_root_terminal_id')
    if not _hash(terminal_id): fail()
    terminal = private_read(ctx['control'] / 'recovery-root-receipts' / (terminal_id + '.terminal.json'))
    if type(terminal) is not dict or set(terminal) != {'receipt_id', 'body'}: fail()
    body = terminal['body']
    fields = {'schema_version', 'kind', 'operation_id', 'root_receipt_id', 'result_sha256',
              'completed_sha256', 'target_binding_sha256'}
    required = {'verify_archive', 'stop', 'before_' + b['action'], 'restore', 'product_restore',
                'post_restore', 'activate', 'start', 'health'}
    if (type(body) is not dict or set(body) != fields or type(body['schema_version']) is not int
            or body['schema_version'] != 1 or terminal['receipt_id'] != terminal_id or digest(body) != terminal_id
            or body['kind'] != 'completed_recovery_root_operation' or body['operation_id'] != 'op_' + token
            or body['root_receipt_id'] != root['receipt_id'] or body['result_sha256'] != digest(state.get('result'))
            or body['completed_sha256'] != digest(state.get('completed'))
            or body['target_binding_sha256'] != digest(b.get('target_binding'))
            or state.get('status') != 'SUCCEEDED' or state.get('recovery_roots_complete') is not True
            or not completed_progress(state)
            or state.get('recovery_replace_started') is not True
            or state.get('recovery_target_stage_started') is not True
            or state.get('recovery_target_stage_complete') is not True
            or not required <= set(state.get('completed', []))
            or state.get('result', {}).get('status') != 'SUCCEEDED'):
        fail()
    return root


def journal_fence(engine, ctx, original_token=None, previous=None):
    """A lost owner marker never turns an unfinished native journal into fresh."""
    from .engine import _read
    folder = ctx['control'] / 'operations'
    receipts = ctx['control'] / 'recovery-root-receipts'
    stages = ctx['control'] / 'recovery-stage-receipts'
    # A surviving explicit stage intent is also an unfinished-history hint.
    # It can fence a lost journal/root receipt, never reconstruct either one.
    for path in stages.glob('*.json') if stages.exists() else ():
        if re.fullmatch('[0-9a-f]{32}[.]json', path.name) is None: fail()
        stage = private_read(path)
        if (type(stage) is not dict or set(stage) != {'receipt_id', 'body'}
                or not _hash(stage['receipt_id']) or type(stage['body']) is not dict
                or digest(stage['body']) != stage['receipt_id']): fail()
        body = stage['body']; token = path.stem
        native = private_read(folder / (token + '.json'))
        root = private_read(receipts / (token + '.json'))
        if (type(native) is not dict or type(root) is not dict or type(root.get('body')) is not dict
                or body.get('kind') != 'recovery_target_stage_intent'
                or body.get('operation_id') != 'op_' + token
                or native.get('recovery_target_stage_receipt_id') != stage['receipt_id']
                or not _hash(root.get('receipt_id')) or digest(root['body']) != root['receipt_id']
                or body.get('root_receipt_id') != root['receipt_id']
                or native.get('recovery_root_receipt_id') != root['receipt_id']
                or root['body'].get('operation_id') != body['operation_id']): fail()
    # A remaining explicit root receipt is a fence even when both mutable
    # marker and native journal were lost. It never reconstructs authority.
    for receipt in receipts.glob('*.json') if receipts.exists() else ():
        if receipt.name.endswith('.terminal.json'): continue
        if re.fullmatch('[0-9a-f]{32}[.]json', receipt.name) is None: fail()
        token = receipt.stem
        record = private_read(receipt)
        if type(record) is not dict or not _hash(record.get('receipt_id')): fail()
        native_path = folder / (token + '.json')
        state = private_read(native_path)
        if (type(state) is not dict or state.get('recovery_root_receipt_id') != record['receipt_id']
                or record['receipt_id'] != digest(record.get('body'))): fail()
        if token == original_token:
            if previous is None or state != previous: fail()
            continue
        terminal_proof(engine, ctx, token, state)
    for path in folder.glob('*.json') if folder.exists() else ():
        state = _read(path)
        if type(state) is not dict: fail()
        if state.get('recovery_root_receipt_id') is None: continue
        if private_read(path) != state: fail()
        if path.stem == original_token:
            if previous is None or state != previous: fail()
            continue
        terminal_proof(engine, ctx, path.stem, state)


def plan_fence(engine, ctx, action):
    marker = pending(engine, ctx)
    if action != 'stop': journal_fence(engine, ctx)
    if marker is None:
        return None
    if action != 'stop':
        fail()
    return {'steps': [{'id': 'stop', 'component_id': 'product', 'weight': 1}],
        'resources': [str(ctx['root']), str(ctx['work'])], 'required_consents': ['action:stop'],
        'scope': 'user-session', 'preserves_user_data': True, 'production_ready': False,
        'pending_recovery': marker, 'product_hooks': False}


def owned_stop(engine, ctx):
    marker = pending(engine, ctx)
    if marker is None: fail()
    token = marker['operation_id'][3:]
    record = private_read(ctx['control'] / 'recovery-root-receipts' / (token + '.json'))
    body = record.get('body', {})
    if (record.get('receipt_id') != marker['receipt_id'] or digest(body) != marker['receipt_id']
            or body.get('operation_id') != marker['operation_id']
            or body.get('installation_id') != engine._owner(ctx, required=True)['installation_id']
            or body.get('root_locators') != {n: str(p) for n, p in ctx['home'].roots.items()}): fail()
    records = private_read(ctx['control'] / 'processes.json') if (ctx['control'] / 'processes.json').exists() else {}
    releases = {body['source_binding']['release'], body['target_binding']['release']}
    if type(records) is not dict or any(r.get('release') not in releases for r in records.values()): fail()
    engine._stop_owned_processes(ctx)
    return {'status': 'SUCCEEDED', 'pending_recovery': marker, 'product_hooks': False, 'production_ready': False}


class RootReplacement:
    def __init__(self, engine, ctx, state=None):
        self.engine, self.ctx = engine, ctx
        self.state = state if state is not None else ctx['operation_state']
        self.token = ctx['operation_token']
        self.operation_id = 'op_' + self.token
        self.directory = ctx['control'] / 'recovery-root-receipts'
        self.path = self.directory / (self.token + '.json')
        self.candidate = ctx['work'] / (self.token + '-restore-candidate')

    def save(self):
        from .engine import _write
        _write(self.ctx['control'] / 'operations' / (self.token + '.json'), self.state)

    def record(self):
        record = private_read(self.path)
        if (type(record) is not dict or set(record) != {'receipt_id', 'body'}
                or not _hash(record['receipt_id']) or record['receipt_id'] != digest(record['body'])
                or record['receipt_id'] != self.state.get('recovery_root_receipt_id')):
            fail()
        b = record['body']
        fields = {'schema_version', 'kind', 'operation_id', 'action', 'installation_id', 'root_locators',
            'root_order', 'work_dir', 'source_archive_sha256', 'candidate_tree_sha256', 'candidate_identity',
            'source_binding', 'target_binding', 'source_metadata_sha256', 'source_witness', 'safety_receipt_id',
            'safety_archive_sha256', 'safety_binding_sha256', 'preserved_services', 'roots'}
        if (type(b) is not dict or set(b) != fields or type(b.get('schema_version')) is not int or b['schema_version'] != 1
                or b.get('kind') != 'recovery_root_replacement'
                or b.get('operation_id') != self.operation_id or b.get('action') != self.ctx['action']
                or b.get('installation_id') != self.engine._owner(self.ctx, required=True)['installation_id']
                or b.get('root_locators') != {n: str(p) for n, p in self.ctx['home'].roots.items()}
                or b.get('root_order') != list(PERSISTENT_ROOTS) or b.get('work_dir') != str(self.ctx['work'])
                or type(b.get('roots')) is not dict or set(b['roots']) != set(PERSISTENT_ROOTS)
                or any(not _hash(b[k]) for k in ('source_archive_sha256', 'candidate_tree_sha256',
                    'source_metadata_sha256', 'safety_receipt_id', 'safety_archive_sha256', 'safety_binding_sha256'))):
            fail()
        for value in b['roots'].values():
            if (type(value) is not dict or set(value) != {'identity', 'old', 'target'}
                    or type(value['identity']) is not list or len(value['identity']) != 5
                    or any(type(i) is not int for i in value['identity'])
                    or not _hash(value['old']) or not _hash(value['target'])): fail()
        return record

    def source_witness(self, expected=None):
        binding = expected or self.ctx['product_adapter_binding']
        adapter, actual = self.engine._release_product_adapter(self.ctx, binding['release'])
        if actual != binding: fail()
        return quiesce_witness(adapter, installation_id=self.engine._owner(self.ctx, required=True)['installation_id'],
            operation_id=self.operation_id, data_home=self.ctx['home'].roots['data'])

    def readonly_paths(self):
        """Never recreate/chmod a missing/drifted root during pending resume."""
        b = self.record()['body']
        for path in [self.ctx['root'], self.ctx['control'], self.ctx['work']]:
            try:
                with directory_handle(path) as fd:
                    info = os.fstat(fd)
                    if info.st_mode & 0o077: fail()
            except OSError: fail()
        owner = private_read(self.ctx['control'] / 'installation.json')
        work_owner = private_read(self.ctx['work'] / 'owner.json')
        if work_owner != {'installation_id': owner['installation_id'], 'owner': owner['owner']}: fail()
        if owner.get('pending_recovery') != {'operation_id': self.operation_id, 'receipt_id': self.state['recovery_root_receipt_id']}: fail()
        for name, path in self.ctx['home'].roots.items():
            try:
                with directory_handle(path) as fd:
                    i = os.fstat(fd)
                    if [i.st_dev, i.st_ino, i.st_mode, i.st_uid, i.st_gid] != b['roots'][name]['identity']: fail()
            except OSError: fail()

    def check_inputs(self):
        from .engine import EngineError, _file_digest
        b = self.record()['body']
        try:
            valid = (_file_digest(Path(self.ctx['options']['archive_path'])) == b['source_archive_sha256']
                and content_digest(self.candidate) == b['candidate_tree_sha256']
                and snapshot(self.candidate)['directories']['.'] == b['candidate_identity']
                and digest(self.engine._metadata(self.ctx, b['source_binding']['release'])) == b['source_metadata_sha256']
                and self.source_witness(b['source_binding']) == b['source_witness'])
        except (OSError, InstallerError, EngineError):
            fail()
        if not valid: fail()
        return b

    def mixture(self):
        self.readonly_paths()
        b = self.check_inputs()
        progress = self.state.get('recovery_root_progress', {})
        if type(progress) is not dict or set(progress) - set(PERSISTENT_ROOTS): fail()
        observed, frontier = {}, False
        for name in PERSISTENT_ROOTS:
            entry = progress.get(name)
            if entry is not None and not typed_progress(entry): fail()
            actual = digest(projection(snapshot(self.ctx['home'].roots[name])))
            old, target = b['roots'][name]['old'], b['roots'][name]['target']
            if entry is None:
                frontier = True
                if actual != old: fail()
                observed[name] = 'old'
            elif entry['complete']:
                if frontier or actual != target: fail()
                observed[name] = 'target'
            else:
                if frontier: fail()
                frontier = True
                if actual == target: observed[name] = 'target'
                elif actual == old: observed[name] = 'old'
                else: fail()
        return observed

    def early_validate(self):
        if private_read(self.ctx['control'] / 'operations' / (self.token + '.json')) != self.state: fail()
        # Registered recovery has no automatic rollback terminal state. Such
        # a flag cannot authorize the engine's legacy terminal shortcut.
        if self.state.get('status') not in {'RUNNING', 'FAILED', 'RECOVERY_REQUIRED', 'CANCELED'}: fail()
        self.readonly_paths()
        for name in ('recovery_replace_started', 'recovery_roots_complete',
                     'recovery_target_stage_started', 'recovery_target_stage_complete'):
            if name in self.state and type(self.state[name]) is not bool: fail()
        started = self.state.get('recovery_target_stage_started') is True
        if self.state.get('recovery_target_stage_receipt_id') is not None:
            from .recovery_stage import TargetStage
            self.mixture() if 'restore' not in self.state.get('completed', []) else self.check_inputs()
            if started: TargetStage(self).published()
            else: TargetStage(self).before_publish()
        elif started and not self.state.get('recovery_target_stage_complete'):
            fail()  # Old/unknown stage without original typed authority.
        # Only root phase is an archive projection. Later product hooks may
        # legitimately change business DB bytes and cannot authorize re-copy.
        if 'restore' not in self.state.get('completed', []):
            if any(v.get('started') for v in self.state.get('product_adapter_recovery_scopes', {}).values()): fail()
            self.mixture()
        else:
            if not self.complete_progress() or not self.state.get('recovery_target_stage_complete'): fail()
            self.check_inputs()

    def complete_progress(self):
        return completed_progress(self.state)

    def _preserved(self, old):
        services = self.ctx['home'].roots['services']
        pointer = self.ctx['manifest']['update']['pointer_file']
        if pointer not in old['files']: fail()
        names = [pointer, 'operation.lock']
        journal_keys = {'version', 'transaction', 'plan', 'old_release', 'release', 'direction',
                        'index', 'failed_phase', 'archive_ready', 'outcome'}
        for name in sorted(old['files']):
            if not name.startswith('update-') or not name.endswith('.json'): continue
            match = re.fullmatch('update-([0-9a-f]{32})[.]json', name)
            if match is None: fail()
            value = owned_data_json(services / name)
            native = private_read(self.ctx['control'] / 'operations' / (match[1] + '.json'))
            if (type(value) is not dict or set(value) != journal_keys
                    or type(value.get('version')) is not int or value['version'] != 1
                    or not _hash(value.get('plan'))
                    or type(value.get('transaction')) is not str
                    or re.fullmatch('[0-9a-f]{32}', value['transaction']) is None
                    or type(value.get('index')) is not int or value['index'] <= 0
                    or value.get('archive_ready') is not True or value.get('failed_phase') is not None
                    or type(native) is not dict
                    or native.get('action') != 'update' or native.get('status') != 'SUCCEEDED'
                    or native.get('result', {}).get('transaction') != value.get('transaction')
                    or native.get('result', {}).get('release') != value.get('release')
                    or native.get('initial_active') != value.get('old_release')
                    or value.get('outcome') != 'updated' or value.get('direction') != 'forward'):
                fail()
            names.append(name)
        if len(names) > 128: fail('CAPABILITY_UNSUPPORTED')
        for name in names:
            if name in old['directories']: fail()
            if name in old['files']:
                if name == 'operation.lock': released_service_lock(services / name)
                elif name == pointer:
                    if owned_data_json(services / name) != {'release': self.ctx['product_adapter_binding']['release']}: fail()
        return sorted(names)

    def prepare(self, safety, source_digest):
        from .engine import _write, _digest
        if self.state.get('recovery_root_receipt_id'):
            self.mixture()
            return self.record()
        if self.state.get('recovery_replace_started'): fail()
        old = {n: snapshot(p) for n, p in self.ctx['home'].roots.items()}
        projections = {n: projection(t) for n, t in old.items()}
        names = self._preserved(projections['services'])
        roots = {}
        for name in PERSISTENT_ROOTS:
            target = projection(snapshot(self.candidate / 'roots' / name), normalized=True)
            if name == 'services':
                for preserved in names:
                    if preserved in target['directories']: fail()
                    target['files'].pop(preserved, None)
                    if preserved in projections[name]['files']:
                        target['files'][preserved] = projections[name]['files'][preserved]
            roots[name] = {'identity': old[name]['directories']['.'],
                'old': digest(projections[name]), 'target': digest(target)}
        body = {'schema_version': 1, 'kind': 'recovery_root_replacement', 'operation_id': self.operation_id,
            'action': self.ctx['action'], 'installation_id': self.engine._owner(self.ctx, required=True)['installation_id'],
            'root_locators': {n: str(p) for n, p in self.ctx['home'].roots.items()}, 'root_order': list(PERSISTENT_ROOTS),
            'work_dir': str(self.ctx['work']), 'source_archive_sha256': source_digest,
            'candidate_tree_sha256': content_digest(self.candidate), 'candidate_identity': snapshot(self.candidate)['directories']['.'],
            'source_binding': self.ctx['product_adapter_binding'],
            'source_metadata_sha256': digest(self.engine._metadata(self.ctx, self.ctx['product_adapter_binding']['release'])),
            'target_binding': self.ctx['recovery_target_binding'], 'source_witness': self.source_witness(),
            'safety_receipt_id': safety['receipt']['receipt_id'], 'safety_archive_sha256': safety['receipt']['archive_sha256'],
            'safety_binding_sha256': digest(safety['binding']), 'preserved_services': names, 'roots': roots}
        record = {'receipt_id': digest(body), 'body': body}
        if self.path.exists():
            if private_read(self.path) != record: fail()
        else: _write(self.path, record)
        self.state['recovery_root_receipt_id'] = record['receipt_id']
        self.state['recovery_root_progress'] = {}; self.save()
        owner = private_read(self.ctx['control'] / 'installation.json')
        owner['pending_recovery'] = {'operation_id': self.operation_id, 'receipt_id': record['receipt_id']}
        _write(self.ctx['control'] / 'installation.json', owner)
        # The complete before-image authorizer is repeated after receipt capture.
        self.mixture()
        return record

    def apply(self):
        b = self.record()['body']
        self.state['recovery_replace_started'] = True
        self.state['product_adapter_recovery_required'] = True; self.save()
        for name in PERSISTENT_ROOTS:
            observed = self.mixture()
            progress = self.state['recovery_root_progress']
            if progress.get(name, {}).get('complete'): continue
            if name not in progress:
                progress[name] = {'started': True, 'complete': False}; self.save()
                observed = self.mixture()
            if observed[name] != 'target':
                FilesystemPorts._reconcile(self.candidate / 'roots' / name, self.ctx['home'].roots[name],
                    set(b['preserved_services']) if name == 'services' else set())
            if digest(projection(snapshot(self.ctx['home'].roots[name]))) != b['roots'][name]['target']: fail()
            progress[name]['complete'] = True; self.save()
        self.mixture()
        self.state['recovery_roots_complete'] = True; self.save()

    def stage_begin(self):
        from .recovery_stage import TargetStage
        stage = TargetStage(self)
        if self.state.get('recovery_target_stage_started'):
            stage.published()
            return False  # Read-only postconditions; never republish.
        stage.before_publish()
        self.state['recovery_target_stage_started'] = True; self.save()
        return True

    def stage_complete(self):
        from .recovery_stage import TargetStage
        TargetStage(self).published()
        self.state['recovery_target_stage_complete'] = True; self.save()

    def issue_terminal(self, result):
        from .engine import _write
        record = self.record(); self.readonly_paths(); self.check_inputs()
        required = {'verify_archive', 'stop', 'before_' + self.ctx['action'], 'restore',
                    'product_restore', 'post_restore', 'activate', 'start', 'health'}
        if (not self.complete_progress() or not self.state.get('recovery_target_stage_complete')
                or not required <= set(self.state.get('completed', [])) or result.get('status') != 'SUCCEEDED'
                or result.get('health', {}).get('release') != record['body']['target_binding']['release']
                or result.get('health', {}).get('product_adapter', {}).get('ready') is not True):
            fail()
        body = {'schema_version': 1, 'kind': 'completed_recovery_root_operation',
            'operation_id': self.operation_id, 'root_receipt_id': record['receipt_id'],
            'result_sha256': digest(result), 'completed_sha256': digest(self.state['completed']),
            'target_binding_sha256': digest(record['body']['target_binding'])}
        terminal_id = digest(body)
        path = self.directory / (terminal_id + '.terminal.json')
        terminal = {'receipt_id': terminal_id, 'body': body}
        if path.exists():
            if private_read(path) != terminal: fail()
        else: _write(path, terminal)
        self.state['recovery_root_terminal_id'] = terminal_id

    def retire_terminal(self):
        from .engine import _write
        record = self.record()
        if private_read(self.ctx['control'] / 'operations' / (self.token + '.json')) != self.state: fail()
        if terminal_proof(self.engine, self.ctx, self.token, self.state) != record: fail()
        owner = private_read(self.ctx['control'] / 'installation.json')
        if owner.get('pending_recovery') is None:
            return  # Already retired: historical terminal proof, no owner rewrite.
        self.readonly_paths(); self.check_inputs()
        owner.pop('pending_recovery')
        _write(self.ctx['control'] / 'installation.json', owner)
