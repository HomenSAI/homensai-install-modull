"""Настоящий loopback HTTP: binary upload, framing/auth и SDK lifecycle.

Исполняемые пакеты ОС и настоящий browser этим набором не проверяются.
"""
from contextlib import contextmanager
import http.client
import json
import socket
import threading
import time

import pytest

from homensai_install.gui import create_server
from test_prerequisites_http import bundle, session_for


@contextmanager
def http_session(session):
    server = create_server(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, body=None, *, binary=None, headers=None, authenticated=True, method=None):
        supplied = {}
        if authenticated: supplied["X-HInstall-Token"] = session.token
        payload = binary if binary is not None else json.dumps(body).encode() if body is not None else None
        if payload is not None:
            supplied["Content-Type"] = "application/octet-stream" if binary is not None else "application/json"
        supplied.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method or ("POST" if payload is not None else "GET"), "/api/v1" + path, payload, supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        yield server, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def prepare(request):
    code, response = request("/prerequisites/plans", {"mode": "native", "locale": "en",
        "choices": {"base": "auto", "client": "auto"}})
    assert code == 200
    value = response["result"]
    code, response = request("/prerequisites/uploads", {"plan_id": value["plan_id"],
        "plan_digest": value["plan_digest"], "component_id": "base", "artifact_id": "base-package"})
    assert code == 200
    return value, response["result"]


def content(bundle, component="base"):
    return bundle["transport"].files["https://downloads.example.org/" + component + "?token=never-public"]


def test_http_file_selection_new_review_and_existing_coordinator(bundle):
    session = session_for(bundle)
    with http_session(session) as (_, request):
        old, allocated = prepare(request)
        path = "/prerequisites/uploads/" + allocated["upload_id"]
        code, response = request(path + "/content", binary=content(bundle))
        assert code == 200 and response["result"]["state"] == "verified"
        assert request(path)[1]["result"]["verified"]
        assert not bundle["calls"] and not bundle["transport"].calls
        code, response = request("/prerequisites/plans", {"mode": "native", "locale": "de",
            "choices": {"base": "auto", "client": "auto"},
            "local_uploads": {"base-package": allocated["upload_id"]}})
        assert code == 200
        reviewed = response["result"]
        assert reviewed["plan_digest"] != old["plan_digest"] and "wizard-uploads" not in json.dumps(response)
        consent = {"plan_id": reviewed["plan_id"], "plan_digest": reviewed["plan_digest"],
            "terms_digest": reviewed["terms_digest"], "accepted_plan": True, "accepted_terms": True,
            "consent_refs": reviewed["plan"]["required_consents"]}
        assert request("/prerequisites/operations", consent)[0] == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = request("/prerequisites/operations/" + reviewed["operation_id"])
            if result["result"]["execution"]["active"] is False: break
            time.sleep(.01)
        assert code == 200 and result["result"]["ready"] is True
        assert bundle["calls"] == ["base", "client"]
        assert bundle["transport"].calls == ["https://downloads.example.org/client?token=never-public"]
        assert request(path + "/cancel", {})[0] == 400


@pytest.mark.parametrize("headers,authenticated", [({}, False), ({"Host": "attacker.invalid"}, True),
    ({"Origin": "https://attacker.invalid"}, True), ({"Sec-Fetch-Site": "cross-site"}, True)])
def test_auth_origin_rejected_before_upload_body_read(bundle, headers, authenticated):
    session = session_for(bundle)
    with http_session(session) as (_, request):
        _, allocated = prepare(request)
        code, _ = request("/prerequisites/uploads/" + allocated["upload_id"] + "/content",
                           binary=content(bundle), headers=headers, authenticated=authenticated)
        assert code == 403
        assert session._local_uploads.status(allocated["upload_id"])["state"] == "pending"
        assert not list(session._local_uploads.directory.iterdir())


@pytest.mark.parametrize("headers", [{"Content-Length": "999999999999"}, {"Content-Length": "-1"},
    {"Content-Length": "+17"}, {"Content-Type": "application/json"},
    {"Transfer-Encoding": "chunked"}])
def test_invalid_binary_framing_rejected_without_staging(bundle, headers):
    session = session_for(bundle)
    with http_session(session) as (_, request):
        _, allocated = prepare(request)
        code, _ = request("/prerequisites/uploads/" + allocated["upload_id"] + "/content",
                           binary=b"", headers=headers)
        assert code == 400 and not list(session._local_uploads.directory.iterdir())


@pytest.mark.parametrize("duplicate", ["Content-Length", "Content-Type", "Host", "X-HInstall-Token", "Origin"])
def test_duplicate_security_headers_rejected_before_read(bundle, duplicate):
    session = session_for(bundle)
    with http_session(session) as (server, request):
        _, allocated = prepare(request)
        headers = {"Host": f"127.0.0.1:{server.server_port}", "X-HInstall-Token": session.token,
                   "Content-Length": str(len(content(bundle))), "Content-Type": "application/octet-stream",
                   "Origin": f"http://127.0.0.1:{server.server_port}"}
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.putrequest("POST", "/api/v1/prerequisites/uploads/" + allocated["upload_id"] + "/content", skip_host=True)
            for key, value in headers.items(): connection.putheader(key, value)
            connection.putheader(duplicate, headers[duplicate])
            connection.endheaders()
            response = connection.getresponse()
            assert response.status in {400, 403}
            response.read()
        finally:
            connection.close()
        assert not list(session._local_uploads.directory.iterdir())


def test_interrupted_upload_cleans_partial_and_can_be_reselected(bundle):
    session = session_for(bundle)
    with http_session(session) as (server, request):
        old, allocated = prepare(request)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.putrequest("POST", "/api/v1/prerequisites/uploads/" + allocated["upload_id"] + "/content")
            connection.putheader("X-HInstall-Token", session.token)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(len(content(bundle))))
            connection.endheaders()
            connection.send(content(bundle)[:2])
            connection.sock.shutdown(socket.SHUT_WR)
            response = connection.getresponse()
            assert response.status == 400 and json.loads(response.read())["error"]["code"] == "INTEGRITY_FAILED"
        finally:
            connection.close()
        assert not list(session._local_uploads.directory.iterdir())
        assert request("/prerequisites/uploads/" + allocated["upload_id"])[1]["result"]["state"] == "failed"
        code, response = request("/prerequisites/uploads", {"plan_id": old["plan_id"], "plan_digest": old["plan_digest"],
            "component_id": "base", "artifact_id": "base-package"})
        assert code == 200 and response["result"]["upload_id"] != allocated["upload_id"]
        assert not bundle["calls"]


def test_http_wrong_hash_and_cancel_are_explicit_nonexecuting_actions(bundle):
    session = session_for(bundle)
    with http_session(session) as (_, request):
        _, allocated = prepare(request)
        path = "/prerequisites/uploads/" + allocated["upload_id"]
        code, value = request(path + "/content", binary=b"X" * len(content(bundle)))
        assert code == 400 and value["error"]["code"] == "INTEGRITY_FAILED"
        assert "wizard-uploads" not in json.dumps(value)
        assert request(path + "/cancel", {})[1]["result"]["state"] == "canceled"
        assert not list(session._local_uploads.directory.iterdir())
        assert not bundle["calls"] and not bundle["transport"].calls
