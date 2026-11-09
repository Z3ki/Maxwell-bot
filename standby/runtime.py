"""Minimal emergency text responder: no plugins, commands, APIs, or file tools.

Do not import bot_common/config or primary modules here: that would load the
primary plugin/media/background stack and defeat the isolation boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

from standby.policy import (
    PolicyDenied,
    activation_record,
    deployment_mode,
    parse_json,
    provider_payload,
    read_credential,
    response_text,
    text_payload,
    validate_provider,
)

from standby.sync import chat_allowed, snowflake, validate_snapshot

LIMITATION = (
    "I'm on the limited standby: text chat only; no tools, files or attachments."
)


class Admission:
    """Bounded emergency allowance, deliberately smaller than primary's quota."""

    def __init__(self):
        self.users: OrderedDict[int, list[float]] = OrderedDict()
        self.active = 0

    def enter(self, user_id: int, now: float) -> bool:
        if self.active >= 2:
            return False
        # Never evict active-window quota records: deny new identities when full.
        for key in list(self.users):
            if not self.users[key] or self.users[key][-1] <= now - 18000:
                del self.users[key]
        if user_id not in self.users and len(self.users) >= 1024:
            return False
        rows = [stamp for stamp in self.users.get(user_id, []) if stamp > now - 18000]
        if len(rows) >= 5:
            return False
        rows.append(now)
        self.users[user_id] = rows
        self.active += 1
        return True

    def leave(self) -> None:
        self.active -= 1


