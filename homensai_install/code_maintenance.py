"""Same signed release code maintenance; never execute the damaged source.

Linux private user namespace and the engine lifecycle lock are prerequisites.
FD/name checks narrow same-UID races; POSIX rename is not compare-and-swap.
"""
import errno
import os
from pathlib import Path
import uuid

from shared import archive, archive_crypto
from shared.adapters_fs import FilesystemPorts, _copy_file

from .api.model import digest
from .errors import InstallerError
from .migration_receipts import BackupReceiptStore, _reader
from .migration_snapshot import assert_recovery_snapshot_matches
from .retained_adapter import adapter_from_retained, prepare_execution, quiesce_witness
from .uninstall_receipts import directory_handle, snapshot, _identity, _read_receipt


def fail(code='RECOVERY_REQUIRED'):
    from .engine import EngineError
    raise EngineError(code, 'Same-release code maintenance authority is not verified')


def archive_failure(error):
    """Preserve a typed budget refusal; never classify by message text."""
    if isinstance(error, archive.InsufficientSpaceError) or isinstance(error, OSError) and error.errno == errno.ENOSPC:
        fail('DISK_SPACE_LOW')
    if isinstance(error, archive.ArchiveError):
        fail('INTEGRITY_FAILED')
    fail()


def read_proof(path):
    try:
        return _read_receipt(path)
    except (OSError, InstallerError):
        fail('INTEGRITY_FAILED')


def rename_at(source_parent, source_name, destination_parent, destination_name):
    """Fault seam: caller durably records the exact intent before this rename."""
    os.rename(source_name, destination_name, src_dir_fd=source_parent, dst_dir_fd=destination_parent)
    os.fsync(source_parent)
    os.fsync(destination_parent)


def ledger_identity(adapter):
    path = adapter.state_dir / 'adapter-invocations.sqlite3'
    try:
        with _reader(path, private=True) as (_, info):
            return [info.st_dev, info.st_ino, info.st_uid]
    except (OSError, InstallerError):
        fail()


def fence(ctx, action, original=None):
    from .engine import _read
    if action == 'stop':
        return
    folder = ctx['control'] / 'operations'
    for path in folder.glob('*.json') if folder.exists() else ():
        if path == original:
            continue
        state = _read(path)
        if state.get('code_maintenance_review') is not None and state.get('status') != 'SUCCEEDED':
            fail()


def recovery_cache(engine, ctx, owner, binding):
    from .engine import _read
    path = ctx['home'].roots['runtime'] / 'recovery' / ctx['release']
    if not path.exists(): return None
    _, actual = adapter_from_retained(engine, ctx, ctx['release'], path)
    if (actual != binding or _read(path / 'release.json') != engine._metadata(ctx, ctx['release'])
            or any(_read(path / 'installation.json').get(k) != owner.get(k)
                   for k in ('schema_version', 'product_id', 'installation_id', 'owner', 'mode'))):
        fail('INTEGRITY_FAILED')
    tree = snapshot(path)
    return {'directory_identity': tree['directories']['.'],
            'files_sha256': digest({n: v['sha256'] for n, v in tree['files'].items()})}


