"""Bind a signed release's selected runtime lock to trusted offline OSV evidence."""
from __future__ import annotations

import hashlib
import json
import re
import sys

from shared.datahome import checked_path
from shared.interactive_install import _Parser, read_bounded_file
from shared.osv_audit import AuditError, MAX_LOCK, MAX_RESPONSE, verify_clean_evidence
from shared.package_signing import MAX_ENVELOPE, verify_signed_package


def verify_signed_runtime_lock(*, package, envelope, trusted_public_key, product_id, version,
                               platform, lock, evidence, evidence_sha256, max_age_seconds):
    """Verify one selected lock, not the complete dependency set or publisher identity."""
    try:
        if type(platform) is not str or platform not in ('linux', 'windows'):
            raise AuditError('unsupported runtime platform')
        lock_path = checked_path(lock)
        options = {'trusted_public_key': trusted_public_key, 'expected_product_id': product_id,
                   'expected_version': version}
        def observe():
            # The external report binds its own source filename and digest.
            report = verify_clean_evidence(evidence, expected_sha256=evidence_sha256,
                locks=(str(lock_path),), max_age_seconds=max_age_seconds)
            lock_bytes = read_bounded_file(lock_path, MAX_LOCK)
            metadata = verify_signed_package(package, envelope, **options)
            selected = metadata['manifest']['runtime']['lock_files'][platform]
            matches = [entry for entry in metadata['files'] if entry['path'] == selected['file']]
            digest = hashlib.sha256(lock_bytes).hexdigest()
            evidence_lock = json.loads(evidence.decode('utf-8'))['locks'][0]
            if (len(matches) != 1 or matches[0]['sha256'] != digest
                    or matches[0]['size'] != len(lock_bytes) or selected['sha256'] != digest
                    or evidence_lock['sha256'] != digest):
                raise AuditError('signed runtime lock differs from evidence')
            return report, metadata, digest
        first = observe()
        if observe() != first:
            raise AuditError('inputs changed during verification')
        return {'product_id': product_id, 'version': version, 'platform': platform,
                'signed_package_verified': True, 'selected_lock_matches_evidence': True,
                'selected_lock_sha256': first[2], 'osv_checked_at': first[0]['checked_at'],
                'package_count': first[0]['package_count'], 'complete_dependency_inventory': False,
                'publisher_provenance_verified': False, 'malware_scanned': False, 'release_safe': False}
    except Exception:
        raise AuditError('could not verify signed runtime lock evidence') from None


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--package', required=True)
    parser.add_argument('--envelope', required=True)
    parser.add_argument('--trusted-public-key-hex', required=True)
    parser.add_argument('--product', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--platform', choices=('linux', 'windows'), required=True)
    parser.add_argument('--lock', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--evidence-sha256', required=True)
    parser.add_argument('--max-age-seconds', required=True, type=int)
    try:
        args = parser.parse_args(argv)
        if re.fullmatch(r'[a-f0-9]{64}', args.trusted_public_key_hex) is None:
            raise ValueError('invalid public key')
        result = verify_signed_runtime_lock(package=args.package,
            envelope=read_bounded_file(args.envelope, MAX_ENVELOPE),
            trusted_public_key=bytes.fromhex(args.trusted_public_key_hex), product_id=args.product,
            version=args.version, platform=args.platform, lock=args.lock,
            evidence=read_bounded_file(args.evidence, MAX_RESPONSE), evidence_sha256=args.evidence_sha256,
            max_age_seconds=args.max_age_seconds)
    except (AuditError, OSError, ValueError, UnicodeError, RecursionError):
        print('Signed runtime evidence check failed or is incomplete', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
