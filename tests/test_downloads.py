"""Проверки загрузчика с внедрённым HTTP transport; сеть не используется."""
import hashlib
import io
import json
import os
import ssl
import urllib.error
from pathlib import Path

import pytest

from homensai_install.downloads import ArtifactSpec, DownloadManager
from homensai_install.errors import InstallerError


DATA = bytes(range(256)) * 16


class Response:
    def __init__(self, body=b"", status=200, headers=None, fail_after=None):
        self.status, self.headers = status, headers or {}
        self.stream = io.BytesIO(body)
        self.fail_after = fail_after
        self.closed = False

    def read(self, size):
        if self.fail_after is not None:
            if self.stream.tell() >= self.fail_after:
                raise OSError("synthetic connection loss")
            size = min(size, self.fail_after - self.stream.tell())
        return self.stream.read(size)

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, url, *, headers, timeout):
        self.calls.append((url, dict(headers)))
        assert self.responses, "unexpected extra HTTP request"
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def spec(**changes):
    values = dict(artifact_id="reference-runtime", version="1.0.0", url="https://downloads.example.org/runtime.bin",
                  sha256=hashlib.sha256(DATA).hexdigest(), size=len(DATA), license_ref="LICENSES/reference-MIT.txt")
    values.update(changes)
    return ArtifactSpec(**values)


def manager(tmp_path, transport, **kwargs):
    return DownloadManager(tmp_path / "cache", trusted_hosts=("downloads.example.org", "cdn.example.org"), transport=transport, retries=0, **kwargs)


def test_exact_bytes_are_verified_before_cache_promotion_and_offline_reuse(tmp_path):
    transport = Transport(Response(DATA, headers={"Content-Length": str(len(DATA)), "ETag": '"release-1"'}))
    downloader = manager(tmp_path, transport)
    events = []
    result = downloader.fetch(spec(), progress=events.append)
    assert result["verified"] and not result["cache_hit"]
    assert Path(result["path"]).read_bytes() == DATA
    assert events[-1]["type"] == "download.verified"
    assert downloader.fetch(spec(), offline=True)["cache_hit"]
    assert len(transport.calls) == 1
    Path(result["path"]).write_bytes(b"damaged")
    with pytest.raises(InstallerError) as caught:
        downloader.fetch(spec(), offline=True)
    assert caught.value.code == "PREREQUISITE_MISSING"


@pytest.mark.parametrize("url", ["http://downloads.example.org/a", "file:///tmp/a", "https://user:pass@downloads.example.org/a",
                                   "https://127.0.0.1/a", "https://localhost/a", "https://downloads.example.org:8443/a",
                                   "https://downloads.example.org/a#fragment", "https://downloads.example.org/a\nheader:x"])
def test_unsafe_source_urls_are_rejected(url):
    with pytest.raises(ValueError):
        spec(url=url)


def test_source_host_must_be_preapproved_not_just_https(tmp_path):
    downloader = manager(tmp_path, Transport())
    with pytest.raises(InstallerError) as caught:
        downloader.fetch(spec(url="https://unapproved.example.org/a"))
    assert caught.value.code == "INTEGRITY_FAILED"
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("body,headers", [(b"x" * len(DATA), {}), (DATA + b"extra", {}), (DATA, {"Content-Length": "999999"})])
def test_bad_hash_or_oversize_never_becomes_a_verified_file(tmp_path, body, headers):
    downloader = manager(tmp_path, Transport(Response(body, headers=headers)))
    with pytest.raises(InstallerError) as caught:
        downloader.fetch(spec())
    assert caught.value.code == "INTEGRITY_FAILED"
    assert not list((tmp_path / "cache").glob("*.artifact"))
    assert not list((tmp_path / "cache").glob("*.part"))


def test_resume_requires_matching_strong_etag_and_correct_range(tmp_path):
    first = Transport(Response(DATA, headers={"Content-Length": str(len(DATA)), "ETag": '"v1"'}, fail_after=1024))
    downloader = manager(tmp_path, first)
    with pytest.raises(InstallerError) as caught:
        downloader.fetch(spec())
    assert caught.value.code == "DOWNLOAD_FAILED"
    assert next((tmp_path / "cache").glob("*.part")).stat().st_size == 1024
    second = Transport(Response(DATA[1024:], status=206, headers={"Content-Length": str(len(DATA) - 1024), "Content-Range": f"bytes 1024-{len(DATA)-1}/{len(DATA)}", "ETag": '"v1"'}))
    result = manager(tmp_path, second).fetch(spec())
    assert result["resumed"] and Path(result["path"]).read_bytes() == DATA
    assert second.calls[0][1]["Range"] == "bytes=1024-"
    assert second.calls[0][1]["If-Range"] == '"v1"'


@pytest.mark.parametrize("etag", [None, 'W/"v1"'])
def test_missing_or_weak_validator_restarts_from_zero(tmp_path, etag):
    headers = {"ETag": etag} if etag else {}
    downloader = manager(tmp_path, Transport(Response(DATA, headers=headers, fail_after=1024)))
    with pytest.raises(InstallerError):
        downloader.fetch(spec())
    transport = Transport(Response(DATA, headers={"ETag": '"new"'}))
    result = manager(tmp_path, transport).fetch(spec())
    assert "Range" not in transport.calls[0][1] and not result["resumed"]


