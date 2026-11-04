"""Веб-страница сборщика: перечни, проверка запросов, команды shared.kit и скачивание."""
import json
import time

import pytest

from shared import kit_builder_web
from shared.kit_builder_web import BuilderWeb
from shared.web_ui import Download, HttpError


@pytest.fixture
def builder(tmp_path):
    projects, keys, output = tmp_path / 'projects', tmp_path / 'keys', tmp_path / 'output'
    for path in (projects, keys, output):
        path.mkdir()
    (projects / 'fresh').mkdir()
    (projects / '.hidden').mkdir()
    (projects / 'broken').mkdir()
    (projects / 'broken/homensai-kit.json').write_text('{"manifest": "installer/manifest.json"}')
    (projects / 'broken/installer').mkdir()
    (projects / 'broken/installer/manifest.json').write_text(
        json.dumps({'product_id': 'broken-app', 'product_name': 'Broken'}))
    kit = output / 'demo-1'
    kit.mkdir()
    (kit / 'kit.json').write_text(json.dumps({'product_id': 'demo', 'version': '1', 'public_key_sha256': 'a' * 64,
                                              'targets': ['linux-x86_64']}))
    (output / 'demo-1.tar.gz').write_bytes(b'synthetic archive')
    return BuilderWeb(projects=projects, keys=keys, output=output)


def wait(web):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = web.progress({}, None)
        if not job['running']:
            return job
        time.sleep(0.05)
    raise AssertionError('команда не завершилась')


def test_overview_lists_projects_keys_and_kits(builder):
    overview = builder.overview({}, None)
    projects = {item['id']: item for item in overview['projects']}
    assert set(projects) == {'fresh', 'broken'}
    assert projects['fresh'] == {'id': 'fresh', 'configured': False}
    assert projects['broken']['configured'] and not projects['broken']['ready']
    assert projects['broken']['product_id'] == 'broken-app' and projects['broken']['problem']
    assert overview['keys'] == []
    assert overview['kits'][0]['name'] == 'demo-1' and overview['kits'][0]['archive'] == 'demo-1.tar.gz'


def test_keygen_with_passphrase_runs_shared_kit(builder):
    builder.keygen({}, {'name': 'publisher', 'passphrase': 'synthetic-key-passphrase'})
    job = wait(builder)
    assert job['code'] == 0, job['lines']
    assert 'synthetic-key-passphrase' not in json.dumps(job)
    keys = builder.overview({}, None)['keys']
    assert keys == [{'name': 'publisher', 'encrypted': True, 'public_key_sha256': keys[0]['public_key_sha256']}]
    assert len(keys[0]['public_key_sha256']) == 64
    with pytest.raises(HttpError) as exists:
        builder.keygen({}, {'name': 'publisher'})
    assert exists.value.code == 'exists'


def test_build_requires_listed_project_and_key(builder):
    builder.keygen({}, {'name': 'locked', 'passphrase': 'synthetic-key-passphrase'})
    wait(builder)
    cases = [
        ({'project': '../etc', 'version': '1', 'key': 'locked'}, 'unknown:project'),
        ({'project': 'fresh', 'version': '1', 'key': 'locked'}, 'not_configured'),
        ({'project': 'broken', 'version': '1', 'key': '../x'}, 'unknown:key'),
        ({'project': 'broken', 'version': '../1', 'key': 'locked'}, 'field:version'),
        ({'project': 'broken', 'version': '1', 'sequence': '1.x', 'key': 'locked'}, 'field:sequence'),
        ({'project': 'broken', 'version': '1', 'key': 'locked'}, 'field:passphrase'),
    ]
    for body, code in cases:
        with pytest.raises(HttpError) as refused:
            builder.build({}, body)
        assert refused.value.code == code
    assert not builder.job.running


def test_build_of_unready_project_reports_error(builder):
    builder.keygen({}, {'name': 'plain'})
    wait(builder)
    builder.build({}, {'project': 'broken', 'version': '1.0.0', 'key': 'plain'})
    job = wait(builder)
    assert job['code'] == 1 and job['title'] == 'build broken-app 1.0.0'
    assert any('Ошибка' in line for line in job['lines'])


def test_only_one_job_at_a_time(builder, monkeypatch):
    builder.job.running = True
    with pytest.raises(HttpError) as busy:
        builder.keygen({}, {'name': 'second'})
    assert busy.value.code == 'busy'


def test_init_connects_free_directory(builder):
    with pytest.raises(HttpError) as refused:
        builder.init({}, {'project': 'broken', 'product_id': 'x', 'product_name': 'X'})
    assert refused.value.code == 'exists'
    with pytest.raises(HttpError) as refused:
        builder.init({}, {'project': 'fresh', 'product_id': 'Bad Id', 'product_name': 'X'})
    assert refused.value.code == 'field:product_id'
    builder.init({}, {'project': 'fresh', 'product_id': 'fresh-app', 'product_name': 'Fresh', 'port': 8811})
    assert wait(builder)['code'] == 0
    project = next(item for item in builder.overview({}, None)['projects'] if item['id'] == 'fresh')
    assert project['configured'] and project['product_id'] == 'fresh-app'


def test_download_and_checksum_only_listed_archives(builder):
    download = builder.download({'name': 'demo-1.tar.gz'}, None)
    assert isinstance(download, Download) and download.path.read_bytes() == b'synthetic archive'
    import hashlib
    assert builder.checksum({'name': 'demo-1.tar.gz'}, None)['sha256'] == hashlib.sha256(b'synthetic archive').hexdigest()
    for name in ('../keys/x', 'demo-1', 'demo-1/kit.json', ''):
        with pytest.raises(HttpError):
            builder.download({'name': name}, None)


def test_verify_takes_kit_from_listing(builder):
    with pytest.raises(HttpError) as refused:
        builder.verify({}, {'kit': '../demo-1'})
    assert refused.value.code == 'unknown:kit'
    builder.verify({}, {'kit': 'demo-1', 'expect_key_sha256': 'a' * 64})
    assert wait(builder)['code'] == 1


def test_main_rejects_weak_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(kit_builder_web.TOKEN_ENV, 'short')
    assert kit_builder_web.main(['--projects', str(tmp_path), '--keys', str(tmp_path),
                                 '--output', str(tmp_path)]) == 2


def test_builder_image_files_are_consistent():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / 'docker/builder/Dockerfile').read_text()
    assert '@sha256:' in dockerfile and '--require-hashes' in dockerfile and 'USER builder' in dockerfile
    compose = (root / 'docker/builder/compose.yml').read_text()
    assert '"127.0.0.1:${HOMENSAI_BUILDER_PORT:-8790}:8790"' in compose
    ignore = (root / '.dockerignore').read_text().split()
    assert ignore[ignore.index('*'):][:4] == ['*', '!shared', '!homensai_installer_client', '!requirements-crypto.txt']
