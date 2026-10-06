"""Exercise the actual publication shell against GitHub CLI response fixtures."""

import json
import os
from pathlib import Path
import subprocess

import pytest


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
