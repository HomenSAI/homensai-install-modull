# HomenS.AI · © 2026 Serhii Khomenko
"""Чистый рендер Docker-заготовок; CLI пишет только два явно запрошенных файла."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from string import Template
from typing import Any

from shared.manifest import ManifestError, load_manifest, validate_manifest


def render_docker(manifest: dict[str, Any]) -> dict[str, str]:
    """Вернуть Dockerfile и compose.yml; команды, Docker и сеть не запускаются."""
    manifest = validate_manifest(manifest)
    if manifest["schema_version"] != 1:
        raise ManifestError("Docker-рендер поддерживает только v1; исполнение контракта v2 не реализовано")
    # Без proxy из окружения и редиректов: health обязан отвечать по своему URL.
    health_code = (
        "import urllib.request as u; "
        "o=u.build_opener(u.ProxyHandler({}), "
        "type('NoRedirect',(u.HTTPRedirectHandler,),"
        "{'redirect_request':lambda *a,**k:None})()); "
        f"r=o.open({manifest['healthcheck_url']!r},timeout=3); "
        "s=r.status; r.close(); raise SystemExit(0 if s==200 else 1)"
    )
    health_command = ["python", "-c", health_code]
    values = {
        "base_image": manifest["docker"]["base_image"],
        "product_id": manifest["product_id"],
        "data_home_env": manifest["data_home_env"],
        "port": manifest["port"],
        "start_command": json.dumps(manifest["docker"]["start_command"], ensure_ascii=True),
        "healthcheck_command": json.dumps(health_command),
        "compose_healthcheck_command": json.dumps(["CMD", *health_command]).replace("$", "$$"),
    }
    templates = Path(__file__).resolve().parent.parent / "docker"
    return {
        output: Template((templates / template).read_text(encoding="utf-8")).substitute(values)
        for output, template in (("Dockerfile", "Dockerfile.template"), ("compose.yml", "compose.template.yml"))
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сформировать Docker-заготовки без сборки и запуска")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        rendered = render_docker(load_manifest(args.manifest))
        args.output_dir.mkdir(parents=True, exist_ok=True)
        # Новая директория или отсутствие обоих файлов: чужой результат не заменяем.
        if any((args.output_dir / name).exists() for name in rendered):
            raise ManifestError("Выходные файлы уже существуют; выберите новый каталог")
        for name, content in rendered.items():
            with (args.output_dir / name).open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
    except (ManifestError, OSError) as error:
        print(error)
        return 2
    print("Сформированы Dockerfile и compose.yml; установка и миграции не выполнялись")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
