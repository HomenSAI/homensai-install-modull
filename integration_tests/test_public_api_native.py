"""API → real Linux backend. Только loopback и синтетические данные.

Это source/integration, не frozen/Windows/Docker acceptance. Test verifier
не заменяет отдельные Ed25519 tests. Сетевые ограничения integration suite
применяются к самому facade/backend в текущем процессе.
"""

import importlib.util
from pathlib import Path
import json
import shutil
import sys
import uuid

import pytest

from homensai_install.api.v1 import InstallerAPI
from homensai_install.backends import BackendRouter
from homensai_install.engine import EngineError


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native integration")
def test_public_facade_reaches_real_native_lifecycle():
    path = Path(__file__).with_name("test_native_engine.py")
    spec = importlib.util.spec_from_file_location("native_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.NativeLifecycleTests(methodName="runTest")
    fixture.setUp()
    try:
        api = InstallerAPI(fixture.base / "public-state", engine=BackendRouter(native=fixture.engine))
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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native integration")
@pytest.mark.parametrize("action", ["backup", "uninstall"])
def test_service_registration_after_review_blocks_native_before_data_or_process_effects(action):
    path = Path(__file__).with_name("test_native_engine.py")
    spec = importlib.util.spec_from_file_location("native_service_boundary_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.NativeLifecycleTests(methodName="runTest")
    fixture.setUp()
    fence = fixture.root / ".hinstall/service-manager"
    try:
        bundle = fixture.bundle("v1")
        api = InstallerAPI(fixture.base / "public-state", engine=BackendRouter(native=fixture.engine))
        options = bundle["options"]
        install = api.plan("install", bundle["manifest"], fixture.target, options=options)
        installed = api.submit(install["plan_id"], install["plan_digest"],
            [install["review_consent_ref"], *install["required_consents"]],
            idempotency_key="install", background=False)
        assert installed["status"] == "SUCCEEDED", installed.get("error")
        fixture.current = bundle
        plan = api.plan(action, bundle["manifest"], fixture.target, options=options)
        context = fixture.engine._context(bundle["manifest"], fixture.target, options)
        owner = fixture.engine._owner(context)
        records = fixture.engine._records(context)
        assert any(fixture.engine._running(record) for record in records.values())
        before = set(context["work"].glob("*.hsac"))
        fence.mkdir(mode=0o700)
        state = {"schema_version":1, "revision":1,
                 "binding":{key:owner[key] for key in ("product_id","installation_id","owner","mode")},
                 "services":{"owned.service":{"removed":False,"enabled":False,"start_uncertain":False}},
                 "operations":{"registered":{"status":"done"}}}
        (fence / "state.json").write_text(json.dumps(state))
        (fence / "state.json").chmod(0o600)
        # Direct engine execution, not only API discover, must recheck the fence.
        with pytest.raises(EngineError) as caught:
            fixture.engine.execute(plan)
        assert caught.value.code == "OPERATION_CONFLICT"
        assert all(fixture.engine._running(record) for record in records.values())
        assert (context["home"].releases / "app-v1").is_dir()
        assert set(context["work"].glob("*.hsac")) == before
    finally:
        if fence.exists():
            shutil.rmtree(fence)  # Only this test's synthetic fence; lets fixture stop its process.
        fixture.tearDown()
