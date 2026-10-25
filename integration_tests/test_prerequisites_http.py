"""Real authenticated loopback HTTP; signed fixtures and fake OS installer.

HTTP-server/SDK recreation is tested, not power-loss, browser or real OS setup.
"""
from contextlib import contextmanager
import http.client
import importlib.util
import json
from pathlib import Path
import threading
import time

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.gui import WizardSession, create_server

_spec = importlib.util.spec_from_file_location("prerequisite_http_fixtures",
    Path(__file__).resolve().parents[1] / "tests/test_prerequisites_execution.py")
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
bundle = _fixtures.bundle


def session_for(value):
    (value["payload"] / "legal").mkdir(exist_ok=True)
    for locale in ("ru", "en", "de"):
        (value["payload"] / "legal" / (locale + ".txt")).write_text("Synthetic product terms " + locale)
    _fixtures.resign(value)
    api = InstallerAPI.from_bootstrap(value["tmp_path"] / "api-state", prerequisites=_fixtures.coordinator(value))
    return WizardSession(api, value["manifest"], value["options"], verifier=value["verifier"], development=True)


@contextmanager
def serving(session):
    server = create_server(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, *, body=None, headers=None, authenticated=True, raw=None):
        supplied = {"Host": f"127.0.0.1:{server.server_port}"}
        if authenticated:
            supplied["X-HInstall-Token"] = session.token
        supplied.update(headers or {})
        payload = raw if raw is not None else json.dumps(body) if body is not None else None
        if payload is not None:
            supplied["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("POST" if payload is not None else "GET", "/api/v1" + path, payload, supplied)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_prerequisite_http_auth_origin_and_strict_input(bundle):
    session = session_for(bundle)
    with serving(session) as request:
        path = "/prerequisites/capabilities"
        assert request(path, authenticated=False)[0] == 403
        for headers in ({"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}):
            assert request(path, headers=headers)[0] == 403
        code, value = request(path)
        assert code == 200 and value["result"]["configured"] is True
        assert value["result"]["durable_byte_progress"] is False
        assert session.token not in json.dumps(value)
        assert request(path + "?mode=native&mode=docker")[0] == 400
        assert request(path + "?mode=native&mode=")[0] == 400
        assert request(path + "?unknown=")[0] == 400
        assert request("/prerequisites/plans", raw='{"mode":"native","mode":"docker"}')[0] == 400
        assert request("/prerequisites/plans", body={"argv": ["sh"]})[0] == 400
        assert request("/prerequisites/plans", body={"local_artifacts": {"base-package": "/tmp/unsafe"}})[0] == 400
        assert request("/prerequisites/operations/foreign/cancel", body={})[0] == 400
    assert not bundle["calls"] and not bundle["transport"].calls


def test_http_lost_ack_server_recreation_and_cancel_while_installer_waits(bundle):
    entered, release = threading.Event(), threading.Event()
    def installer():
        entered.set()
        assert release.wait(10)
        _fixtures.present(bundle, "base")
    bundle["effects"]["base"] = installer
    first = session_for(bundle)
    operation_id = None
    try:
        with serving(first) as request:
            code, response = request("/prerequisites/plans", body={"mode": "native", "locale": "en",
                "choices": {"base": "auto", "client": "auto"}})
            assert code == 200
            plan = response["result"]
            operation_id = plan["operation_id"]
            body = {"plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"],
                    "consent_refs": plan["plan"]["required_consents"], "accepted_terms": True,
                    "accepted_plan": True, "terms_digest": plan["terms_digest"]}
            assert request("/prerequisites/operations", body=body)[0] == 200  # ACK not used by client.
            assert entered.wait(3)
            code, repeated = request("/prerequisites/operations", body=body)
            assert code == 200 and repeated["result"]["operation_id"] == operation_id
            assert bundle["calls"] == ["base"]
        # A new HTTP server/token/SDK restores the same authoritative operation.
        second = session_for(bundle)
        with serving(second) as request:
            code, listing = request("/prerequisites/operations")
            assert code == 200 and [item["operation_id"] for item in listing["result"]["operations"]] == [operation_id]
            code, events = request("/prerequisites/operations/" + operation_id + "/events")
            assert code == 200 and events["result"]["events"]
            code, canceled = request("/prerequisites/operations/" + operation_id + "/cancel", body={})
            assert code == 200 and canceled["result"]["cancel_requested"] and not release.is_set()
            assert "token=never-public" not in json.dumps(events)
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                code, current = request("/prerequisites/operations/" + operation_id)
                if current["result"]["execution"]["active"] is False:
                    break
                threading.Event().wait(.01)
            assert code == 200 and current["result"]["status"] == "CANCELED"
            assert bundle["calls"] == ["base"]
    finally:
        release.set()
        if operation_id is not None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and first.api.prerequisites_status(operation_id)["execution"]["active"] is not False:
                threading.Event().wait(.01)
