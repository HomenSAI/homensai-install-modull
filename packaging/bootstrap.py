"""Точка входа frozen reference; trust root входит в исполняемый bootstrap."""
import argparse
import json
import os
from pathlib import Path
import sys

from homensai_install.gui import main as wizard_main


def main():
    parser = argparse.ArgumentParser(description="HomenSAI Reference DEV — ru/en/de")
    parser.add_argument("--install-root")
    parser.add_argument("--state-dir")
    parser.add_argument("--locale", choices=("ru", "en", "de"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--url-file")
    args = parser.parse_args()
    if not getattr(sys, "frozen", False):
        parser.error("Этот entry point запускается из frozen build; для source используйте модуль gui")
    bundle = Path(sys.executable).parent
    trusted = Path(sys._MEIPASS) / "publisher-trust.json"
    inventory = json.loads((bundle / "inventory.json").read_bytes())
    # Поля считаются недоверенными до SignedPayloadVerifier в GUI/engine.
    root = Path(args.install_root) if args.install_root else Path.home() / "HomenSAI-Reference-DEV"
    state = Path(args.state_dir) if args.state_dir else root.parent / ".homensai-reference-installer"
    argv = ["--manifest", str(bundle / "manifest.json"), "--state-dir", str(state),
            "--payload-dir", str(bundle / "payload"), "--inventory-file", str(bundle / "inventory.json"),
            "--signature-file", str(bundle / "signature.json"), "--release", inventory["release"],
            "--runtime-executable", inventory["runtime_executable"], "--trust-config", str(trusted),
            "--install-root", str(root), "--allow-development"]
    if args.locale:
        argv += ["--locale", args.locale]
    if args.url_file:
        argv += ["--url-file", args.url_file]
    if args.no_browser:
        argv.append("--no-browser")
    return wizard_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
