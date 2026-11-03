"""Explicit release-time OSV lookup for a restricted, hash-pinned pip lock.

This is not run by the installer. The result is not a malware scan, a complete
dependency inventory, or proof that a release is safe.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

ENDPOINT = 'https://api.osv.dev/v1/querybatch'
MAX_LOCK = 128 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_PACKAGES = 128
MAX_PAGES = 20
_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?\Z')
_VERSION = re.compile(r'[0-9]+(?:\.[0-9]+){0,7}\Z')
_HASH = re.compile(r'--hash=sha256:[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


class AuditError(ValueError):
    """Incomplete or invalid input or response; never treat as a clean result."""


def read_locks(paths):
    """Accept only exact pinned versions and SHA-256 hashes; reject omissions."""
    packages, sources = {}, []
    for path in paths:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_LOCK + 1)
        if len(raw) > MAX_LOCK:
            raise AuditError('lock too large')
        sources.append({'file': str(path), 'sha256': hashlib.sha256(raw).hexdigest()})
        pending = ''
        for line in raw.decode('utf-8').splitlines():
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            pending += ' ' + line.rstrip('\\').strip()
            if line.endswith('\\'):
                continue
            parts = pending.split()
            pending = ''
            if len(parts) == 1 and parts[0] in ('--require-hashes', '--only-binary=:all:'):
                continue
            if not parts or parts[0].count('==') != 1:
                raise AuditError('unsupported lock syntax')
            name, version = parts[0].split('==')
            if (not _NAME.fullmatch(name) or not _VERSION.fullmatch(version) or len(parts) < 2
                    or any(not _HASH.fullmatch(part) for part in parts[1:])):
                raise AuditError('lock must contain exact versions and hashes')
            name = re.sub(r'[-_.]+', '-', name).lower()
            if name in packages:
                raise AuditError('duplicate dependency')
            packages[name] = version
            if len(packages) > MAX_PACKAGES:
                raise AuditError('too many dependencies')
        if pending:
            raise AuditError('unfinished lock line')
    if not packages:
        raise AuditError('empty dependency inventory')
    return packages, sources


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuditError('redirect refused')


def _send(queries):
    request = urllib.request.Request(ENDPOINT, data=json.dumps({'queries': queries}).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise AuditError('OSV response too large')
    return json.loads(data, object_pairs_hook=_unique)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError('duplicate response key')
        result[key] = value
    return result


def _crypto_block(path, packages, sources):
    from shared import crypto_wheelhouse

    checked = crypto_wheelhouse.verify_wheelhouse(path)
    if (any(packages.get(name) != version for name, version in crypto_wheelhouse.LOCKED_VERSIONS.items())
            or not any(source['sha256'] == checked['lock_files']['linux']['sha256'] for source in sources)
            or checked['lock_files']['linux']['sha256'] != checked['lock_files']['windows']['sha256']):
        raise AuditError('wheelhouse and queried lock do not match')
    return checked


def audit(paths, *, send=_send, crypto_wheelhouse=None):
    paths = tuple(paths)
    packages, sources = read_locks(paths)
    block = _crypto_block(crypto_wheelhouse, packages, sources) if crypto_wheelhouse is not None else None
    pending = [(name, version, None) for name, version in sorted(packages.items())]
    findings = {name: {} for name in packages}
    tokens_seen = set()
    for _ in range(MAX_PAGES):
        queries = []
        for name, version, token in pending:
            query = {'package': {'name': name, 'ecosystem': 'PyPI'}, 'version': version}
            if token is not None:
                query['page_token'] = token
            queries.append(query)
        response = send(queries)
        if (type(response) is not dict or set(response) != {'results'}
                or type(response['results']) is not list or len(response['results']) != len(pending)):
            raise AuditError('incomplete OSV response')
        following = []
        for (name, version, _), result in zip(pending, response['results']):
            if (type(result) is not dict or set(result) - {'vulns', 'next_page_token'}
                    or type(result.get('vulns', [])) is not list):
                raise AuditError('invalid OSV result')
            for item in result.get('vulns', []):
                if (type(item) is not dict or set(item) != {'id', 'modified'}
                        or type(item['id']) is not str or not _ID.fullmatch(item['id'])
                        or type(item['modified']) is not str or len(item['modified']) > 64):
                    raise AuditError('invalid OSV advisory')
                try:
                    modified = datetime.fromisoformat(item['modified'].replace('Z', '+00:00'))
                except ValueError:
                    raise AuditError('invalid OSV timestamp') from None
                if modified.tzinfo is None or modified.utcoffset() is None:
                    raise AuditError('invalid OSV timestamp')
                if item['id'] in findings[name] and findings[name][item['id']] != item['modified']:
                    raise AuditError('conflicting OSV advisory')
                findings[name][item['id']] = item['modified']
            if 'next_page_token' in result:
                token = result['next_page_token']
                if type(token) is not str or not 0 < len(token) <= 4096 or (name, token) in tokens_seen:
                    raise AuditError('invalid OSV pagination')
                tokens_seen.add((name, token))
                following.append((name, version, token))
        pending = following
        if not pending:
            break
    if pending:
        raise AuditError('OSV pagination limit exceeded')
    if read_locks(paths) != (packages, sources):
        raise AuditError('lock changed during OSV check')
    if block is not None and _crypto_block(crypto_wheelhouse, packages, sources) != block:
        raise AuditError('wheelhouse changed during OSV check')
    return {'source': ENDPOINT, 'checked_at': datetime.now(timezone.utc).isoformat(), 'locks': sources,
            'packages': [{'name': name, 'version': packages[name],
                          'advisories': [{'id': key, 'modified': value} for key, value in sorted(findings[name].items())]}
                         for name in sorted(packages)],
            'crypto_wheelhouse': block, 'complete_dependency_inventory': False,
            'publisher_provenance_verified': False, 'malware_scanned': False, 'release_safe': False}


def verify_clean_evidence(raw, *, expected_sha256, locks, max_age_seconds, now=None):
    """Verify bounded OSV evidence against an independently trusted digest.

    The caller must authenticate the digest and the original audit process.
    This checks neither unlisted dependencies nor malware or publisher identity;
    it does not prove a release safe and never downloads or executes code.
    """
    try:
        if (type(raw) is not bytes or not raw or len(raw) > MAX_RESPONSE
                or type(expected_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', expected_sha256) is None
                or type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 31 * 24 * 3600):
            raise AuditError('invalid evidence parameters')
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
            raise AuditError('evidence digest mismatch')
        report = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(AuditError('invalid JSON number')))
        keys = {'source', 'checked_at', 'locks', 'packages', 'crypto_wheelhouse',
                'complete_dependency_inventory', 'publisher_provenance_verified', 'malware_scanned', 'release_safe'}
        if (type(report) is not dict or set(report) != keys or report['source'] != ENDPOINT
                or (report['crypto_wheelhouse'] is not None and type(report['crypto_wheelhouse']) is not dict)
                or any(report[name] is not False for name in ('complete_dependency_inventory',
                    'publisher_provenance_verified', 'malware_scanned', 'release_safe'))
                or type(report['checked_at']) is not str or len(report['checked_at']) > 64):
            raise AuditError('invalid evidence')
        checked_at = datetime.fromisoformat(report['checked_at'].replace('Z', '+00:00'))
        current = datetime.now(timezone.utc) if now is None else now
        if (type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None
                or checked_at.tzinfo is None or checked_at.utcoffset() is None):
            raise AuditError('evidence time has no timezone')
        age = (current - checked_at).total_seconds()
        if not 0 <= age <= max_age_seconds:
            raise AuditError('evidence is stale or from the future')
        paths = tuple(locks)
        packages, sources = read_locks(paths)
        if report['locks'] != sources or type(report['packages']) is not list:
            raise AuditError('evidence lock mismatch')
        expected_packages = [{'name': name, 'version': packages[name], 'advisories': []}
                             for name in sorted(packages)]
        if report['packages'] != expected_packages or read_locks(paths) != (packages, sources):
            raise AuditError('findings, incomplete inventory or changed lock')
        return {'checked_at': report['checked_at'], 'package_count': len(packages),
                'evidence_sha256': expected_sha256, 'complete_dependency_inventory': False,
                'publisher_provenance_verified': False, 'malware_scanned': False, 'release_safe': False}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        raise AuditError('could not verify OSV evidence') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--crypto-wheelhouse', help='Optional prepared crypto block to verify before and after lookup')
    parser.add_argument('--verify-evidence', help='Verify a previously saved OSV report without network')
    parser.add_argument('--evidence-sha256', help='Independently trusted SHA-256 of the saved report')
    parser.add_argument('--max-age-seconds', type=int, help='Maximum evidence age in seconds (up to 31 days)')
    parser.add_argument('locks', nargs='+', help='Explicit hash-pinned pip lock files (names are sent to OSV)')
    args = parser.parse_args()
    if args.verify_evidence is not None:
        if args.crypto_wheelhouse is not None or args.evidence_sha256 is None or args.max_age_seconds is None:
            parser.error('offline evidence verification needs its digest and maximum age, without --crypto-wheelhouse')
    elif args.evidence_sha256 is not None or args.max_age_seconds is not None:
        parser.error('evidence digest and age require --verify-evidence')
    try:
        if args.verify_evidence is not None:
            with Path(args.verify_evidence).open('rb') as source:
                raw = source.read(MAX_RESPONSE + 1)
            report = verify_clean_evidence(raw, expected_sha256=args.evidence_sha256,
                                           locks=args.locks, max_age_seconds=args.max_age_seconds)
        else:
            report = audit(args.locks, crypto_wheelhouse=args.crypto_wheelhouse)
    except (AuditError, OSError, UnicodeError, ValueError, RecursionError, urllib.error.URLError):
        print('OSV check failed or is incomplete', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    if args.verify_evidence is not None:
        return 0
    return 1 if any(item['advisories'] for item in report['packages']) else 0


if __name__ == '__main__':
    sys.exit(main())