async def read_bounded(content, limit: int = 1024 * 1024) -> bytes:
    """Read through EOF, not merely a currently available network chunk."""
    output = bytearray()
    while True:
        chunk = await content.read(min(65536, limit + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            raise PolicyDenied("Provider output too large")


async def run() -> None:
    if deployment_mode() != "standby":
        raise PolicyDenied("Use the explicit standby deployment mode")
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    activation_record(credentials / "activation")
    snapshot = validate_snapshot(
        parse_json(read_credential(credentials / "chat-config"))
    )
    controls = snapshot["controls"]
    token = read_credential(credentials / "discord-token").strip()
    provider = validate_provider(
        parse_json(read_credential(credentials / "provider"))
    )
    base = provider["base_url"].rstrip("/")
    if not token:
        raise PolicyDenied("Missing operator-provisioned credential")
    # A separate, reviewed emergency audience is required, rather than silently
    # bypassing primary per-user privacy, consent, opt-out and allowance state.
    raw_channels = os.getenv("MAXWELL_STANDBY_CHANNEL_IDS", "").split(",")
    if (
        not raw_channels
        or len(raw_channels) > 10000
        or any(not item for item in raw_channels)
    ):
        raise PolicyDenied("Explicit emergency channel allowlist required")
    allowed_channels = frozenset(int(snowflake(item)) for item in raw_channels)
    # Imports are lazy so policy/check tooling needs no Discord/HTTP dependencies.
    import aiohttp
    import discord

    endpoint = base + (
        "/chat/completions" if base.endswith("/v1") else "/v1/chat/completions"
    )
    admission = Admission()
    consented: set[int] = set()
    inflight: dict[int, set[asyncio.Task]] = {}

    class TextOnlyClient(discord.Client):
        async def setup_hook(self):
            # No extensions, command tree sync, API server, caches, or workers.
            pass

        async def on_interaction(self, interaction):
            if interaction.guild_id is None:
                return
            # Do not dispatch existing registered slash commands from the primary.
            permitted = interaction.channel_id in allowed_channels and chat_allowed(
                controls,
                user_id=interaction.user.id,
                channel_id=interaction.channel_id,
                guild_id=interaction.guild_id,
            )
            if (
                permitted
                and not interaction.response.is_done()
                and admission.enter(interaction.user.id, time.monotonic())
            ):
                try:
                    await interaction.response.send_message(
                        LIMITATION
                        + " Mention me in an approved channel for a text reply.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                finally:
                    admission.leave()

        async def on_message(self, message):
            if (
                message.author.bot
                or message.guild is None
                or message.channel.id not in allowed_channels
            ):
                return
            if not chat_allowed(
                controls,
                user_id=message.author.id,
                channel_id=message.channel.id,
                guild_id=message.guild.id if message.guild else None,
            ):
                return
            if self.user not in message.mentions:
                return
            text = message.content
            command = (
                re.sub(r"<@!?\d+>", "", text).strip().lower()
                if isinstance(text, str) and len(text) <= 4000
                else ""
            )
            if command == "standby-revoke":
                changed = message.author.id in consented or bool(
                    inflight.get(message.author.id)
                )
                consented.discard(message.author.id)
                for task in tuple(inflight.get(message.author.id, ())):
                    task.cancel()
                if changed:
                    await self.send_text(
                        message.channel.id,
                        "Standby provider consent cleared; pending requests cancelled. Already transmitted data cannot be recalled.",
                    )
                return
            if not admission.enter(message.author.id, time.monotonic()):
                return
            try:
                if message.attachments or message.stickers:
                    await self.send_text(message.channel.id, LIMITATION)
                    return
                text = message.content
                if not isinstance(text, str) or len(text) > 4000:
                    await self.send_text(
                        message.channel.id,
                        "Please send at most 4000 characters of text.",
                    )
                    return
                command = re.sub(r"<@!?\d+>", "", text).strip().lower()
                if command == "standby-consent":
                    if len(consented) >= 1024 and message.author.id not in consented:
                        return
                    consented.add(message.author.id)
                    await self.send_text(
                        message.channel.id,
                        "Opted in to text-only standby via OpenRouter for this process lifetime. Mention me again to chat; standby-revoke clears consent.",
                    )
                    return
                if message.author.id not in consented:
                    await self.send_text(
                        message.channel.id,
                        "Limited emergency standby uses OpenRouter for your current text, without saved history. No text has been forwarded. To opt in, mention me with exactly standby-consent; standby-revoke clears consent.",
                    )
                    return
                current = asyncio.current_task()
                assert current is not None
                inflight.setdefault(message.author.id, set()).add(current)
                payload = provider_payload(
                    text,
                    provider["model"],
                    style=controls.get("base_personality", ""),
                    config_exported_at=snapshot["exported_at"],
                )
                async with session.post(
                    endpoint,
                    headers={"Authorization": "Bearer " + provider["api_key"]},
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise PolicyDenied("Independent provider unavailable")
                    raw = await read_bounded(response.content)
                    output = response_text(json.loads(raw))
                if message.author.id in consented:
                    await self.send_text(message.channel.id, output)
            except Exception:
                # Never expose provider response bodies, credentials or stack traces.
                await self.send_text(
                    message.channel.id,
                    "Standby chat is temporarily unavailable. Please try later.",
                )
            finally:
                tasks = inflight.get(message.author.id)
                if tasks is not None:
                    tasks.discard(asyncio.current_task())
                    if not tasks:
                        del inflight[message.author.id]
                admission.leave()

        async def send_text(self, channel_id: int, text: str, **extras):
            # No files, embeds, stickers, components, webhook or arbitrary routes.
            payload = text_payload(text, **extras)
            route = discord.http.Route(
                "POST", "/channels/{channel_id}/messages", channel_id=channel_id
            )
            await self.http.request(route, json=payload)

    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=35),
        connector=aiohttp.TCPConnector(limit=2),
        trust_env=False,
    ) as session:
        async with TextOnlyClient(intents=intents, max_messages=None) as client:
            await client.start(token, reconnect=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Offline capability inventory; never logs in",
    )
    args = parser.parse_args()
    if args.check:
        print(
            json.dumps(
                {
                    "mode": "limited_standby",
                    "connected": False,
                    "tools": [],
                    "plugins": False,
                    "files": False,
                    "api": False,
                    "automatic_failover": False,
                    "requires_manual_fence": True,
                    "audience": "explicit emergency channel allowlist",
                }
            )
        )
        return
    asyncio.run(run())


if __name__ == "__main__":
    main()
