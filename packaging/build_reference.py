"""Повторяемая процедура сборки DEV reference bundle; release gate отдельно.

Сетевых скачиваний нет. Передать существующий проверенный Python 3.14 runtime.
Тестовый ключ создаётся в памяти либо читается из явно заданного приватного файла.
Ключ никогда не включается в bundle. Побайтовая reproducible-build приёмка не заявлена.
Production signing этим скриптом не предоставляется.
"""
from __future__ import annotations
import argparse
import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from homensai_install.trust import canonical_bytes, hash_manifest
from shared.adapters_fs import _tree
from shared.manifest import validate_manifest


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def frozen_resource_hashes(root=ROOT):
    """Read the SDK's declared data contract from this source tree only."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    resources = {}
    for package, patterns in project["tool"]["setuptools"]["package-data"].items():
        directory = root.joinpath(*package.split("."))
        for pattern in patterns:
            matches = sorted(path for path in directory.glob(pattern) if path.is_file())
            if not matches and not glob.has_magic(pattern):
                raise RuntimeError(f"Declared frozen resource pattern is empty: {package}/{pattern}")
            for path in matches:
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise RuntimeError("Frozen resources must be regular files in the source tree")
                resources[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not resources:
        raise RuntimeError("Declared frozen resource contract is empty")
    return resources


def verify_frozen_resources(executable, expected):
    """Check the actual one-file archive, not just PyInstaller's exit status."""
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    packaged = {name for name in archive.toc if name.startswith(("homensai_install/", "shared/"))}
    if packaged != set(expected):
        raise RuntimeError("Frozen SDK resource inventory differs from the declared source contract")
    for name, digest in expected.items():
        if hashlib.sha256(archive.extract(name)).hexdigest() != digest:
            raise RuntimeError(f"Frozen SDK resource bytes differ from source: {name}")
    return {"files_checked": len(expected),
            "manifest_sha256": hashlib.sha256(canonical_bytes(expected)).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release", default="reference-1.0.0-dev")
    parser.add_argument("--port", type=int, default=18480)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--development-key-file", type=Path,
                        help="Optional private 32-byte DEV key for a series of update tests; never packaged")
    args = parser.parse_args(argv)
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64"):
        parser.error("Этот builder пока принят только для Linux x64")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", args.release):
        parser.error("Небезопасный release ID")
    if not 1024 <= args.port <= 65535:
        parser.error("Port должен быть 1024..65535")
    sys.path.insert(0, str(ROOT / "reference/src"))
    from homensai_reference.profile import DEFAULT_PROFILE, read_json, validate_profile
    profile = validate_profile(read_json(args.profile.absolute())) if args.profile else dict(DEFAULT_PROFILE)
    runtime = args.runtime.resolve(strict=True)
    executable = runtime / "bin/python3.14"
    version = subprocess.check_output([str(executable), "-I", "-c", "import platform;print(platform.python_version())"], text=True).strip()
    if not version.startswith("3.14."):
        parser.error("Требуется Python 3.14.x")
    if not (runtime / "lib/python3.14/LICENSE.txt").is_file():
        parser.error("Python LICENSE.txt отсутствует")
    for candidate in runtime.rglob("*"):
        if candidate.is_symlink() and not candidate.resolve().is_relative_to(runtime):
            parser.error("Runtime содержит ссылку за пределы проверенного дерева")
    out = args.output.absolute()
    out.mkdir(parents=True, exist_ok=False)
    build = out / "build-work"
    build.mkdir()
    payload = out / "payload"
    payload.mkdir()
    # Собственный настоящий wheel с сервером и migration, без скачивания dependencies.
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation",
                    "--outdir", str(build / "wheel"), str(ROOT / "reference")], check=True)
    wheel = next((build / "wheel").glob("*.whl"))
    vendor = payload / "vendor"
    vendor.mkdir()
    shutil.copy2(wheel, vendor / wheel.name)
    # Дерево изолированного runtime копируется с разыменованием внутренних ссылок;
    # runtime pycache/pip не нужны конечному продукту. Все остальные bytes подписываются.
    def ignore(directory, names):
        result = [n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))]
        if Path(directory).name == "site-packages":
            result.extend(n for n in names if n != "README.txt")
        if Path(directory) == runtime / "bin":
            result.extend(n for n in names if n.startswith(("pip", "idle", "pydoc")))
        if Path(directory) == runtime / "share":
            # Web-only runtime does not use terminal descriptions; their Unix
            # case-colliding paths cannot enter a portable signed inventory.
            result.extend(n for n in names if n == "terminfo")
        if Path(directory) == runtime / "lib":
            result.extend(n for n in names if n.startswith(("tcl", "tk", "itcl", "thread", "libtcl")))
        if Path(directory) == runtime / "lib/python3.14":
            result.extend(n for n in names if n in {"ensurepip", "tkinter", "idlelib", "turtledemo", "dbm"})
        if Path(directory) == runtime / "lib/python3.14/lib-dynload":
            # Berkeley DB source obligations are not approved for this profile.
            # These extension files are not required by the SQLite web product.
            result.extend(n for n in names if n.startswith(("_dbm.", "_gdbm.", "_tkinter.")))
        return result
    shutil.copytree(runtime, payload / "runtime", symlinks=False, ignore=ignore)
    site = payload / "runtime/lib/python3.14/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            dest = site / item.filename
            if not dest.resolve().is_relative_to(site.resolve()) or item.filename.startswith("/"):
                raise RuntimeError("Unsafe own wheel entry")
        archive.extractall(site)
    shutil.copytree(ROOT / "reference/legal", payload / "legal")
    write_json(payload / "reference-profile.json", profile)
    locks = payload / "locks"
    locks.mkdir()
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    text = f"homensai-reference==1.0.0 --hash=sha256:{digest}\n"
    for target in ("linux", "windows"):
        (locks / (target + ".lock")).write_text(text)
    write_json(payload / "runtime-provenance.json", {"python": version,
        "source": "caller-supplied runtime; exact bytes covered by signed inventory",
        "license_review": "PENDING_TRANSITIVE_RUNTIME_REVIEW", "production_ready": False})
    manifest = json.loads((ROOT / "examples/rent.manifest.json").read_text())
    manifest.update(product_id="homensai-reference", product_name="HomenSAI Reference DEV")
    manifest["paths"] = {key: key for key in ("app", "releases", "data", "backup", "services", "config", "runtime")}
    manifest["runtime"]["lock_files"] = {target: {"file": "locks/" + target + ".lock",
        "sha256": hashlib.sha256((locks / (target + ".lock")).read_bytes()).hexdigest()} for target in ("windows", "linux")}
    manifest["runtime"]["vendor_wheels"] = [{"file": "vendor/" + wheel.name, "sha256": digest}]
    manifest["components"] = [{"name": "web", "cmd": ["python", "-m", "homensai_reference.server"], "after": [],
        "heartbeat": {"file": "web.json", "interval": 10, "timeout": 30},
        "restart": {"delay": 2, "max": 3, "window": 60}}]
    manifest["modes"] = [{"name": "local", "components": ["web"]}]
    manifest["mutual_exclusion"] = []
    manifest["web"] = {"host": "127.0.0.1", "port": args.port, "healthz": "/healthz"}
    manifest["secrets"] = []
    if profile["require_api_key"]:
        manifest["secrets"] = [{"name": "REFERENCE_API_KEY", "required": True,
                                "contexts": ["service"], "same_value": True}]
    manifest["migrations"]["cmd"] = ["python", "-m", "homensai_reference.migrate"]
    manifest["backup"]["cmd"] = ["python", "-m", "homensai_reference.archive"]
    manifest["restore"]["cmd"] = ["python", "-m", "homensai_reference.archive"]
    manifest["hooks"] = {"pre_start": [], "health_checks": [["python", "-m", "homensai_reference.archive"]], "post_update": []}
    manifest["update"]["health_wait"] = 20
    validate_manifest(manifest)
    write_json(out / "manifest.json", manifest)
    _, files = _tree(payload)
    inventory = {"schema_version": 1, "product_id": manifest["product_id"], "release": args.release,
        "target": {"os": "linux", "arch": "x64", "mode": "native"}, "files": files,
        "development": True, "manifest_sha256": hash_manifest(manifest),
        "runtime_executable": "runtime/bin/python3.14"}
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    if args.development_key_file:
        from shared.datahome import checked_path
        key_path = checked_path(args.development_key_file.absolute())
        info = key_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 32
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.geteuid()):
            parser.error("DEV key must be an owned private 0600 regular 32-byte file")
        key = Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())
    else:
        key = Ed25519PrivateKey.generate()
    key_id = "reference-dev-" + hashlib.sha256(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).hexdigest()[:12]
    write_json(out / "inventory.json", inventory)
    write_json(out / "signature.json", {"algorithm": "ed25519", "key_id": key_id,
        "signature": base64.b64encode(key.sign(canonical_bytes(inventory))).decode()})
    write_json(out / "publisher-trust.json", {"schema_version": 1,
        "keys": {key_id: base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}})
    # The private key is never exported or included in the bundle. An explicitly
    # supplied private DEV key lets test upgrades retain the same publisher identity.
    del key
    frozen_resources = None
    if args.freeze:
        expected_resources = frozen_resource_hashes()
        # Explicit source paths avoid ambient imports during spec evaluation
        # and do not collect undeclared files such as source-control placeholders.
        resource_arguments = []
        for name in sorted(expected_resources):
            resource_arguments.extend(["--add-data", str(ROOT / name) + ":" + str(Path(name).parent)])
        environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(build / "pyinstaller-cache"))
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", "HomenSAI-Reference-DEV", "--paths", str(ROOT),
            "--distpath", str(out), "--workpath", str(build / "freeze"), "--specpath", str(build),
            *resource_arguments,
            "--hidden-import", "cryptography.hazmat.primitives.asymmetric.ed25519",
            "--exclude-module", "_dbm", "--exclude-module", "dbm", "--exclude-module", "ensurepip",
            "--exclude-module", "tkinter",
            "--add-data", str(out / "publisher-trust.json") + ":.",
            str(ROOT / "packaging/bootstrap.py")], env=environment, cwd=ROOT, check=True)
        frozen_resources = verify_frozen_resources(out / "HomenSAI-Reference-DEV", expected_resources)
    write_json(out / "BUILD_REPORT.json", {"schema_version": 1, "release": args.release,
        "python": version, "target": inventory["target"], "reference_profile": profile, "files": len(files),
        "payload_bytes": sum((payload / name).stat().st_size for name in files),
        "development": True, "production_ready": False, "freeze": args.freeze,
        "frozen_resources": frozen_resources,
        "blockers": ["production publisher signature/certificate", "runtime transitive licence audit",
                     "approved product EULA", "Windows/Docker/system-service acceptance"]})
    print(json.dumps({"output": str(out), "files": len(files), "production_ready": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
