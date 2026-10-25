"""Read-only refresh of the 04 reference audit for the pruned reference profile.

This reuses reviewed source-evidence candidates, never grants legal approval,
and never executes or writes inside the inspected bundle. It is deliberately
not a general-purpose binary license detector. Run check_licenses.py separately.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import stat
import sysconfig
import zipfile

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
PRUNED_COMPONENTS = {"pbs-bdb", "pbs-tcl", "pbs-tk", "pbs-libX11", "pbs-libXau", "pbs-libxcb",
                     "itcl", "tcl-thread", "tk-demo-assets", "ensurepip-pip"}
PRUNED_MODULES = {"dbm", "_dbm", "_gdbm", "ensurepip", "pip", "tkinter", "_tkinter", "idlelib", "turtledemo"}
PRUNED_DIR_PREFIXES = ("tcl", "tk", "itcl", "thread", "libtcl")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_record(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise ValueError("Audit requires regular files, without symlinks")
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Audit input changed during hashing")
    return {"sha256": value.hexdigest(), "size": before.st_size}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def candidate_pending(component):
    value = copy.deepcopy(component)
    value["review"] = {"state": "PENDING_REVIEW", "reviewer": None, "reviewed_at": None, "evidence": []}
    value["transitive_review"] = "PENDING_REVIEW"
    value["license_conflict"] = None
    value["notice_files"] = []
    value["obligations"] = {}
    value.pop("special_review", None)
    if value["scope"] == "first_party":
        value["owner_authorization"] = "PENDING_PRODUCT_LICENSE_AND_EULA"
    return value


def frozen_inventory(path):
    archive = CArchiveReader(str(path))
    entries = []
    for name in sorted(archive.toc):
        raw = archive.extract(name)
        entries.append({"path": name, "size": len(raw), "sha256": digest(raw)})
    pyz = archive.open_embedded_archive("PYZ.pyz")
    return entries, sorted(pyz.toc)


def native_wheel_bindings(snapshot, wheel_dir, frozen_entries):
    """Match frozen native bytes to verified official wheel member bytes."""
    if wheel_dir is None:
        return {"status": "NOT_RUN", "matches": []}
    frozen = {entry["path"]: entry for entry in frozen_entries}
    matches = []
    for package in snapshot["packages"]:
        if package["name"] not in {"cffi", "cryptography"}:
            continue
        for wheel in package["files"]:
            if "linux-x64" not in wheel["targets"]:
                continue
            path = wheel_dir / wheel["filename"]
            observed = file_record(path)
            if observed != {"size": wheel["size"], "sha256": wheel["sha256"]}:
                raise ValueError("Cached official wheel bytes differ from pinned snapshot")
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name not in frozen or not name.endswith(".so"):
                        continue
                    raw = archive.read(name)
                    if digest(raw) == frozen[name]["sha256"]:
                        matches.append({"component": package["name"], "version": package["version"],
                                        "frozen_entry": name, "entry_sha256": digest(raw),
                                        "wheel_filename": wheel["filename"], "wheel_sha256": wheel["sha256"],
                                        "official_url": wheel["url"]})
    return {"status": "OBSERVED", "matches": matches,
            "limitation": "Exact native members only; does not approve licenses or prove every installed Python source file"}


def refresh(bundle, output, wheel_dir=None):
    bundle = bundle.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(bundle):
        raise ValueError("Audit output must be outside the inspected bundle")
    baseline_path = ROOT / "packaging/audit-evidence/reference-dev-bundle-04.inventory.json"
    prior_receipt_path = ROOT / "packaging/audit-evidence/reference-dev-bundle-04.receipt.json"
    baseline, prior_receipt = load(baseline_path), load(prior_receipt_path)
    if any(component["review"]["state"] == "APPROVED" for component in baseline["components"]):
        raise ValueError("This factual refresh must not silently inherit reviewed approvals")
    files, build_work = {}, []
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks require explicit inventory review")
        if path.is_dir():
            continue
        name = path.relative_to(bundle).as_posix()
        record = {"path": name, **file_record(path)}
        if name.startswith("build-work/"):
            build_work.append(record)
        else:
            files[name] = record
    signed = load(bundle / "inventory.json")
    actual = {name.removeprefix("payload/"): value["sha256"] for name, value in files.items() if name.startswith("payload/")}
    declared = signed["files"]
    differences = {"missing": sorted(set(declared) - set(actual)),
                   "extra": sorted(set(actual) - set(declared)),
                   "changed": sorted(name for name in set(actual) & set(declared) if actual[name] != declared[name])}
    payload_check = {"status": "MATCH" if not any(differences.values()) else "MISMATCH",
                     "declared_files": len(declared), "actual_files": len(actual), **differences,
                     "signature_verification": "NOT_RUN_BY_LICENSE_SCAN"}
    frozen_entries, modules = frozen_inventory(bundle / "HomenSAI-Reference-DEV")
    forbidden_paths = []
    for name in files:
        parts = Path(name).parts
        if name.startswith("payload/runtime/"):
            if any(part in PRUNED_MODULES for part in parts):
                forbidden_paths.append(name)
            elif any(Path(name).name.startswith(mod + ".") for mod in ("_dbm", "_gdbm", "_tkinter")):
                forbidden_paths.append(name)
            elif name.startswith("payload/runtime/lib/") and parts[3].startswith(PRUNED_DIR_PREFIXES):
                forbidden_paths.append(name)
    forbidden_frozen = sorted(item["path"] for item in frozen_entries
                              if Path(item["path"]).name.startswith(("_dbm.", "_gdbm.", "_tkinter.")))
    forbidden_modules = sorted(name for name in modules if name.split(".")[0] in PRUNED_MODULES)
    pruning_verified = not (forbidden_paths or forbidden_frozen or forbidden_modules)
    if not pruning_verified:
        raise ValueError("The requested minimal-profile pruning is not present; retain full component inventory")
    removed = PRUNED_COMPONENTS | {component["component_id"] for component in baseline["components"]
                                   if component["component_id"].startswith("pip-vendor-")}
    components = [candidate_pending(c) for c in baseline["components"] if c["component_id"] not in removed]
    identifiers = {component["component_id"] for component in components}
    previous_artifacts = {record["path"]: record for record in baseline["artifacts"]}
    artifacts, unknown = [], []
    for name, record in files.items():
        if name in previous_artifacts:
            owners = previous_artifacts[name]["components"]
            if set(owners) & removed:
                raise ValueError("A pruned component still owns a delivered artifact")
        elif (name == "payload/reference-profile.json"
              or name.startswith("payload/runtime/lib/python3.14/site-packages/homensai_reference")
              or name.startswith(("payload/vendor/homensai_reference-", "payload/legal/", "payload/app/"))):
            owners = ["homensai-first-party"]
        else:
            owners = ["unclassified-new-artifacts"]
            unknown.append(name)
        if name == "HomenSAI-Reference-DEV":
            owners = [owner for owner in owners if owner in identifiers]
        artifacts.append({"path": name, "sha256": record["sha256"], "components": owners})
    if unknown:
        components.append({"component_id": "unclassified-new-artifacts", "name": "Unclassified new files",
                           "version": "UNKNOWN", "scope": "redistributed", "license_expression": None,
                           "selected_expression": None, "review": {"state": "PENDING_REVIEW"},
                           "transitive_review": "PENDING_REVIEW", "license_conflict": None,
                           "notice_files": [], "obligations": {}, "observed_evidence": unknown,
                           "open_issues": ["New artifact mapping needs specific review; no automatic license inference."]})
    by_id = {component["component_id"]: component for component in components}
    lib_name = "payload/runtime/lib/libpython3.14.so.1.0"
    frozen_lib = next(item for item in frozen_entries if item["path"] == "libpython3.14.so.1.0")
    lib_identity = {"payload_sha256": files[lib_name]["sha256"],
                    "prior_bundle_04_sha256": previous_artifacts[lib_name]["sha256"],
                    "frozen_sha256": frozen_lib["sha256"]}
    lib_identity["all_equal"] = len(set(lib_identity.values())) == 1
    by_id["cpython"]["observed_evidence"].append("Fresh payload/frozen libpython hashes compared with historical 04; see new receipt.")
    by_id["cpython"]["source_evidence"].append("packaging/runtime-source-receipt.json")
    for component in components:
        if component["component_id"].startswith("pbs-"):
            component["observed_evidence"].append("Retained native dependency candidate; removing Python modules does not remove builtin libpython bytes.")
    snapshot_path = ROOT / "packaging/build-dependencies.json"
    snapshot = load(snapshot_path)
    bindings = native_wheel_bindings(snapshot, wheel_dir, frozen_entries)
    for match in bindings["matches"]:
        component = by_id["pypi-" + match["component"]]
        component["observed_evidence"].append("Exact frozen native member equals verified official wheel: " + match["wheel_filename"])
        component["open_issues"] = ["Native member is now bound to a verified wheel; complete runtime/notice/license/source obligations still require review."]
    sites = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    environment = []
    for item in sorted(metadata.distributions(path=sites), key=lambda d: d.metadata["Name"].lower()):
        name = item.metadata["Name"].lower().replace("_", "-")
        path = Path(item._path) / "METADATA"
        environment.append({"name": name, "version": item.version, "metadata_sha256": file_record(path)["sha256"]})
    build_report = load(bundle / "BUILD_REPORT.json")
    inventory_id = bundle.name + "-2026-10-25"
    source_receipt = load(ROOT / "packaging/runtime-source-receipt.json")
    inventory = {**copy.deepcopy(baseline), "inventory_id": inventory_id, "components": components, "artifacts": artifacts,
                 "derived_from": {"inventory": baseline_path.name, "sha256": file_record(baseline_path)["sha256"]},
                 "payload_integrity": payload_check,
                 "minimal_profile": {"pruned_component_ids": sorted(removed), "file_and_module_absence_checked": True,
                                     "static_binary_closure_complete": False},
                 "runtime_origin": {**baseline["runtime_origin"],
                                    "expected_archive_sha256_reconstructed": source_receipt["upstream_sha256"],
                                    "reconstructed_digest_is_historical_verification": False},
                 "release_reviews": {name: {"state": "PENDING_REVIEW", "reviewer": None, "reviewed_at": None, "evidence": []}
                                     for name in baseline["release_reviews"]}}
    if payload_check["status"] != "MATCH":
        inventory["limitations"].append("PAYLOAD INTEGRITY MISMATCH: extra/missing/changed files are retained as findings, never ignored.")
    prior_names, new_names = set(previous_artifacts), set(files)
    delta = {"removed_artifact_paths": sorted(prior_names - new_names),
             "new_artifact_paths": sorted(new_names - prior_names),
             "changed_artifact_paths": sorted(name for name in prior_names & new_names
                                              if previous_artifacts[name]["sha256"] != files[name]["sha256"]),
             "unchanged_artifacts": sum(previous_artifacts[name]["sha256"] == files[name]["sha256"] for name in prior_names & new_names)}
    receipt = {"schema_version": 1, "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "bundle": bundle.name, "build_report": build_report,
               "selected_artifact_count": len(artifacts), "component_candidates": len(components),
               "payload_integrity": payload_check,
               "frozen_executable": {**file_record(bundle / "HomenSAI-Reference-DEV"), "entries": frozen_entries,
                                     "pyz_module_count": len(modules), "pyz_modules": modules},
               "native_official_wheel_bindings": bindings, "libpython_identity": lib_identity,
               "minimal_profile": {"pruning_verified_by_paths_and_module_names": pruning_verified,
                                   "forbidden_payload_paths": forbidden_paths, "forbidden_frozen_entries": forbidden_frozen,
                                   "forbidden_pyz_modules": forbidden_modules, "removed_component_ids": sorted(removed)},
               "build_environment": environment, "build_work_files_not_declared_as_release": build_work,
               "new_first_party_mapping_evidence": {
                   "payload/reference-profile.json": "packaging/build_reference.py writes validated HomenSAI reference profile JSON; no third-party code.",
                   "profile_and_storage_modules": "reference/src/homensai_reference/profile.py and storage.py are HomenSAI first-party sources."},
               "baseline_delta": delta,
               "inherited_source_evidence": {"prior_receipt": prior_receipt_path.name,
                                            "prior_receipt_sha256": file_record(prior_receipt_path)["sha256"],
                                            "native_recipe_pins": prior_receipt["native_recipe_pins"],
                                            "original_runtime_probe_was_not_rerun": True,
                                            "prior_python_facts": prior_receipt["facts"]},
               "scan_script_sha256": file_record(Path(__file__))["sha256"],
               "build_dependency_snapshot_sha256": file_record(snapshot_path)["sha256"],
               "receipt_kind": "READ_ONLY_FACTUAL_DELTA; ALL COMPONENTS PENDING_REVIEW; NO PRODUCTION APPROVAL"}
    # Exclusive creation prevents accidentally replacing historical evidence.
    output.mkdir(parents=True, exist_ok=True)
    for suffix, value in (("inventory", inventory), ("receipt", receipt)):
        with (output / (bundle.name + "." + suffix + ".json")).open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return {"bundle": bundle.name, "payload_integrity": payload_check,
            "artifacts": len(artifacts), "components": len(components), "removed_components": len(removed),
            "frozen_entries": len(frozen_entries), "pyz_modules": len(modules), "native_wheel_matches": bindings["matches"],
            "build_work_files": len(build_work), "unclassified_artifacts": unknown,
            "license_review": "PENDING_REVIEW", "production_eligible": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "packaging/audit-evidence")
    parser.add_argument("--wheel-dir", type=Path)
    args = parser.parse_args(argv)
    result = refresh(args.bundle, args.output, args.wheel_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
