"""Explicit release-time OSV lookup for a restricted, hash-pinned pip lock.

This is not run by the installer. The result is not a malware scan, a complete
dependency inventory, or proof that a release is safe.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
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


def audit(paths, *, send=_send):
    packages, sources = read_locks(paths)
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
    return {'source': ENDPOINT, 'checked_at': datetime.now(timezone.utc).isoformat(), 'locks': sources,
            'packages': [{'name': name, 'version': packages[name],
                          'advisories': [{'id': key, 'modified': value} for key, value in sorted(findings[name].items())]}
                         for name in sorted(packages)],
            'complete_dependency_inventory': False, 'malware_scanned': False, 'release_safe': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('locks', nargs='+', help='Explicit hash-pinned pip lock files (names are sent to OSV)')
    args = parser.parse_args()
    try:
        report = audit(args.locks)
    except (AuditError, OSError, UnicodeError, ValueError, RecursionError, urllib.error.URLError):
        print('OSV check failed or is incomplete', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 1 if any(item['advisories'] for item in report['packages']) else 0


if __name__ == '__main__':
    sys.exit(main())