def test_server_ignoring_range_is_safely_treated_as_full_response(tmp_path):
    with pytest.raises(InstallerError):
        manager(tmp_path, Transport(Response(DATA, headers={"ETag": '"old"'}, fail_after=1024))).fetch(spec())
    transport = Transport(Response(DATA, headers={"ETag": '"new"', "Content-Length": str(len(DATA))}))
    result = manager(tmp_path, transport).fetch(spec())
    assert not result["resumed"] and Path(result["path"]).read_bytes() == DATA


def test_incorrect_partial_response_restarts_once_without_using_wrong_bytes(tmp_path):
    with pytest.raises(InstallerError):
        manager(tmp_path, Transport(Response(DATA, headers={"ETag": '"v1"'}, fail_after=1024))).fetch(spec())
    transport = Transport(Response(DATA[1024:], status=206, headers={"ETag": '"wrong"', "Content-Range": f"bytes 1024-{len(DATA)-1}/{len(DATA)}"}), Response(DATA))
    result = manager(tmp_path, transport).fetch(spec())
    assert Path(result["path"]).read_bytes() == DATA
    assert "Range" in transport.calls[0][1] and "Range" not in transport.calls[1][1]


def test_redirects_are_explicit_exact_host_allowlist(tmp_path):
    transport = Transport(Response(status=302, headers={"Location": "https://cdn.example.org/pinned"}), Response(DATA))
    result = manager(tmp_path, transport).fetch(spec(allowed_redirect_hosts=("cdn.example.org",)))
    assert result["verified"] and len(transport.calls) == 2
    other = tmp_path / "other"
    rejected = Transport(Response(status=302, headers={"Location": "https://unapproved.example.org/a"}))
    with pytest.raises(InstallerError) as caught:
        manager(other, rejected).fetch(spec())
    assert caught.value.code == "INTEGRITY_FAILED" and len(rejected.calls) == 1


def test_tls_failure_is_not_hidden_by_retry_or_verification_disable(tmp_path):
    transport = Transport(urllib.error.URLError(ssl.SSLCertVerificationError("synthetic TLS error")))
    with pytest.raises(InstallerError) as caught:
        manager(tmp_path, transport).fetch(spec())
    assert caught.value.code == "TLS_VERIFICATION_FAILED" and len(transport.calls) == 1


def test_cancel_preserves_partial_only_and_never_executes(tmp_path):
    calls = 0

    def canceled():
        nonlocal calls
        calls += 1
        return calls >= 3

    with pytest.raises(InstallerError) as caught:
        manager(tmp_path, Transport(Response(DATA, headers={"ETag": '"v1"'})), chunk_size=1024).fetch(spec(), cancel_requested=canceled)
    assert caught.value.code == "OPERATION_CANCELED"
    assert not list((tmp_path / "cache").glob("*.artifact"))


def test_cdn_query_credentials_are_not_stored_or_reported(tmp_path):
    artifact = spec(url="https://downloads.example.org/file?access_token=synthetic-query-secret")
    downloader = manager(tmp_path, Transport(Response(DATA)))
    events = []
    downloader.fetch(artifact, progress=events.append)
    stored = "".join(path.read_text() for path in (tmp_path / "cache").glob("*.json"))
    assert "synthetic-query-secret" not in stored + json.dumps(events)


def test_license_and_pin_fields_are_required():
    with pytest.raises(ValueError):
        spec(license_ref="")
    with pytest.raises(ValueError):
        spec(version="latest")
    with pytest.raises(ValueError):
        spec(sha256="not-a-hash")


def test_cache_symlink_is_never_followed(tmp_path):
    target = tmp_path / "unrelated"
    target.write_bytes(b"private unrelated data")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (spec().sha256 + ".artifact")).symlink_to(target)
    with pytest.raises(InstallerError) as caught:
        manager(tmp_path, Transport()).fetch(spec())
    assert caught.value.code == "INTEGRITY_FAILED"
    assert target.read_bytes() == b"private unrelated data"


def test_cache_hardlink_cannot_truncate_unrelated_data(tmp_path):
    target = tmp_path / "unrelated"
    target.write_bytes(b"private unrelated data")
    cache = tmp_path / "cache"
    cache.mkdir()
    os.link(target, cache / (spec().sha256 + ".part"))
    with pytest.raises(InstallerError) as caught:
        manager(tmp_path, Transport(Response(DATA))).fetch(spec())
    assert caught.value.code == "INTEGRITY_FAILED"
    assert target.read_bytes() == b"private unrelated data"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_shared_writable_cache_directory_is_rejected(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cache.chmod(0o777)
    with pytest.raises(InstallerError) as caught:
        manager(tmp_path, Transport(Response(DATA))).fetch(spec())
    assert caught.value.code == "PERMISSION_REQUIRED"
