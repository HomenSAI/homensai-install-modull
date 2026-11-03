# HomenS.AI · © 2026 Serhii Khomenko
"""Перечень состава и происхождения подписанного выпуска, без сети и исполнения.

Записываются хэши пакета, подписи и ключа, все файлы payload, закреплённые
зависимости из lock-файлов и сведения wheels о версиях и лицензиях. Перечень
описывает то, что лежит в пакете; полноту зависимостей, юридическую оценку
лицензий, уязвимости и отсутствие вредоносного кода он не подтверждает.
"""
from __future__ import annotations

import argparse
from email.parser import BytesHeaderParser
import hashlib
import hmac
import io
import json
import os
import re
import sys
import zipfile

from shared.archive import _open_regular
from shared.crypto_wheelhouse import _validate_wheel
from shared.package import _sha256
from shared.package_signing import MAX_ENVELOPE, verify_signed_package
from shared.wheel_audit import MAX_TOTAL_WHEEL_BYTES, MAX_WHEEL_BYTES, MAX_WHEELS

FORMAT = 'homensai-release-inventory/1'
MAX_LOCK = 1024 * 1024
MAX_METADATA = 1024 * 1024
_REQUIREMENT = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]{0,127})==([A-Za-z0-9][A-Za-z0-9.+!_-]{0,63})\Z')
_HASH = re.compile(r'--hash=sha256:[a-f0-9]{64}\Z')
_LICENSE_NAME = re.compile(r'(LICEN[CS]E|COPYING|NOTICE|AUTHORS)([.-].*)?\Z', re.IGNORECASE)


class InventoryError(ValueError):
    """Перечень не построен; сообщение не раскрывает содержимое пакета."""


def _pinned(raw):
    """Пары name==version с хэшами; None, если lock не в строгом формате."""
    packages, pending = [], ''
    try:
        lines = raw.decode('utf-8').splitlines()
    except UnicodeError:
        return None
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        pending += ' ' + line.rstrip('\\').strip()
        if line.endswith('\\'):
            continue
        parts, pending = pending.split(), ''
        if parts in (['--require-hashes'], ['--only-binary=:all:']):
            continue
        match = _REQUIREMENT.fullmatch(parts[0])
        if match is None or len(parts) < 2 or any(_HASH.fullmatch(part) is None for part in parts[1:]):
            return None
        packages.append({'name': re.sub(r'[-_.]+', '-', match[1]).lower(), 'version': match[2],
                         'sha256': sorted(part.rpartition(':')[2] for part in parts[1:])})
    if pending or not packages or len({item['name'] for item in packages}) != len(packages):
        return None
    return sorted(packages, key=lambda item: item['name'])


def _wheel(data):
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        _validate_wheel(wheel)
        names = [info.filename for info in wheel.infolist() if not info.is_dir()]
        metadata = [name for name in names if re.fullmatch(r'[^/]+\.dist-info/METADATA', name)]
        result = {'name': None, 'version': None, 'license_expression': None, 'license': None,
                  'license_classifiers': [], 'license_files': []}
        if len(metadata) == 1:
            with wheel.open(metadata[0]) as stream:
                raw = stream.read(MAX_METADATA + 1)
            if len(raw) > MAX_METADATA:
                raise InventoryError('Метаданные wheel слишком велики')
            headers = BytesHeaderParser().parsebytes(raw)
            for key, field in (('name', 'Name'), ('version', 'Version'),
                               ('license_expression', 'License-Expression'), ('license', 'License')):
                value = headers.get(field)
                if value is not None:
                    # Длинный текст лицензии в поле License сокращается до первой строки.
                    result[key] = str(value).strip().splitlines()[0][:256] if str(value).strip() else None
            result['license_classifiers'] = sorted(
                str(value).strip() for value in headers.get_all('Classifier', [])
                if str(value).strip().startswith('License ::'))
        for name in sorted(names):
            parts = name.split('/')
            if parts[0].endswith('.dist-info') and (
                    (len(parts) > 2 and parts[1] == 'licenses') or _LICENSE_NAME.fullmatch(parts[-1])):
                result['license_files'].append({'path': name, 'sha256': hashlib.sha256(wheel.read(name)).hexdigest()})
        return result


