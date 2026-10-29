"""Actual authenticated loopback refuses request-selected execution domains."""

from test_prerequisites_http import bundle, serving, session_for


def test_http_request_cannot_choose_execution_domain(bundle):
    session = session_for(bundle)
    original = session.api.execution_domain
    untrusted = bundle["tmp_path"] / "request-selected-domain"
    with serving(session) as request:
        for field in ("execution_domain", "execution_domain_dir", "owner_id"):
            for route, body in (
                ("/plans", {"action": "install", "mode": "native"}),
                ("/prerequisites/plans", {"mode": "native", "choices": {"base": "manual", "client": "manual"}}),
            ):
                code, result = request(route, body={**body, field: str(untrusted)})
                assert code == 400 and result["error"]["code"] == "INVALID_REQUEST"
    assert session.api.execution_domain is original
    assert not original.directory.exists() and not untrusted.exists()
    assert bundle["calls"] == [] and bundle["transport"].calls == []
