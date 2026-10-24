# HomenS.AI · © 2026 Serhii Khomenko
"""Детерминированные ТЕКСТЫ Linux v2. Никакого исполнения или записи файлов."""

from __future__ import annotations

from pathlib import PurePosixPath
import re
import shlex

from shared.manifest import ManifestError, validate_manifest
from shared.services import component_order, render_services


def _path(value: str, *, base: PurePosixPath | None = None) -> PurePosixPath:
    if (type(value) is not str or not value or "\\" in value or ":" in value
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                   or c in "\u0085\u2028\u2029" for c in value)
            or any(part in (".", "..") for part in value.split("/"))
            or "//" in value):
        raise ManifestError("Linux: небезопасный путь")
    path = PurePosixPath(value)
    if not path.is_absolute():
        if base is None:
            raise ManifestError("Linux: требуется абсолютный путь")
        path = base / path
    if path == PurePosixPath("/"):
        raise ManifestError("Linux: корень ФС запрещён")
    return path


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _unit_path(value):
    # EnvironmentFile/WorkingDirectory: $ не раскрывается, % — specifier.
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _script(lines, *, root=False):
    header = ["#!/bin/sh", "# HomenS.AI · © 2026 Serhii Khomenko",
              "# Сгенерированный текст; генератор этот сценарий не исполнял.",
              "set -eu", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "export PATH", "umask 077"]
    if root:
        header += ['if [ "$(id -u)" -ne 0 ]; then',
                   "    printf '%s\\n' 'Требуются права root' >&2", "    exit 1", "fi"]
    return "\n".join([*header, "", *lines, ""])


