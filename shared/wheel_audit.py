"""Ограниченная структурная проверка wheels внутри подписанного пакета."""
from __future__ import annotations

import hmac
import io
import json
import zipfile

from shared.archive import _open_regular
from shared.crypto_wheelhouse import _validate_wheel
from shared.package import _sha256
from shared.package_signing import verify_signed_package

MAX_WHEELS = 128
MAX_WHEEL_BYTES = 64 * 1024 * 1024
MAX_TOTAL_WHEEL_BYTES = 256 * 1024 * 1024


class WheelAuditError(ValueError):
    """Проверка не подтверждена; сообщение не раскрывает содержимое пакета."""


def audit_signed_wheels(package_path, envelope, *, trusted_public_key, expected_product_id, expected_version):
    """Проверить каждый .whl в подписанном ZIP, без извлечения и исполнения."""
    try:
        metadata = verify_signed_package(package_path, envelope, trusted_public_key=trusted_public_key,
            expected_product_id=expected_product_id, expected_version=expected_version)
        expected_digest = json.loads(envelope.decode('utf-8'))['sha256']
        wheels = [entry for entry in metadata['files'] if entry['path'].lower().endswith('.whl')]
        if (len(wheels) > MAX_WHEELS or any(entry['size'] > MAX_WHEEL_BYTES for entry in wheels)
                or sum(entry['size'] for entry in wheels) > MAX_TOTAL_WHEEL_BYTES):
            raise WheelAuditError('Превышены ограничения проверки wheel')
        with _open_regular(package_path) as incoming:
            if not hmac.compare_digest(_sha256(incoming), expected_digest):
                raise WheelAuditError('Пакет изменился после проверки')
            with zipfile.ZipFile(incoming) as outer:
                for entry in wheels:
                    with outer.open('payload/' + entry['path']) as member:
                        data = member.read(MAX_WHEEL_BYTES + 1)
                    if len(data) != entry['size'] or len(data) > MAX_WHEEL_BYTES:
                        raise WheelAuditError('Размер wheel не подтверждён')
                    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
                        _validate_wheel(wheel)
            if not hmac.compare_digest(_sha256(incoming), expected_digest):
                raise WheelAuditError('Пакет изменился во время проверки')
        return {'product_id': metadata['product_id'], 'version': metadata['version'],
                'package_signature_verified': True, 'wheel_count': len(wheels),
                'wheel_paths_checked': True, 'malicious_code_scanned': False,
                'wheel_format_completeness_verified': False, 'publisher_trust_verified': False,
                'execution_authorized': False}
    except WheelAuditError:
        raise
    except Exception:
        raise WheelAuditError('Не удалось проверить структуру wheels') from None
