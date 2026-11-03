"""Link signed release contents to externally trusted, fresh OSV evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from shared.archive import _safe_name
from shared.datahome import checked_path
from shared.osv_audit import AuditError, verify_clean_evidence
from shared.package import _files
from shared.package_signing import verify_signed_package


def verify_signed_crypto_evidence(*, package, envelope, trusted_public_key, product_id, version,
                                  crypto_wheelhouse, package_prefix, evidence, evidence_sha256,
                                  locks, max_age_seconds):
    """Check the exact signed crypto subtree against a clean, fresh OSV report.

    The caller independently trusts the public key, report digest, audit origin,
    clock, lock inputs and local code. This neither scans for malware nor checks
    all package dependencies, and it does not install or extract the release.
    """
    try:
        if type(package_prefix) is not str or _safe_name(package_prefix) != package_prefix:
            raise AuditError('invalid crypto prefix')
        root = checked_path(crypto_wheelhouse)
        paths = tuple(locks)
        options = dict(trusted_public_key=trusted_public_key, expected_product_id=product_id,
                       expected_version=version)
        def inspect():
            checked = verify_clean_evidence(evidence, expected_sha256=evidence_sha256,
                locks=paths, max_age_seconds=max_age_seconds, crypto_wheelhouse=root)
            contents, signatures = _files(root)
            metadata = verify_signed_package(package, envelope, **options)
            expected = [{**item, 'path': package_prefix + '/' + item['path']} for item in contents]
            packaged = [item for item in metadata['files'] if item['path'].startswith(package_prefix + '/')]
            markers = [item['path'] for item in metadata['files']
                       if item['path'].rsplit('/', 1)[-1].casefold() == 'crypto-wheelhouse.json']
            if packaged != expected or markers != [package_prefix + '/crypto-wheelhouse.json']:
                raise AuditError('signed crypto subtree differs from audited block')
            return checked, contents, signatures, metadata
        first = inspect()
        if inspect() != first:
            raise AuditError('release evidence changed during verification')
        return {'product_id': product_id, 'version': version, 'signed_package_verified': True,
                'crypto_block_matches_package': True, 'osv_checked_at': first[0]['checked_at'],
                'package_count': first[0]['package_count'], 'complete_dependency_inventory': False,
                'publisher_provenance_verified': False, 'malware_scanned': False, 'release_safe': False}
    except Exception:
        raise AuditError('could not verify signed release evidence') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True)
    parser.add_argument('--envelope', required=True)
    parser.add_argument('--trusted-public-key-hex', required=True)
    parser.add_argument('--product', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--crypto-wheelhouse', required=True)
    parser.add_argument('--package-prefix', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--evidence-sha256', required=True)
    parser.add_argument('--max-age-seconds', required=True, type=int)
    parser.add_argument('locks', nargs='+')
    args = parser.parse_args(argv)
    try:
        from shared.osv_audit import MAX_RESPONSE
        from shared.package_signing import MAX_ENVELOPE
        if re.fullmatch(r'[a-f0-9]{64}', args.trusted_public_key_hex) is None:
            raise AuditError('invalid public key')
        with Path(args.envelope).open('rb') as stream:
            envelope = stream.read(MAX_ENVELOPE + 1)
        with Path(args.evidence).open('rb') as stream:
            evidence = stream.read(MAX_RESPONSE + 1)
        result = verify_signed_crypto_evidence(package=args.package, envelope=envelope,
            trusted_public_key=bytes.fromhex(args.trusted_public_key_hex), product_id=args.product,
            version=args.version, crypto_wheelhouse=args.crypto_wheelhouse, package_prefix=args.package_prefix,
            evidence=evidence, evidence_sha256=args.evidence_sha256, locks=args.locks,
            max_age_seconds=args.max_age_seconds)
    except (AuditError, OSError, UnicodeError, ValueError, RecursionError):
        print('Signed release evidence check failed or is incomplete', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
