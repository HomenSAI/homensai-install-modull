"""SIGKILL на настоящей ФС с вымышленным продуктом и синтетическими данными."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from test_e2e_fs import FakeEffects, layout
from shared.adapters_fs import FilesystemPorts
from shared.effect_receipt import read_effect_receipt
from shared.update_plan import UpdateIndeterminate, UpdatePlan


@pytest.mark.skipif(os.name != 'posix', reason='Проверка SIGKILL предназначена для POSIX')
def test_killed_process_recovers_encrypted_archive_and_resumes_update(layout):
    pytest.importorskip('cryptography')
    manifest, root, home, work, make_ports = layout
    original = make_ports()
    payloads = {version: {'directory': str(source.directory), 'sha256': dict(source.sha256)}
                for version, source in original.payloads.items()}
    request = {'manifest': manifest, 'root': str(root), 'work': str(work), 'payloads': payloads}
    script = r'''
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_e2e_fs import FakeEffects
import shared.adapters_fs as fs
from shared.datahome import resolve_datahome
from shared.update_plan import UpdatePlan
request = json.load(sys.stdin)
manifest = request['manifest']
root, work = Path(request['root']), Path(request['work'])
payloads = {version: fs.ReleaseSource(Path(value['directory']), value['sha256'])
            for version, value in request['payloads'].items()}
ports = fs.FilesystemPorts(manifest, root, work_dir=work, payloads=payloads,
    effects=FakeEffects(work, resolve_datahome(manifest, root)), password_provider=lambda: 'test-password')
plan = UpdatePlan(manifest, root, ports, release='2.0', modes=('local',), guard_effects=True)
write = fs._atomic_write
def interrupted(path, value):
    if str(path).endswith('.archive.json') and value.get('status') == 'done':
        print(value['sha256'], flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    write(path, value)
fs._atomic_write = interrupted
plan.run()
'''
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps(request), text=True,
                            capture_output=True, timeout=120, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == -signal.SIGKILL, result.stderr
    digest = result.stdout.strip()
    assert len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
    ports = FilesystemPorts(manifest, root, work_dir=work, payloads=original.payloads,
                            effects=FakeEffects(work, home), password_provider=lambda: 'test-password')
    plan = UpdatePlan(manifest, root, ports, release='2.0', modes=('local',), guard_effects=True)
    state = plan.status()
    assert state['indeterminate'] is True and state['index'] == plan.forward.index('archive')
    key = state['transaction'] + '/archive'
    path = ports.archive_path(key)
    snapshot = path.read_bytes()
    assert read_effect_receipt(path.with_suffix('.archive.json'))['status'] == 'pending'
    with pytest.raises(UpdateIndeterminate):
        plan.run()
    assert path.read_bytes() == snapshot
    ports.recover_archive_binding(plan, key, digest)
    assert plan.status() == state and path.read_bytes() == snapshot
    assert plan.run(resume_indeterminate=True)['outcome'] == 'updated'
    assert ports.current_release() == '2.0'
    assert (home.roots['data'] / 'database.bin').read_bytes() == b'schema-2 + new records'
    assert path.read_bytes() == snapshot
