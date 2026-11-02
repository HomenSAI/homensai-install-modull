"""Bounded HTTP health check for an already confirmed signed Compose instance."""
from __future__ import annotations

from shared.docker_lifecycle import DockerLifecycleError, control_signed_compose
from shared.health import check_local_health
from shared.package_signing import verify_signed_package


def check_signed_compose_health(package_path: str, envelope: bytes, *, trusted_public_key: bytes,
                                expected_product_id: str, expected_version: str, target: dict,
                                modes: tuple[str, ...], instance_id: str, work_dir: str,
                                preparation_id: str, action_id: str, executable: str,
                                executable_sha256: str, socket_path: str, timeout_seconds: int,
                                health_timeout_seconds: int) -> dict:
    """Check the signed endpoint between two read-only observations of the running set.

    The response can be impersonated by another local process. This function
    does not start, stop, install, or attest the health of every component.
    """
    try:
        if type(health_timeout_seconds) is not int or not 1 <= health_timeout_seconds <= 30:
            raise DockerLifecycleError('Недопустимый таймаут проверки')
        common = {'trusted_public_key': trusted_public_key, 'expected_product_id': expected_product_id,
                  'expected_version': expected_version, 'target': target, 'modes': modes,
                  'instance_id': instance_id, 'work_dir': work_dir, 'preparation_id': preparation_id,
                  'action_id': action_id, 'action': 'start', 'executable': executable,
                  'executable_sha256': executable_sha256, 'socket_path': socket_path,
                  'timeout_seconds': timeout_seconds, 'verify_only': True}
        before = control_signed_compose(package_path, envelope, **common)
        options = {'trusted_public_key': trusted_public_key, 'expected_product_id': expected_product_id,
                   'expected_version': expected_version}
        metadata = verify_signed_package(package_path, envelope, **options)
        web = metadata['manifest']['web']
        check_local_health(web['port'], product_id=expected_product_id, version=expected_version,
                           path=web['healthz'], timeout=health_timeout_seconds)
        if (verify_signed_package(package_path, envelope, **options) != metadata
                or control_signed_compose(package_path, envelope, **common) != before):
            raise DockerLifecycleError('Состояние изменилось')
        return {'health_response_matches': True, 'daemon_state_verified': True,
                'process_identity_verified': False, 'all_components_healthy': False,
                'installation_complete': False}
    except Exception:
        raise DockerLifecycleError('Не удалось подтвердить состояние продукта') from None
