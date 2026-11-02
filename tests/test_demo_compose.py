"""Synthetic signed demo preparation; never starts containers."""
import json
import base64
from pathlib import Path
import platform
import sys

import pytest

from examples.demo_product.build_compose_demo import build
from shared.api import handle
from shared.package_signing import verify_signed_package


pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux') or platform.machine().lower() != 'x86_64',
                                reason='Signed Compose preparation currently requires Linux x86-64')


def test_demo_prepares_signed_multi_service_plan_and_replays_api(tmp_path):
    image = 'localhost/synthetic@sha256:' + 'a' * 64
    output = tmp_path / 'new-demo'
    result = build(output, image, 43817)
    plan = json.loads(Path(result['compose_file']).read_text())
    assert plan['name'] == result['project']
    assert set(plan['services']) == {'web', 'bot', 'scheduler'}
    assert all(service['image'] == image for service in plan['services'].values())
    assert plan['services']['web']['ports'] == ['127.0.0.1:43817:8080']
    assert plan['services']['web']['command'][-2:] == ['--listen', '0.0.0.0']
    assert '/data/bot' in plan['services']['bot']['command']
    assert '/data/scheduler' in plan['services']['scheduler']['command']
    request = json.loads((output / 'api-request.json').read_text())
    params = request['params']
    metadata = verify_signed_package(params['package_path'], base64.b64decode(params['envelope']),
        trusted_public_key=base64.b64decode(params['trusted_public_key']),
        expected_product_id='homensai-test-demo', expected_version='1')
    assert metadata['manifest']['web']['healthz'] == '/health'
    assert handle(request)['result']['state'] == 'prepared'
    before = (output / 'package.zip').read_bytes()
    with pytest.raises(FileExistsError):
        build(output, image, 43817)
    assert (output / 'package.zip').read_bytes() == before


def test_demo_rejects_invalid_port_before_creating_directory(tmp_path):
    output = tmp_path / 'new-demo'
    with pytest.raises(ValueError):
        build(output, 'localhost/synthetic@sha256:' + 'a' * 64, 0)
    assert not output.exists()