def release_inventory(package_path, envelope, *, trusted_public_key, expected_product_id, expected_version):
    """Построить перечень по подписанному пакету; ничего не извлекает и не запускает."""
    try:
        metadata = verify_signed_package(package_path, envelope, trusted_public_key=trusted_public_key,
            expected_product_id=expected_product_id, expected_version=expected_version)
        digest = json.loads(envelope.decode('utf-8'))['sha256']
        manifest = metadata['manifest']
        entries = {item['path']: item for item in metadata['files']}
        wheels = [item for item in metadata['files'] if item['path'].lower().endswith('.whl')]
        if (len(wheels) > MAX_WHEELS or any(item['size'] > MAX_WHEEL_BYTES for item in wheels)
                or sum(item['size'] for item in wheels) > MAX_TOTAL_WHEEL_BYTES):
            raise InventoryError('Превышены ограничения перечня')
        locks, components = [], []
        with _open_regular(package_path) as incoming:
            if not hmac.compare_digest(_sha256(incoming), digest):
                raise InventoryError('Пакет изменился после проверки')
            with zipfile.ZipFile(incoming) as outer:
                def read(entry, limit):
                    with outer.open('payload/' + entry['path']) as member:
                        data = member.read(limit + 1)
                    if len(data) != entry['size'] or not hmac.compare_digest(
                            hashlib.sha256(data).hexdigest(), entry['sha256']):
                        raise InventoryError('Файл пакета не подтверждён')
                    return data
                if manifest['schema_version'] == 2:
                    for platform, item in sorted(manifest['runtime']['lock_files'].items()):
                        entry = entries[item['file']]
                        if entry['size'] > MAX_LOCK:
                            raise InventoryError('Lock-файл слишком велик')
                        locks.append({'platform': platform, 'file': item['file'], 'sha256': entry['sha256'],
                                      'pinned_packages': _pinned(read(entry, MAX_LOCK))})
                for entry in wheels:
                    components.append({'file': entry['path'], 'sha256': entry['sha256'], 'size': entry['size'],
                                       **_wheel(read(entry, MAX_WHEEL_BYTES))})
            if not hmac.compare_digest(_sha256(incoming), digest):
                raise InventoryError('Пакет изменился во время перечня')
        canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        result = {'format': FORMAT, 'product_id': metadata['product_id'], 'version': metadata['version'],
                  'package_sha256': digest, 'envelope_sha256': hashlib.sha256(envelope).hexdigest(),
                  'publisher_key_sha256': hashlib.sha256(trusted_public_key).hexdigest(),
                  'manifest_sha256': hashlib.sha256(canonical).hexdigest(),
                  'files': [{'path': item['path'], 'size': item['size'], 'sha256': item['sha256']}
                            for item in metadata['files']],
                  'runtime_python': manifest.get('runtime', {}).get('python'),
                  'lock_files': locks, 'wheels': components,
                  'package_signature_verified': True, 'dependency_completeness_verified': False,
                  'licenses_reviewed': False, 'vulnerabilities_checked': False,
                  'malicious_code_scanned': False, 'publisher_trust_verified': False}
        return result
    except InventoryError:
        raise
    except Exception:
        raise InventoryError('Не удалось построить перечень выпуска') from None


def _read(path, limit):
    with _open_regular(path) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise InventoryError('Файл слишком велик')
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Release inventory of a signed package', allow_abbrev=False)
    parser.add_argument('--package', required=True)
    parser.add_argument('--envelope-file', required=True)
    parser.add_argument('--public-key-file', required=True)
    parser.add_argument('--product-id', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        result = release_inventory(os.path.abspath(args.package), _read(args.envelope_file, MAX_ENVELOPE),
            trusted_public_key=_read(args.public_key_file, 32), expected_product_id=args.product_id,
            expected_version=args.version)
        body = (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
    except (InventoryError, OSError):
        print('Не удалось построить перечень выпуска', file=sys.stderr)
        return 1
    print(json.dumps({'package_sha256': result['package_sha256'], 'files': len(result['files']),
                      'wheels': len(result['wheels'])}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
