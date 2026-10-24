# HomenS.AI · © 2026 Serhii Khomenko
"""Локальный stdlib CLI библиотек; служебные/Docker эффекты не реализованы."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys

from shared.archive import ArchiveError, build_archive
from shared import archive_crypto
from shared.adapters_docker import DockerAdapterError, DockerComposeAdapter
from shared.datahome import DataHomeError, checked_path, contains, resolve_datahome
from shared.doctor import DoctorError, evaluate_health
from shared.linux_sh import render_linux
from shared.manifest import ManifestError, load_manifest
from shared.render_docker import render_docker
from shared.services import component_order, render_services
from shared.update_plan import UpdateError, UpdatePlan
from shared.windows_ps import render_windows


class UnimplementedPortError(NotImplementedError):
    """Реальный адаптер отсутствует; ошибка не содержит аргументов вызова."""


class UnimplementedUpdatePorts:
    """Явные заглушки всех UpdatePorts; CLI никогда не вызывает run()."""

    def _unimplemented(self, *args, **kwargs):
        raise UnimplementedPortError("Реальные адаптеры служб, БД и обновления не реализованы")

    current_release = _unimplemented
    stop = _unimplemented
    backup = _unimplemented
    stage_release = _unimplemented
    migrate = _unimplemented
    switch_pointer = _unimplemented
    start = _unimplemented
    health = _unimplemented
    restore = _unimplemented
    post_update = _unimplemented


class UnimplementedDockerPorts:
    """Рендер Docker не является адаптером сборки/запуска."""

    def _unimplemented(self, *args, **kwargs):
        raise UnimplementedPortError("Реальный адаптер Docker не реализован")

    build = _unimplemented
    start = _unimplemented
    stop = _unimplemented


class UsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        # Имена опций точные: --password не должен стать --password-env.
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        # argparse обычно повторяет произвольные argv, в том числе секреты.
        raise UsageError("Неверные аргументы; используйте --help")


def _parser() -> Parser:
    parser = Parser(description="Локальный контроллер: планы, тексты и ZIP без запуска служб")
    parser.add_argument("--manifest", help="Публичный JSON-манифест (кроме doctor-eval: обязателен)")
    parser.add_argument("--json", action="store_true", help="Один JSON-объект результата")
    commands = parser.add_subparsers(dest="command", required=True, title="Команды")
    descriptions = {
        "validate": "Проверить манифест",
        "plan-install": "Показать каталоги и файлы будущей установки без записи",
        "render-docker": "Записать Dockerfile/compose.yml без сборки",
        "docker-plan": "Показать argv Compose без исполнения и записи",
        "render-services": "Записать XML/units без установки служб",
        "render-linux": "Записать тексты Linux без исполнения",
        "doctor-eval": "Оценить внедрённые факты без проверки живого продукта",
        "render-windows": "Записать PowerShell 5.1/XML без исполнения",
        "archive-create": "Создать полный архив остановленных данных (шифрование: --encrypt)",
        "archive-verify": "Проверить полный архив и идентичность продукта",
        "archive-restore": "Восстановить архив в отдельный пустой каталог",
        "update-plan-status": "Прочитать checkpoint без исполнения обновления",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                             help="Один JSON-объект результата")
        if name in ("plan-install", "render-services", "archive-create", "archive-restore",
                    "update-plan-status"):
            command.add_argument("--install-root", required=True, help="Абсолютный корень текущей ОС")
        if name in ("plan-install", "archive-create", "archive-restore"):
            command.add_argument("--data-home", help="v1: явный абсолютный DataHome")
            command.add_argument("--app-dir", help="v1: абсолютный каталог программы")
            command.add_argument("--releases-dir", help="v1: абсолютный каталог выпусков")
        if name in ("plan-install", "render-services"):
            choices = ("docker", "linux", "windows") if name == "plan-install" else ("linux", "windows")
            command.add_argument("--platform", choices=choices, required=True, help="Формат результата")
            command.add_argument("--mode", action="append", default=[], help="Режим v2, можно повторять")
        if name in ("render-docker", "render-services", "render-windows"):
            command.add_argument("--output-dir", required=True, help="Каталог новых файлов")
        if name == "render-services":
            command.add_argument("--winsw-file", help="Локальный артефакт для проверки pin Windows")
        if name == "render-linux":
            command.add_argument("--install-root", required=True, help="Абсолютный Linux-путь как текст")
            command.add_argument("--mode", action="append", required=True, help="Режим v2")
            command.add_argument("--output-dir", required=True, help="Каталог новых текстов")
            command.add_argument("--user", default="homensai", help="Отдельный непривилегированный пользователь")
            command.add_argument("--environment-file", help="Публичный внешний путь EnvironmentFile")
        if name == "doctor-eval":
            command.add_argument("--facts", required=True, help="Публичный JSON с фактами и часами")
        if name == "render-windows":
            command.add_argument("--install-root", required=True, help="Абсолютный локальный Windows-путь")
            command.add_argument("--mode", action="append", required=True, help="Явный режим v2")
            command.add_argument("--winsw-file", required=True, help="Публичный WinSW-артефакт для проверки SHA-256")
            command.add_argument("--dpapi-scope", choices=("CurrentUser", "LocalMachine"),
                                 default="LocalMachine", help="Область DPAPI будущего исполнения")
        if name == "docker-plan":
            command.add_argument("--compose-file", required=True, help="Путь публичного compose.yml")
            command.add_argument("--action", choices=("up", "stop", "ps", "logs"), required=True,
                                 help="Только план команды Compose")
        if name in ("archive-create", "archive-verify", "archive-restore"):
            command.add_argument("--archive", required=True, help="Путь архива (create: новый файл)")
            command.add_argument("--password-env", help="Только имя переменной пароля; иначе скрытый запрос")
        if name == "archive-create":
            command.add_argument("--encrypt", action="store_true", help="Полный ZIP в AES-256-GCM оболочке")
        if name == "archive-restore":
            command.add_argument("--target", required=True, help="Существующий отдельный пустой каталог")
        if name == "update-plan-status":
            command.add_argument("--release", required=True, help="Целевой публичный ID выпуска плана")
            command.add_argument("--mode", action="append", required=True, help="Исходный набор режимов")
            migrations = command.add_mutually_exclusive_group()
            migrations.add_argument("--migration-id", action="append", help="Исходный порядок миграций")
            migrations.add_argument("--no-migrations", action="store_true", help="План без миграций")
    return parser


def _path(value: str) -> Path:
    # Сохраняем '..' до checked_path, а не нормализуем небезопасный ввод заранее.
    path = Path(value)
    if not path.is_absolute() and (PureWindowsPath(value).drive or value.startswith(("\\", "/"))):
        raise DataHomeError("Неоднозначный путь или путь другой ОС")
    result = checked_path(value if path.is_absolute() else str(Path.cwd()) + "/" + value)
    if result == Path(result.anchor):
        raise DataHomeError("Корень диска не является допустимой целью CLI")
    return result


def _file(value: str) -> Path:
    path = _path(value)
    if not path.is_file():
        raise OSError("Требуется обычный файл")
    return path


def _home(manifest, args):
    root = checked_path(args.install_root)
    legacy = (args.data_home, args.app_dir, args.releases_dir)
    if manifest["schema_version"] == 2:
        if any(value is not None for value in legacy):
            raise UsageError("Пути v2 задаются манифестом; параметры v1 не применимы")
        return resolve_datahome(manifest, root)
    if not all(legacy):
        raise UsageError("v1 требует --data-home, --app-dir и --releases-dir")
    return resolve_datahome(manifest, root,
                            environ={manifest["data_home_env"]: args.data_home},
                            app_dir=args.app_dir, releases_dir=args.releases_dir)


def _write_rendered(rendered: dict[str, str], destination: str) -> list[str]:
    directory = _path(destination)
    files = [checked_path(directory / name) for name in rendered]
    if any(path.exists() for path in files):
        raise DataHomeError("Результат уже существует; перезапись запрещена")
    directory.mkdir(parents=True, exist_ok=True)
    for path, content in zip(files, rendered.values()):
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    return [str(path) for path in files]


def _execute(manifest, args, *, password_provider=None) -> tuple[str, dict]:
    name = args.command
    if name == "doctor-eval":
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise DoctorError("Повтор поля фактов")
                result[key] = value
            return result

        def constant(value):
            raise DoctorError("Нечисловая константа фактов")

        facts = json.loads(_file(args.facts).read_text(encoding="utf-8-sig"),
                           object_pairs_hook=unique_object, parse_constant=constant)
        report = evaluate_health(facts)
        return "Продукт исправен" if report["ok"] else "Обнаружены проблемы продукта", {"report": report}
    if name == "render-linux":
        rendered = render_linux(manifest, modes=tuple(args.mode), install_root=args.install_root,
                                user=args.user, environment_file=args.environment_file)
        files = _write_rendered(rendered, args.output_dir)
        return "Тексты Linux записаны; сценарии и службы не исполнялись", {"files": files}
    if name == "docker-plan":
        adapter = DockerComposeAdapter(manifest, _path(args.compose_file))
        return "План Compose; исполнение не выполнялось", {
            "dry_run": True, "argv": adapter.command(args.action)}
    if name == "validate":
        return "Манифест корректен", {"product_id": manifest["product_id"],
                                      "schema_version": manifest["schema_version"]}
    if name == "plan-install":
        home = _home(manifest, args)
        if args.platform == "docker":
            if args.mode:
                raise UsageError("Docker v1 не поддерживает режимы")
            filenames = list(render_docker(manifest))
            files = [str(checked_path(home.app / filename)) for filename in filenames]
        else:
            order = component_order(manifest, tuple(args.mode))
            suffix = ".xml" if args.platform == "windows" else ".service"
            files = [str(checked_path(home.roots["services"] / (manifest["product_id"] + "-" + item + suffix)))
                     for item in order]
        directories = list(dict.fromkeys(str(path) for path in (home.app, home.releases, *home.roots.values())))
        return "План установки; выполнение отсутствует", {"dry_run": True, "directories": directories,
                                                          "files": files}
    if name == "render-docker":
        files = _write_rendered(render_docker(manifest), args.output_dir)
        return "Docker-заготовки записаны; сборка и запуск не выполнялись", {"files": files}
    if name == "render-windows":
        rendered = render_windows(manifest, install_root=args.install_root, modes=tuple(args.mode),
                                  winsw_payload=_file(args.winsw_file).read_bytes(),
                                  dpapi_scope=args.dpapi_scope)
        files = _write_rendered(rendered, args.output_dir)
        return "Тексты PowerShell/XML записаны; исполнение и Windows-приёмка не выполнялись", {"files": files}
    if name == "render-services":
        component_order(manifest, tuple(args.mode))
        # Корень целевой ОС используется только как текст, запись идёт в output-dir.
        root = args.install_root
        portable = root.replace("\\", "/")
        tail = portable[2:] if PureWindowsPath(root).drive else portable
        path_type = PureWindowsPath if args.platform == "windows" else PurePosixPath
        if (not path_type(root).is_absolute() or portable.startswith("//") or root.startswith("\\")
                or ":" in tail or any(part == ".." or (part not in ("", ".") and part.endswith((".", " ")))
                                      for part in portable.split("/"))):
            raise DataHomeError("Небезопасный корень целевой ОС")
        if (args.platform == "windows") == (os.name == "nt"):
            resolve_datahome(manifest, checked_path(root))
        if args.winsw_file and args.platform != "windows":
            raise UsageError("--winsw-file применим только к Windows")
        payload = _file(args.winsw_file).read_bytes() if args.winsw_file else None
        rendered = render_services(manifest, platform=args.platform, modes=tuple(args.mode),
                                   install_root=root, winsw_payload=payload)
        files = _write_rendered(rendered, args.output_dir)
        return "Тексты служб записаны; службы не установлены", {"files": files}
    if name == "update-plan-status":
        ids = () if args.no_migrations else tuple(args.migration_id or ["manifest"])
        plan = UpdatePlan(manifest, checked_path(args.install_root), UnimplementedUpdatePorts(),
                          release=args.release, modes=tuple(args.mode), migration_ids=ids)
        state = plan.status()
        return ("Состояние обновления прочитано" if state else "Обновление ещё не начато"), {"state": state}
    provider = password_provider if password_provider is not None else archive_crypto.password_provider(
        env_var=args.password_env)
    encrypted = False
    if name == "archive-create":
        home = _home(manifest, args)
        encrypted = args.encrypt
        if encrypted:
            receipt = archive_crypto.build_encrypted_archive(home, _path(args.archive),
                                                             password_provider=provider)
        else:
            if args.password_env:
                raise UsageError("--password-env при создании требует --encrypt")
            receipt = build_archive(home, _path(args.archive))
        message = ("Полный шифрованный архив создан и проверен" if encrypted else
                   "Полный незашифрованный ZIP создан и проверен")
    elif name == "archive-verify":
        path = _file(args.archive)
        encrypted = archive_crypto.is_encrypted(path)
        receipt = archive_crypto.verify_archive(path, password_provider=provider,
                                                expected_product_id=manifest["product_id"])
        message = "Архив полностью проверен"
    else:  # archive-restore: только отдельный каталог, включая защиту пустых живых корней.
        home = _home(manifest, args)
        target = _path(args.target)
        if any(contains(root, target) or contains(target, root)
               for root in (home.app, home.releases, *home.roots.values())):
            raise DataHomeError("Восстановление пересекается с живой установкой")
        path = _file(args.archive)
        encrypted = archive_crypto.is_encrypted(path)
        receipt = archive_crypto.restore_archive(path, target, password_provider=provider,
                                                 expected_product_id=manifest["product_id"])
        message = "Архив восстановлен в отдельный каталог и проверен"
    # Имена/содержимое/хэши файлов архива не попадают в stdout.
    return message, {"product_id": receipt["product_id"], "file_count": len(receipt["files"]),
                     "directory_count": len(receipt["directories"]), "encrypted": encrypted}


def _emit(code: int, message: str, data: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"ok": code == 0, "code": code, "message": message, "data": data},
                         ensure_ascii=True, sort_keys=True))
    else:
        print(message, file=sys.stderr if code else sys.stdout)
        if "directories" in data:
            print("Каталоги:")
            for path in data["directories"]:
                print("  " + path)
        if "files" in data:
            print("Файлы:")
            for path in data["files"]:
                print("  " + path)
        if "report" in data:
            for check in data["report"]["checks"]:
                component = " [" + check["component"] + "]" if "component" in check else ""
                print(check["code"] + component + ": " + check["message"])
        if "product_id" in data:
            print("Продукт: " + data["product_id"])
        if "argv" in data:
            # JSON-массив явно сохраняет границы аргументов; это не shell-строка.
            print("argv: " + json.dumps(data["argv"], ensure_ascii=True))
        if "schema_version" in data:
            print("Версия схемы: " + str(data["schema_version"]))
        if "file_count" in data:
            protection = "AES-256-GCM" if data["encrypted"] else "ZIP без шифрования"
            print(f"Файлов: {data['file_count']}; каталогов: {data['directory_count']}; {protection}")
        if data.get("state") is not None:
            state = data["state"]
            print(f"Направление: {state['direction']}; индекс следующего шага: {state['index']}; "
                  f"результат: {state['outcome']}")
            print(f"Выпуск: {state['old_release']} → {state['release']}")
            if state["failed_phase"] is not None:
                print("Фаза сбоя: " + state["failed_phase"])
    return code


def main(argv: list[str] | None = None, *, password_provider=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in arguments
    try:
        args = _parser().parse_args(arguments)
        as_json = args.json
        if args.command != "doctor-eval" and not args.manifest:
            raise UsageError("Требуется --manifest")
        manifest = load_manifest(_file(args.manifest)) if args.manifest else None
        message, data = _execute(manifest, args, password_provider=password_provider)
    except SystemExit as error:  # --help остаётся удобным и для main(argv).
        return int(error.code)
    except UsageError as error:
        return _emit(2, str(error), {}, as_json)
    except ManifestError as error:
        # Контракт ManifestError исключает значения входных полей; OSError и
        # произвольные исключения ниже всегда заменяются фиксированным текстом.
        return _emit(2, str(error), {}, as_json)
    except DataHomeError:
        return _emit(2, "Небезопасный путь или пересечение каталогов; операция запрещена", {}, as_json)
    except DockerAdapterError:
        return _emit(2, "Недопустимый план Docker Compose", {}, as_json)
    except DoctorError:
        return _emit(1, "Неверные факты диагностики", {}, as_json)
    except archive_crypto.ArchiveCryptoError as error:
        return _emit(1, str(error), {}, as_json)
    except ArchiveError:
        return _emit(1, "Операция ZIP не завершена: проверьте архив, место и каталоги", {}, as_json)
    except UpdateError:
        return _emit(1, "Состояние обновления повреждено, недоступно или относится к другому плану", {}, as_json)
    except UnimplementedPortError:
        return _emit(1, "Реальный адаптер не реализован; выполнение запрещено", {}, as_json)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return _emit(1, "Не удалось выполнить локальную операцию; проверьте входные файлы и доступ", {}, as_json)
    return _emit(data.get("report", {}).get("exit_code", 0), message, data, as_json)


if __name__ == "__main__":
    raise SystemExit(main())
