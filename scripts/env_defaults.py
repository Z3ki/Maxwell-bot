#!/usr/bin/env python3
"""Read wizard defaults without sourcing .env as executable shell code.

Uses only the standard library: the installer runs before Python dependencies
exist. Supports literal dotenv assignments, quoted values, comments and export.
Interpolation is deliberately left to the runtime's python-dotenv loader.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
    r"(?:'((?:\\.|[^'\\])*)'|\"((?:\\.|[^\"\\])*)\"|([^\r\n]*))",
    re.MULTILINE,
)
_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def read_defaults(path: Path, keys: set[str]) -> dict[str, str]:
    values = {}
    for match in _ASSIGNMENT.finditer(path.read_text(encoding="utf-8")):
        key, single, double, bare = match.groups()
        if key not in keys:
            continue
        if single is not None:
            value = re.sub(r"\\([\\'])", lambda m: m[1], single)
        elif double is not None:
            value = re.sub(
                r"\\([\\\"\'abfnrtv])", lambda m: _ESCAPES.get(m[1], m[1]), double
            )
        else:
            value = re.sub(r"\s+#.*", "", bare).rstrip()
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"{key} must be a single-line value for the installer")
        values[key] = value
    return values


if __name__ == "__main__":
    for name, value in read_defaults(Path(sys.argv[1]), set(sys.argv[2:])).items():
        sys.stdout.buffer.write(f"{name}\0{value}\0".encode())
