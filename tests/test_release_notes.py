from pathlib import Path

import pytest

from scripts.release_notes import release_notes


def test_release_notes_group_only_the_current_version():
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text().strip()
    notes = release_notes((root / "CHANGELOG.md").read_text(), version)
    assert notes.startswith(f"## {version} — ")
    assert "#57" in notes and "#58" in notes and "#59" in notes
    assert "## 0.1.10" not in notes
    assert "## Unreleased" not in notes


def test_missing_release_changelog_fails_instead_of_publishing_old_notes():
    with pytest.raises(ValueError, match="No changelog section"):
        release_notes("# Changelog\n\n## 0.1.10 — old\nOld notes\n", "0.1.11")