def review(engine, ctx):
    """Read-only candidate authorization independent of corrupt installed code."""
    from .engine import _file_digest
    owner = engine._owner(ctx, required=True)
    release = engine._pointer(ctx)
    if release != ctx['release'] or not release:
        fail('OPERATION_CONFLICT')
    meta = engine._metadata(ctx, release)
    if (meta['manifest'] != ctx['manifest'] or meta['target'] != ctx['target']
            or tuple(meta['modes']) != ctx['modes']
            or meta['runtime_executable'] != ctx['options'].get('runtime_executable')):
        fail('CAPABILITY_UNSUPPORTED')
    adapter, binding = engine._product_adapter(ctx)
    locale = ctx['options'].get('eula_locale', 'en')
    if locale not in {'en', 'de', 'ru'} or adapter._inventory.get('legal/' + locale + '.txt') != ctx['options'].get('eula_digest'):
        fail('CONSENT_REQUIRED')
    if (digest(adapter._inventory) != digest(meta['inventory'])
            or not {'migration_plan', 'quiesce', 'operation_status', 'readiness'} <= set(adapter.capabilities()['hooks'])):
        fail('CAPABILITY_UNSUPPORTED')
    metadata = {suffix: _file_digest(ctx['control'] / 'releases' / (release + suffix))
                for suffix in ('.json', '.inventory.json', '.signature.json')}
    if (metadata['.inventory.json'] != _file_digest(ctx['options']['inventory_file'])
            or metadata['.signature.json'] != _file_digest(ctx['options']['signature_file'])):
        fail('INTEGRITY_FAILED')
    retained = None
    if owner.get('installed') is not True:
        if ctx['action'] != 'reinstall':
            fail('OPERATION_CONFLICT')
        from .retained_adapter import RetainedSource
        selected = RetainedSource.selected(engine, {**ctx, 'action': 'restore'})
        retained = selected.reference
        if selected.binding != binding:
            fail('INTEGRITY_FAILED')
    path = engine._release(ctx, release)
    tree = snapshot(path) if path.exists() else None
    result = {'schema_version': 1, 'kind': 'same_release_code_maintenance',
        'action': ctx['action'], 'release': release, 'owner': owner,
        'installation_id': owner['installation_id'], 'candidate_binding_sha256': digest(binding),
        'metadata': metadata, 'metadata_sha256': digest(meta), 'source': tree,
        'release_entries': sorted(p.name for p in ctx['home'].releases.iterdir()),
        'candidate_binding': binding, 'ledger_identity': ledger_identity(adapter),
        'retained_source': retained,
        'recovery_cache': recovery_cache(engine, ctx, owner, binding),
        'credential_policy': 'installed_release_refs',
        'failure_policy': 'preserve_data_and_quarantine_no_automatic_restore'}
    return {**result, 'review_sha256': digest(result)}


