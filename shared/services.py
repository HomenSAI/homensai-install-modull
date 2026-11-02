# HomenS.AI · © 2026 Serhii Khomenko
"""Только тексты служб v2 и чистая проверка WinSW; установки/процессов нет."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any
import xml.etree.ElementTree as ET

from shared.manifest import ManifestError, validate_manifest


def component_order(manifest: dict[str, Any], modes: tuple[str, ...]) -> tuple[str, ...]:
    """Выбор режимов и топологический порядок; остановка в обратном порядке."""
    manifest = validate_manifest(manifest)
    if manifest["schema_version"] != 2:
        raise ManifestError("Службы и обновление требуют манифест v2")
    known = {item["name"]: item["components"] for item in manifest["modes"]}
    if not modes or len(set(modes)) != len(modes) or set(modes) - known.keys():
        raise ManifestError("Требуется явный допустимый набор режимов")
    if any(len(set(modes) & set(group)) > 1 for group in manifest["mutual_exclusion"]):
        raise ManifestError("Режимы взаимно исключаются")
    selected = {name for mode in modes for name in known[mode]}
    remaining = {item["name"]: set(item["after"]) for item in manifest["components"]
                 if item["name"] in selected}
    result = []
    while remaining:
        ready = sorted(name for name, after in remaining.items() if not after)
        result.extend(ready)
        remaining = {name: after - set(ready) for name, after in remaining.items() if name not in ready}
    return tuple(result)


def verify_winsw(payload: bytes, sha256: str) -> bool:
    """Чистая функция: проверить байты по явно закреплённому digest, без I/O."""
    if (type(payload) is not bytes or type(sha256) is not str
            or re.fullmatch(r"[a-f0-9]{64}", sha256) is None):
        return False
    return hmac.compare_digest(hashlib.sha256(payload).hexdigest(), sha256)


def _windows_arg(value: str) -> str:
    # CRT argv quoting, без subprocess. Всегда заключаем в кавычки.
    value = re.sub(r'(\\*)"', lambda match: match[1] * 2 + '\\"', value)
    value = re.sub(r'\\+$', lambda match: match[0] * 2, value)
    return '"' + value + '"'


def _unit_arg(value: str) -> str:
    # systemd: кавычки, C escapes, literal specifiers и environment expansion.
    return '"' + (value.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("%", "%%").replace("$", "$$")) + '"'


def render_services(manifest: dict[str, Any], *, platform: str,
                    modes: tuple[str, ...], install_root: str,
                    winsw_payload: bytes | None = None) -> dict[str, str]:
    """Вернуть XML/units. Для Windows обязательна проверка предоставленных байтов.

    cmd и пути — публичные данные, секреты допускаются только в secrets[].name.
    Окружение процесса и файлы секретов никогда не читаются.
    """
    manifest = validate_manifest(manifest)
    order = component_order(manifest, modes)
    if platform not in ("windows", "linux"):
        raise ManifestError("Неизвестный формат службы")
    path_type = PureWindowsPath if platform == "windows" else PurePosixPath
    base = path_type(install_root)
    app = path_type(manifest["paths"]["app"])
    if (not base.is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in install_root)
            or ".." in base.parts or ".." in app.parts):
        raise ManifestError("Требуется абсолютный корень целевой ОС")
    if ((platform == "linux" and PureWindowsPath(manifest["paths"]["app"]).drive)
            or (platform == "windows" and manifest["paths"]["app"].startswith("/"))):
        raise ManifestError("Рабочий каталог задан для другой ОС")
    app_text = str(app if app.is_absolute() else base / app)
    if not path_type(app_text).is_absolute():
        raise ManifestError("Требуется абсолютный рабочий каталог")
    if platform == 'linux' and '\\' in app_text:
        raise ManifestError('systemd: обратная косая черта в рабочем каталоге не поддерживается')
    if platform == "windows" and ("winsw" not in manifest or not verify_winsw(
            winsw_payload, manifest["winsw"]["sha256"])):
        raise ManifestError("WinSW: отсутствует pin или SHA-256 не совпадает")
    names = sorted(item["name"] for item in manifest["secrets"] if "service" in item["contexts"])
    if set(names) & {"HOMENSAI_WEB_HOST", "HOMENSAI_WEB_PORT"}:
        raise ManifestError("Имя секрета конфликтует с loopback-настройками")
    components = {item["name"]: item for item in manifest["components"]}
    prefix = manifest["product_id"] + "-"
    result = {}
    for name in order:
        item = components[name]
        restart = item["restart"]
        if platform == "windows":
            # %NAME% — синтаксис подстановки WinSW: произвольный % в argv/пути
            # нельзя безопасно представить как literal для всех версий wrapper.
            if any("%" in value for value in [*item["cmd"], app_text]):
                raise ManifestError("WinSW: подстановка в cmd/пути запрещена")
            if any(any(0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF) for c in text)
                   for text in [*item["cmd"], app_text]):
                raise ManifestError("WinSW: недопустимые символы XML")
            root = ET.Element("service")
            for tag, text in (("id", prefix + name), ("name", prefix + name),
                              ("executable", item["cmd"][0]),
                              ("arguments", " ".join(_windows_arg(arg) for arg in item["cmd"][1:])),
                              ("workingdirectory", app_text),
                              ("startmode", "Automatic"), ("delayedAutoStart", "true"),
                              ("resetfailure", str(restart["window"]) + " sec")):
                ET.SubElement(root, tag).text = text
            for dependency in item["after"]:
                ET.SubElement(root, "depend").text = prefix + dependency
            for _ in range(restart["max"] - 1):
                ET.SubElement(root, "onfailure", action="restart", delay=str(restart["delay"]) + " sec")
            ET.SubElement(root, "onfailure", action="none")
            ET.SubElement(root, "env", name="HOMENSAI_WEB_HOST", value="127.0.0.1")
            ET.SubElement(root, "env", name="HOMENSAI_WEB_PORT", value=str(manifest["web"]["port"]))
            for env in names:
                ET.SubElement(root, "env", name=env, value="%" + env + "%")
            ET.indent(root, space="  ")
            result[prefix + name + ".xml"] = ET.tostring(root, encoding="unicode") + "\n"
        else:
            executable = item["cmd"][0]
            if (executable.startswith(("-", ":", "@", "+", "!", "|"))
                    or ("/" in executable and not executable.startswith("/"))):
                raise ManifestError("systemd: требуется абсолютный executable или простое имя")
            dependencies = " ".join(prefix + dep + ".service" for dep in item["after"])
            # Без shell; $$ и %% сохраняют literal dollar/specifier.
            argv = " ".join(_unit_arg(arg) for arg in item["cmd"])
            lines = ["[Unit]", "Description=" + prefix + name,
                     "After=" + dependencies, "Requires=" + dependencies,
                     f"StartLimitIntervalSec={restart['window']}", f"StartLimitBurst={restart['max']}",
                     "", "[Service]", "Type=simple", "ExecStart=" + argv,
                     "WorkingDirectory=" + app_text.replace("%", "%%"),
                     "Restart=on-failure", f"RestartSec={restart['delay']}",
                     "Environment=HOMENSAI_WEB_HOST=127.0.0.1",
                     f"Environment=HOMENSAI_WEB_PORT={manifest['web']['port']}",
                     "PassEnvironment=" + " ".join(names), "", "[Install]", "WantedBy=multi-user.target", ""]
            result[prefix + name + ".service"] = "\n".join(lines)
    return result


def render_fixed_user_services(manifest: dict[str, Any], *, modes: tuple[str, ...],
                               install_root: str) -> dict[str, str]:
    """Создать тексты user-units для заранее известного корня, без записи.

    Результат содержит абсолютный путь установки. Его нужно сформировать до
    подписи пакета; перенос на другой корень этим интерфейсом не поддерживается.
    Доставка секретов, очистка окружения и проверка исполняемого кода не входят
    в этот генератор. Продукты, которым нужны service-секреты, отклоняются.
    """
    manifest = validate_manifest(manifest)
    if type(modes) is not tuple or not modes or any(type(mode) is not str for mode in modes):
        raise ManifestError('systemd user: недопустимые режимы')
    if (type(install_root) is not str or not install_root.startswith('/') or install_root == '/'
            or '\\' in install_root or '//' in install_root
            or any(part in ('.', '..') for part in install_root.split('/'))):
        raise ManifestError('systemd user: недопустимый корень')
    app = manifest['paths']['app']
    if app.startswith('/') or '\\' in app or ':' in app:
        raise ManifestError('systemd user: нужен относительный каталог app')
    if any('service' in item['contexts'] for item in manifest['secrets']):
        raise ManifestError('systemd user: доставка секретов не поддерживается')
    order = component_order(manifest, modes)
    components = {item['name']: item for item in manifest['components']}
    values = [install_root, app]
    for name in order:
        command = components[name]['cmd']
        if not command[0].startswith('/') or command[0].startswith('//') or '\\' in command[0]:
            raise ManifestError('systemd user: требуется абсолютный executable')
        values.extend(command)
    if any(any(ord(char) < 32 or 0x7f <= ord(char) <= 0x9f or
                   0xd800 <= ord(char) <= 0xdfff or char in '\u2028\u2029'
               for char in value) for value in values):
        raise ManifestError('systemd user: недопустимый символ')
    units = render_services(manifest, platform='linux', modes=modes, install_root=install_root)
    result = {}
    for name, text in units.items():
        lines = [line for line in text.splitlines()
                 if not line.startswith(('PassEnvironment=', 'WantedBy=')) and line != '[Install]']
        result[name] = '\n'.join(lines).rstrip() + '\n'
    return result
