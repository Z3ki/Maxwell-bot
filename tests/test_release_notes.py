from pathlib import Path
import re

import pytest

from scripts.release_notes import release_notes


def test_release_notes_group_only_the_current_version():
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text().strip()
    changelog = (root / "CHANGELOG.md").read_text()
    notes = release_notes(changelog, version)
    assert notes.startswith(f"## {version} — ")
    for other in re.findall(r"(?m)^## (\d+\.\d+\.\d+) — ", changelog):
        if other != version:
            assert f"## {other} — " not in notes
    assert "## Unreleased" not in notes


def test_release_notes_do_not_mix_unreleased_or_previous_changes():
    changelog = ("# Changelog\n\n## Unreleased\nFuture changes\n\n"
                 "## 1.2.0 — today\nCurrent changes (#12)\n\n"
                 "## 1.1.0 — before\nPrevious changes (#11)\n")
    assert release_notes(changelog, "1.2.0") == "## 1.2.0 — today\nCurrent changes (#12)\n"


def test_missing_release_changelog_fails_instead_of_publishing_old_notes():
    with pytest.raises(ValueError, match="No changelog section"):
        release_notes("# Changelog\n\n## 0.1.10 — old\nOld notes\n", "0.1.11")
