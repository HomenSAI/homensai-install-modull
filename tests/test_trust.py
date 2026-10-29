"""Отказ до исполнения при подмене манифеста, runtime, подписи или дерева."""
import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from homensai_install.errors import InstallerError
from homensai_install.trust import SignedPayloadVerifier, canonical_bytes, hash_manifest, load_trust_config


@pytest.fixture
def signed(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    payload = tmp_path / 'payload'
    payload.mkdir()
    (payload / 'runner').write_bytes(b'no execution in verifier test')
    manifest = {'product_id': 'reference', 'hooks': [['runner']]}
    target = {'os': 'linux', 'arch': 'x86_64', 'mode': 'native', 'scope': 'user'}
    inventory = {'schema_version': 1, 'product_id': 'reference', 'release': '1.0.0',
                 'target': {k: target[k] for k in ('os', 'arch', 'mode')},
                 'files': {'runner': hashlib.sha256((payload / 'runner').read_bytes()).hexdigest()},
                 'development': True, 'manifest_sha256': hash_manifest(manifest),
                 'runtime_executable': 'runner'}
    signature = {'algorithm': 'ed25519', 'key_id': 'test',
                 'signature': base64.b64encode(private.sign(canonical_bytes(inventory))).decode()}
    inv, sig = tmp_path / 'inventory.json', tmp_path / 'signature.json'
    inv.write_bytes(canonical_bytes(inventory))
    sig.write_bytes(canonical_bytes(signature))
    options = {'payload_dir': str(payload), 'inventory_file': str(inv),
               'signature_file': str(sig), 'release': '1.0.0', 'runtime_executable': 'runner'}
    return SignedPayloadVerifier({'test': public}, allow_test_keys=True), manifest, target, options, private, inventory, signature


def test_valid_tree(signed):
    verifier, manifest, target, options, *_ = signed
    assert set(verifier(manifest, target, options)) == {'runner'}


@pytest.mark.parametrize('change', ['manifest', 'runtime', 'target', 'release', 'bytes', 'extra', 'signature'])
def test_tampering_rejected_before_execution(signed, change):
    verifier, manifest, target, options, _, _, signature = signed
    from pathlib import Path
    if change == 'manifest': manifest['hooks'] = [['runner', '-c', 'untrusted']]
    elif change == 'runtime': options['runtime_executable'] = 'different'
    elif change == 'target': target['os'] = 'windows'
    elif change == 'release': options['release'] = '0.0.1'
    elif change == 'bytes': (Path(options['payload_dir']) / 'runner').write_bytes(b'changed')
    elif change == 'extra': (Path(options['payload_dir']) / 'extra').write_bytes(b'changed')
    elif change == 'signature':
        signature['signature'] = base64.b64encode(bytes(64)).decode()
        Path(options['signature_file']).write_bytes(canonical_bytes(signature))
    with pytest.raises(InstallerError) as exc:
        verifier(manifest, target, options)
    assert exc.value.code == 'INTEGRITY_FAILED'
    for language in ('ru', 'en', 'de'):
        envelope = exc.value.to_dict(language)
        assert envelope['code'] == 'INTEGRITY_FAILED'
        assert envelope['cause_status'] == 'confirmed'
        assert envelope['evidence'][0]['result'] == 'failed'
        assert envelope['locale'] == language
        json.dumps(envelope, allow_nan=False)


def test_development_forbidden_by_default(signed):
    verifier, manifest, target, options, *_ = signed
    with pytest.raises(InstallerError):
        SignedPayloadVerifier(verifier._keys)(manifest, target, options)


def test_signature_does_not_supply_trust_root(signed):
    verifier, manifest, target, options, *_ = signed
    other = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    with pytest.raises(InstallerError):
        SignedPayloadVerifier({'test': other}, allow_test_keys=True)(manifest, target, options)


def test_public_config_only_and_duplicate_keys(tmp_path, signed):
    verifier, *_ = signed
    config = tmp_path / 'trust.json'
    config.write_text(json.dumps({'schema_version': 1, 'keys': {'test': base64.b64encode(verifier._keys['test']).decode()}}))
    assert isinstance(load_trust_config(config, allow_development=True), SignedPayloadVerifier)
    config.write_text('{"schema_version":1,"schema_version":1,"keys":{}}')
    with pytest.raises(InstallerError): load_trust_config(config)
