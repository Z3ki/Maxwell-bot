"""Extract exactly one version's release notes from the changelog."""

from pathlib import Path
import re


def release_notes(changelog: str, version: str) -> str:
    sections = re.split(r"(?m)^## ", changelog)
    for section in sections[1:]:
        heading, _, body = section.partition("\n")
        if heading.split(" — ", 1)[0] == version:
            return f"## {heading}\n{body}".strip() + "\n"
    raise ValueError(f"No changelog section for {version}")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text().strip()
    print(release_notes((root / "CHANGELOG.md").read_text(), version), end="")