class CodeMaintenance:
    def __init__(self, engine, ctx):
        self.engine, self.ctx = engine, ctx
        self.state = ctx['operation_state']
        self.reviewed = ctx['code_maintenance_review']
        self.token = ctx['operation_token']
        self.container = ctx['control'] / 'code-maintenance' / self.token
        self.closure = self.container / 'candidate'
        self.stage = self.container / 'stage'
        self.old = self.container / 'quarantine'
        self.live = engine._release(ctx, ctx['release'])
        self.archive = ctx['work'] / (self.token + '-before-' + ctx['action'] + '.hsac')

    def save(self):
        from .engine import _write
        _write(self.ctx['control'] / 'operations' / (self.token + '.json'), self.state)

    @staticmethod
    def bind(engine, ctx, detail, previous):
        """Validate original review, selecting only its cited durable closure."""
        reviewed = detail.get('code_maintenance') if type(detail) is dict else None
        if type(reviewed) is not dict:
            fail('PLAN_STALE')
        if reviewed.get('review_sha256') != digest({k: v for k, v in reviewed.items() if k != 'review_sha256'}):
            fail('PLAN_STALE')
        if previous and previous.get('code_maintenance_review') is not None:
            if previous['code_maintenance_review'] != reviewed:
                fail('PLAN_STALE')
            ctx['code_maintenance_review'] = reviewed
            if previous.get('code_maintenance_closure') is not None:
                temporary = {**ctx, 'operation_state': previous}
                protocol = CodeMaintenance(engine, temporary)
                adapter, binding = protocol.candidate()
                ctx['options'] = dict(adapter.options)
            else:
                if review(engine, ctx) != reviewed:
                    fail('PLAN_STALE')
        elif review(engine, ctx) != reviewed:
            fail('PLAN_STALE')
        ctx['code_maintenance_review'] = reviewed
        # Ignore incoming credential aliases, including on post-uninstall reinstall.
        ctx['options']['secret_refs'] = engine._secret_refs(
            {**ctx, 'release': None, 'action': None, 'options': {}}, ctx['release'])
        return engine._verify(ctx['manifest'], ctx['target'], ctx['options'])

    def stable(self):
        from .engine import _file_digest
        r = self.reviewed
        if (self.engine._pointer(self.ctx) != r['release']
                or digest(self.engine._metadata(self.ctx, r['release'])) != r['metadata_sha256']):
            fail('PLAN_STALE')
        current = self.engine._owner(self.ctx, required=True)
        # installed/retained marker may change only at this operation's final activation.
        if not self.state.get('code_maintenance_activation_started') and current != r['owner']:
            fail('PLAN_STALE')
        if any(current.get(k) != r['owner'].get(k) for k in
               ('schema_version', 'product_id', 'installation_id', 'owner', 'mode')):
            fail('PLAN_STALE')
        for suffix, sha in r['metadata'].items():
            if _file_digest(self.ctx['control'] / 'releases' / (r['release'] + suffix)) != sha:
                fail('PLAN_STALE')

    def candidate(self):
        r = self.state.get('code_maintenance_closure')
        if type(r) is not dict:
            fail()
        tree = snapshot(self.closure)
        if tree['directories']['.'] != r['directory_identity'] or {
                n: v['sha256'] for n, v in tree['files'].items()} != r['files']:
            fail('INTEGRITY_FAILED')
        adapter, binding = adapter_from_retained(self.engine, self.ctx, self.ctx['release'], self.closure)
        if binding != self.reviewed['candidate_binding'] or ledger_identity(adapter) != self.reviewed['ledger_identity']:
            fail()
        return adapter, binding

    def prepare(self):
        from .engine import _private_dir, _write
        self.stable()
        if not self.state.get('code_maintenance_recovery_started'):
            if recovery_cache(self.engine, self.ctx, self.reviewed['owner'], self.reviewed['candidate_binding']) != self.reviewed['recovery_cache']:
                fail('PLAN_STALE')
        if 'code_maintenance_closure' not in self.state:
            # Refuse an unbound pre-existing namespace, even if its bytes look valid.
            if self.container.exists():
                fail()
            _private_dir(self.container.parent)
            with directory_handle(self.container.parent) as parent:
                os.mkdir(self.container.name, 0o700, dir_fd=parent); os.fsync(parent)
            if self.container.stat().st_dev != self.ctx['home'].releases.stat().st_dev:
                fail('CAPABILITY_UNSUPPORTED')
            _private_dir(self.closure)
            FilesystemPorts._reconcile(Path(self.ctx['options']['payload_dir']), self.closure / 'payload', set())
            for suffix in ('.json', '.inventory.json', '.signature.json'):
                _copy_file(self.ctx['control'] / 'releases' / (self.ctx['release'] + suffix), self.closure / ('release' + suffix))
            _write(self.closure / 'installation.json', self.reviewed['owner'])
            adapter, binding = adapter_from_retained(self.engine, self.ctx, self.ctx['release'], self.closure)
            if binding != self.reviewed['candidate_binding']:
                fail('INTEGRITY_FAILED')
            prepare_execution(adapter)
            tree = snapshot(self.closure)
            self.state['code_maintenance_closure'] = {'directory_identity': tree['directories']['.'],
                'files': {n: v['sha256'] for n, v in tree['files'].items()}}
            self.save()
        adapter, _ = self.candidate()
        prepare_execution(adapter)
        self.ctx['code_maintenance_adapter'] = adapter
        return {'candidate_binding_sha256': digest(self.reviewed['candidate_binding'])}

    def prepare_recovery(self):
        """Preserve standard hsac compatibility, including first-ever repair."""
        from .engine import _read, _private_dir, _write
        destination = self.ctx['home'].roots['runtime'] / 'recovery' / self.ctx['release']
        if destination.exists():
            adapter, binding = adapter_from_retained(self.engine, self.ctx, self.ctx['release'], destination)
            if (binding != self.reviewed['candidate_binding']
                    or _read(destination / 'release.json') != self.engine._metadata(self.ctx, self.ctx['release'])
                    or any(_read(destination / 'installation.json').get(k) != self.reviewed['owner'].get(k)
                           for k in ('schema_version', 'product_id', 'installation_id', 'owner', 'mode'))):
                fail('INTEGRITY_FAILED')
            snapshot(destination)
        else:
            if self.state.get('code_maintenance_recovery_started'):
                fail()
            self.state['code_maintenance_recovery_started'] = True; self.save()
            _private_dir(destination)
            FilesystemPorts._reconcile(self.closure, destination, set())
        self.state['code_maintenance_recovery_ready'] = True; self.save()

    def schema(self):
        """Trusted candidate reads only a disposable copy, with a separate ledger."""
        from .engine import EngineError, _private_dir
        adapter, _ = self.candidate()
        probe = self.container / ('schema-' + uuid.uuid4().hex)
        _private_dir(probe / 'roots')
        for name, path in self.ctx['home'].roots.items():
            FilesystemPorts._reconcile(path, probe / 'roots' / name, set())
        before = {name: snapshot(probe / 'roots' / name) for name in self.ctx['home'].roots}
        adapter.environment['HOMENSAI_DATA_HOME'] = str(probe / 'roots/data')
        if 'HOMENSAI_CONFIG_HOME' in adapter.environment:
            adapter.environment['HOMENSAI_CONFIG_HOME'] = str(probe / 'roots/config')
        adapter._validate_environment()
        adapter.state_dir = probe / 'diagnostic-ledger'
        try:
            response = adapter.invoke('migration_plan', {'operation_id': 'op_' + self.token,
                'idempotency_key': 'maintenance_probe_' + uuid.uuid4().hex, 'phase': 'diagnostic',
                'data': {'installation_id': self.reviewed['owner']['installation_id'],
                         'from_release': self.ctx['release'], 'to_release': self.ctx['release'], 'mode': 'upgrade'}})
        except InstallerError:
            raise EngineError('CAPABILITY_UNSUPPORTED', 'Same-schema candidate maintenance was not confirmed') from None
        plan = response['result']
        if (response.get('replayed') is not False or plan['from_version'] != plan['to_version'] or plan['steps']
                or plan['from_version'].casefold() in {'empty', 'unknown'}
                or before != {name: snapshot(probe / 'roots' / name) for name in self.ctx['home'].roots}):
            fail('CAPABILITY_UNSUPPORTED')
        return {k: plan[k] for k in ('from_version', 'to_version', 'plan_digest', 'steps')}

    def witness(self):
        adapter, _ = self.candidate()
        witness = quiesce_witness(adapter, installation_id=self.reviewed['owner']['installation_id'],
            operation_id='op_' + self.token, data_home=self.ctx['home'].roots['data'])
        if witness['ledger_identity'] != self.reviewed['ledger_identity']:
            fail()
        return witness

    def quiesce(self, cleanup=False):
        from .engine import _UncertainEffect
        adapter, _ = self.candidate()
        prepare_execution(adapter)
        key = ('cleanup_quiesce_' if cleanup else 'quiesce_') + self.token
        name = 'code_maintenance_cleanup' if cleanup else 'code_maintenance_quiesce'
        scope = self.state.setdefault(name, {'started': False, 'complete': False})
        guard = {'expected_ledger_identity': self.reviewed['ledger_identity'][:2]}
        if scope['complete']:
            witness = quiesce_witness(adapter, installation_id=self.reviewed['installation_id'],
                operation_id='op_' + self.token, data_home=self.ctx['home'].roots['data'], idempotency_key=key)
            if witness != scope.get('witness'): fail()
            return scope['result']
        request = {'operation_id': 'op_' + self.token, 'idempotency_key': key, 'phase': 'execute',
            'data': {'installation_id': self.reviewed['installation_id'], 'release': self.ctx['release'],
                     'data_home': str(self.ctx['home'].roots['data'])}}
        try:
            if scope['started']:
                if not self.ctx.get('adapter_resume'): raise _UncertainEffect()
                outcome = adapter.reconcile(key, **guard)
                if outcome['outcome'] != 'applied': raise _UncertainEffect()
                result = outcome['result']
            else:
                scope['started'] = True
                self.state['product_adapter_unknown'] = True; self.save()
                result = adapter.invoke('quiesce', request, require_existing_ledger=True, **guard)['result']
            if digest(result) != digest({'quiesced': True, 'active_writers': 0}): raise _UncertainEffect()
            scope.update(complete=True, result=result, witness=quiesce_witness(adapter,
                installation_id=self.reviewed['installation_id'], operation_id='op_' + self.token,
                data_home=self.ctx['home'].roots['data'], idempotency_key=key))
            if not cleanup: self.state['product_adapter_quiesce_complete'] = True
            self.state.pop('product_adapter_unknown', None); self.save()
            return result
        except InstallerError:
            raise _UncertainEffect() from None

    def store(self, password, compare_live=True):
        from .engine import _read
        def verify(candidate, binding):
            recovery = candidate / 'roots/runtime/recovery' / self.ctx['release']
            adapter, candidate_binding = adapter_from_retained(self.engine, self.ctx, self.ctx['release'], recovery)
            if (candidate_binding != self.reviewed['candidate_binding']
                    or _read(recovery / 'release.json') != self.engine._metadata(self.ctx, self.ctx['release'])
                    or any(_read(recovery / 'installation.json').get(k) != self.reviewed['owner'].get(k)
                           for k in ('schema_version', 'product_id', 'installation_id', 'owner', 'mode'))):
                return False
            return not compare_live or assert_recovery_snapshot_matches(self.ctx['home'].roots, candidate)
        return BackupReceiptStore(self.ctx['control'] / 'code-maintenance-receipts',
            password_provider=password, recovery_verifier=verify)

    def backup(self, password):
        from .engine import _write
        self.stable(); self.prepare_recovery()
        witness = self.witness()
        schema = self.schema()
        if self.engine._records(self.ctx):
            fail()
        if not self.state.get('code_maintenance_archive_started'):
            if self.archive.exists(): fail()
            self.state['code_maintenance_archive_started'] = True; self.save()
        try:
            if not self.archive.exists():
                # A crashed archive creation may be retried only while no ciphertext
                # has been published. An existing file is re-proven in full below.
                archive_crypto.build_encrypted_archive(self.ctx['home'], self.archive, password_provider=password)
            self.store(password)._directory(create=True)
            verified = self.store(password)._verified(self.archive, {'product_id': self.ctx['manifest']['product_id']})
        except (archive.ArchiveError, OSError) as error:
            archive_failure(error)
        body = {'schema_version': 1, 'kind': 'before_code_maintenance_backup',
            'maintenance_action': self.ctx['action'], 'operation_id': 'op_' + self.token,
            'product_id': self.ctx['manifest']['product_id'], 'installation_id': self.reviewed['owner']['installation_id'],
            'from_release': self.ctx['release'], 'review_sha256': self.reviewed['review_sha256'],
            'candidate_binding_sha256': digest(self.reviewed['candidate_binding']),
            'schema_plan': schema, 'quiesce_witness': witness, **verified,
            'encrypted': True, 'includes_recovery_payload': True, 'migration_eligible': False,
            'verification_scope': 'full_snapshot_and_signed_recovery_payload_restored'}
        proof = {'receipt_id': digest(body), **body}
        path = self.ctx['control'] / 'code-maintenance-receipts' / (self.token + '.json')
        if path.exists() and read_proof(path) != proof:
            fail('INTEGRITY_FAILED')
        if not path.exists(): _write(path, proof)
        self.state['code_maintenance_backup'] = proof; self.save()
        return proof

    def verify_backup(self, password, *, compare_live):
        proof = self.state.get('code_maintenance_backup')
        if type(proof) is not dict: fail()
        path = self.ctx['control'] / 'code-maintenance-receipts' / (self.token + '.json')
        if (read_proof(path) != proof or proof['receipt_id'] != digest({k: v for k, v in proof.items() if k != 'receipt_id'})
                or proof['review_sha256'] != self.reviewed['review_sha256']
                or proof['quiesce_witness'] != self.witness()):
            fail('INTEGRITY_FAILED')
        try:
            checked = self.store(password, compare_live)._verified(self.archive,
                {'product_id': self.ctx['manifest']['product_id']}, proof['archive_sha256'])
        except (archive.ArchiveError, OSError) as error:
            archive_failure(error)
        if checked['archive_manifest_sha256'] != proof['archive_manifest_sha256']: fail()
        return proof

    def publish(self, password):
        from .engine import _private_dir
        self.stable(); self.candidate()
        intents = self.state.setdefault('code_maintenance_renames', {})
        if not intents:
            self.verify_backup(password, compare_live=True)
            actual = snapshot(self.live) if self.live.exists() else None
            if actual != self.reviewed['source'] or sorted(p.name for p in self.ctx['home'].releases.iterdir()) != self.reviewed['release_entries']:
                fail('PLAN_STALE')
            if self.stage.exists(): fail()
            _private_dir(self.stage)
            FilesystemPorts._reconcile(self.closure / 'payload', self.stage, set())
            staged = snapshot(self.stage)
            if {n: v['sha256'] for n, v in staged['files'].items()} != self.engine._metadata(self.ctx, self.ctx['release'])['inventory']:
                fail('INTEGRITY_FAILED')
            intents['stage_identity'] = staged['directories']['.']; self.save()
        else:
            actual_published = (self.live.exists() and not self.stage.exists()
                and _identity(self.live.lstat()) == intents.get('stage_identity'))
            self.verify_backup(password, compare_live=not actual_published)
        with directory_handle(self.container) as container, directory_handle(self.live.parent) as releases:
            if os.fstat(container).st_dev != os.fstat(releases).st_dev: fail('CAPABILITY_UNSUPPORTED')
            if self.reviewed['source'] is not None:
                if not intents.get('quarantine_started'):
                    if snapshot(self.live) != self.reviewed['source'] or self.old.exists(): fail('PLAN_STALE')
                    intents['quarantine_started'] = True; self.save()
                if self.old.exists():
                    if snapshot(self.old) != self.reviewed['source']: fail('INTEGRITY_FAILED')
                    if self.live.exists() and not intents.get('publish_started'): fail()
                else:
                    if snapshot(self.live) != self.reviewed['source']: fail('PLAN_STALE')
                    rename_at(releases, self.live.name, container, 'quarantine')
                intents['quarantine_complete'] = True; self.save()
            elif self.live.exists() and not intents.get('publish_started'):
                fail()
            if not intents.get('publish_started'):
                intents['publish_started'] = True; self.save()
            if self.stage.exists():
                if self.live.exists() or snapshot(self.stage)['directories']['.'] != intents['stage_identity']: fail()
                if {n: v['sha256'] for n, v in snapshot(self.stage)['files'].items()} != self.engine._metadata(self.ctx, self.ctx['release'])['inventory']: fail()
                self.verify_backup(password, compare_live=True)
                rename_at(container, 'stage', releases, self.live.name)
            if not self.live.exists() or snapshot(self.live)['directories']['.'] != intents['stage_identity']: fail()
            self.engine._installed(self.ctx, self.ctx['release'])
            intents['publish_complete'] = True; self.save()
        # All executable grants use the verified FD helper, including published target.
        adapter, _ = self.engine._product_adapter({**self.ctx, 'options': {
            **self.ctx['options'], 'payload_dir': str(self.live)}})
        prepare_execution(adapter)
        return {'release': self.ctx['release'], 'code_only': True, 'quarantine_preserved': self.reviewed['source'] is not None}

    def run(self, password, step):
        from .engine import _write, _safe_error_code, _UncertainEffect
        self.state['code_maintenance_review'] = self.reviewed; self.save()
        if 'verify' in self.state.get('completed', []):
            self.prepare()
        else:
            step('verify', self.prepare)
        if (self.state.get('code_maintenance_cleanup', {}).get('started')
                or self.state.get('code_maintenance_cleanup_error_code')):
            self.engine._stop_owned_processes(self.ctx)
            self.quiesce(cleanup=True)
        if not self.state.get('code_maintenance_renames', {}).get('publish_started'):
            step('stop', lambda: self.engine._stop_owned_processes(self.ctx))
            step('schema', self.schema)
            step('quiesce', self.quiesce)
        proof = step('backup', lambda: self.backup(password))
        step('repair', lambda: self.publish(password))
        # A completed code step does not turn a missing/tampered safety artifact
        # into a usable before-maintenance snapshot on a later start/resume.
        self.verify_backup(password, compare_live=False)
        self.state['code_maintenance_activation_started'] = True; self.save()
        owner = self.engine._owner(self.ctx, required=True)
        owner.update(installed=True); owner.pop('retained_uninstall', None)
        _write(self.ctx['control'] / 'installation.json', owner)
        try:
            step('start', lambda: self.engine._start(self.ctx))
            health = step('health', lambda: self.engine._health(self.ctx))
        except Exception as error:
            self.state['code_maintenance_primary_error_code'] = _safe_error_code(error)
            self.state['product_adapter_recovery_required'] = True; self.save()
            try:
                self.engine._stop_owned_processes(self.ctx)
                self.quiesce(cleanup=True)
            except _UncertainEffect:
                self.state['code_maintenance_cleanup_error_code'] = 'RECOVERY_REQUIRED'; self.save()
                raise
            except Exception as cleanup_error:
                self.state['code_maintenance_cleanup_error_code'] = _safe_error_code(cleanup_error); self.save()
                if hasattr(error, 'details'):
                    error.details.update(primary_error_code=self.state['code_maintenance_primary_error_code'],
                        cleanup_error_code=self.state['code_maintenance_cleanup_error_code'])
            self.save()
            raise
        self.state.pop('product_adapter_recovery_required', None)
        return {'status': 'SUCCEEDED', 'health': health, 'code_only': True,
            'before_maintenance': proof, 'before_maintenance_archive': {
                'path': str(self.archive), 'sha256': proof['archive_sha256'], 'release': self.ctx['release'],
                'encrypted': True, 'includes_recovery_payload': True}}
