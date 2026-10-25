"""Закреплённые HTTPS-артефакты, проверяемый кэш и безопасное продолжение.

Модуль не исполняет полученные файлы. ArtifactSpec и trusted_hosts должны
поступать из уже проверенного каталога/плана, а не из произвольного remote JSON.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import ssl
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .errors import InstallerError

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ETAG = re.compile(r'^"[\x21\x23-\x7e]{1,254}"$')
_CONTENT_RANGE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
_REDIRECTS = {301, 302, 303, 307, 308}


def _host(value: str) -> str:
    if not isinstance(value, str) or not value or any(ch in value for ch in "@/:*\\"):
        raise ValueError("invalid trusted host")
    try:
        normalized = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("invalid trusted host") from exc
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", normalized):
        raise ValueError("invalid trusted host")
    if normalized == "localhost" or normalized.endswith((".localhost", ".local", ".internal")):
        raise ValueError("local artifact sources are not HTTPS download hosts")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise ValueError("literal IP download hosts are not supported")
    return normalized


def _url(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(ch) <= 32 or ord(ch) == 127 for ch in value):
        raise ValueError("invalid artifact URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443):
            raise ValueError("artifact URL must be credential-free HTTPS on port 443")
        host = _host(parsed.hostname or "")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid artifact HTTPS URL") from exc
    return value, host


@dataclass(frozen=True)
class ArtifactSpec:
    artifact_id: str
    version: str
    url: str
    sha256: str
    size: int
    license_ref: str
    allowed_redirect_hosts: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.artifact_id, str) or not _ID.fullmatch(self.artifact_id):
            raise ValueError("invalid artifact identifier")
        if not isinstance(self.version, str) or not _ID.fullmatch(self.version) or self.version.lower() in {"latest", "main", "master", "nightly"}:
            raise ValueError("artifact version must be pinned")
        _url(self.url)
        if not isinstance(self.sha256, str) or not _SHA.fullmatch(self.sha256):
            raise ValueError("a pinned SHA-256 is required")
        if type(self.size) is not int or not 0 < self.size <= 64 * 1024**3:
            raise ValueError("a bounded exact artifact size is required")
        if not isinstance(self.license_ref, str) or not self.license_ref.strip() or len(self.license_ref) > 512 or any(ord(ch) < 32 for ch in self.license_ref):
            raise ValueError("a license reference is required")
        if not isinstance(self.allowed_redirect_hosts, (tuple, list)) or len(self.allowed_redirect_hosts) > 16:
            raise ValueError("invalid redirect host list")
        object.__setattr__(self, "allowed_redirect_hosts", tuple(_host(host) for host in self.allowed_redirect_hosts))

    @classmethod
    def from_dict(cls, value: dict):
        if not isinstance(value, dict) or set(value) - {"artifact_id", "version", "url", "sha256", "size", "license_ref", "allowed_redirect_hosts"}:
            raise ValueError("invalid artifact specification")
        return cls(**value)

    def public_metadata(self):
        # Query strings may contain temporary CDN access data. Never echo URLs.
        return {"artifact_id": self.artifact_id, "version": self.version, "sha256": self.sha256,
                "size": self.size, "license_ref": self.license_ref, "source_host": _url(self.url)[1]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """TLS verification uses the system trust store; redirects are handled above."""
    def __init__(self):
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def open(self, url: str, *, headers: dict, timeout: float):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as response:
            # HTTPError is a response with status/headers/read/close, including
            # redirects disabled above. No error body is exposed to logs.
            return response


class _RestartDownload(Exception):
    pass


def _private_dir(path: Path):
    for item in (path.absolute(), *path.absolute().parents):
        if item.is_symlink():
            raise InstallerError("INTEGRITY_FAILED", phase="download", details={"reason": "cache_symlink"})
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or (os.name != "nt" and
            (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022)):
        raise InstallerError("PERMISSION_REQUIRED", phase="download", details={"reason": "cache_directory_not_private"})


def _open(path: Path, flags: int):
    if path.is_symlink():
        raise InstallerError("INTEGRITY_FAILED", phase="download", details={"reason": "cache_file_symlink"})
    # Validate the opened inode before truncating it. A path precheck alone
    # cannot prevent a hardlink or special-file race from touching other data.
    descriptor = os.open(path, (flags & ~os.O_TRUNC) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise InstallerError("INTEGRITY_FAILED", phase="download", details={"reason": "cache_inode_not_regular_private_file"})
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR))
        if writing and os.name != "nt" and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022):
            raise InstallerError("PERMISSION_REQUIRED", phase="download", details={"reason": "cache_inode_not_private"})
        if flags & os.O_TRUNC:
            os.ftruncate(descriptor, 0)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _atomic_json(path: Path, value: dict):
    descriptor, temporary = tempfile.mkstemp(prefix=".download-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise InstallerError("INTEGRITY_FAILED", phase="download")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class DownloadManager:
    def __init__(self, cache_dir, *, trusted_hosts, transport=None, max_bytes=8 * 1024**3,
                 timeout=30.0, max_redirects=5, retries=2, chunk_size=128 * 1024):
        self.cache_dir = Path(cache_dir)
        if isinstance(trusted_hosts, str):
            raise ValueError("trusted_hosts must be an explicit list")
        self.trusted_hosts = frozenset(_host(host) for host in trusted_hosts)
        if not self.trusted_hosts or len(self.trusted_hosts) > 64:
            raise ValueError("trusted source hosts are required")
        if type(max_bytes) is not int or not 0 < max_bytes <= 64 * 1024**3:
            raise ValueError("invalid maximum artifact size")
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError("invalid request timeout")
        if type(max_redirects) is not int or not 0 <= max_redirects <= 10 or type(retries) is not int or not 0 <= retries <= 5:
            raise ValueError("invalid redirect/retry limit")
        if type(chunk_size) is not int or not 1024 <= chunk_size <= 1024 * 1024:
            raise ValueError("invalid chunk size")
        self.transport = transport if transport is not None else UrllibTransport()
        self.max_bytes, self.timeout, self.max_redirects = max_bytes, timeout, max_redirects
        self.retries, self.chunk_size = retries, chunk_size
        self._mutex = threading.RLock()

    def _approve(self, spec):
        if not isinstance(spec, ArtifactSpec):
            spec = ArtifactSpec.from_dict(spec)
        _, source_host = _url(spec.url)
        allowed = {source_host, *spec.allowed_redirect_hosts}
        if not allowed <= self.trusted_hosts:
            raise InstallerError("INTEGRITY_FAILED", phase="download", component_id=spec.artifact_id,
                                 details={"reason": "source_host_not_approved"})
        if spec.size > self.max_bytes:
            raise InstallerError("INVALID_REQUEST", phase="download", details={"reason": "artifact_too_large"})
        return spec, allowed

    @contextmanager
    def _lock(self, spec):
        _private_dir(self.cache_dir)
        with self._mutex:
            descriptor = _open(self.cache_dir / (spec.sha256 + ".lock"), os.O_CREAT | os.O_RDWR)
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            acquired = False
            try:
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                deadline = time.monotonic() + 2
                while True:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise InstallerError("OPERATION_CONFLICT", phase="download", component_id=spec.artifact_id)
                        time.sleep(0.02)
                yield
            finally:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def verify(self, spec, path) -> bool:
        """Повторная проверка локального пакета; не доверяет cache receipt."""
        spec, _ = self._approve(spec)
        path = Path(path)
        if not path.exists():
            return False
        descriptor = _open(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as handle:
            if os.fstat(handle.fileno()).st_size != spec.size:
                return False
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = handle.read(min(self.chunk_size, spec.size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > spec.size:
                    return False
                digest.update(chunk)
        return total == spec.size and hmac.compare_digest(digest.hexdigest(), spec.sha256)

    @staticmethod
    def _notify(progress, event):
        if progress is not None:
            try:
                progress(dict(event))
            except Exception:
                # A failed UI observer must not invalidate already verified bytes.
                pass

    def _response(self, spec, allowed, headers):
        current = spec.url
        for redirects in range(self.max_redirects + 1):
            _, host = _url(current)
            if host not in allowed:
                raise InstallerError("INTEGRITY_FAILED", phase="download", details={"reason": "redirect_host_not_approved"})
            response = self.transport.open(current, headers=dict(headers), timeout=self.timeout)
            status = getattr(response, "status", None)
            if status in _REDIRECTS:
                location = response.headers.get("Location") or response.headers.get("location")
                response.close()
                if not location or redirects >= self.max_redirects:
                    raise InstallerError("DOWNLOAD_FAILED", phase="download", details={"reason": "redirect_limit_or_missing_location"})
                try:
                    current, redirected_host = _url(urljoin(current, location))
                except ValueError as exc:
                    raise InstallerError("INTEGRITY_FAILED", phase="download", details={"reason": "unsafe_redirect"}) from exc
                if redirected_host not in allowed:
                    raise InstallerError("INTEGRITY_FAILED", phase="download", details={"reason": "redirect_host_not_approved"})
                continue
            return response
        raise InstallerError("DOWNLOAD_FAILED", phase="download")

    @staticmethod
    def _remove_partial(partial, metadata):
        for path in (partial, metadata):
            if path.is_symlink():
                raise InstallerError("INTEGRITY_FAILED", phase="download")
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _partial(self, spec, partial, metadata):
        if not partial.exists() or not metadata.exists():
            self._remove_partial(partial, metadata)
            return 0, None
        descriptor = _open(metadata, os.O_RDONLY)
        try:
            with os.fdopen(descriptor, "rb") as handle:
                content = handle.read(8193)
            if len(content) > 8192:
                raise ValueError("oversized partial metadata")
            value = json.loads(content)
            if (value.get("sha256") != spec.sha256 or value.get("size") != spec.size or
                    value.get("source_url_sha256") != hashlib.sha256(spec.url.encode()).hexdigest() or
                    not isinstance(value.get("etag"), str) or not _ETAG.fullmatch(value["etag"])):
                raise ValueError("unusable resume metadata")
            descriptor = _open(partial, os.O_RDONLY)
            try:
                offset = os.fstat(descriptor).st_size
            finally:
                os.close(descriptor)
            if not 0 < offset < spec.size:
                raise ValueError("unusable resume size")
            return offset, value["etag"]
        except (ValueError, TypeError, AttributeError):
            self._remove_partial(partial, metadata)
            return 0, None

    def _attempt(self, spec, allowed, partial, metadata, progress, canceled):
        offset, etag = self._partial(spec, partial, metadata)
        headers = {"Accept-Encoding": "identity", "User-Agent": "HomenSAI-Installer/1"}
        if offset:
            headers.update(Range=f"bytes={offset}-", **{"If-Range": etag})
        if shutil.disk_usage(self.cache_dir).free < spec.size - offset + 65536:
            raise InstallerError("DISK_SPACE_LOW", phase="download", component_id=spec.artifact_id)
        response = self._response(spec, allowed, headers)
        resumed = False
        try:
            status = getattr(response, "status", None)
            response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            encoding = response_headers.get("content-encoding", "identity").lower()
            if encoding not in ("", "identity"):
                raise InstallerError("DOWNLOAD_FAILED", phase="download", details={"reason": "encoded_response_not_supported"})
            new_etag = response_headers.get("etag")
            if offset and status == 206:
                content_range = _CONTENT_RANGE.fullmatch(response_headers.get("content-range", ""))
                if not content_range or tuple(map(int, content_range.groups())) != (offset, spec.size - 1, spec.size) or new_etag != etag:
                    self._remove_partial(partial, metadata)
                    raise _RestartDownload()
                resumed = True
            elif status == 200:
                offset = 0  # Server ignored Range or validator changed: use full body.
            elif status == 416 and offset:
                self._remove_partial(partial, metadata)
                raise _RestartDownload()
            else:
                raise InstallerError("DOWNLOAD_FAILED", phase="download", component_id=spec.artifact_id,
                                     details={"http_status": status if type(status) is int else 0})
            expected_remaining = spec.size - offset
            if "content-length" in response_headers:
                try:
                    content_length = int(response_headers["content-length"])
                except ValueError as exc:
                    raise InstallerError("DOWNLOAD_FAILED", phase="download", details={"reason": "invalid_content_length"}) from exc
                if content_length != expected_remaining:
                    raise InstallerError("INTEGRITY_FAILED", phase="download", details={"reason": "unexpected_content_length"})
            strong_etag = new_etag if isinstance(new_etag, str) and _ETAG.fullmatch(new_etag) else None
            _atomic_json(metadata, {"schema_version": 1, "sha256": spec.sha256, "size": spec.size,
                         "source_url_sha256": hashlib.sha256(spec.url.encode()).hexdigest(), "etag": strong_etag})
            descriptor = _open(partial, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if offset else os.O_TRUNC))
            with os.fdopen(descriptor, "ab" if offset else "wb") as output:
                completed = offset
                self._notify(progress, {"type": "download.started", "component_id": spec.artifact_id, "completed": completed, "total": spec.size, "unit": "bytes", "resumed": resumed})
                try:
                    while True:
                        if canceled is not None and canceled():
                            raise InstallerError("OPERATION_CANCELED", phase="download", component_id=spec.artifact_id)
                        chunk = response.read(min(self.chunk_size, spec.size - completed + 1))
                        if not isinstance(chunk, (bytes, bytearray)):
                            raise InstallerError("DOWNLOAD_FAILED", phase="download", details={"reason": "invalid_transport_bytes"})
                        if not chunk:
                            break
                        if completed + len(chunk) > spec.size:
                            raise InstallerError("INTEGRITY_FAILED", phase="download", details={"reason": "oversized_response"})
                        output.write(chunk)
                        completed += len(chunk)
                        self._notify(progress, {"type": "download.bytes", "component_id": spec.artifact_id, "bytes_delta": len(chunk), "completed": completed, "total": spec.size, "unit": "bytes"})
                finally:
                    output.flush()
                    os.fsync(output.fileno())
            if completed != spec.size:
                raise OSError("incomplete artifact transfer")
            if not self.verify(spec, partial):
                self._remove_partial(partial, metadata)
                raise InstallerError("INTEGRITY_FAILED", phase="download", component_id=spec.artifact_id,
                                     details={"reason": "sha256_mismatch"})
            return resumed
        finally:
            response.close()

    def fetch(self, spec, *, progress=None, cancel_requested=None, offline=False):
        spec, allowed = self._approve(spec)
        if type(offline) is not bool:
            raise ValueError("offline must be boolean")
        with self._lock(spec):
            final = self.cache_dir / (spec.sha256 + ".artifact")
            partial = self.cache_dir / (spec.sha256 + ".part")
            metadata = self.cache_dir / (spec.sha256 + ".part.json")
            if self.verify(spec, final):
                self._notify(progress, {"type": "download.cache_hit", "component_id": spec.artifact_id, "completed": spec.size, "total": spec.size, "unit": "bytes"})
                return {"path": str(final), "sha256": spec.sha256, "size": spec.size, "cache_hit": True, "resumed": False, "verified": True}
            if final.exists():
                final.unlink()  # Only this owned digest cache entry; never user data.
            # A previous process may have died after receiving all bytes.
            if self.verify(spec, partial):
                os.replace(partial, final)
                metadata.unlink(missing_ok=True)
                return {"path": str(final), "sha256": spec.sha256, "size": spec.size, "cache_hit": True, "resumed": True, "verified": True}
            if offline:
                raise InstallerError("PREREQUISITE_MISSING", phase="download", component_id=spec.artifact_id,
                                     details={"reason": "verified_offline_artifact_missing"})
            restarts = 0
            for attempt in range(self.retries + 2):
                if cancel_requested is not None and cancel_requested():
                    raise InstallerError("OPERATION_CANCELED", phase="download", component_id=spec.artifact_id)
                try:
                    resumed = self._attempt(spec, allowed, partial, metadata, progress, cancel_requested)
                    os.replace(partial, final)
                    metadata.unlink(missing_ok=True)
                    _atomic_json(self.cache_dir / (spec.sha256 + ".verified.json"), {"schema_version": 1, **spec.public_metadata()})
                    self._notify(progress, {"type": "download.verified", "component_id": spec.artifact_id, "completed": spec.size, "total": spec.size, "unit": "bytes"})
                    return {"path": str(final), "sha256": spec.sha256, "size": spec.size, "cache_hit": False, "resumed": resumed, "verified": True}
                except _RestartDownload:
                    restarts += 1
                    if restarts > 1:
                        raise InstallerError("DOWNLOAD_FAILED", phase="download", details={"reason": "invalid_range_response"})
                except urllib.error.URLError as exc:
                    if isinstance(exc.reason, ssl.SSLCertVerificationError):
                        raise InstallerError("TLS_VERIFICATION_FAILED", phase="download", component_id=spec.artifact_id) from exc
                    if attempt - restarts >= self.retries:
                        raise InstallerError("DOWNLOAD_FAILED", phase="download", component_id=spec.artifact_id) from exc
                except ssl.SSLCertVerificationError as exc:
                    raise InstallerError("TLS_VERIFICATION_FAILED", phase="download", component_id=spec.artifact_id) from exc
                except InstallerError as exc:
                    if exc.code == "INTEGRITY_FAILED":
                        self._remove_partial(partial, metadata)
                    raise
                except (TimeoutError, OSError) as exc:
                    if attempt - restarts >= self.retries:
                        raise InstallerError("DOWNLOAD_FAILED", phase="download", component_id=spec.artifact_id) from exc
                self._notify(progress, {"type": "download.retry", "component_id": spec.artifact_id})
            raise InstallerError("DOWNLOAD_FAILED", phase="download", component_id=spec.artifact_id)
