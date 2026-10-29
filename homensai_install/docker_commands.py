# HomenS.AI · © 2026 Serhii Khomenko
"""Узкий argv-контракт provisioning: без pull, shell, force и volume removal.

Builders используются одним DockerEngine. Независимая проверка в DockerCLI
не разрешает расширить привилегии через добавленный flag или options payload.
"""
from __future__ import annotations

from pathlib import Path
import re

from shared.datahome import checked_path


NAME = re.compile(r"hsai-[0-9a-f]{32}-[0-9a-f]{16}\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,240}@sha256:[0-9a-f]{64}\Z")
USER = re.compile(r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}\Z")
PREFIX = "io.homensai.installer."
ENV_NAMES = frozenset({"HOMENSAI_PRODUCT_ID", "HOMENSAI_INSTALLATION_ID", "HOMENSAI_RELEASE",
    "HOMENSAI_COMPONENT", "HOMENSAI_DATA_HOME", "HOMENSAI_CONFIG_HOME", "HOMENSAI_WEB_HOST",
    "HOMENSAI_WEB_PORT", "HOMENSAI_MAINTENANCE"})
TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=67108864"


def network_create(spec):
    args = ["network", "create", "--driver", "bridge", "--internal"]
    for key, value in sorted(spec["labels"].items()):
        args.extend(("--label", key + "=" + value))
    return tuple(args + ["--", spec["name"]])


def container_create(spec):
    args = ["container", "create", "--pull=never", "--platform", spec["platform"],
        "--name", spec["name"], "--user", spec["user"], "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--ipc", "private", "--cgroupns", "private",
        "--restart", "no", "--log-driver", "none", "--memory", str(spec["memory_bytes"]),
        "--pids-limit", str(spec["pids_limit"]), "--stop-timeout", str(spec["stop_timeout_seconds"]),
        "--stop-signal", "SIGTERM", "--network", spec["network_name"], "--tmpfs", TMPFS]
    for flag, values in (("--label", spec["labels"]), ("--env", spec["environment"])):
        for key, value in sorted(values.items()):
            args.extend((flag, key + "=" + value))
    for mount in sorted(spec["mounts"], key=lambda item: item["Destination"]):
        # CSV escaping расширило бы допустимые неоднозначные формы. Такие host
        # paths явно отклоняются preflight и независимым CLI validator.
        value = "type=bind,src=" + mount["Source"] + ",dst=" + mount["Destination"] + ",bind-propagation=rprivate"
        args.extend(("--mount", value + (",readonly" if not mount["RW"] else "")))
    for guest, bindings in sorted(spec["ports"].items()):
        for binding in bindings:
            args.extend(("--publish", "127.0.0.1:" + binding["HostPort"] + ":" + guest))
    args.extend(("--entrypoint", spec["argv"][0], "--", spec["image"]))
    return tuple(args + list(spec["argv"][1:]))


def _labels(values, start, *, container):
    labels = {}
    while start + 1 < len(values) and values[start] == "--label":
        key, separator, value = values[start + 1].partition("=")
        if not separator or not value or not key.startswith(PREFIX) or key[len(PREFIX):] in labels:
            return None, start
        labels[key[len(PREFIX):]] = value
        start += 2
    expected = {"installation", "product", "release", "contract", "component"}
    if container:
        expected.add("spec")
    if (set(labels) != expected or not re.fullmatch(r"[0-9a-f]{32}", labels["installation"])
            or not re.fullmatch(r"[0-9a-f]{64}", labels["contract"])
            or (container and not re.fullmatch(r"[0-9a-f]{64}", labels["spec"]))):
        return None, start
    return labels, start


def provisioning_arguments(values):
    """True только для ограниченных форм create; effects/ownership проверяет engine."""
    try:
        if values[:5] == ("network", "create", "--driver", "bridge", "--internal"):
            labels, pos = _labels(values, 5, container=False)
            return (labels is not None and labels["component"] == "network"
                and values[pos:pos + 1] == ("--",) and len(values) == pos + 2
                and bool(NAME.fullmatch(values[-1])) and values[-1].split("-")[1] == labels["installation"])
        if values[:4] != ("container", "create", "--pull=never", "--platform") or len(values) < 38:
            return False
        if (values[4] not in {"linux/amd64", "linux/arm64"} or values[5] != "--name"
                or not NAME.fullmatch(values[6]) or values[7] != "--user" or not USER.fullmatch(values[8])):
            return False
        if values[9:22] != ("--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--ipc", "private", "--cgroupns", "private", "--restart", "no", "--log-driver", "none"):
            return False
        for pos, flag, low, high in ((22, "--memory", 16777216, 68719476736),
                (24, "--pids-limit", 8, 4096), (26, "--stop-timeout", 1, 600)):
            if values[pos] != flag or not values[pos + 1].isdigit() or not low <= int(values[pos + 1]) <= high:
                return False
        if (values[28:31] != ("--stop-signal", "SIGTERM", "--network") or not NAME.fullmatch(values[31])
                or values[32:34] != ("--tmpfs", TMPFS)):
            return False
        labels, pos = _labels(values, 34, container=True)
        if labels is None or any(values[index].split("-")[1] != labels["installation"] for index in (6, 31)):
            return False
        environment = {}
        while values[pos] == "--env":
            key, separator, value = values[pos + 1].partition("=")
            if not separator or key in environment:
                return False
            environment[key] = value
            pos += 2
        if (set(environment) != ENV_NAMES or environment["HOMENSAI_INSTALLATION_ID"] != labels["installation"]
                or environment["HOMENSAI_WEB_HOST"] != "0.0.0.0" or environment["HOMENSAI_MAINTENANCE"] != "1"
                or environment["HOMENSAI_DATA_HOME"] != "/homensai/data" or environment["HOMENSAI_CONFIG_HOME"] != "/homensai/config"):
            return False
        destinations = set()
        while values[pos] == "--mount":
            fields = values[pos + 1].split(",")
            if len(fields) not in {4, 5} or fields[0] != "type=bind" or not fields[1].startswith("src=") or not fields[2].startswith("dst="):
                return False
            source, destination = fields[1][4:], fields[2][4:]
            if (fields[3] != "bind-propagation=rprivate" or (len(fields) == 5 and fields[4] != "readonly")
                    or source != str(checked_path(source)) or Path(source) == Path(source).parent
                    or '"' in source or "=" in source
                    or not destination.startswith("/homensai/") or ".." in Path(destination).parts
                    or destination in destinations or any(destination.startswith(old + "/") or old.startswith(destination + "/") for old in destinations)):
                return False
            destinations.add(destination)
            pos += 2
        if not 2 <= len(destinations) <= 5 or not {"/homensai/data", "/homensai/config"} <= destinations:
            return False
        if values[pos] == "--publish":
            matched = re.fullmatch(r"127\.0\.0\.1:([0-9]+):([0-9]+)/tcp", values[pos + 1])
            if matched is None or not all(1024 <= int(number) <= 65535 for number in matched.groups()):
                return False
            pos += 2
        return (values[pos] == "--entrypoint" and not values[pos + 1].startswith("-")
            and values[pos + 2] == "--" and bool(IMAGE.fullmatch(values[pos + 3])))
    except (ValueError, IndexError, TypeError):
        return False
