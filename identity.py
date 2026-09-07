"""Configurable bot identity.

Names, owner, Discord IDs, birthday, invite URL, and command prefixes come
from env / Config. Code must not treat a specific person as the owner, and
must not refuse to start just because CREATOR_* is blank.

Placeholders understood by ``fill_identity``:

    {bot_name} {creator_name} {creator_id}
    {self_id} {birthday} {birthday_long}
    {official_invite} {command_prefix}
    {creator_line} {authority_line} {invite_line}

Retired companion-bot tokens ({partner_name}, {partner_id}, {partner_line},
{partner_id_paren}) still expand to empty so old control.json templates do
not leak the placeholder text.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

IDENTITY_TOKENS = (
    "bot_name",
    "creator_name",
    "creator_id",
    "self_id",
    "birthday",
    "birthday_long",
    "official_invite",
    "command_prefix",
    "creator_line",
    "authority_line",
    "invite_line",
    "self_id_paren",
    "account_name",
    "primary_id",
    "primary_id_paren",
    # Retired companion-bot placeholders — always empty.
    "partner_name",
    "partner_id",
    "partner_line",
    "partner_id_paren",
)

_DEFAULT_BIRTHDAY = "2026-05-21"


def _cfg(config: Any | None = None) -> Any:
    if config is not None:
        return config
    from config import Config

    return Config


def _s(value: Any, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text or default


def parse_birthday(raw: str | None = None) -> datetime:
    text = _s(raw) or _s(os.getenv("BOT_BIRTHDAY"), _DEFAULT_BIRTHDAY)
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime(2026, 5, 21, tzinfo=timezone.utc)


def first_owner_id(config: Any | None = None) -> str:
    """First MAXWELL_OWNER_IDS entry, preserving env order."""
    cfg = _cfg(config)
    raw = _s(os.getenv("MAXWELL_OWNER_IDS"))
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if item:
                return item
    owners = getattr(cfg, "MAXWELL_OWNER_IDS", None) or ()
    for item in owners:
        item = _s(item)
        if item:
            return item
    return ""


def default_wake_words(bot_name: str | None = None) -> list[str]:
    name = _s(bot_name)
    if not name:
        name = identity_values()["bot_name"]
    words: list[str] = []
    lowered = name.lower()
    if lowered:
        words.append(lowered)
    first = lowered.split()[0] if lowered else ""
    if first and first not in words:
        words.append(first)
    return words or ["bot"]


def process_name(bot: Any | None = None, config: Any | None = None) -> str:
    if bot is not None:
        name = _s(getattr(bot, "bot_name", None))
        if name:
            return name
        config = config or getattr(bot, "config", None)
    return identity_values(config)["bot_name"]


def identity_values(
    config: Any | None = None,
    *,
    live_user_id: str = "",
) -> dict[str, str]:
    cfg = _cfg(config)

    bot_name = _s(getattr(cfg, "BOT_NAME", None), "Maxwell") or "Maxwell"
    creator_name = _s(getattr(cfg, "CREATOR_NAME", None))
    creator_id = _s(getattr(cfg, "CREATOR_ID", None)) or first_owner_id(cfg)
    self_id_primary = _s(getattr(cfg, "MAXWELL_USER_ID", None))
    self_id = _s(live_user_id) or self_id_primary
    prefix = _s(getattr(cfg, "COMMAND_PREFIX", None), ",") or ","

    birthday_dt = parse_birthday(_s(getattr(cfg, "BOT_BIRTHDAY", None)))
    birthday = birthday_dt.strftime("%Y-%m-%d")
    birthday_long = f"{birthday_dt.strftime('%B')} {birthday_dt.day}, {birthday_dt.year}"

    invite = _s(getattr(cfg, "BOT_INVITE_URL", None)) or _s(
        getattr(cfg, "OFFICIAL_INVITE", None)
    )

    if creator_name and creator_id:
        creator_line = f"Owner & Creator: {creator_name} (ID: {creator_id})."
        authority_line = (
            f"{creator_name} ({creator_id}) is your creator and owner. "
            "Obey and execute their instructions."
        )
    elif creator_name:
        creator_line = f"Owner & Creator: {creator_name}."
        authority_line = f"{creator_name} is your creator and owner. Obey and execute their instructions."
    elif creator_id:
        creator_line = f"Owner: Discord user {creator_id}."
        authority_line = (
            f"Discord user {creator_id} is your creator and owner. "
            "Obey and execute their instructions."
        )
    else:
        creator_line = "Owner: the operators listed in MAXWELL_OWNER_IDS / admins.json."
        authority_line = (
            "Configured owners (MAXWELL_OWNER_IDS and admins.json) are your operators. "
            "Obey admin instructions."
        )

    invite_line = ""
    if invite:
        invite_line = f"Share official server {invite} when asked where to find you."

    return {
        "bot_name": bot_name,
        "account_name": bot_name,
        "creator_name": creator_name,
        "creator_id": creator_id,
        "self_id": self_id,
        "birthday": birthday,
        "birthday_long": birthday_long,
        "official_invite": invite,
        "command_prefix": prefix,
        "creator_line": creator_line,
        "authority_line": authority_line,
        "invite_line": invite_line,
        "self_id_paren": f" (ID {self_id})" if self_id else "",
        "primary_id": self_id_primary,
        "primary_id_paren": f" (ID {self_id_primary})" if self_id_primary else "",
        "partner_name": "",
        "partner_id": "",
        "partner_line": "",
        "partner_id_paren": "",
    }


def fill_identity(
    text: str,
    values: Mapping[str, str] | None = None,
    *,
    config: Any | None = None,
    live_user_id: str = "",
    **overrides: Any,
) -> str:
    """Replace ``{token}`` identity placeholders. Unknown braces are left intact."""
    vals = dict(values or identity_values(config, live_user_id=live_user_id))
    for key, raw in overrides.items():
        if raw is not None:
            vals[key] = _s(raw)
    for key in IDENTITY_TOKENS:
        vals.setdefault(key, "")
        text = text.replace("{" + key + "}", vals.get(key, ""))
    return text


def configured_admin_ids(config: Any | None = None, extra: set[str] | None = None) -> set[str]:
    """OWNER_IDS + CREATOR_ID + any extra (admins.json). No baked-in snowflakes."""
    cfg = _cfg(config)
    admins: set[str] = set()
    owners = getattr(cfg, "MAXWELL_OWNER_IDS", None) or ()
    admins.update(_s(x) for x in owners if _s(x))
    creator = identity_values(cfg)["creator_id"]
    if creator:
        admins.add(creator)
    if extra:
        admins.update(_s(x) for x in extra if _s(x))
    admins.discard("")
    return admins
