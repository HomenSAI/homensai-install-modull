"""Fail-closed проверка подготовленного лицензионного inventory.

Не сканер юридических разрешений и не генератор APPROVED. Проверяет записи
доверенного reviewer, SPDX AND/OR/WITH, exact bytes, notices и полноту scope.
Входные policy/inventory должны проходить обычный code review релиза.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path, PurePosixPath

_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9][A-Za-z0-9.+:-]*)")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON = 8 * 1024 * 1024
_MAX_FILES = 50000


class InvalidAudit(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidAudit("duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    with Path(path).open("rb") as handle:
        raw = handle.read(_MAX_JSON + 1)
    if len(raw) > _MAX_JSON:
        raise InvalidAudit("audit JSON exceeds limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(InvalidAudit("non-finite JSON")))


def expression_choices(expression):
    """Ограниченная DNF: tuple[frozenset[license WITH optional-exception]]."""
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 4096:
        raise InvalidAudit("missing or oversized license expression")
    tokens, position = [], 0
    expression = expression.strip()
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if not match:
            raise InvalidAudit("invalid license expression token")
        tokens.append(match.group(1))
        position = match.end()
        if len(tokens) > 256:
            raise InvalidAudit("license expression is too complex")
    index = 0

    def atom(depth):
        nonlocal index
        if depth > 24 or index >= len(tokens):
            raise InvalidAudit("incomplete or nested license expression")
        token = tokens[index]
        index += 1
        if token == "(":
            value = disjunction(depth + 1)
            if index >= len(tokens) or tokens[index] != ")":
                raise InvalidAudit("unclosed license expression")
            index += 1
            return value
        if token in {"AND", "OR", "WITH", ")"}:
            raise InvalidAudit("missing license identifier")
        if index < len(tokens) and tokens[index] == "WITH":
            index += 1
            if index >= len(tokens) or tokens[index] in {"AND", "OR", "WITH", "(", ")"}:
                raise InvalidAudit("missing license exception")
            token += " WITH " + tokens[index]
            index += 1
        return [frozenset((token,))]

    def conjunction(depth):
        nonlocal index
        value = atom(depth)
        while index < len(tokens) and tokens[index] == "AND":
            index += 1
            right = atom(depth)
            if len(value) * len(right) > 64:
                raise InvalidAudit("too many license choices")
            value = list(dict.fromkeys(left | other for left in value for other in right))
        return value

    def disjunction(depth):
        nonlocal index
        value = conjunction(depth)
        while index < len(tokens) and tokens[index] == "OR":
            index += 1
            value.extend(conjunction(depth))
            value = list(dict.fromkeys(value))
            if len(value) > 64:
                raise InvalidAudit("too many license choices")
        return value

    choices = disjunction(0)
    if index != len(tokens):
        raise InvalidAudit("unexpected license expression suffix")
    return tuple(choices)


def selected_licenses(declared, selected):
    choices = expression_choices(declared)
    if selected is None:
        if len(choices) != 1:
            raise InvalidAudit("OR license choice must be explicit")
        return choices[0]
    selected_choices = expression_choices(selected)
    if len(selected_choices) != 1 or selected_choices[0] not in choices:
        raise InvalidAudit("selected license does not satisfy the declared expression")
    return selected_choices[0]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def component_subject_digest(component, artifacts):
    body = {key: value for key, value in component.items() if key not in {"review", "special_review"}}
    body["artifact_bindings"] = sorted(
        [{"path": item.get("path"), "sha256": item.get("sha256")} for item in artifacts
         if isinstance(item, dict) and component.get("component_id") in item.get("components", [])],
        key=lambda item: item["path"] or "")
    return _digest(body)


def release_subject_digest(inventory):
    return _digest({key: value for key, value in inventory.items() if key != "release_reviews"})


def _relative(path):
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path or ":" in path:
        raise InvalidAudit("unsafe evidence path")
    value = PurePosixPath(path)
    if value.is_absolute() or any(item in {"", ".", ".."} for item in path.split("/")):
        raise InvalidAudit("unsafe evidence path")
    return value


def _hash_file(root, relative):
    relative = _relative(relative)
    path = root.joinpath(*relative.parts)
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise InvalidAudit("symlink in artifact/evidence path")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 8 * 1024**3:
        raise InvalidAudit("artifact/evidence must be a bounded regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate(policy, inventory, *, artifact_root=None, evidence_root=None, production=True):
    if not isinstance(policy, dict) or policy.get("schema_version") != 1 or policy.get("production_fail_closed") is not True:
        raise InvalidAudit("unsupported/non-fail-closed policy")
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 1:
        raise InvalidAudit("unsupported inventory")
    rules = policy.get("license_rules")
    required_reviews = policy.get("required_release_reviews")
    if not isinstance(rules, dict) or not isinstance(required_reviews, list) or not required_reviews:
        raise InvalidAudit("missing policy rules")
    components = inventory.get("components")
    files = inventory.get("artifacts")
    if not isinstance(components, list) or not components or len(components) > 4096 or not isinstance(files, list) or len(files) > _MAX_FILES:
        raise InvalidAudit("missing or oversized component/artifact inventory")
    issues, component_results = [], []
    artifact_root = Path(artifact_root).absolute() if artifact_root is not None else None
    evidence_root = Path(evidence_root).absolute() if evidence_root is not None else None

    def issue(code, component=None, detail=None):
        entry = {"code": code}
        if component:
            entry["component_id"] = str(component)[:128]
        if detail:
            entry["detail"] = str(detail)[:300]
        issues.append(entry)

    def evidence(items, component=None, root=None):
        if not isinstance(items, list) or not items or len(items) > 256:
            issue("REVIEW_EVIDENCE_MISSING", component)
            return False
        valid = True
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("sha256"), str) or not _SHA.fullmatch(item["sha256"]):
                issue("EVIDENCE_INVALID", component)
                valid = False
                continue
            try:
                _relative(item.get("path"))
                if root is None:
                    issue("EVIDENCE_ROOT_REQUIRED", component)
                    valid = False
                elif _hash_file(root, item["path"]) != item["sha256"]:
                    issue("EVIDENCE_HASH_MISMATCH", component)
                    valid = False
            except (OSError, InvalidAudit):
                issue("EVIDENCE_UNAVAILABLE", component)
                valid = False
        return valid

    def reviewed(record, component=None, subject=None):
        if not isinstance(record, dict) or record.get("state") != "APPROVED":
            issue("REVIEW_NOT_APPROVED", component, record.get("state", "UNKNOWN") if isinstance(record, dict) else "UNKNOWN")
            return False
        if not isinstance(record.get("reviewer"), str) or not record["reviewer"].strip() or not isinstance(record.get("reviewed_at"), str) or not record["reviewed_at"].strip():
            issue("REVIEW_ATTRIBUTION_MISSING", component)
            return False
        if subject is None or record.get("subject_sha256") != subject:
            issue("REVIEW_SUBJECT_MISMATCH", component)
            return False
        return evidence(record.get("evidence"), component, evidence_root)

    release_digest = release_subject_digest(inventory)
    for name in required_reviews:
        reviewed(inventory.get("release_reviews", {}).get(name), "release:" + name, release_digest)
    identities = set()
    component_map = {}
    for component in components:
        if not isinstance(component, dict):
            raise InvalidAudit("component must be an object")
        identifier = component.get("component_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}", identifier) or identifier in identities:
            raise InvalidAudit("invalid/duplicate component identifier")
        identities.add(identifier)
        component_map[identifier] = component
        start = len(issues)
        scope = component.get("scope")
        if scope not in {"redistributed", "build_only", "host_system", "first_party"}:
            issue("COMPONENT_SCOPE_UNKNOWN", identifier)
        if not isinstance(component.get("version"), str) or not component["version"].strip() or component["version"].upper() in {"UNKNOWN", "LATEST"}:
            issue("COMPONENT_VERSION_UNKNOWN", identifier)
        subject = component_subject_digest(component, files)
        reviewed(component.get("review"), identifier, subject)
        if component.get("transitive_review") != "COMPLETE":
            issue("TRANSITIVE_REVIEW_INCOMPLETE", identifier)
        if component.get("license_conflict") is not False:
            issue("LICENSE_CONFLICT_NOT_CLEARED", identifier)
        if scope == "first_party":
            if component.get("owner_authorization") != "VERIFIED":
                issue("FIRST_PARTY_AUTHORIZATION_UNVERIFIED", identifier)
            selected = frozenset()
        else:
            try:
                selected = selected_licenses(component.get("license_expression"), component.get("selected_expression"))
            except InvalidAudit as error:
                issue("LICENSE_EXPRESSION_UNRESOLVED", identifier, str(error))
                selected = frozenset()
        requirements = set()
        for license_id in selected:
            rule = rules.get(license_id)
            if not isinstance(rule, dict):
                issue("LICENSE_UNKNOWN", identifier, license_id)
                continue
            mode = "build" if scope == "build_only" else "runtime"
            decision = rule.get(mode)
            if decision == "deny":
                issue("LICENSE_DENIED", identifier, license_id)
            elif decision not in {"candidate", "review"}:
                issue("LICENSE_UNKNOWN", identifier, license_id)
            elif decision == "review" and not reviewed(component.get("special_review"), identifier, subject):
                issue("SPECIAL_REVIEW_REQUIRED", identifier, license_id)
            if scope == "redistributed":
                obligations = rule.get("obligations")
                if not isinstance(obligations, list) or not all(isinstance(value, str) for value in obligations):
                    raise InvalidAudit("invalid policy obligations")
                requirements.update(obligations)
        obligations = component.get("obligations", {})
        if not isinstance(obligations, dict):
            raise InvalidAudit("invalid component obligations")
        for name in sorted(requirements):
            obligation = obligations.get(name)
            if not isinstance(obligation, dict) or obligation.get("state") != "SATISFIED":
                issue("OBLIGATION_UNSATISFIED", identifier, name)
            else:
                evidence(obligation.get("evidence"), identifier, evidence_root)
        if scope == "redistributed" and any(name in requirements for name in ("retain_license_notice", "retain_applicable_notice")):
            evidence(component.get("notice_files"), identifier, artifact_root)
        component_results.append({"component_id": identifier, "scope": scope, "selected_licenses": sorted(selected),
                                  "status": "PASS" if len(issues) == start else "BLOCKED"})

    declared = {}
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("sha256"), str) or not _SHA.fullmatch(record["sha256"]):
            raise InvalidAudit("invalid artifact record")
        name = str(_relative(record.get("path")))
        if name in declared:
            raise InvalidAudit("duplicate artifact path")
        declared[name] = record["sha256"]
        owners = record.get("components")
        if not isinstance(owners, list) or not owners or not all(isinstance(owner, str) for owner in owners):
            issue("ARTIFACT_COMPONENT_UNMAPPED", detail=name)
        else:
            for owner in owners:
                component = component_map.get(owner)
                if component is None:
                    issue("ARTIFACT_COMPONENT_UNKNOWN", owner, name)
                elif component.get("scope") in {"build_only", "host_system"}:
                    issue("REDISTRIBUTED_COMPONENT_MISCLASSIFIED", owner, name)
    if not declared:
        issue("ARTIFACT_SCOPE_EMPTY")
    if artifact_root is None:
        issue("ARTIFACT_ROOT_REQUIRED")
    elif not artifact_root.is_dir() or artifact_root.is_symlink():
        issue("ARTIFACT_ROOT_UNSAFE")
    else:
        actual = {}
        for path in artifact_root.rglob("*"):
            if len(actual) >= _MAX_FILES:
                issue("ARTIFACT_SCOPE_TOO_LARGE")
                break
            if path.is_symlink():
                issue("ARTIFACT_SYMLINK", detail=path.relative_to(artifact_root).as_posix())
            elif path.is_file():
                name = path.relative_to(artifact_root).as_posix()
                try:
                    actual[name] = _hash_file(artifact_root, name)
                except (OSError, InvalidAudit):
                    issue("ARTIFACT_READ_FAILED", detail=name)
            elif not path.is_dir():
                issue("ARTIFACT_SPECIAL_FILE", detail=path.relative_to(artifact_root).as_posix())
        for name in sorted(set(actual) - set(declared)):
            issue("ARTIFACT_UNDECLARED", detail=name)
        for name in sorted(set(declared) - set(actual)):
            issue("ARTIFACT_MISSING", detail=name)
        for name in sorted(set(declared) & set(actual)):
            if declared[name] != actual[name]:
                issue("ARTIFACT_HASH_MISMATCH", detail=name)
    return {"schema_version": 1, "policy_id": policy.get("policy_id"), "inventory_id": inventory.get("inventory_id"),
            "mode": "production" if production else "audit", "status": "PASS" if not issues else "BLOCKED",
            "production_eligible": not issues, "issues": issues, "components": component_results,
            "counts": {"components": len(components), "artifacts": len(files), "issues": len(issues)},
            "limitations": ["Checks trusted review records and files; does not infer legal permission or prove a reviewer identity.",
                            "Static/embedded dependency completeness requires an approved closure review bound to this inventory.",
                            "A PASS here does not replace security, publisher-signature or installed-platform acceptance gates."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=str(Path(__file__).resolve().parents[1] / "packaging" / "license-policy.json"))
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--report")
    parser.add_argument("--production", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        result = evaluate(read_json(arguments.policy), read_json(arguments.inventory),
            artifact_root=arguments.artifact_root, evidence_root=arguments.evidence_root, production=arguments.production)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        result = {"schema_version": 1, "status": "BLOCKED", "production_eligible": False,
                  "issues": [{"code": "AUDIT_INPUT_INVALID", "detail": type(error).__name__}]}
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if arguments.report:
        destination = Path(arguments.report)
        # Receipts never silently overwrite review evidence or a release file.
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
    print(encoded, end="")
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
