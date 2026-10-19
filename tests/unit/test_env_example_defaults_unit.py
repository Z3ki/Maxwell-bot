import re
from pathlib import Path


def test_notice_version_example_matches_current_default():
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    match = re.search(r"^MAXWELL_NOTICE_VERSION=(\S+)", text, re.M)
    assert match, "MAXWELL_NOTICE_VERSION missing from .env.example"
    assert match.group(1) == "processing-notice-3"
