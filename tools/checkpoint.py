#!/usr/bin/env python3
"""Явные, возобновляемые Git-checkpoint для разработки (Python 3.12+, Git 2.36+).

Это инструмент разработчика, не телеметрия и не часть установки приложения.
Команды Git и hook-вывод никогда не попадают в публичный JSON результата.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit
import uuid


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
ID_RE = re.compile(r"CP-[A-Za-z0-9][A-Za-z0-9_.-]{1,95}\Z")
TASK_RE = re.compile(r"[A-Z][A-Z0-9_-]{1,63}\Z")
REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
PROTECTED_BRANCHES = {"main", "master", "release", "production", "prod"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
BINARY_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".zip", ".7z", ".tar", ".gz",
                   ".exe", ".dll", ".so", ".pyc", ".pyo"}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(rb"https?://[^\s/:@]+:[^\s/@]+@", re.I),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_checkpoint_id() -> str:
    return "CP-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class CheckpointError(Exception):
    """Код и безопасные метаданные; raw stderr и содержимое файлов не экспортируются."""

    def __init__(self, code: str, *, checkpoint_id: str | None = None,
                 path: str | None = None, remote: str | None = None):
        super().__init__(code)
        self.code = code
        self.context = {k: v for k, v in {"checkpoint_id": checkpoint_id,
                                         "path": path, "remote": remote}.items() if v is not None}

    def to_dict(self) -> dict:
        return {"code": self.code, **self.context}


@dataclass
class GitResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False


def _regular_file(path: Path, *, optional: bool = False) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise CheckpointError("FILE_NOT_FOUND") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise CheckpointError("UNSAFE_FILE_TYPE")
    return info


def _fsync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_json(path: Path, value: dict) -> None:
    payload = canonical(value) + b"\n"
    if len(payload) > MAX_RECEIPT_BYTES:
        raise CheckpointError("RECEIPT_TOO_LARGE")
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


class Checkpointer:
    """Один локальный коммит, затем независимые подтверждения выбранных remotes."""

    def __init__(self, repo: str | Path, *, timeout: int = 30):
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
            raise CheckpointError("INVALID_TIMEOUT")
        self.timeout = timeout
        self.root = Path(repo).absolute()
        root = self._git("rev-parse", "--show-toplevel", required=True).stdout.decode().strip()
        self.root = Path(root).resolve()
        common = self._git("rev-parse", "--path-format=absolute", "--git-common-dir",
                           required=True).stdout.decode().strip()
        self.common_dir = Path(common).resolve()
        index = self._git("rev-parse", "--path-format=absolute", "--git-path", "index",
                          required=True).stdout.decode().strip()
        self.index = Path(index).absolute()
        self.state = self.common_dir / "hinstall-checkpoints"
        self.repo_id = digest(os.fsencode(str(self.common_dir)))
        self.worktree_id = digest(os.fsencode(str(self.root)))

    def _git(self, *args: str, index: Path | None = None,
             required: bool = False, input_bytes: bytes | None = None) -> GitResult:
        env = os.environ.copy()
        for key in list(env):
            if key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                       "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                       "GIT_PREFIX", "GIT_SHALLOW_FILE", "GIT_CONFIG_PARAMETERS", "GIT_CURL_VERBOSE"} or key.startswith(
                           ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TRACE")) or key == "GIT_CONFIG_COUNT":
                env.pop(key)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never", "LC_ALL": "C"})
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        command = ["git", "--no-pager", "--literal-pathspecs", "-C", str(self.root), *args]
        try:
            completed = subprocess.run(command, input=input_bytes, capture_output=True,
                                       env=env, timeout=self.timeout, check=False)
            result = GitResult(completed.returncode, completed.stdout, completed.stderr)
        except subprocess.TimeoutExpired:
            result = GitResult(124, timed_out=True)
        except OSError:
            result = GitResult(127)
        if required and result.returncode:
            raise CheckpointError("GIT_TIMEOUT" if result.timed_out else "GIT_COMMAND_FAILED")
        return result

    def _branch(self) -> str:
        result = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
        if result.returncode:
            raise CheckpointError("DETACHED_HEAD")
        branch = result.stdout.decode().strip()
        self._check_branch(branch)
        return branch

    def _check_branch(self, branch: str) -> None:
        if not isinstance(branch, str):
            raise CheckpointError("INVALID_RECEIPT")
        if (branch.lower() in PROTECTED_BRANCHES or branch.lower().startswith("release/")
                or self._git("check-ref-format", "--branch", branch).returncode):
            raise CheckpointError("PROTECTED_BRANCH")

    def _head(self, branch: str | None = None) -> str:
        ref = "refs/heads/" + branch if branch else "HEAD"
        result = self._git("rev-parse", "--verify", ref)
        value = result.stdout.decode().strip()
        if result.returncode or not SHA_RE.fullmatch(value):
            raise CheckpointError("INITIAL_COMMIT_REQUIRED")
        return value

    def _private_dir(self, path: Path) -> None:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise CheckpointError("UNSAFE_STATE_DIRECTORY")
        if os.name != "nt":
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CheckpointError("INSECURE_STATE_PERMISSIONS")

    @contextmanager
    def _lock(self):
        self._private_dir(self.state)
        lock_path = self.state / "checkpoint.lock"
        if lock_path.exists() or lock_path.is_symlink():
            info = _regular_file(lock_path)
            if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise CheckpointError("INSECURE_STATE_PERMISSIONS")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        locked = False
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            yield
        except (BlockingIOError, PermissionError):
            raise CheckpointError("CHECKPOINT_BUSY") from None
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _id(value: str) -> str:
        if not isinstance(value, str) or not ID_RE.fullmatch(value) or ".." in value:
            raise CheckpointError("INVALID_CHECKPOINT_ID")
        return value

    def _path(self, value: str) -> str:
        if (not isinstance(value, str) or not value or len(value) > 512 or "\\" in value
                or any(ord(char) < 32 for char in value) or any(char in value for char in ":*?[]")):
            raise CheckpointError("INVALID_FILE_PATH")
        path = PurePosixPath(value)
        if (path.is_absolute() or value != path.as_posix()
                or any(part in {".", "..", ".git"} for part in path.parts)):
            raise CheckpointError("INVALID_FILE_PATH")
        current = self.root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise CheckpointError("UNSAFE_FILE_TYPE", path=value)
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise CheckpointError("UNSAFE_FILE_TYPE", path=value)
        name = path.name.lower()
        if ((name == ".env" or name.startswith(".env."))
                and not name.endswith((".example", ".template", ".sample"))):
            raise CheckpointError("SENSITIVE_FILE_REJECTED", path=value)
        if (path.suffix.lower() in SECRET_SUFFIXES | BINARY_SUFFIXES
                or name in {"id_rsa", "id_ed25519", "credentials", "credentials.json"}
                or any(part.lower() in {".ssh", ".aws", ".gnupg"} for part in path.parts)):
            raise CheckpointError("SENSITIVE_FILE_REJECTED", path=value)
        return value

    @staticmethod
    def _scan(content: bytes, path: str | None = None) -> None:
        if len(content) > MAX_FILE_BYTES:
            raise CheckpointError("FILE_TOO_LARGE", path=path)
        if b"\0" in content:
            raise CheckpointError("BINARY_FILE_REJECTED", path=path)
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise CheckpointError("NON_UTF8_FILE_REJECTED", path=path) from None
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            raise CheckpointError("SECRET_PATTERN_REJECTED", path=path)

    def _assert_idle(self) -> None:
        for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
            result = self._git("rev-parse", "--path-format=absolute", "--git-path", name, required=True)
            if Path(result.stdout.decode().strip()).exists():
                raise CheckpointError("GIT_OPERATION_IN_PROGRESS")
        if self._git("ls-files", "--unmerged", "-z", required=True).stdout:
            raise CheckpointError("UNMERGED_INDEX")

    def _entries(self, files: list[str], *, index: Path | None = None) -> bytes:
        return self._git("ls-files", "--stage", "-z", "--", *files,
                         index=index, required=True).stdout

    def _store(self, receipt: dict) -> None:
        receipt["updated_at"] = utc_now()
        _atomic_json(self.state / (receipt["checkpoint_id"] + ".json"), receipt)

    def _read(self, checkpoint_id: str) -> dict:
        path = self.state / (self._id(checkpoint_id) + ".json")
        try:
            info = _regular_file(path)
            if info.st_size > MAX_RECEIPT_BYTES:
                raise CheckpointError("INVALID_RECEIPT")
            if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise CheckpointError("INSECURE_STATE_PERMISSIONS")
            receipt = json.loads(path.read_bytes())
            if (receipt["schema_version"] != SCHEMA_VERSION
                    or receipt["checkpoint_id"] != checkpoint_id
                    or receipt["repo_id"] != self.repo_id
                    or receipt["request_digest"] != digest(canonical(receipt["request"]))):
                raise CheckpointError("INVALID_RECEIPT")
            if receipt["worktree_id"] != self.worktree_id:
                raise CheckpointError("WRONG_WORKTREE", checkpoint_id=checkpoint_id)
            self._validate_request(**receipt["request"])
            self._check_branch(receipt["branch"])
            for field in ("base_head", "tree_sha", "commit_sha"):
                if receipt.get(field) is not None and not SHA_RE.fullmatch(receipt[field]):
                    raise CheckpointError("INVALID_RECEIPT")
            return receipt
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise CheckpointError("INVALID_RECEIPT") from None

    def _validate_request(self, *, task_id: str, checkpoint_id: str, files: list[str],
                          todo: str, remotes: list[str], message: str,
                          expected_heads: dict[str, str | None]) -> dict:
        self._id(checkpoint_id)
        if not isinstance(task_id, str) or not TASK_RE.fullmatch(task_id):
            raise CheckpointError("INVALID_TASK_ID")
        if (not isinstance(files, list) or not files or len(files) > 300
                or len(set(files)) != len(files)):
            raise CheckpointError("INVALID_ALLOWLIST")
        files = sorted(self._path(path) for path in files)
        if self._path(todo) not in files:
            raise CheckpointError("TODO_NOT_IN_ALLOWLIST")
        if (not isinstance(remotes, list) or len(remotes) > 8 or len(set(remotes)) != len(remotes)
                or any(not isinstance(name, str) or not REMOTE_RE.fullmatch(name) for name in remotes)):
            raise CheckpointError("INVALID_REMOTE_NAME")
        if not isinstance(expected_heads, dict) or set(expected_heads) - set(remotes):
            raise CheckpointError("INVALID_EXPECTED_HEAD")
        for value in expected_heads.values():
            if value is not None and (not isinstance(value, str) or not SHA_RE.fullmatch(value)):
                raise CheckpointError("INVALID_EXPECTED_HEAD")
        if (not isinstance(message, str) or not message.strip() or len(message) > 2000
                or "Checkpoint-ID:" in message or "Task-ID:" in message):
            raise CheckpointError("INVALID_COMMIT_MESSAGE")
        self._scan(message.encode())
        return {"task_id": task_id, "checkpoint_id": checkpoint_id, "files": files,
                "todo": todo, "remotes": sorted(remotes), "message": message,
                "expected_heads": expected_heads}

    def save(self, *, task_id: str, files: list[str], todo: str, remotes: list[str] | None = None,
             checkpoint_id: str | None = None, message: str | None = None,
             expected_heads: dict[str, str | None] | None = None) -> dict:
        request = self._validate_request(task_id=task_id, files=files, todo=todo,
                                         remotes=remotes or [], checkpoint_id=checkpoint_id or new_checkpoint_id(),
                                         message=message or f"Контрольная точка {task_id}",
                                         expected_heads=expected_heads or {})
        with self._lock():
            checkpoint_id = request["checkpoint_id"]
            receipt_path = self.state / (checkpoint_id + ".json")
            if receipt_path.exists() or receipt_path.is_symlink():
                receipt = self._read(checkpoint_id)
                if receipt["request_digest"] != digest(canonical(request)):
                    raise CheckpointError("CHECKPOINT_ID_REUSED", checkpoint_id=checkpoint_id)
                return self._resume_locked(receipt)
            self._assert_idle()
            branch, base = self._branch(), self._head()
            # Не присваиваем себе уже staged-изменения, включая partial staging и intent-to-add.
            baseline = self._entries(request["files"])
            expected_index = []
            tree = self._git("ls-tree", "-r", "-z", base, "--", *request["files"], required=True).stdout
            for entry in tree.split(b"\0"):
                if entry:
                    meta, path = entry.split(b"\t", 1)
                    mode, kind, oid = meta.split(b" ")
                    if mode not in {b"100644", b"100755"} or kind != b"blob":
                        raise CheckpointError("UNSAFE_FILE_TYPE")
                    expected_index.append(mode + b" " + oid + b" 0\t" + path + b"\0")
            if baseline != b"".join(expected_index):
                raise CheckpointError("ALLOWLIST_ALREADY_STAGED")
            fingerprints = {}
            for filename in request["files"]:
                path = self.root / filename
                info = _regular_file(path, optional=True)
                if info is not None:
                    if info.st_size > MAX_FILE_BYTES:
                        raise CheckpointError("FILE_TOO_LARGE", path=filename)
                    content = path.read_bytes()
                    self._scan(content, filename)
                    fingerprints[filename] = digest(content)
                elif not any(entry.endswith(b"\t" + filename.encode() + b"\0") for entry in expected_index):
                    raise CheckpointError("FILE_NOT_FOUND", path=filename)
                else:
                    fingerprints[filename] = None
            receipt = {"schema_version": SCHEMA_VERSION, "checkpoint_id": checkpoint_id,
                       "repo_id": self.repo_id, "worktree_id": self.worktree_id,
                       "request": request, "request_digest": digest(canonical(request)),
                       "created_at": utc_now(), "branch": branch, "base_head": base,
                       "phase": "PREPARED", "baseline_entries_sha256": digest(baseline),
                       "worktree_fingerprints": fingerprints,
                       "remotes": {name: {"state": "PENDING", "attempts": 0,
                                          "expected_head": request["expected_heads"].get(name),
                                          "expectation_set": name in request["expected_heads"]}
                                   for name in request["remotes"]}}
            self._store(receipt)
            return self._resume_locked(receipt)

    def resume(self, checkpoint_id: str) -> dict:
        with self._lock():
            return self._resume_locked(self._read(checkpoint_id))

    def status(self, checkpoint_id: str) -> dict:
        with self._lock():
            return self._public(self._read(checkpoint_id))

    def _resume_locked(self, receipt: dict) -> dict:
        try:
            if receipt.get("commit_sha") is None:
                self._prepare_commit(receipt)
            if receipt["phase"] not in {"LOCAL_SAVED", "SYNCING", "COMPLETE", "PARTIAL_SYNC"}:
                self._finish_local(receipt)
            self._validate_commit(receipt)
            if not self._ancestor(receipt["commit_sha"], self._head(receipt["branch"])):
                raise CheckpointError("LOCAL_HISTORY_DIVERGED")
            for remote in receipt["request"]["remotes"]:
                self._sync_remote(receipt, remote)
            receipt["phase"] = ("COMPLETE" if receipt["remotes"] and all(
                item["state"] == "SYNCED" for item in receipt["remotes"].values()) else
                "PARTIAL_SYNC" if receipt["remotes"] else "LOCAL_SAVED")
            receipt.pop("last_error", None)
            self._store(receipt)
            return self._public(receipt)
        except CheckpointError as error:
            receipt["last_error"] = error.to_dict()
            self._store(receipt)
            raise

    def _work(self, receipt: dict) -> Path:
        path = self.state / (receipt["checkpoint_id"] + ".work")
        self._private_dir(path)
        return path

    def _prepare_commit(self, receipt: dict) -> None:
        self._assert_idle()
        if self._branch() != receipt["branch"] or self._head() != receipt["base_head"]:
            raise CheckpointError("LOCAL_HEAD_CHANGED")
        files = receipt["request"]["files"]
        if digest(self._entries(files)) != receipt["baseline_entries_sha256"]:
            raise CheckpointError("INDEX_CHANGED")
        for filename in files:
            path = self.root / filename
            info = _regular_file(path, optional=True)
            if info and info.st_size > MAX_FILE_BYTES:
                raise CheckpointError("FILE_TOO_LARGE", path=filename)
            current = digest(path.read_bytes()) if info else None
            if current != receipt["worktree_fingerprints"][filename]:
                raise CheckpointError("WORKTREE_CHANGED", path=filename)
        work = self._work(receipt)
        alternate = work / "snapshot.index"
        # Наш snapshot можно пересоздать до создания коммита; пользовательский index не трогаем.
        for path in (alternate, alternate.with_name(alternate.name + ".lock")):
            if path.exists() or path.is_symlink():
                _regular_file(path)
                path.unlink()
        self._git("read-tree", receipt["base_head"], index=alternate, required=True)
        self._git("add", "-A", "--", *files, index=alternate, required=True)
        message_file = work / "message.txt"
        message = (receipt["request"]["message"].strip() + "\n\nTask-ID: " + receipt["request"]["task_id"]
                   + "\nCheckpoint-ID: " + receipt["checkpoint_id"] + "\n")
        if message_file.exists() or message_file.is_symlink():
            _regular_file(message_file)
        message_file.write_text(message, encoding="utf-8")
        if os.name != "nt":
            message_file.chmod(0o600)
        for hook, args in (("pre-commit", []), ("prepare-commit-msg", [str(message_file), "message"]),
                           ("commit-msg", [str(message_file)])):
            result = self._git("hook", "run", "--ignore-missing", hook, "--", *args, index=alternate)
            if result.returncode:
                raise CheckpointError("COMMIT_HOOK_FAILED")
        final_message = message_file.read_bytes()
        self._scan(final_message)
        lines = final_message.decode().splitlines()
        if (sum(line.startswith("Checkpoint-ID:") for line in lines) != 1
                or sum(line.startswith("Task-ID:") for line in lines) != 1
                or "Checkpoint-ID: " + receipt["checkpoint_id"] not in lines
                or "Task-ID: " + receipt["request"]["task_id"] not in lines):
            raise CheckpointError("COMMIT_TRAILER_CHANGED")
        changed = self._git("diff", "--cached", "--name-only", "--no-renames", "-z", receipt["base_head"],
                            index=alternate, required=True).stdout
        changed_paths = {item.decode() for item in changed.split(b"\0") if item}
        if not changed_paths:
            raise CheckpointError("NO_CHECKPOINT_CHANGES")
        if not changed_paths <= set(files):
            raise CheckpointError("HOOK_CHANGED_OUTSIDE_ALLOWLIST")
        blobs = {}
        for entry in self._entries(files, index=alternate).split(b"\0"):
            if not entry:
                continue
            meta, name = entry.split(b"\t", 1)
            mode, oid, stage = meta.split(b" ")
            if mode not in {b"100644", b"100755"} or stage != b"0":
                raise CheckpointError("UNSAFE_STAGED_FILE")
            filename = name.decode()
            size = self._git("cat-file", "-s", oid.decode(), required=True).stdout.strip()
            if not size.isdigit() or int(size) > MAX_FILE_BYTES:
                raise CheckpointError("FILE_TOO_LARGE", path=filename)
            content = self._git("cat-file", "blob", oid.decode(), required=True).stdout
            self._scan(content, filename)
            blobs[filename] = oid.decode()
            if filename == receipt["request"]["todo"]:
                if (receipt["checkpoint_id"].encode() not in content
                        or receipt["request"]["task_id"].encode() not in content):
                    raise CheckpointError("TODO_CHECKPOINT_MARKER_REQUIRED")
        if receipt["request"]["todo"] not in blobs:
            raise CheckpointError("TODO_CHECKPOINT_MARKER_REQUIRED")
        tree = self._git("write-tree", index=alternate, required=True).stdout.decode().strip()
        args = ["commit-tree", tree, "-p", receipt["base_head"], "-F", str(message_file)]
        signing = self._git("config", "--bool", "--get", "commit.gpgsign")
        if signing.returncode not in {0, 1}:
            raise CheckpointError("INVALID_SIGNING_CONFIGURATION")
        if signing.stdout.strip() == b"true":
            args.append("-S")
        commit = self._git(*args, index=alternate, required=True).stdout.decode().strip()
        if not SHA_RE.fullmatch(commit):
            raise CheckpointError("INVALID_COMMIT_RESPONSE")
        receipt.update({"phase": "COMMIT_PREPARED", "tree_sha": tree, "commit_sha": commit,
                        "file_blobs": blobs, "changed_files": sorted(changed_paths)})
        self._store(receipt)

    def _validate_commit(self, receipt: dict) -> None:
        commit = receipt["commit_sha"]
        result = self._git("show", "-s", "--format=%T%n%P%n%B", commit, required=True).stdout.decode()
        lines = result.splitlines()
        if (len(lines) < 3 or lines[0] != receipt["tree_sha"] or lines[1] != receipt["base_head"]
                or "Checkpoint-ID: " + receipt["checkpoint_id"] not in lines
                or "Task-ID: " + receipt["request"]["task_id"] not in lines):
            raise CheckpointError("LOCAL_COMMIT_MISMATCH")
        changed = self._git("diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z",
                            commit, required=True).stdout
        if {name.decode() for name in changed.split(b"\0") if name} - set(receipt["request"]["files"]):
            raise CheckpointError("LOCAL_COMMIT_SCOPE_MISMATCH")

    def _index_digest(self) -> str | None:
        info = _regular_file(self.index, optional=True)
        return digest(self.index.read_bytes()) if info else None

    def _finish_local(self, receipt: dict) -> None:
        self._validate_commit(receipt)
        self._assert_idle()
        if self._branch() != receipt["branch"]:
            raise CheckpointError("WRONG_BRANCH")
        head = self._head()
        if head not in {receipt["base_head"], receipt["commit_sha"]}:
            raise CheckpointError("LOCAL_HEAD_CHANGED")
        lock_path = self.index.with_name(self.index.name + ".lock")
        own_lock = receipt.get("index_transaction")
        if lock_path.exists() or lock_path.is_symlink():
            info = _regular_file(lock_path)
            if (not own_lock or own_lock.get("sha256") != digest(lock_path.read_bytes())
                    or own_lock.get("device") != info.st_dev or own_lock.get("inode") != info.st_ino
                    or own_lock.get("before_sha256") != self._index_digest()):
                raise CheckpointError("INDEX_LOCK_EXISTS")
        else:
            if head == receipt["commit_sha"]:
                # Ответ потерян уже после atomic rename index: проверяем, что наши entries совпадают.
                expected = self._git("ls-files", "--stage", "-z", "--", *receipt["request"]["files"],
                                     index=self._work(receipt) / "snapshot.index", required=True).stdout
                if self._entries(receipt["request"]["files"]) != expected:
                    raise CheckpointError("INDEX_RECOVERY_CONFLICT")
                receipt["phase"] = "LOCAL_SAVED"
                self._store(receipt)
                return
            try:
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise CheckpointError("INDEX_LOCK_EXISTS") from None
            keep_lock = False
            try:
                if digest(self._entries(receipt["request"]["files"])) != receipt["baseline_entries_sha256"]:
                    raise CheckpointError("INDEX_CHANGED")
                before = self._index_digest()
                reconcile = self._work(receipt) / "reconcile.index"
                if reconcile.exists() or reconcile.is_symlink():
                    _regular_file(reconcile)
                    reconcile.unlink()
                if self.index.exists():
                    shutil.copyfile(self.index, reconcile)
                else:
                    self._git("read-tree", receipt["base_head"], index=reconcile, required=True)
                self._git("reset", "-q", receipt["commit_sha"], "--", *receipt["request"]["files"],
                          index=reconcile, required=True)
                payload = reconcile.read_bytes()
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                info = os.fstat(fd)
                receipt["index_transaction"] = {"sha256": digest(payload), "before_sha256": before,
                                                 "device": info.st_dev, "inode": info.st_ino}
                receipt["phase"] = "INDEX_PREPARED"
                self._store(receipt)
                keep_lock = True
            finally:
                os.close(fd)
                if not keep_lock:
                    lock_path.unlink(missing_ok=True)
        # Compare-and-swap: чужое продвижение ветки никогда не перезаписываем.
        latest = self._head()
        if latest not in {receipt["base_head"], receipt["commit_sha"]}:
            self._release_owned_index_lock(receipt, lock_path)
            raise CheckpointError("LOCAL_HEAD_CHANGED")
        if latest == receipt["base_head"]:
            result = self._git("update-ref", "-m", "checkpoint " + receipt["checkpoint_id"],
                               "refs/heads/" + receipt["branch"], receipt["commit_sha"], receipt["base_head"])
            if result.returncode and self._head() != receipt["commit_sha"]:
                # Наш ref не записан; index ещё старый. Освобождаем только наш известный lock.
                self._release_owned_index_lock(receipt, lock_path)
                raise CheckpointError("LOCAL_REF_UPDATE_FAILED")
        os.replace(lock_path, self.index)
        _fsync_dir(self.index.parent)
        receipt["phase"] = "LOCAL_SAVED"
        self._store(receipt)
        # Не пытаемся отменить уже сохранённый коммит при сбое post-commit.
        result = self._git("hook", "run", "--ignore-missing", "post-commit")
        if result.returncode:
            receipt["post_commit_hook"] = "FAILED"
            self._store(receipt)

    def _release_owned_index_lock(self, receipt: dict, path: Path) -> None:
        info = _regular_file(path)
        saved = receipt["index_transaction"]
        if (saved["sha256"] != digest(path.read_bytes()) or info.st_dev != saved["device"]
                or info.st_ino != saved["inode"] or self._index_digest() != saved["before_sha256"]):
            raise CheckpointError("INDEX_RECOVERY_CONFLICT")
        path.unlink()
        receipt.pop("index_transaction", None)
        receipt["phase"] = "COMMIT_PREPARED"
        self._store(receipt)

    def _remote_url(self, remote: str) -> str:
        result = self._git("remote", "get-url", "--push", "--all", remote)
        values = result.stdout.decode().splitlines()
        if result.returncode or len(values) != 1 or not values[0] or any(ord(c) < 32 for c in values[0]):
            raise CheckpointError("REMOTE_NOT_CONFIGURED", remote=remote)
        url = values[0]
        if "://" in url:
            try:
                parsed = urlsplit(url)
            except ValueError:
                raise CheckpointError("REMOTE_PROTOCOL_REJECTED", remote=remote) from None
            if parsed.scheme not in {"https", "http", "ssh", "file"}:
                raise CheckpointError("REMOTE_PROTOCOL_REJECTED", remote=remote)
            if (parsed.password is not None or parsed.query or parsed.fragment
                    or (parsed.scheme in {"https", "http"} and parsed.username is not None)):
                raise CheckpointError("CREDENTIALS_IN_REMOTE_URL", remote=remote)
        elif re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]+:.+", url):
            if "::" in url:
                raise CheckpointError("REMOTE_PROTOCOL_REJECTED", remote=remote)
        else:
            # Локальный bare repo: относительный путь зависит от worktree, фиксируем абсолютный.
            path = Path(url)
            url = str((self.root / path).resolve() if not path.is_absolute() else path.resolve())
        if url.startswith("-"):
            raise CheckpointError("REMOTE_PROTOCOL_REJECTED", remote=remote)
        return url

    def _remote_head(self, url: str, branch: str) -> tuple[bool, str | None]:
        ref = "refs/heads/" + branch
        result = self._git("ls-remote", "--heads", "--", url, ref)
        if result.returncode:
            return False, None
        if not result.stdout.strip():
            return True, None
        lines = result.stdout.decode().splitlines()
        if len(lines) != 1:
            return False, None
        fields = lines[0].split("\t")
        if len(fields) != 2 or fields[1] != ref or not SHA_RE.fullmatch(fields[0]):
            return False, None
        return True, fields[0]

    def _ensure_object(self, url: str, oid: str) -> bool:
        if self._git("cat-file", "-e", oid + "^{commit}").returncode == 0:
            return True
        result = self._git("fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules", "--", url, oid)
        return result.returncode == 0 and self._git("cat-file", "-e", oid + "^{commit}").returncode == 0

    def _ancestor(self, older: str, newer: str) -> bool:
        return self._git("merge-base", "--is-ancestor", older, newer).returncode == 0

    def _sync_remote(self, receipt: dict, remote: str) -> None:
        item = receipt["remotes"][remote]
        item["attempts"] += 1
        item["checked_at"] = utc_now()
        receipt["phase"] = "SYNCING"
        try:
            url = self._remote_url(remote)
            identity = digest(url.encode())
            if item.get("remote_identity") not in {None, identity}:
                raise CheckpointError("REMOTE_IDENTITY_CHANGED", remote=remote)
            item["remote_identity"] = identity
            self._store(receipt)
            ok, head = self._remote_head(url, receipt["branch"])
            if not ok:
                raise CheckpointError("REMOTE_UNAVAILABLE", remote=remote)
            item["observed_head"] = head
            commit = receipt["commit_sha"]
            if head == commit or (head and self._ensure_object(url, head) and self._ancestor(commit, head)):
                item.update({"state": "SYNCED", "confirmed_head": head, "confirmed_at": utc_now()})
                item.pop("error", None)
                self._store(receipt)
                return
            if not item["expectation_set"]:
                item.update({"expectation_set": True, "expected_head": head})
                self._store(receipt)
            if head != item["expected_head"]:
                raise CheckpointError("REMOTE_HEAD_CHANGED", remote=remote)
            if head and (not self._ensure_object(url, head) or not self._ancestor(head, commit)):
                raise CheckpointError("REMOTE_DIVERGED", remote=remote)
            # Повторная проверка непосредственно перед push; сам push — только fast-forward.
            ready, current = self._remote_head(url, receipt["branch"])
            if not ready:
                raise CheckpointError("REMOTE_UNAVAILABLE", remote=remote)
            if current != head:
                raise CheckpointError("REMOTE_HEAD_CHANGED", remote=remote)
            item["state"] = "PUSHING"
            self._store(receipt)
            result = self._git("-c", "push.followTags=false", "push", "--porcelain", "--no-force",
                               "--no-follow-tags", "--recurse-submodules=no", "--", url,
                               commit + ":refs/heads/" + receipt["branch"])
            # Ненулевой код/timeout не доказывает, что сервер не принял push.
            verified, after = self._remote_head(url, receipt["branch"])
            if verified and (after == commit or (after and self._ensure_object(url, after)
                                                and self._ancestor(commit, after))):
                item.update({"state": "SYNCED", "confirmed_head": after, "confirmed_at": utc_now(),
                             "response_recovered": result.returncode != 0})
                item.pop("error", None)
            else:
                item["state"] = "PENDING"
                item["error"] = "PUSH_NOT_CONFIRMED" if not verified else "PUSH_REJECTED"
                if verified:
                    item["observed_head"] = after
        except CheckpointError as error:
            item["state"] = "CONFLICT" if error.code in {
                "REMOTE_IDENTITY_CHANGED", "REMOTE_HEAD_CHANGED", "REMOTE_DIVERGED"} else "BLOCKED"
            item["error"] = error.code
        self._store(receipt)

    @staticmethod
    def _public(receipt: dict) -> dict:
        return {"schema_version": SCHEMA_VERSION, "checkpoint_id": receipt["checkpoint_id"],
                "task_id": receipt["request"]["task_id"], "branch": receipt["branch"],
                "phase": receipt["phase"], "commit_sha": receipt.get("commit_sha"),
                "files": receipt.get("changed_files", receipt["request"]["files"]),
                "local_saved": receipt["phase"] in {"LOCAL_SAVED", "SYNCING", "COMPLETE", "PARTIAL_SYNC"},
                "remotes": receipt["remotes"], "last_error": receipt.get("last_error"),
                "post_commit_hook": receipt.get("post_commit_hook", "OK_OR_NOT_RUN")}


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse по умолчанию повторяет неизвестные аргументы, включая случайно переданный секрет.
        raise CheckpointError("INVALID_ARGUMENTS")


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description="Явные Git-checkpoint без захвата чужих изменений")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("id", help="Создать стабильный ID до обновления ToDo")
    for name in ("save", "resume", "status"):
        command = commands.add_parser(name)
        command.add_argument("--repo", default=".")
        command.add_argument("--timeout", type=int, default=30)
        command.add_argument("--checkpoint-id", required=True)
        if name == "save":
            command.add_argument("--task-id", required=True)
            command.add_argument("--todo", required=True)
            command.add_argument("--file", action="append", required=True, dest="files")
            command.add_argument("--remote", action="append", default=[], dest="remotes")
            command.add_argument("--expect-remote", action="append", default=[], metavar="NAME=SHA|absent")
            command.add_argument("--message")
    try:
        args = parser.parse_args(argv)
        if args.command == "id":
            result = {"checkpoint_id": new_checkpoint_id()}
        else:
            subject = Checkpointer(args.repo, timeout=args.timeout)
            if args.command == "save":
                expected = {}
                for entry in args.expect_remote:
                    name, separator, value = entry.partition("=")
                    if not separator or name in expected:
                        raise CheckpointError("INVALID_EXPECTED_HEAD")
                    expected[name] = None if value == "absent" else value
                result = subject.save(task_id=args.task_id, files=args.files, todo=args.todo,
                                      remotes=args.remotes, checkpoint_id=args.checkpoint_id,
                                      message=args.message, expected_heads=expected)
            else:
                result = getattr(subject, args.command)(args.checkpoint_id)
        print(json.dumps({"result": result}, ensure_ascii=False, sort_keys=True))
        return 2 if result.get("phase") == "PARTIAL_SYNC" else 0
    except CheckpointError as error:
        print(json.dumps({"error": error.to_dict()}, ensure_ascii=False, sort_keys=True))
        return 1
    except (OSError, UnicodeError, ValueError, TypeError):
        # Без traceback: сообщения файловой системы/Git могут содержать секреты или URL.
        print(json.dumps({"error": {"code": "CHECKPOINT_IO_ERROR"}}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
