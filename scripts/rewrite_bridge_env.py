#!/usr/bin/env python3
"""Adapt local service addresses for Docker Desktop without touching secrets."""

import re
import sys
from pathlib import Path

_URL_KEYS = {
    "OLLAMA_BASE_URL",
    "OLLAMA_FALLBACK_BASE_URL",
    "OLLAMA_VISION_BASE_URL",
    "AUTONOMY_BASE_URL",
    "AUX_BASE_URL",
    "MAXWELL_EMBED_BASE_URL",
    "EMBED_BASE_URL",
    "NVIDIA_IMAGE_URL",
    "GPT_IMAGE_URL",
    "GEMINI_IMAGE_BASE_URL",
    "MAXWELL_USAGE_URL",
    "X_API_BASE_URL",
    "X_RSS_BASE_URL",
}
_HOST_KEYS = {"MAXWELL_SMTP_HOST", "MAXWELL_IMAP_HOST"}
_ASSIGNMENT = re.compile(
    r"^([ \t]*(?:export[ \t]+)?)(\w+)([ \t]*=[ \t]*)(.*)$", re.MULTILINE
)
_LOCAL_URL = re.compile(
    r"^(?P<prefix>[\"']?https?://(?:[^/@\s]*@)?)(?:localhost|127\.0\.0\.1)(?=[:/\"'\s?#]|$)",
    re.IGNORECASE,
)
_LOCAL_HOST = re.compile(
    r"^([\"']?)(?:localhost|127\.0\.0\.1)(?=[\"'\s]|$)", re.IGNORECASE
)


def rewrite_bridge_env(path: Path) -> None:
    def replace(match: re.Match) -> str:
        prefix, key, separator, value = match.groups()
        if key in _URL_KEYS:
            value = _LOCAL_URL.sub(r"\g<prefix>host.docker.internal", value, count=1)
        elif key in _HOST_KEYS:
            value = _LOCAL_HOST.sub(r"\1host.docker.internal", value, count=1)
        elif key == "MAXWELL_API_HOST":
            value = _LOCAL_HOST.sub(r"\g<1>0.0.0.0", value, count=1)
        return prefix + key + separator + value

    text = path.read_text(encoding="utf-8")
    updated = _ASSIGNMENT.sub(replace, text)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    rewrite_bridge_env(Path(sys.argv[1]))
