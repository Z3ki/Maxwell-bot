"""Exercise the actual publication shell against GitHub CLI response fixtures."""

import json
import os
from pathlib import Path
import re
import subprocess

import pytest


def _workflow_step_script(workflow, name):
    step = workflow.split(f"      - name: {name}\n", 1)[1]
    block = step.split("        run: |\n", 1)[1]
    return re.split(r"\n(?=\S| {1,9}\S)", block, maxsplit=1)[0].replace("\n          ", "\n")[10:]


@pytest.mark.parametrize("tag_state", ["missing", "matching", "mismatched", "annotated-matching", "annotated-mismatched", "unreachable"])
def test_release_guard_prevents_publication_from_a_different_commit(tmp_path, tag_state):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/release.yml").read_text()
    script = _workflow_step_script(workflow, "Check release tag before publication")
    # Run against real git refs, including peeled annotated tags, rather than
    # faking tag resolution and accidentally accepting a tag object's SHA.
    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, env=git_env, check=True, capture_output=True, text=True).stdout.strip()

    git("init")
    git("commit", "--allow-empty", "-m", "first")
    first = git("rev-parse", "HEAD")
    if tag_state.startswith("annotated"):
        git("tag", "-a", "v0.1.11", "-m", "release")
    elif tag_state in {"matching", "mismatched"}:
        git("tag", "v0.1.11")
    if "mismatched" in tag_state:
        git("commit", "--allow-empty", "-m", "later workflow edit")
    sha = git("rev-parse", "HEAD")
    git("remote", "add", "origin", str(repo if tag_state != "unreachable" else tmp_path / "missing"))
    output = tmp_path / "outputs"
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=repo, capture_output=True, text=True,
                            env={**git_env, "RELEASE_TAG": "v0.1.11", "GITHUB_SHA": sha,
                                 "GITHUB_OUTPUT": str(output), "GITHUB_REF_TYPE": "branch"})
    if tag_state == "unreachable":
        assert result.returncode != 0
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text().strip() == ("publish=false" if "mismatched" in tag_state else "publish=true")
        if "mismatched" in tag_state:
            assert git("rev-parse", "v0.1.11^{commit}") == first
            assert "Skipping" in result.stdout
    # The publishing job must depend on the guard's result, including every
    # Docker build/push and release upload; concurrent SHAs share one lock.
    assert "needs: prepare" in workflow
    assert "if: needs.prepare.outputs.publish == 'true'" in workflow
    assert re.search(r"group: release-\$\{\{ github.repository \}\}", workflow)


@pytest.mark.parametrize("tag_state", ["missing", "matching", "mismatched"])
def test_release_publication_handles_missing_and_existing_tags(tmp_path, tag_state):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/release.yml").read_text()
    script = "\n".join(line[10:] for line in workflow.rsplit("        run: |\n", 1)[1].splitlines())
    cli = tmp_path / "gh"
    cli.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['GH_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'api':
    endpoint = next(arg for arg in args if arg.startswith('repos/'))
    if '/git/ref/tags/' in endpoint:
        if os.environ['TAG_STATE'] == 'missing':
            # gh api emits an error body on stdout even when its exit code
            # indicates failure. This must never be mistaken for a tag SHA.
            print('{"message":"Not Found","status":"404"}')
            print('HTTP 404', file=sys.stderr)
            sys.exit(1)
        print('{"object":{"type":"tag","sha":"annotated-tag-object"}}')
    elif '/commits/refs/tags/' in endpoint:
        print(os.environ['GITHUB_SHA'] if os.environ['TAG_STATE'] == 'matching' else 'wrong-commit')
    else:
        print('{"ref":"refs/tags/v0.1.11"}')
elif args[:2] == ['release', 'view']:
    sys.exit(1)
elif args[:2] == ['release', 'create']:
    print('Release created')
else:
    raise AssertionError(args)
''')
    cli.chmod(0o755)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "RELEASE_NOTES.md").write_text("Current release notes")
    calls_file = tmp_path / "calls.jsonl"
    result = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, text=True,
                            capture_output=True, env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                                                       "GH_CALLS": str(calls_file), "TAG_STATE": tag_state,
                                                       "GITHUB_REPOSITORY": "acme/app", "RELEASE_TAG": "v0.1.11", "GITHUB_SHA": "a" * 40})
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    created_tag = any("POST" in call for call in calls)
    created_release = any(call[:2] == ["release", "create"] for call in calls)
    if tag_state == "mismatched":
        assert result.returncode != 0
        assert not created_tag and not created_release
    else:
        assert result.returncode == 0, result.stderr
        assert created_tag == (tag_state == "missing")
        assert created_release
