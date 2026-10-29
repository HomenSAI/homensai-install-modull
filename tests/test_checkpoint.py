"""Checkpoint на настоящих временных Git/bare repos, без сети и пользовательского Git."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "checkpoint.py"
SPEC = importlib.util.spec_from_file_location("checkpoint_tool", MODULE_PATH)
checkpoint = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checkpoint
SPEC.loader.exec_module(checkpoint)
Checkpointer = checkpoint.Checkpointer
CheckpointError = checkpoint.CheckpointError
CP = "CP-test-001"


def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if check and result.returncode:
        pytest.fail("fixture git command failed: " + args[0])
    return result.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Изоляция от signing/hooks/credentials пользователя: только тестовая локальная конфигурация.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "work"
    path.mkdir()
    git(path, "init", "--initial-branch=codex/checkpoints")
    git(path, "config", "user.name", "Checkpoint Test")
    git(path, "config", "user.email", "checkpoint@example.invalid")
    for name, content in {"app.py": "print('one')\n", "TODO.txt": "HIM-009: planned\n",
                          "foreign.txt": "foreign baseline\n", "remove.txt": "obsolete\n"}.items():
        (path / name).write_text(content)
    git(path, "add", "app.py", "TODO.txt", "foreign.txt", "remove.txt")
    git(path, "commit", "-m", "Начальная тестовая версия")
    return path


def prepare(repo, cp=CP):
    (repo / "app.py").write_text("print('two')\n")
    (repo / "TODO.txt").write_text("HIM-009: source tests passed; " + cp + "\n")


def save(repo, cp=CP, subject=None, **kwargs):
    return (subject or Checkpointer(repo)).save(
        task_id="HIM-009", checkpoint_id=cp, files=kwargs.pop("files", ["app.py", "TODO.txt"]),
        todo="TODO.txt", **kwargs)


def bare(repo, tmp_path, name):
    target = tmp_path / (name + ".git")
    target.mkdir()
    git(target, "init", "--bare")
    git(repo, "remote", "add", name, str(target))
    return target


def remote_head(repo, name="origin"):
    output = git(repo, "ls-remote", "--heads", name, "refs/heads/codex/checkpoints")
    return output.split()[0] if output else None


def hook(repo, name, text):
    path = repo / ".git" / "hooks" / name
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o755)


def test_allowlist_preserves_foreign_staged_and_unstaged(repo):
    prepare(repo)
    (repo / "foreign.txt").write_text("foreign staged\n")
    git(repo, "add", "foreign.txt")
    (repo / "foreign.txt").write_text("foreign still working\n")
    (repo / "untracked.txt").write_text("not ours\n")
    staged = git(repo, "show", ":foreign.txt")
    result = save(repo)
    assert result["phase"] == "LOCAL_SAVED" and result["local_saved"]
    assert git(repo, "show", ":foreign.txt") == staged
    assert (repo / "foreign.txt").read_text() == "foreign still working\n"
    assert git(repo, "diff", "--cached", "--name-only") == "foreign.txt"
    assert git(repo, "show", "HEAD:foreign.txt") == "foreign baseline"
    assert set(git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()) == {
        "app.py", "TODO.txt"}
    assert (repo / "untracked.txt").read_text() == "not ours\n"
    assert (repo / ".git" / "hinstall-checkpoints" / (CP + ".json")).is_file()
    assert "Checkpoint-ID: " + CP in git(repo, "log", "-1", "--format=%B")


def test_allowlisted_partial_staging_rejected(repo):
    prepare(repo)
    git(repo, "add", "app.py")
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(CheckpointError, match="ALLOWLIST_ALREADY_STAGED"):
        save(repo)
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "diff", "--cached", "--name-only") == "app.py"


def test_two_independent_remotes_confirm_exact_commit(repo, tmp_path):
    bare(repo, tmp_path, "origin")
    bare(repo, tmp_path, "forgejo")
    prepare(repo)
    result = save(repo, remotes=["origin", "forgejo"])
    assert result["phase"] == "COMPLETE"
    for name in ("origin", "forgejo"):
        assert result["remotes"][name]["state"] == "SYNCED"
        assert remote_head(repo, name) == result["commit_sha"]


def test_disconnected_remote_keeps_local_and_other_remote_then_resumes(repo, tmp_path):
    bare(repo, tmp_path, "origin")
    disconnected = tmp_path / "offline.git"
    git(repo, "remote", "add", "forgejo", str(disconnected))
    prepare(repo)
    result = save(repo, remotes=["origin", "forgejo"])
    assert result["phase"] == "PARTIAL_SYNC" and result["local_saved"]
    assert result["remotes"]["origin"]["state"] == "SYNCED"
    assert result["remotes"]["forgejo"]["error"] == "REMOTE_UNAVAILABLE"
    commit = result["commit_sha"]
    disconnected.mkdir()
    git(disconnected, "init", "--bare")
    resumed = Checkpointer(repo).resume(CP)
    assert resumed["phase"] == "COMPLETE" and resumed["commit_sha"] == commit
    assert git(repo, "rev-list", "--count", "HEAD") == "2"
    assert remote_head(repo, "forgejo") == commit


def test_expected_remote_head_mismatch_never_pushes(repo, tmp_path):
    bare(repo, tmp_path, "origin")
    git(repo, "push", "origin", "HEAD:refs/heads/codex/checkpoints")
    before = remote_head(repo)
    prepare(repo)
    result = save(repo, remotes=["origin"], expected_heads={"origin": "a" * 40})
    assert result["local_saved"]
    assert result["remotes"]["origin"]["error"] == "REMOTE_HEAD_CHANGED"
    assert remote_head(repo) == before


def test_divergent_remote_never_overwritten(repo, tmp_path):
    remote = bare(repo, tmp_path, "origin")
    git(repo, "push", "origin", "HEAD:refs/heads/codex/checkpoints")
    competitor = tmp_path / "competitor"
    git(tmp_path, "clone", "--branch", "codex/checkpoints", str(remote), str(competitor))
    git(competitor, "config", "user.name", "Other Test")
    git(competitor, "config", "user.email", "other@example.invalid")
    (competitor / "other.txt").write_text("independent work\n")
    git(competitor, "add", "other.txt")
    git(competitor, "commit", "-m", "Другое изменение")
    git(competitor, "push", "origin", "HEAD")
    before = remote_head(repo)
    prepare(repo)
    result = save(repo, remotes=["origin"])
    assert result["remotes"]["origin"]["error"] == "REMOTE_DIVERGED"
    assert remote_head(repo) == before
    assert result["local_saved"]


def test_lost_push_response_verified_without_duplicate_commit(repo, tmp_path):
    bare(repo, tmp_path, "origin")
    prepare(repo)
    marker = "private-credential-that-must-never-be-output"

    class LostResponse(Checkpointer):
        def _git(self, *args, **kwargs):
            result = super()._git(*args, **kwargs)
            if "push" in args:
                assert result.returncode == 0
                return checkpoint.GitResult(124, stderr=marker.encode(), timed_out=True)
            return result

    result = save(repo, subject=LostResponse(repo), remotes=["origin"])
    assert result["phase"] == "COMPLETE"
    assert result["remotes"]["origin"]["response_recovered"]
    assert marker not in json.dumps(result)
    assert marker not in (repo / ".git" / "hinstall-checkpoints" / (CP + ".json")).read_text()


def test_remote_identity_change_requires_explicit_new_resolution(repo, tmp_path):
    first = tmp_path / "missing.git"
    git(repo, "remote", "add", "origin", str(first))
    prepare(repo)
    save(repo, remotes=["origin"])
    second = tmp_path / "different.git"
    second.mkdir()
    git(second, "init", "--bare")
    git(repo, "remote", "set-url", "origin", str(second))
    result = Checkpointer(repo).resume(CP)
    assert result["remotes"]["origin"]["error"] == "REMOTE_IDENTITY_CHANGED"
    assert remote_head(repo) is None


def test_remote_ahead_containing_checkpoint_is_confirmed(repo, tmp_path):
    bare(repo, tmp_path, "origin")
    prepare(repo)
    first = save(repo, remotes=["origin"])
    (repo / "extra.txt").write_text("newer independent work\n")
    git(repo, "add", "extra.txt")
    git(repo, "commit", "-m", "Следующее изменение")
    git(repo, "push", "origin", "HEAD")
    newer = git(repo, "rev-parse", "HEAD")
    result = Checkpointer(repo).resume(CP)
    assert result["phase"] == "COMPLETE"
    assert result["commit_sha"] == first["commit_sha"]
    assert result["remotes"]["origin"]["confirmed_head"] == newer
    assert remote_head(repo) == newer


@pytest.mark.skipif(os.name == "nt", reason="Тест POSIX shell hook; Windows runner — отдельная матрица")
def test_failing_commit_hook_blocks_commit_without_dumping_output(repo):
    prepare(repo)
    before = git(repo, "rev-parse", "HEAD")
    hook(repo, "pre-commit", "echo do-not-export-hook-output >&2\nexit 1\n")
    with pytest.raises(CheckpointError, match="COMMIT_HOOK_FAILED") as caught:
        save(repo)
    assert "do-not-export" not in json.dumps(caught.value.to_dict())
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.skipif(os.name == "nt", reason="Тест POSIX shell hook")
def test_hook_cannot_expand_commit_allowlist(repo):
    prepare(repo)
    (repo / "foreign.txt").write_text("hook tries to stage this\n")
    hook(repo, "pre-commit", "git add foreign.txt\n")
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(CheckpointError, match="HOOK_CHANGED_OUTSIDE_ALLOWLIST"):
        save(repo)
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "diff", "--cached", "--name-only") == ""


@pytest.mark.skipif(os.name == "nt", reason="Тест POSIX shell hook")
def test_scan_covers_exact_staged_blob_after_hook(repo):
    prepare(repo)
    token = "gh" + "p_" + "A" * 36
    hook(repo, "pre-commit", "printf '%s\\n' '" + token + "' > app.py\ngit add app.py\n")
    with pytest.raises(CheckpointError, match="SECRET_PATTERN_REJECTED"):
        save(repo)
    assert git(repo, "rev-list", "--count", "HEAD") == "1"


@pytest.mark.parametrize("bad", ["../outside.txt", "/tmp/outside.txt", "./app.py", ".git/config",
                                "folder/../app.py", "a\\b", "C:/anything", "folder/"])
def test_nonliteral_or_outside_paths_rejected(repo, bad):
    prepare(repo)
    with pytest.raises(CheckpointError, match="INVALID_FILE_PATH"):
        save(repo, files=[bad, "TODO.txt"])


def test_sensitive_filenames_and_actual_tokens_rejected(repo):
    prepare(repo)
    (repo / ".env").write_text("TEST=example\n")
    with pytest.raises(CheckpointError, match="SENSITIVE_FILE_REJECTED"):
        save(repo, files=[".env", "TODO.txt"])
    (repo / "app.py").write_text("gh" + "p_" + "A" * 36)
    with pytest.raises(CheckpointError, match="SECRET_PATTERN_REJECTED"):
        save(repo)


def test_symlinks_and_binary_rejected(repo):
    prepare(repo)
    (repo / "link.py").symlink_to(repo / "app.py")
    with pytest.raises(CheckpointError, match="UNSAFE_FILE_TYPE"):
        save(repo, files=["link.py", "TODO.txt"])
    (repo / "app.py").write_bytes(b"some\0binary")
    with pytest.raises(CheckpointError, match="BINARY_FILE_REJECTED"):
        save(repo)


def test_protected_branch_rejected(repo):
    git(repo, "branch", "-m", "main")
    prepare(repo)
    with pytest.raises(CheckpointError, match="PROTECTED_BRANCH"):
        save(repo)


def test_todo_evidence_marker_required_and_never_written_by_tool(repo):
    prepare(repo)
    content = "HIM-009: incomplete, no checkpoint marker\n"
    (repo / "TODO.txt").write_text(content)
    with pytest.raises(CheckpointError, match="TODO_CHECKPOINT_MARKER_REQUIRED"):
        save(repo)
    assert (repo / "TODO.txt").read_text() == content
    assert git(repo, "rev-list", "--count", "HEAD") == "1"


def test_linked_worktree_receipt_outside_tree_and_original_index_preserved(repo, tmp_path):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "codex/linked", str(linked))
    (repo / "foreign.txt").write_text("original worktree staged\n")
    git(repo, "add", "foreign.txt")
    prepare(linked)
    result = save(linked)
    assert result["branch"] == "codex/linked"
    assert (repo / ".git" / "hinstall-checkpoints" / (CP + ".json")).exists()
    assert git(repo, "diff", "--cached", "--name-only") == "foreign.txt"
    assert git(linked, "diff", "--cached", "--name-only") == ""
    with pytest.raises(CheckpointError, match="WRONG_WORKTREE"):
        Checkpointer(repo).resume(CP)


def test_crash_after_ref_update_recovers_owned_index_lock_and_same_commit(repo):
    prepare(repo)
    (repo / "foreign.txt").write_text("keep staged across crash\n")
    git(repo, "add", "foreign.txt")

    class Interrupted(Checkpointer):
        def _git(self, *args, **kwargs):
            result = super()._git(*args, **kwargs)
            if args and args[0] == "update-ref":
                assert result.returncode == 0
                raise KeyboardInterrupt("simulated process loss")
            return result

    with pytest.raises(KeyboardInterrupt):
        save(repo, subject=Interrupted(repo))
    commit = git(repo, "rev-parse", "HEAD")
    assert (repo / ".git" / "index.lock").exists()
    result = Checkpointer(repo).resume(CP)
    assert result["local_saved"] and result["commit_sha"] == commit
    assert not (repo / ".git" / "index.lock").exists()
    assert git(repo, "diff", "--cached", "--name-only") == "foreign.txt"
    assert git(repo, "rev-list", "--count", "HEAD") == "2"


def test_crash_after_local_save_resumes_only_remote_work(repo, tmp_path):
    bare(repo, tmp_path, "origin")
    prepare(repo)

    class Interrupted(Checkpointer):
        def _sync_remote(self, receipt, remote):
            raise KeyboardInterrupt("simulated loss before push")

    with pytest.raises(KeyboardInterrupt):
        save(repo, subject=Interrupted(repo), remotes=["origin"])
    before = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("do not include subsequent work\n")
    result = Checkpointer(repo).resume(CP)
    assert result["phase"] == "COMPLETE" and result["commit_sha"] == before
    assert git(repo, "show", "HEAD:app.py") == "print('two')"
    assert (repo / "app.py").read_text() == "do not include subsequent work\n"


def test_foreign_index_lock_is_not_removed(repo):
    prepare(repo)
    lock = repo / ".git" / "index.lock"
    lock.write_bytes(b"another git operation owns this")
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(CheckpointError, match="INDEX_LOCK_EXISTS"):
        save(repo)
    assert lock.read_bytes() == b"another git operation owns this"
    assert git(repo, "rev-parse", "HEAD") == before
    lock.unlink()
    result = Checkpointer(repo).resume(CP)
    assert result["local_saved"]


def test_head_compare_and_swap_never_overwrites_concurrent_commit(repo):
    prepare(repo)
    competing = None

    class Racing(Checkpointer):
        def _git(self, *args, **kwargs):
            nonlocal competing
            if args and args[0] == "update-ref" and competing is None:
                base = git(repo, "rev-parse", "HEAD")
                tree = git(repo, "rev-parse", "HEAD^{tree}")
                competing = git(repo, "commit-tree", tree, "-p", base, "-m", "Чужой коммит")
                git(repo, "update-ref", "refs/heads/codex/checkpoints", competing, base)
            return super()._git(*args, **kwargs)

    with pytest.raises(CheckpointError, match="LOCAL_REF_UPDATE_FAILED"):
        save(repo, subject=Racing(repo))
    assert git(repo, "rev-parse", "HEAD") == competing
    assert not (repo / ".git" / "index.lock").exists()
    assert (repo / "app.py").read_text() == "print('two')\n"


def test_same_id_is_idempotent_but_different_request_conflicts(repo):
    prepare(repo)
    first = save(repo)
    (repo / "app.py").write_text("later uncommitted work\n")
    again = save(repo)
    assert first["commit_sha"] == again["commit_sha"]
    assert git(repo, "rev-list", "--count", "HEAD") == "2"
    with pytest.raises(CheckpointError, match="CHECKPOINT_ID_REUSED"):
        save(repo, files=["app.py", "TODO.txt", "foreign.txt"])


def test_tracked_deletion_uses_literal_allowlist(repo):
    prepare(repo)
    (repo / "remove.txt").unlink()
    result = save(repo, files=["app.py", "TODO.txt", "remove.txt"])
    assert "remove.txt" in result["files"]
    assert "remove.txt" not in git(repo, "ls-tree", "--name-only", "HEAD").splitlines()


def test_changed_files_before_uncommitted_resume_require_new_review(repo):
    prepare(repo)

    class Interrupted(Checkpointer):
        def _prepare_commit(self, receipt):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        save(repo, subject=Interrupted(repo))
    (repo / "app.py").write_text("new work after interrupted review\n")
    with pytest.raises(CheckpointError, match="WORKTREE_CHANGED"):
        Checkpointer(repo).resume(CP)
    assert git(repo, "rev-list", "--count", "HEAD") == "1"


def test_credential_url_is_never_echoed_and_local_save_survives(repo):
    prepare(repo)
    marker = "must-not-appear-secret"
    url = "https" + "://user:" + marker + "@example.invalid/repo.git"
    git(repo, "remote", "add", "origin", url)
    result = save(repo, remotes=["origin"])
    assert result["local_saved"]
    assert result["remotes"]["origin"]["error"] == "CREDENTIALS_IN_REMOTE_URL"
    assert marker not in json.dumps(result)


def test_cli_json_exit_status_and_unique_ids(repo, capsys):
    assert checkpoint.main(["id"]) == 0
    first = json.loads(capsys.readouterr().out)["result"]["checkpoint_id"]
    assert checkpoint.main(["id"]) == 0
    second = json.loads(capsys.readouterr().out)["result"]["checkpoint_id"]
    assert first != second and checkpoint.ID_RE.fullmatch(first)
    prepare(repo)
    code = checkpoint.main(["save", "--repo", str(repo), "--checkpoint-id", CP, "--task-id", "HIM-009",
                            "--todo", "TODO.txt", "--file", "app.py", "--file", "TODO.txt", "--remote", "missing"])
    result = json.loads(capsys.readouterr().out)["result"]
    assert code == 2 and result["local_saved"]
    assert result["remotes"]["missing"]["error"] == "REMOTE_NOT_CONFIGURED"


def test_invalid_cli_does_not_repeat_unknown_secret(capsys):
    marker = "accidental-argument-secret-must-not-repeat"
    assert checkpoint.main(["id", "--unknown", marker]) == 1
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
    assert json.loads(captured.out)["error"]["code"] == "INVALID_ARGUMENTS"


def test_required_commit_signing_is_never_disabled(repo, tmp_path):
    prepare(repo)
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", str(tmp_path / "unavailable-signing-program"))
    with pytest.raises(CheckpointError, match="GIT_COMMAND_FAILED"):
        save(repo)
    assert git(repo, "rev-list", "--count", "HEAD") == "1"
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_worktree_race_before_ref_cas_releases_only_own_lock(repo):
    prepare(repo)
    competing = None

    class Racing(Checkpointer):
        def _store(self, receipt):
            nonlocal competing
            super()._store(receipt)
            if receipt["phase"] == "INDEX_PREPARED" and competing is None:
                base = git(repo, "rev-parse", "HEAD")
                tree = git(repo, "rev-parse", "HEAD^{tree}")
                competing = git(repo, "commit-tree", tree, "-p", base, "-m", "Конкурентный коммит")
                git(repo, "update-ref", "refs/heads/codex/checkpoints", competing, base)

    with pytest.raises(CheckpointError, match="LOCAL_HEAD_CHANGED"):
        save(repo, subject=Racing(repo))
    assert git(repo, "rev-parse", "HEAD") == competing
    assert not (repo / ".git" / "index.lock").exists()
    assert git(repo, "diff", "--cached", "--name-only") == ""
