"""Read-only verification of the original pointer-v2 systemd group during update."""
from __future__ import annotations

import hashlib
import json
import re

from shared.datahome import checked_path
from shared.fresh_prepare import status_fresh_native
from shared.package_signing import verify_signed_package
from shared.pointer_update import inspect_started_pointer_runtime
from shared.services import component_order, render_pointer_user_services_v2
from shared.systemd_pointer import _pointer_publication_binding
from shared.systemd_user import SystemdUserAdapter, _run
from shared.systemd_group import SystemdUserGroup
from shared.systemd_user_load import _absent, _manager_call, _read_receipt
from shared.systemd_user_publish import _publication_receipt, _unit_directory
from shared.update_plan import UpdateError


def _inspect_started_pointer_group(*, unit_dir, load_key, runtime_key, runtime_reserve_bytes,
                                   runner=None, timeout=30, **options):
    """Observe signed unit files and systemd state, without authorizing any effect.

    Caller must independently trust the keys, manager, local installer and all
    writers. Results are observations, not a process identity or health check.
    """
    try:
        if (any(type(key) is not str or re.fullmatch(r'[a-f0-9]{32}', key) is None
                for key in (load_key, runtime_key, options['operation_id']))
                or len({load_key, runtime_key, options['operation_id']}) != 3
                or type(timeout) not in (int, float) or not 0 < timeout <= 120
                or (runner is not None and not callable(runner))):
            raise UpdateError('Недопустимые параметры служб')
        runtime = inspect_started_pointer_runtime(runtime_key=runtime_key,
            runtime_reserve_bytes=runtime_reserve_bytes, **options)
        old_options = dict(trusted_public_key=options['old_key'], expected_product_id=options['product_id'],
                           expected_version=options['old_version'])
        old = verify_signed_package(options['old_package'], options['old_envelope'], **old_options)
        fresh = status_fresh_native(options['old_package'], options['old_envelope'], options['install_root'],
                                    **old_options)
        root, directory = checked_path(options['install_root']), _unit_directory(unit_dir)
        units = render_pointer_user_services_v2(old['manifest'], modes=options['modes'], install_root=str(root))
        plan = {'generator': 'homensai-systemd-user-pointer-v2', 'units': [
            {'name': name, 'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()}
            for name, body in units.items()]}
        order = component_order(old['manifest'], options['modes'])
        if [item['name'] for item in plan['units']] != [options['product_id'] + '-' + name + '.service' for name in order]:
            raise UpdateError('Состав служб изменился')
        control = checked_path(root / '.homensai-control')
        publication_path = checked_path(control / 'user-systemd-publication.json')
        load_path = checked_path(control / 'user-systemd-reload.json')
        publication = _publication_receipt(publication_path)
        expected_binding = _pointer_publication_binding(product_id=options['product_id'],
            version=options['old_version'], transaction=fresh['transaction'], plan=plan, modes=options['modes'],
            root=root, directory=directory, trusted_public_key=options['old_key'],
            runtime_operation_id=runtime_key, reserve_bytes=runtime_reserve_bytes)
        if (publication is None or publication['status'] != 'done' or publication['binding'] != expected_binding
                or publication['operation_id'] in (load_key, runtime_key, options['operation_id'])):
            raise UpdateError('Публикация служб не подтверждена')
        context = {'product_id': options['product_id'], 'version': options['old_version'],
                   'transaction': fresh['transaction'], 'units': plan['units'],
                   'install_sha256': hashlib.sha256(str(root).encode()).hexdigest(),
                   'directory_sha256': hashlib.sha256(str(directory).encode()).hexdigest(),
                   'publisher_sha256': hashlib.sha256(options['old_key']).hexdigest(),
                   'operation_id': load_key, 'modes': options['modes'], 'generator': plan['generator'],
                   'runtime_operation_id': runtime_key, 'reserve_bytes': runtime_reserve_bytes}
        load = {'version': 1, 'binding': hashlib.sha256(json.dumps(context, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest(), 'status': 'done'}
        if _read_receipt(load_path) != load:
            raise UpdateError('Загрузка служб не подтверждена')
        manager = _run if runner is None else runner
        if str(directory) not in _manager_call(manager, ['show', '--property=UnitPath', '--value'], timeout).split():
            raise UpdateError('Каталог не принадлежит менеджеру')
        if not _absent(directory / ('.' + options['product_id'] + '.homensai-units.pending')):
            raise UpdateError('Публикация служб не завершена')
        observed, adapters = [], []
        for component, item in zip(order, plan['units']):
            if not _absent(directory / (item['name'] + '.d')):
                raise UpdateError('Обнаружен drop-in службы')
            adapter = SystemdUserAdapter(old['manifest'], modes=options['modes'], component=component,
                fragment_path=str(directory / item['name']), fragment_sha256=item['sha256'], runner=manager,
                timeout=timeout)
            state = adapter.inspect()
            if not (state['active_state'] == 'active' or
                    (state['active_state'] == 'inactive' and state['sub_state'] == 'dead')):
                raise UpdateError('Состояние службы не установилось')
            observed.append({'name': item['name'], 'active_state': state['active_state'],
                             'sub_state': state['sub_state']})
            adapters.append(adapter)
        if (_read_receipt(load_path) != load or _publication_receipt(publication_path) != publication
                or not _absent(directory / ('.' + options['product_id'] + '.homensai-units.pending'))
                or any(not _absent(directory / (item['name'] + '.d')) for item in plan['units'])
                or inspect_started_pointer_runtime(runtime_key=runtime_key,
                    runtime_reserve_bytes=runtime_reserve_bytes, **options) != runtime):
            raise UpdateError('Состояние изменилось во время проверки')
        for adapter, previous in zip(adapters, observed):
            current = adapter.inspect()
            if (current['active_state'] != previous['active_state'] or
                    current['sub_state'] != previous['sub_state']):
                raise UpdateError('Состояние служб изменилось')
        public = {'state': runtime['state'], 'units': observed, 'runtime_verified': True,
                  'process_identity_verified': False, 'resume_authorized': False}
        group = SystemdUserGroup(old['manifest'], modes=options['modes'],
                                 adapters=dict(zip(order, adapters)))
        return public, group
    except Exception:
        raise UpdateError('Не удалось проверить службы начатого обновления') from None


def inspect_started_pointer_group(**options):
    """Return only observations, without exposing an action-capable group."""
    return _inspect_started_pointer_group(**options)[0]
