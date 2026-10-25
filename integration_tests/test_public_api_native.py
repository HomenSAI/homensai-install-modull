"""API → real Linux backend. Только loopback и синтетические данные.

Это source/integration, не frozen/Windows/Docker acceptance. Test verifier
не заменяет отдельные Ed25519 tests. Сетевые ограничения integration suite
применяются к самому facade/backend в текущем процессе.
"""

import importlib.util
from pathlib import Path
import sys
import uuid

import pytest

from homensai_install.api.v1 import InstallerAPI


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native integration")
def test_public_facade_reaches_real_native_lifecycle():
    path = Path(__file__).with_name("test_native_engine.py")
    spec = importlib.util.spec_from_file_location("native_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.NativeLifecycleTests(methodName="runTest")
    fixture.setUp()
    try:
        api = InstallerAPI(fixture.base / "public-state", engine=fixture.engine)
        v1 = fixture.bundle("v1")
        bad = fixture.bundle("bad", bad_health=True)
        outcomes = []

        def run(action, bundle, **overrides):
            options = {**bundle["options"], **overrides}
            plan = api.plan(action, bundle["manifest"], fixture.target, options=options)
            result = api.submit(plan["plan_id"], plan["plan_digest"],
                                [plan["review_consent_ref"], *plan["required_consents"]],
                                idempotency_key=uuid.uuid4().hex, background=False)
            outcomes.append(result["status"])
            assert result["status"] in {"SUCCEEDED", "FAILED_ROLLED_BACK"}, result.get("error")
            events = api.events(result["operation_id"])
            assert events["snapshot"]["status"] == result["status"]
            assert events["events"][-1]["sequence"] == events["snapshot"]["last_sequence"]
            return result

        installed = run("install", v1)
        fixture.current = v1
        assert installed["result"]["health"]["ready"]
        user_file = fixture.root / "data" / "user-file.txt"
        user_file.write_text("keep user data")
        backup = run("backup", v1)["result"]["archive"]["path"]
        failed = run("update", bad)
        assert failed["status"] == "FAILED_ROLLED_BACK"
        run("stop", v1)
        run("start", v1)
        run("uninstall", v1)
        assert user_file.read_text() == "keep user data"
        restored = run("restore", v1, archive_path=backup)
        assert restored["result"]["health"]["ready"]
        assert user_file.read_text() == "keep user data"
        assert len(outcomes) == 7
        assert outcomes.count("FAILED_ROLLED_BACK") == 1
    finally:
        fixture.tearDown()
