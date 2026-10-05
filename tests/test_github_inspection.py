"""Exercise the exact standalone inspector against harmless hostile fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from plugins.github_projects.inspection import INSPECTION_SCRIPT


def git(worktree: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(worktree.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "LC_ALL": "C.UTF-8",
        },
    )
    return result.stdout


def repository(tmp_path: Path, *, object_format: str = "sha1") -> Path:
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    git(
        worktree,
        "init",
        "-q",
        "--initial-branch=main",
        f"--object-format={object_format}",
    )
    git(worktree, "config", "user.name", "Inspection fixture")
    git(worktree, "config", "user.email", "fixture@example.invalid")
    (worktree / "app.txt").write_text("original\n", encoding="utf-8")
    git(worktree, "add", "app.txt")
    git(worktree, "commit", "-qm", "initial")
    return worktree


def inspect(
    worktree: Path,
    operation: str = "status",
    ref: str = "",
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", INSPECTION_SCRIPT, operation, str(worktree), ref],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def metadata_snapshot(worktree: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(worktree)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (worktree / ".git").rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_inspection_preserves_normal_status_diff_and_head(tmp_path, object_format):
    worktree = repository(tmp_path, object_format=object_format)
    head = git(worktree, "rev-parse", "HEAD").strip()
    (worktree / "app.txt").write_text("updated\n", encoding="utf-8")
    before = metadata_snapshot(worktree)

    status = inspect(worktree)
    assert status.returncode == 0, status.stderr
    assert "## main" in status.stdout
    assert " M app.txt" in status.stdout
    diff = inspect(worktree, "diff", "HEAD")
    assert diff.returncode == 0, diff.stderr
    assert "-original" in diff.stdout
    assert "+updated" in diff.stdout
    summary = inspect(worktree, "checkout_head")
    assert summary.returncode == 0, summary.stderr
    assert "branch=main" in summary.stdout
    assert f"head={head}" in summary.stdout
    assert metadata_snapshot(worktree) == before


def test_verify_reports_scan_and_whitespace_errors_without_writing(tmp_path):
    worktree = repository(tmp_path)
    (worktree / "app.txt").write_text("TODO: finish this  \n", encoding="utf-8")
    before = metadata_snapshot(worktree)
    result = inspect(worktree, "verify")
    assert result.returncode != 0
    assert "[diff-check]" in result.stdout
    assert "trailing whitespace" in result.stdout
    assert "[placeholder scan]" in result.stdout
    assert "TODO: finish this" in result.stdout
    assert metadata_snapshot(worktree) == before


def test_verify_succeeds_when_scan_has_no_matches(tmp_path):
    worktree = repository(tmp_path)
    result = inspect(worktree, "verify")
    assert result.returncode == 0, result.stderr
    assert "[placeholder scan]" in result.stdout


@pytest.mark.parametrize("operation", ["status", "diff", "verify", "checkout_head"])
def test_repository_execution_hooks_are_never_loaded(tmp_path, operation):
    worktree = repository(tmp_path)
    marker = tmp_path / "executed-marker"
    executable = tmp_path / "marker-helper"
    executable.write_text(
        f"#!/bin/sh\nprintf ran > '{marker}'\ncat\n", encoding="utf-8"
    )
    executable.chmod(0o700)
    (worktree / ".gitattributes").write_text(
        "app.txt filter=marker diff=marker\n", encoding="utf-8"
    )
    for key in (
        "core.fsmonitor",
        "filter.marker.clean",
        "filter.marker.smudge",
        "filter.marker.process",
        "diff.marker.textconv",
        "diff.marker.command",
        "diff.external",
        "core.pager",
    ):
        git(worktree, "config", key, str(executable))
    git(worktree, "config", "filter.marker.required", "true")
    hooks = worktree / ".git" / "hooks"
    for name in ("post-index-change", "fsmonitor-watchman", "post-checkout"):
        (hooks / name).write_text(executable.read_text(), encoding="utf-8")
        (hooks / name).chmod(0o700)
    (worktree / "app.txt").write_text("updated\n", encoding="utf-8")
    before = metadata_snapshot(worktree)

    result = inspect(worktree, operation)
    assert result.returncode == 0, result.stderr + result.stdout
    assert not marker.exists()
    assert metadata_snapshot(worktree) == before


def test_git_config_includes_and_info_attributes_are_not_loaded(tmp_path):
    worktree = repository(tmp_path)
    marker = tmp_path / "included-helper-ran"
    helper = tmp_path / "included-helper"
    helper.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\ncat\n", encoding="utf-8")
    helper.chmod(0o700)
    include = tmp_path / "included-config"
    include.write_text(f'[filter "included"]\nclean = {helper}\n', encoding="utf-8")
    git(worktree, "config", "include.path", str(include))
    (worktree / ".git" / "info" / "attributes").write_text(
        "app.txt filter=included\n", encoding="utf-8"
    )
    (worktree / "app.txt").write_text("changed\n", encoding="utf-8")

    result = inspect(worktree, "diff")
    assert result.returncode == 0, result.stderr
    assert "+changed" in result.stdout
    assert not marker.exists()


def test_hostile_project_modules_and_environment_cannot_run(tmp_path):
    worktree = repository(tmp_path)
    marker = tmp_path / "python-module-ran"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    for name in ("subprocess", "selectors", "pathlib", "tempfile", "sitecustomize"):
        (worktree / f"{name}.py").write_text(payload, encoding="utf-8")
    config = tmp_path / "hostile-global-config"
    config.write_text(f"[core]\nfsmonitor = touch {marker}\n", encoding="utf-8")
    rg_config = tmp_path / "ripgrep-config"
    rg_config.write_text(f"--pre=touch {marker}\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=str(worktree),
        GIT_CONFIG_GLOBAL=str(config),
        GIT_CONFIG_SYSTEM=str(config),
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="core.fsmonitor",
        GIT_CONFIG_VALUE_0=f"touch {marker}",
        RIPGREP_CONFIG_PATH=str(rg_config),
    )

    result = inspect(worktree, "verify", env=environment)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    "ref",
    ["--ext-diff", "--textconv", "--all", "--recurse-submodules", "../HEAD", "a@{0}"],
)
def test_script_rejects_option_and_path_like_refs(tmp_path, ref):
    worktree = repository(tmp_path)
    result = inspect(worktree, "diff", ref)
    assert result.returncode == 2
    assert "invalid git ref" in result.stderr


@pytest.mark.parametrize("layout", ["git-file", "git-symlink", "commondir"])
def test_nonstandard_git_layouts_fail_clearly(tmp_path, layout):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    if layout == "git-file":
        (worktree / ".git").write_text("gitdir: somewhere\n", encoding="utf-8")
    elif layout == "git-symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (worktree / ".git").symlink_to(elsewhere, target_is_directory=True)
    else:
        (worktree / ".git").mkdir()
        (worktree / ".git" / "commondir").write_text("elsewhere\n", encoding="utf-8")
    result = inspect(worktree)
    assert result.returncode == 2
    assert "unsupported" in result.stderr


def test_metadata_snapshot_is_bounded(tmp_path):
    worktree = repository(tmp_path)
    with (worktree / ".git" / "index").open("wb") as index:
        index.truncate(64 * 1024 * 1024 + 1)
    result = inspect(worktree)
    assert result.returncode == 2
    assert "metadata exceeds 64 MiB" in result.stderr


@pytest.mark.parametrize("metadata", ["HEAD", "index", "refs/heads/main"])
def test_symbolic_metadata_is_refused(tmp_path, metadata):
    worktree = repository(tmp_path)
    source = worktree / ".git" / metadata
    actual = tmp_path / "external-metadata"
    actual.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(actual)
    result = inspect(worktree)
    assert result.returncode == 2


def test_large_diff_output_is_bounded_and_process_is_drained(tmp_path):
    worktree = repository(tmp_path)
    (worktree / "app.txt").write_text("new data line\n" * 50000, encoding="utf-8")
    result = inspect(worktree, "diff")
    assert result.returncode == 0, result.stderr
    assert "[inspection output truncated]" in result.stdout
    assert len(result.stdout) < 120100


def test_packed_refs_and_split_index_are_preserved(tmp_path):
    worktree = repository(tmp_path)
    git(worktree, "tag", "release")
    git(worktree, "pack-refs", "--all")
    git(worktree, "update-index", "--split-index")
    (worktree / "app.txt").write_text("changed\n", encoding="utf-8")
    before = metadata_snapshot(worktree)
    result = inspect(worktree, "diff", "release")
    assert result.returncode == 0, result.stderr
    assert "+changed" in result.stdout
    assert metadata_snapshot(worktree) == before


def test_unborn_branch_can_be_inspected(tmp_path):
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    git(worktree, "init", "-q", "--initial-branch=main")
    (worktree / "new.txt").write_text("new\n", encoding="utf-8")
    result = inspect(worktree)
    assert result.returncode == 0, result.stderr
    assert "No commits yet on main" in result.stdout
    assert "?? new.txt" in result.stdout


def test_fifo_metadata_does_not_block_inspection(tmp_path):
    worktree = repository(tmp_path)
    index = worktree / ".git" / "index"
    index.unlink()
    os.mkfifo(index)
    result = inspect(worktree)
    assert result.returncode == 2
    assert "non-regular Git metadata" in result.stderr


def test_too_many_ref_directories_are_refused(tmp_path):
    worktree = repository(tmp_path)
    refs = worktree / ".git" / "refs"
    for count in range(10001):
        (refs / f"empty-{count}").mkdir()
    result = inspect(worktree)
    assert result.returncode == 2
    assert "metadata exceeds 10000 entries" in result.stderr


def test_nonstandard_ref_storage_is_refused(tmp_path):
    worktree = repository(tmp_path)
    git(worktree, "config", "extensions.refStorage", "reftable")
    result = inspect(worktree)
    assert result.returncode == 2
    assert "unsupported Git reference storage" in result.stderr


def test_script_does_not_offer_custom_execution(tmp_path):
    worktree = repository(tmp_path)
    result = inspect(worktree, "run")
    assert result.returncode == 2
    assert "usage:" in result.stderr
