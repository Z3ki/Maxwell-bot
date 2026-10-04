"""Stable deduplication keys that never merge facts across access boundaries."""

import hashlib
import json
import sqlite3
from collections.abc import Callable

from .scope import _decode_metadata


def fact_content_hash(
    kind: str,
    scope: str,
    normalized_content: str,
    metadata,
    *,
    channel_id: str = "",
    guild_id: str = "",
    author_id: str = "",
) -> str:
    """Bind fact identity to the provenance used by the visibility policy.

    Text alone is insufficient: two users can state the same private fact,
    and one user can independently confirm a fact in a DM and a server. A
    duplicate must neither erase another source nor change its visibility.
    """
    meta = metadata if isinstance(metadata, dict) else _decode_metadata(metadata)
    identity = [
        "scoped-fact-v1",
        str(kind),
        str(scope),
        str(meta.get("source_user_id") or author_id or ""),
        str(meta.get("source_channel_id") or channel_id or ""),
        str(meta.get("source_guild_id") or guild_id or ""),
        meta.get("source_is_dm") is True,
        meta.get("source_channel_public") is True,
        str(meta.get("visibility") or "private").strip().lower(),
        str(meta.get("source_kind") or ""),
        meta.get("public_approved") is True,
        normalized_content,
    ]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def web_result_hash(scope: str, guild_id: str, channel_id: str, url: str) -> str:
    """Deduplicate URLs within the storage guild/channel, including DM rooms."""
    encoded = json.dumps(
        ["scoped-web-v1", scope, guild_id, channel_id, url],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


SCOPED_HASH_MIGRATION = "scoped_content_identity_v1"


def migrate_scoped_hashes(
    db: sqlite3.Connection, normalize_content: Callable[[str], str]
) -> None:
    """Upgrade keys once; repair only missing legacy hashes on later opens.

    The marker commits together with the index and row changes. A temporary
    composite index keeps collision checks bounded even for a large channel.
    Existing duplicate facts keep their IDs with stable secondary digests.
    """
    db.execute(
        "CREATE TABLE IF NOT EXISTS memory_migrations ("
        "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = db.execute(
        "SELECT 1 FROM memory_migrations WHERE name=?", (SCOPED_HASH_MIGRATION,)
    ).fetchone()
    missing_where = (
        "kind IN ('ltm', 'shared_context', 'entity', 'web_result') "
        "AND (content_hash='' OR content_hash IS NULL)"
    )
    if (
        applied
        and not db.execute(
            "SELECT 1 FROM vectors WHERE " + missing_where + " LIMIT 1"
        ).fetchone()
    ):
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        # Another process may have completed the upgrade while this connection
        # waited for the write lock. Read the marker again inside the transaction.
        applied = db.execute(
            "SELECT 1 FROM memory_migrations WHERE name=?", (SCOPED_HASH_MIGRATION,)
        ).fetchone()
        if not applied:
            db.execute("DROP INDEX IF EXISTS idx_unique_content")
            db.execute("DROP INDEX IF EXISTS idx_unique_fact_content")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_migration_hash "
            "ON vectors(kind, channel_id, content_hash)"
        )
        where = (
            missing_where
            if applied
            else ("kind IN ('ltm', 'shared_context', 'entity', 'web_result')")
        )
        rows = db.execute(
            "SELECT id, kind, channel_id, guild_id, author_id, content, scope, "
            "metadata, content_hash FROM vectors WHERE "
            + where
            + " ORDER BY created_at, id"
        )
        for row in rows:
            metadata = _decode_metadata(row["metadata"])
            if row["kind"] == "web_result":
                url = str(metadata.get("url") or "").strip()
                if url:
                    digest = web_result_hash(
                        row["scope"], row["guild_id"], row["channel_id"], url
                    )
                else:
                    # Malformed historical rows still need a completed key;
                    # leaving an empty hash would rebuild the repair index on
                    # every subsequent startup. Visibility remains unchanged.
                    digest = fact_content_hash(
                        row["kind"],
                        row["scope"],
                        normalize_content(row["content"]),
                        metadata,
                        channel_id=row["channel_id"],
                        guild_id=row["guild_id"],
                        author_id=row["author_id"],
                    )
            else:
                digest = fact_content_hash(
                    row["kind"],
                    row["scope"],
                    normalize_content(row["content"]),
                    metadata,
                    channel_id=row["channel_id"],
                    guild_id=row["guild_id"],
                    author_id=row["author_id"],
                )
            duplicate = db.execute(
                "SELECT 1 FROM vectors WHERE id!=? AND kind=? AND channel_id=? "
                "AND content_hash=? LIMIT 1",
                (row["id"], row["kind"], row["channel_id"], digest),
            ).fetchone()
            if duplicate:
                digest = hashlib.sha256(
                    f"retained-duplicate:{row['id']}\0{digest}".encode("utf-8")
                ).hexdigest()
            if row["content_hash"] != digest:
                db.execute(
                    "UPDATE vectors SET content_hash=? WHERE id=?",
                    (digest, row["id"]),
                )
        if not applied:
            # Preserve legacy non-fact duplicates if they prevent the index
            # being restored. No migration is allowed to discard stored data.
            try:
                db.execute(
                    "CREATE UNIQUE INDEX idx_unique_fact_content "
                    "ON vectors(kind, channel_id, content_hash) "
                    "WHERE content_hash != '' AND kind NOT IN ('message','bot_output')"
                )
            except sqlite3.IntegrityError:
                pass
            db.execute(
                "INSERT INTO memory_migrations (name) VALUES (?)",
                (SCOPED_HASH_MIGRATION,),
            )
        db.execute("DROP INDEX idx_memory_migration_hash")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
