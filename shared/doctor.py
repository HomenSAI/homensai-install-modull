# HomenS.AI · © 2026 Serhii Khomenko
"""Чистая диагностика внедрённых фактов; без I/O, часов ОС и уведомлений."""

from __future__ import annotations

import math
import re

HEARTBEAT_LIMIT = 180
CRASH_WINDOW = 600
CRASH_FAILURES = 3
ALERT_INTERVAL = 3600
DAY = 86400


class DoctorError(ValueError):
    """Неверный контракт фактов; значения входа не включаются в сообщение."""


def _number(value):
    # Не преобразуем произвольные строки/объекты и не принимаем bool как int.
    if (type(value) not in (int, float) or value < 0 or value > 2**53
            or (type(value) is float and not math.isfinite(value))):
        raise DoctorError("Ожидается конечное неотрицательное число")
    return value


def _component(value):
    if type(value) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value) is None:
        raise DoctorError("Недопустимое имя компонента")


def crash_loop(failure_timestamps: list, *, now: float) -> bool:
    """Три падения в замкнутом интервале [now-600, now]; будущее запрещено."""
    _number(now)
    if type(failure_timestamps) is not list:
        raise DoctorError("Требуется список времён падений")
    for stamp in failure_timestamps:
        if _number(stamp) > now:
            raise DoctorError("Время падения находится в будущем")
    return sum(now - stamp <= CRASH_WINDOW for stamp in failure_timestamps) >= CRASH_FAILURES


def rate_limit_alert(component: str, last_alerts: dict, *, now: float) -> tuple[bool, dict]:
    """Решение и новая копия state; не отправляет сигнал и не меняет вход.

    При откате часов сигнал подавляется до last+3600. Вызывающая сторона
    атомарно сохраняет возвращённый state только для принятого к отправке сигнала.
    """
    _component(component)
    _number(now)
    if type(last_alerts) is not dict:
        raise DoctorError("Требуется состояние лимита сигналов")
    for name, stamp in last_alerts.items():
        _component(name)
        _number(stamp)
    updated = dict(last_alerts)
    previous = updated.get(component)
    allowed = previous is None or now - previous >= ALERT_INTERVAL
    if allowed:
        updated[component] = now
    return allowed, updated


def _facts(facts):
    required = {"now", "db_opens", "schema_current", "data_dir_writable", "free_disk_bytes",
                "min_free_disk_bytes", "components", "last_archive_age_seconds",
                "archive_schedule_seconds"}
    if type(facts) is not dict or facts.keys() != required:
        raise DoctorError("Неверный набор полей фактов")
    _number(facts["now"])
    for key in ("db_opens", "schema_current", "data_dir_writable"):
        if type(facts[key]) is not bool:
            raise DoctorError("Ожидается логический факт")
    for key in ("free_disk_bytes", "min_free_disk_bytes"):
        if type(facts[key]) is not int or facts[key] < 0:
            raise DoctorError("Ожидается неотрицательное число байтов")
    if _number(facts["archive_schedule_seconds"]) == 0:
        raise DoctorError("Интервал архива должен быть положительным")
    if facts["last_archive_age_seconds"] is not None:
        _number(facts["last_archive_age_seconds"])
    components = facts["components"]
    if type(components) is not dict or not components:
        raise DoctorError("Требуются факты активных компонентов")
    for name, item in components.items():
        _component(name)
        if type(item) is not dict or item.keys() != {"heartbeat_age_seconds", "failure_timestamps"}:
            raise DoctorError("Неверные факты компонента")
        if item["heartbeat_age_seconds"] is not None:
            _number(item["heartbeat_age_seconds"])
        crash_loop(item["failure_timestamps"], now=facts["now"])


def evaluate_health(facts: dict) -> dict:
    """Вернуть JSON-совместимый report с checks и exit_code 0/1.

    Факты недоверенные: неизвестные/неполные поля дают INVALID_FACTS, без эха
    значений. Имена компонентов — публичные ID; сообщения всегда фиксированы.
    """
    def check(code, ok, message, component=None):
        result = {"code": code, "ok": ok, "message": message}
        if component is not None:
            result["component"] = component
        return result

    try:
        _facts(facts)
    except DoctorError:
        return {"ok": False, "exit_code": 1, "checks": [
            check("INVALID_FACTS", False, "Неверные или неполные факты диагностики")]}

    checks = []
    for key, code, good, bad in (
        ("db_opens", "DB", "База данных открывается", "База данных не открывается"),
        ("schema_current", "SCHEMA", "Схема базы актуальна", "Схема базы требует обновления"),
        ("data_dir_writable", "DATA_DIR", "Каталог данных доступен для записи",
         "Каталог данных недоступен для записи"),
    ):
        ok = facts[key]
        checks.append(check(code + ("_OK" if ok else "_FAILED"), ok, good if ok else bad))
    ok = facts["free_disk_bytes"] > facts["min_free_disk_bytes"]
    checks.append(check("DISK_OK" if ok else "DISK_LOW", ok,
                        "Свободного места достаточно" if ok else "Недостаточно свободного места"))
    for name, item in sorted(facts["components"].items()):
        age = item["heartbeat_age_seconds"]
        if age is None:
            checks.append(check("HEARTBEAT_MISSING", False, "Пульс компонента отсутствует", name))
        else:
            ok = age <= HEARTBEAT_LIMIT
            checks.append(check("HEARTBEAT_OK" if ok else "HEARTBEAT_STALE", ok,
                                "Пульс компонента актуален" if ok else "Пульс старше трёх минут", name))
        loop = crash_loop(item["failure_timestamps"], now=facts["now"])
        checks.append(check("CRASH_LOOP" if loop else "CRASH_OK", not loop,
                            "Три падения за десять минут: требуется остановка компонента"
                            if loop else "Цикл падений не обнаружен", name))
    age = facts["last_archive_age_seconds"]
    if age is None:
        checks.append(check("ARCHIVE_MISSING", False, "Подтверждённый архив отсутствует"))
    else:
        ok = age <= facts["archive_schedule_seconds"] + DAY
        checks.append(check("ARCHIVE_OK" if ok else "ARCHIVE_STALE", ok,
                            "Архив актуален" if ok else "Архив просрочен более чем на сутки"))
    healthy = all(item["ok"] for item in checks)
    return {"ok": healthy, "exit_code": int(not healthy), "checks": checks}