def render_linux(manifest: dict, *, modes: tuple[str, ...], install_root: str,
                 user: str = "homensai", environment_file: str | None = None) -> dict[str, str]:
    """Вернуть плоский bundle: три .sh, units и отдельные *.override.conf.

    Все входные строки публичные. Секреты принимаются только как дескрипторы
    имён в manifest; API не принимает env и никогда его не читает.
    """
    manifest = validate_manifest(manifest)
    order = component_order(manifest, modes)
    if (type(user) is not str or re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", user) is None
            or user == "root"):
        raise ManifestError("Linux: требуется имя отдельной непривилегированной учётной записи")
    base = _path(install_root)
    paths = {key: _path(value, base=base) for key, value in manifest["paths"].items()}
    roots = list(paths.values())
    if any(_overlap(a, b) for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ManifestError("Linux: каталоги установки пересекаются")
    prefix = manifest["product_id"] + "-"
    env_path = _path(environment_file if environment_file is not None else
                     "/etc/" + manifest["product_id"] + "/service.env")
    unit_dir = PurePosixPath("/etc/systemd/system")
    if (len(env_path.parent.parts) < 3 or _overlap(env_path.parent, unit_dir)
            or any(_overlap(env_path.parent, path) or _overlap(unit_dir, path) for path in roots)):
        raise ManifestError("Linux: файл секретов/units пересекается с каталогами установки")

    for component in manifest["components"]:
        if ("$" in component["cmd"][0]
                or any(any(c in "\u0085\u2028\u2029" or 0xD800 <= ord(c) <= 0xDFFF
                           for c in arg) for arg in component["cmd"])):
            raise ManifestError("Linux: недопустимый executable или символ команды")
    names = sorted(item["name"] for item in manifest["secrets"] if "service" in item["contexts"])
    # Имена не должны подменять фиксированный loopback-контракт в EnvironmentFile.
    if set(names) & {"HOMENSAI_WEB_HOST", "HOMENSAI_WEB_PORT"}:
        raise ManifestError("Linux: имя секрета конфликтует с loopback-настройками")
    units = render_services(manifest, platform="linux", modes=modes, install_root=str(base))
    result = {}
    for filename, text in units.items():
        # Доставка секретов только через защищённый EnvironmentFile.
        result[filename] = "\n".join(line for line in text.split("\n")
                                      if not line.startswith("PassEnvironment="))
        result[filename + ".override.conf"] = "\n".join([
            "[Service]", "User=" + user, "Group=" + user, "UMask=0077",
            "NoNewPrivileges=true", "EnvironmentFile=" + _unit_path(env_path), ""])

    q = lambda value: shlex.quote(str(value))
    install = [
        '# Bundle должен содержать .service и .override.conf рядом с install.sh.',
        'bundle_dir=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)',
        f"if ! getent group {q(user)} >/dev/null; then",
        f"    groupadd --system -- {q(user)}", "fi",
        f"if ! getent passwd {q(user)} >/dev/null; then",
        f"    useradd --system --gid {q(user)} --no-create-home --home-dir /nonexistent "
        f"--shell /usr/sbin/nologin -- {q(user)}", "fi",
        f'if [ "$(id -u -- {q(user)})" -eq 0 ]; then',
        "    printf '%s\\n' 'Учётная запись имеет UID 0' >&2", "    exit 1", "fi",
    ]
    for key in ("app", "releases", "runtime", "services", "data", "backup", "config"):
        owner, mode = (user, "0700") if key in ("data", "backup", "config") else ("root", "0755")
        install.append(f"install -d -m {mode} -o {q(owner)} -g {q(owner)} -- {q(paths[key])}")
    install += [
        f"install -d -m 0700 -o root -g root -- {q(env_path.parent)}",
        f"if [ -L {q(env_path)} ] || {{ [ -e {q(env_path)} ] && [ ! -f {q(env_path)} ]; }}; then",
        "    printf '%s\\n' 'Небезопасный тип файла окружения' >&2", "    exit 1", "fi",
        f"if [ ! -e {q(env_path)} ]; then",
        f"    install -m 0600 -o root -g root -- /dev/null {q(env_path)}",
    ]
    for name in names:
        # Только комментарии с именами: нет даже пустых NAME= назначений.
        install.append(f"    printf '%s\\n' {q('# ' + name)} >> {q(env_path)}")
    install += ["fi", f"chown root:root -- {q(env_path)}", f"chmod 0600 -- {q(env_path)}",
                f"install -d -m 0755 -o root -g root -- {q(unit_dir)}"]
    for name in order:
        filename = prefix + name + ".service"
        destination = unit_dir / filename
        override_dir = unit_dir / (filename + ".d")
        install += [
            f'install -m 0644 -o root -g root -- "$bundle_dir"/{q(filename)} {q(destination)}',
            f"install -d -m 0755 -o root -g root -- {q(override_dir)}",
            f'install -m 0644 -o root -g root -- "$bundle_dir"/{q(filename + ".override.conf")} '
            f"{q(override_dir / 'override.conf')}",
        ]
    install += ["printf '%s\\n' 'Файлы подготовлены. Заполните защищённый EnvironmentFile вручную.'",
                "printf '%s\\n' 'Payload, runtime, миграции и запуск служб требуют отдельной приёмки.'"]
    result["install.sh"] = _script(install, root=True)

    uninstall = ["# Данные, программа, runtime, архивы, секреты и учётная запись сохраняются."]
    for name in reversed(order):
        filename = prefix + name + ".service"
        uninstall += [f"systemctl stop -- {q(filename)}", f"systemctl disable -- {q(filename)}"]
    for name in reversed(order):
        destination = unit_dir / (prefix + name + ".service")
        override_dir = unit_dir / (destination.name + ".d")
        uninstall += [f"rm -f -- {q(override_dir / 'override.conf')} {q(destination)}",
                      f"rmdir -- {q(override_dir)} 2>/dev/null || :"]
    uninstall += ["systemctl daemon-reload"]
    result["uninstall.sh"] = _script(uninstall, root=True)
    # show выбирает только публичные свойства, без Environment и журнальных строк.
    status = ["failed=0"]
    for name in order:
        filename = prefix + name + ".service"
        status += [f"systemctl show --no-pager --property=Id,LoadState,ActiveState,SubState -- {q(filename)}",
                   f"systemctl is-active --quiet -- {q(filename)} || failed=1"]
    status += ['exit "$failed"']
    result["status.sh"] = _script(status)
    return result
