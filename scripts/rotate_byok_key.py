#!/usr/bin/env python3
"""Rotate Maxwell's BYOK AES-GCM master key during a maintenance window."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.maxwell_extras.byok import CredentialVault


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        default=os.path.join(os.environ.get("DATA_DIR", "data"), "byok.sqlite3"),
        help="BYOK SQLite file (default: DATA_DIR/byok.sqlite3)",
    )
    args = parser.parse_args()
    vault = CredentialVault(Path(args.database))
    if not vault.enabled:
        parser.error("set the current MAXWELL_BYOK_ENCRYPTION_KEY before rotating")
    new_key = getpass.getpass("New 64-character hex key: ").strip()
    confirm = getpass.getpass("Confirm new key: ").strip()
    if new_key != confirm or not re.fullmatch(r"[0-9a-fA-F]{64}", new_key):
        parser.error("new keys must match and contain exactly 64 hex characters")
    vault.rotate_master_key(new_key)
    print(
        "Rotation complete. Update MAXWELL_BYOK_ENCRYPTION_KEY to the new value "
        "before restarting Maxwell."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
