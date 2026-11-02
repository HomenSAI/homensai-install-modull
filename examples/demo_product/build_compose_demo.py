"""Create a signed synthetic Compose example. Does not invoke Docker or start services."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from shared.api import handle
from shared.manifest import load_manifest
from shared.package import build_package
from shared.package_signing import sign_package
from shared.preflight import NAMES


def build(output: Path, image: str, port: int) -> dict:
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    output.mkdir(mode=0o700)  # Never overwrite an existing directory.
    output = output.resolve(strict=True)
    payload = output / 'payload'
    recipe_path = payload / 'homensai/docker/recipe-v1.json'
    recipe_path.parent.mkdir(parents=True)
    work = output / 'work'
    work.mkdir(mode=0o700)
    manifest = load_manifest(Path(__file__).resolve().parents[1] / 'rent.manifest.json')
    manifest['product_id'] = 'homensai-test-demo'
    manifest['product_name'] = 'HomenS.AI Synthetic Demo'
    manifest['paths'] = {name: name for name in NAMES}
    manifest['compatibility'] = {'targets': [{'os': 'linux', 'architecture': 'x86_64', 'execution': 'docker'}]}
    manifest['secrets'] = []
    manifest['hooks'] = {name: [] for name in manifest['hooks']}
    manifest['web']['port'] = port
    manifest['web']['healthz'] = '/health'
    # The demo does not install a native runtime; these are synthetic fixtures.
    artifacts = [*manifest['runtime']['lock_files'].values(), *manifest['runtime']['vendor_wheels']]
    for index, artifact in enumerate(artifacts):
        data = ('fictional test artifact ' + str(index)).encode()
        destination = payload / artifact['file']
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        artifact['sha256'] = hashlib.sha256(data).hexdigest()
    for component in manifest['components']:
        command = ['python', '-B', '/opt/demo/main.py']
        if component['name'] == 'web':
            command += ['serve', '--data-dir', '/data/web', '--port', '8080', '--listen', '0.0.0.0']
        else:
            command += ['worker', '--data-dir', '/data/' + component['name']]
        component['cmd'] = command
    recipe = {'format': 1, 'images': {item['name']: image for item in manifest['components']},
              'web_component': 'web', 'container_port': 8080}
    recipe_path.write_text(json.dumps(recipe), encoding='utf-8')
    package = output / 'package.zip'
    build_package(manifest, '1', payload, package)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    envelope = sign_package(package, private_key=key, expected_product_id=manifest['product_id'],
                            expected_version='1')
    instance = uuid.uuid4().hex
    request = {'api_major': 1, 'request_id': 'synthetic-compose-demo', 'operation': 'prepare-signed-compose',
               'params': {'package_path': str(package), 'envelope': base64.b64encode(envelope).decode('ascii'),
                          'trusted_public_key': base64.b64encode(public).decode('ascii'),
                          'expected_product_id': manifest['product_id'], 'expected_version': '1',
                          'target': manifest['compatibility']['targets'][0], 'modes': ['autonomous'],
                          'instance_id': instance, 'work_dir': str(work), 'idempotency_key': uuid.uuid4().hex}}
    result = handle(request)
    if not result.get('ok'):
        raise RuntimeError('API could not prepare the synthetic plan: ' + json.dumps(result))
    (output / 'api-request.json').write_text(json.dumps(request, indent=2) + '\n', encoding='utf-8')
    return {'project': manifest['product_id'] + '-' + instance, 'port': port,
            'compose_file': str(work / result['result']['compose_name']), 'package': str(package),
            'api_request': str(output / 'api-request.json'), 'image': image}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='new directory in an existing parent')
    parser.add_argument('--image', required=True, help='local demo image reference pinned by sha256 digest')
    parser.add_argument('--port', type=int, required=True, help='available loopback TCP port')
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.image, args.port)))


if __name__ == '__main__':
    main()
