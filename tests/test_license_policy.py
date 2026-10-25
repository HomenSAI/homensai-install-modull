"""Synthetic review fixtures verify a release gate, never grant legal approval."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hinstall_license_checker", ROOT / "tools" / "check_licenses.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def policy():
    return checker.read_json(ROOT / "packaging" / "license-policy.json")


def approve_test_fixture(value, evidence, policy):
    """Only synthetic data is approved in tests; no real release files used."""
    for component in value["components"]:
        subject = checker.component_subject_digest(component, value["artifacts"])
        record = {"state": "APPROVED", "reviewer": "SYNTHETIC-TEST-ONLY", "reviewed_at": "2026-01-01T00:00:00Z",
                  "subject_sha256": subject, "evidence": [evidence]}
        component["review"] = record
        if component.pop("needs_test_special_review", False):
            subject = checker.component_subject_digest(component, value["artifacts"])
            record["subject_sha256"] = subject
            component["special_review"] = copy.deepcopy(record)
    subject = checker.release_subject_digest(value)
    value["release_reviews"] = {name: {"state": "APPROVED", "reviewer": "SYNTHETIC-TEST-ONLY",
        "reviewed_at": "2026-01-01T00:00:00Z", "subject_sha256": subject, "evidence": [evidence]}
        for name in policy["required_release_reviews"]}
    return value


@pytest.fixture
def sample(tmp_path, policy):
    artifact_root = tmp_path / "staging"
    evidence_root = tmp_path / "evidence"
    artifact_root.mkdir()
    evidence_root.mkdir()
    (artifact_root / "program.txt").write_bytes(b"synthetic program fixture")
    (artifact_root / "NOTICE.txt").write_bytes(b"synthetic notice fixture")
    (evidence_root / "review.txt").write_bytes(b"Synthetic test only: no approval of real components.")
    proof = {"path": "review.txt", "sha256": digest((evidence_root / "review.txt").read_bytes())}
    component = {"component_id": "fixture", "name": "Fixture", "version": "1.0.0", "scope": "redistributed",
                 "license_expression": "MIT", "selected_expression": None, "transitive_review": "COMPLETE",
                 "license_conflict": False, "obligations": {"retain_license_notice": {"state": "SATISFIED", "evidence": [proof]}},
                 "notice_files": [{"path": "NOTICE.txt", "sha256": digest((artifact_root / "NOTICE.txt").read_bytes())}]}
    value = {"schema_version": 1, "inventory_id": "synthetic", "components": [component],
             "artifacts": [{"path": name, "sha256": digest((artifact_root / name).read_bytes()), "components": ["fixture"]}
                           for name in ("program.txt", "NOTICE.txt")]}
    approve_test_fixture(value, proof, policy)
    return value, artifact_root, evidence_root, proof


def evaluate(sample, policy):
    value, artifact_root, evidence_root, _ = sample
    return checker.evaluate(policy, value, artifact_root=artifact_root, evidence_root=evidence_root, production=True)


def codes(result):
    return {item["code"] for item in result["issues"]}


def test_complete_synthetic_review_and_exact_bytes_pass(sample, policy):
    result = evaluate(sample, policy)
    assert result["status"] == "PASS" and result["production_eligible"]


def test_spdx_boolean_structure_and_exceptions_are_preserved():
    choices = checker.expression_choices("(MIT OR Apache-2.0) AND Unicode-3.0")
    assert set(choices) == {frozenset(("MIT", "Unicode-3.0")), frozenset(("Apache-2.0", "Unicode-3.0"))}
    assert checker.selected_licenses("Apache-2.0 OR GPL-2.0-only", "Apache-2.0") == {"Apache-2.0"}
    assert checker.selected_licenses("GPL-2.0-or-later WITH Bootloader-exception", None) == {"GPL-2.0-or-later WITH Bootloader-exception"}
    with pytest.raises(checker.InvalidAudit):
        checker.selected_licenses("MIT OR Apache-2.0", None)
    with pytest.raises(checker.InvalidAudit):
        checker.selected_licenses("MIT AND BSD-3-Clause", "MIT")
    with pytest.raises(checker.InvalidAudit):
        checker.selected_licenses("GPL-2.0-or-later WITH Bootloader-exception", "GPL-2.0-or-later")


@pytest.mark.parametrize("value", ["", "MIT;execute", "MIT OR", "(MIT", "MIT WITH", "MIT XOR Apache-2.0", "(MIT OR Apache-2.0) WITH Foo"])
def test_malformed_expressions_fail_closed(value):
    with pytest.raises(checker.InvalidAudit):
        checker.expression_choices(value)


def test_expression_expansion_is_bounded():
    with pytest.raises(checker.InvalidAudit):
        checker.expression_choices(" AND ".join(f"(MIT{i} OR Apache{i})" for i in range(8)))


@pytest.mark.parametrize("state", ["UNKNOWN", "PENDING_REVIEW", "UNAPPROVED", "CONFLICT", "DENIED"])
def test_candidate_license_never_overrides_unapproved_state(sample, policy, state):
    sample[0]["components"][0]["review"]["state"] = state
    result = evaluate(sample, policy)
    assert not result["production_eligible"] and "REVIEW_NOT_APPROVED" in codes(result)


def test_explicit_noncommercial_restriction_is_denied_even_if_marked_approved(sample, policy):
    sample[0]["components"][0]["license_expression"] = "CC-BY-NC-4.0"
    approve_test_fixture(sample[0], sample[3], policy)
    assert "LICENSE_DENIED" in codes(evaluate(sample, policy))


def test_unknown_custom_license_is_not_approved_by_a_license_ref(sample, policy):
    sample[0]["components"][0]["license_expression"] = "LicenseRef-Customer-Permission"
    sample[0]["components"][0]["license_ref"] = "https://example.test/license"
    approve_test_fixture(sample[0], sample[3], policy)
    assert "LICENSE_UNKNOWN" in codes(evaluate(sample, policy))


def test_gpl_exception_requires_scope_review_but_is_not_blanket_commercial_ban(sample, policy):
    component = sample[0]["components"][0]
    component["license_expression"] = "GPL-2.0-or-later WITH Bootloader-exception"
    component["obligations"] = {"verify_exception_scope": {"state": "SATISFIED", "evidence": [sample[3]]}}
    approve_test_fixture(sample[0], sample[3], policy)
    first = evaluate(sample, policy)
    assert "SPECIAL_REVIEW_REQUIRED" in codes(first) and "LICENSE_DENIED" not in codes(first)
    component["needs_test_special_review"] = True
    approve_test_fixture(sample[0], sample[3], policy)
    assert evaluate(sample, policy)["status"] == "PASS"


def test_build_tool_gpl_is_distinguished_from_embedded_runtime(sample, policy):
    value = sample[0]
    value["components"].append({"component_id": "build-tool", "name": "Fixture builder", "version": "1.0",
        "scope": "build_only", "license_expression": "GPL-2.0-or-later", "transitive_review": "COMPLETE", "license_conflict": False})
    approve_test_fixture(value, sample[3], policy)
    assert evaluate(sample, policy)["status"] == "PASS"
    value["artifacts"][0]["components"].append("build-tool")
    approve_test_fixture(value, sample[3], policy)
    assert "REDISTRIBUTED_COMPONENT_MISCLASSIFIED" in codes(evaluate(sample, policy))


def test_all_and_license_obligations_are_required(sample, policy):
    component = sample[0]["components"][0]
    component["license_expression"] = "MIT AND Apache-2.0"
    approve_test_fixture(sample[0], sample[3], policy)
    assert "OBLIGATION_UNSATISFIED" in codes(evaluate(sample, policy))


def test_notice_and_review_evidence_are_checked_by_content(sample, policy):
    value, artifacts, evidence, _ = sample
    (artifacts / "NOTICE.txt").write_bytes(b"different notice")
    (evidence / "review.txt").write_bytes(b"altered evidence")
    result = evaluate(sample, policy)
    assert {"EVIDENCE_HASH_MISMATCH", "ARTIFACT_HASH_MISMATCH"} <= codes(result)


def test_review_is_bound_to_component_version_and_inventory(sample, policy):
    sample[0]["components"][0]["version"] = "2.0.0"
    result = evaluate(sample, policy)
    assert "REVIEW_SUBJECT_MISMATCH" in codes(result)
    assert not result["production_eligible"]


def test_extra_or_missing_files_block_artifact_scope(sample, policy):
    (sample[1] / "unexpected-binary.bin").write_bytes(b"unreviewed")
    (sample[1] / "program.txt").unlink()
    result = evaluate(sample, policy)
    assert {"ARTIFACT_UNDECLARED", "ARTIFACT_MISSING"} <= codes(result)


def test_symlink_notice_is_not_trusted(sample, policy):
    notice = sample[1] / "NOTICE.txt"
    notice.unlink()
    notice.symlink_to(sample[2] / "review.txt")
    result = evaluate(sample, policy)
    assert "ARTIFACT_SYMLINK" in codes(result) and not result["production_eligible"]


def test_duplicate_json_keys_are_rejected(tmp_path):
    value = tmp_path / "ambiguous.json"
    value.write_text('{"review":"UNKNOWN","review":"APPROVED"}')
    with pytest.raises(checker.InvalidAudit):
        checker.read_json(value)


def test_cli_returns_failure_for_unknown_review_and_writes_new_receipt(sample, policy, tmp_path, capsys):
    sample[0]["release_reviews"]["transitive_closure"]["state"] = "PENDING_REVIEW"
    source = tmp_path / "inventory.json"
    source.write_text(json.dumps(sample[0]))
    receipt = tmp_path / "receipt.json"
    result = checker.main(["--inventory", str(source), "--artifact-root", str(sample[1]),
                           "--evidence-root", str(sample[2]), "--production", "--report", str(receipt)])
    assert result == 2 and json.loads(receipt.read_text())["production_eligible"] is False
    assert "BLOCKED" in capsys.readouterr().out
