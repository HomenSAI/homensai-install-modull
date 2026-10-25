"""Настоящий HTTP transport на loopback: Host/Origin/token/body boundaries."""
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from homensai_install.gui import WizardSession, create_server


def test_loopback_http_security_boundaries(tmp_path):
    session = WizardSession(SimpleNamespace(state_dir=tmp_path / "state"),
                            {"product_id": "http-test"}, {}, development=True)
    server = create_server(session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"127.0.0.1:{server.server_port}"
    def request(path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        supplied = {"Host": origin, **(headers or {})}
        if body is not None:
            supplied["Content-Type"] = "application/json"
        connection.request("POST" if body is not None else "GET", path, body, supplied)
        response = connection.getresponse()
        value = response.read()
        status, result_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, value, result_headers
    try:
        status, _, headers = request("/")
        assert status == 200 and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert request("/api/v1/session")[0] == 403
        auth = {"X-HInstall-Token": session.token}
        assert request("/api/v1/session", auth)[0] == 200
        assert request("/api/v1/session", {**auth, "Host": "attacker.test"})[0] == 403
        assert request("/api/v1/session", {**auth, "Origin": "https://attacker.test"})[0] == 403
        assert request("/api/v1/session", {**auth, "Sec-Fetch-Site": "cross-site"})[0] == 403
        assert request("/api/v1/preferences", auth, '{"locale":"en","locale":"de"}')[0] == 400
        assert request("/api/v1/secrets", auth, json.dumps({"archive_password": "short"}))[0] == 400
        assert request("/api/v1/preferences", auth, json.dumps({"locale": "de"}))[0] == 200
        status, raw, _ = request("/api/v1/session", auth)
        assert json.loads(raw)["result"]["locale"] == "de"
        assert session.token.encode() not in raw
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
