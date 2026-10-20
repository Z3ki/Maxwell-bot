"""Dashboard panels for Premium features: plan, files (/storage), schedules.

These run in the API process against the same sqlite files and directory
trees the bot uses. The stores do their own locking; nothing here caches
state across requests that the bot could change underneath it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from api.user_content import open_user_storage, plan_lookup_for

UPLOAD_LIMIT = 25 * 1024 * 1024
TEXT_LIMIT = 512 * 1024
_SIGNED_TTL = 3600


def _control(root: Path) -> dict:
    from plugins.maxwell_extras.dashboard_settings import read_json

    return read_json(root / "bot_control.json", dict, {})


def _plugin_on(root: Path, plugin_id: str) -> bool:
    from plugins.maxwell_extras.dashboard_settings import read_json

    state = read_json(root / "plugins.json", dict, {}).get("plugins", {})
    return bool(state.get(plugin_id, {}).get("enabled_globally", True))


class Panels:
    def __init__(self, dashboard):
        self.dashboard = dashboard
        self._storage = None
        self._schedules = None
        self._root: Path | None = None

    # ---- plumbing -------------------------------------------------------

    def root(self) -> Path:
        root = Path(self.dashboard.data_dir())
        if root != self._root:
            self._root, self._storage, self._schedules = root, None, None
        return root

    def storage(self):
        root = self.root()
        if self._storage is None:
            self._storage = open_user_storage(root)
        return self._storage

    def schedules(self):
        from plugins.schedules.store import ScheduleStore

        root = self.root()
        if self._schedules is None:
            self._schedules = ScheduleStore(root / "plugins" / "schedules" / "schedules.sqlite3")
        return self._schedules

    def premium(self, uid: str) -> bool:
        try:
            return plan_lookup_for(self.root())(uid) == "premium"
        except Exception:
            return False

    @staticmethod
    def uid(session) -> str:
        return str(session["user"]["id"])

    @staticmethod
    def _bad(exc: Exception):
        return web.HTTPBadRequest(text=str(exc)[:240] or "That change was not accepted.")

    async def _json(self, request) -> dict:
        return await self.dashboard.body(request)

    # ---- plan -----------------------------------------------------------

    def _plan(self, uid: str) -> dict:
        from message_quota import (
            MessageQuota,
            enforced_message_limit,
            enforced_window_seconds,
            usage_percent,
        )
        from plugins.schedules.store import daily_run_limit, max_per_user, min_interval_minutes

        root = self.root()
        control = _control(root)
        ledger = MessageQuota(root / "message_quota.sqlite3")
        quota = ledger.status(uid, enforced_message_limit(control), enforced_window_seconds(control))
        premium = quota["plan"] == "premium"
        storage = self.storage().status(uid)
        workers = bool(control.get("premium_task_workers_enabled", True))
        enabled = bool(control.get("message_quota_enabled", False))
        unlimited = not enabled or bool(quota["exempt"])
        return {
            "plan": quota["plan"],
            # Percent only. The raw count, limit and window stay server-side so
            # retuning the allowance doesn't turn into a support thread.
            "messages": {
                "unlimited": unlimited,
                "exempt": bool(quota["exempt"]),
                "percent_used": 0 if unlimited else usage_percent(quota["used"], quota["limit"]),
                "resets_in": 0 if unlimited else quota["resets_in"],
            },
            "storage": {
                "enabled": _plugin_on(root, "user_storage"),
                "used_bytes": storage["used_bytes"],
                "quota_bytes": storage["quota_bytes"],
                "files": storage["files"],
            },
            "schedules": {
                "enabled": _plugin_on(root, "schedules") and workers,
                "max": max_per_user(),
                "min_minutes": min_interval_minutes(),
                "daily_runs": daily_run_limit(),
            },
            "features": [
                {"id": "subagents", "label": "Background subagents (delegate_task)", "on": premium and workers},
                {"id": "storage", "label": "Persistent /storage that survives sandbox wipes", "on": premium},
                {"id": "hosting", "label": "Your own public web folder at /u/<handle>/", "on": premium},
                {"id": "schedules", "label": "Scheduled wake-ups (cron or one time)", "on": premium and workers},
                {"id": "shell", "label": "Longer shell commands", "on": premium},
            ],
        }

    async def plan(self, request, session):
        return self.dashboard.response(await asyncio.to_thread(self._plan, self.uid(session)))

    # ---- files ----------------------------------------------------------

    def _listing(self, uid: str) -> dict:
        from plugins.user_storage.urls import public_root, public_url

        storage = self.storage()
        status = storage.status(uid)
        handle = status["handle"]
        entries = []
        if status["quota_bytes"] > 0 or status["files"]:
            for entry in storage.list(uid, "", limit=2000):
                row = entry.as_dict()
                row["public_url"] = public_url(handle, entry.path) if handle else ""
                entries.append(row)
        return {
            "status": status,
            "public_root": public_root(handle) if handle else "",
            "entries": entries,
            "upload_limit": UPLOAD_LIMIT,
            "enabled": _plugin_on(self.root(), "user_storage"),
        }

    async def files(self, request, session):
        from plugins.user_storage.store import StorageError

        uid = self.uid(session)
        try:
            if request.method == "DELETE":
                path = request.query.get("path", "")
                await asyncio.to_thread(self.storage().delete, uid, path)
            return self.dashboard.response(await asyncio.to_thread(self._listing, uid))
        except StorageError as exc:
            raise self._bad(exc) from None

    def _require_space(self, uid: str) -> None:
        if self.storage().quota(uid) <= 0:
            raise web.HTTPForbidden(text="Persistent storage is part of Premium on this bot.")

    async def upload(self, request, session):
        from plugins.user_storage.store import StorageError

        uid = self.uid(session)
        await asyncio.to_thread(self._require_space, uid)
        path = request.query.get("path", "")
        if request.content_length is not None and request.content_length > UPLOAD_LIMIT:
            raise web.HTTPRequestEntityTooLarge(max_size=UPLOAD_LIMIT, actual_size=request.content_length)
        chunks, size = [], 0
        # client_max_size caps request.read(), not the raw stream; this is the cap.
        async for chunk in request.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > UPLOAD_LIMIT:
                raise web.HTTPRequestEntityTooLarge(max_size=UPLOAD_LIMIT, actual_size=size)
            chunks.append(chunk)
        try:
            await asyncio.to_thread(self.storage().write, uid, path, b"".join(chunks))
            return self.dashboard.response(await asyncio.to_thread(self._listing, uid))
        except StorageError as exc:
            raise self._bad(exc) from None

    async def mkdir(self, request, session):
        from plugins.user_storage.store import StorageError

        uid = self.uid(session)
        await asyncio.to_thread(self._require_space, uid)
        body = await self._json(request)
        try:
            await asyncio.to_thread(self.storage().mkdir, uid, str(body.get("path", "")))
            return self.dashboard.response(await asyncio.to_thread(self._listing, uid))
        except StorageError as exc:
            raise self._bad(exc) from None

    async def move(self, request, session):
        from plugins.user_storage.store import StorageError

        uid = self.uid(session)
        body = await self._json(request)
        try:
            await asyncio.to_thread(
                self.storage().move, uid, str(body.get("src", "")), str(body.get("dst", ""))
            )
            return self.dashboard.response(await asyncio.to_thread(self._listing, uid))
        except StorageError as exc:
            raise self._bad(exc) from None

    async def text(self, request, session):
        """Read a small text file for the in-browser editor. JSON only, never rendered."""
        from plugins.user_storage.store import StorageError

        uid = self.uid(session)
        try:
            data = await asyncio.to_thread(
                self.storage().read, uid, request.query.get("path", ""), max_size=TEXT_LIMIT
            )
        except StorageError as exc:
            raise self._bad(exc) from None
        if b"\0" in data:
            raise web.HTTPBadRequest(text="That file is binary; download it instead.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise web.HTTPBadRequest(text="That file is not UTF-8 text; download it instead.") from None
        return self.dashboard.response({"path": request.query.get("path", ""), "text": text})

    async def link(self, request, session):
        from plugins.user_storage.store import StorageError, clean_path
        from plugins.user_storage.urls import public_url, signed_url

        uid = self.uid(session)
        body = await self._json(request)
        storage = self.storage()
        try:
            rel = clean_path(str(body.get("path", "")))
            if await asyncio.to_thread(storage.file_path, uid, rel) is None:
                raise StorageError("file not found in /storage")
            handle = await asyncio.to_thread(storage.handle_for, uid)
            premium = await asyncio.to_thread(storage.quota, uid) > 0
            url = public_url(handle, rel) if handle and premium and not body.get("download") else ""
            if not url:
                url, _ = await asyncio.to_thread(signed_url, storage, uid, rel, ttl=_SIGNED_TTL)
                if url and body.get("download"):
                    url += "?download=1"
        except StorageError as exc:
            raise self._bad(exc) from None
        if not url:
            raise web.HTTPServiceUnavailable(text="This bot has no public address for file links.")
        return self.dashboard.response({"url": url})

    async def handle(self, request, session):
        from plugins.user_storage.store import StorageError

        uid = self.uid(session)
        await asyncio.to_thread(self._require_space, uid)
        body = await self._json(request)
        try:
            await asyncio.to_thread(self.storage().set_handle, uid, str(body.get("handle", "")))
            return self.dashboard.response(await asyncio.to_thread(self._listing, uid))
        except StorageError as exc:
            raise self._bad(exc) from None

    # ---- schedules ------------------------------------------------------

    def _schedule_rows(self, uid: str) -> dict:
        from plugins.schedules.planning import summary
        from plugins.schedules.store import daily_run_limit, max_per_user, min_interval_minutes

        store = self.schedules()
        rows = []
        for row in store.list_user(uid):
            try:
                when = summary(row)
            except Exception:
                when = row["spec"]
            rows.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "spec": row["spec"],
                    "tz": row["tz"],
                    "summary": when,
                    "prompt": row["prompt"],
                    "enabled": row["enabled"],
                    "deliver": "dm" if row["dm"] else "channel",
                    "channel_id": row["channel_id"],
                    "next_run": row["next_run"] if row["enabled"] else None,
                    "last_run": row["last_run"],
                    "last_status": row["last_status"],
                    "runs": row["runs"],
                }
            )
        control = _control(self.root())
        return {
            "premium": self.premium(uid),
            "enabled": _plugin_on(self.root(), "schedules")
            and bool(control.get("premium_task_workers_enabled", True)),
            "schedules": rows,
            "history": store.history(uid, 20),
            "limits": {"max": max_per_user(), "min_minutes": min_interval_minutes(), "daily_runs": daily_run_limit()},
        }

    def _require_premium(self, uid: str) -> None:
        if not self.premium(uid):
            raise web.HTTPForbidden(text="Scheduled wake-ups are part of Premium on this bot.")

    @staticmethod
    def _clean_text(value, limit: int, label: str, *, required: bool) -> str:
        text = str(value or "").strip()
        if required and not text:
            raise ValueError(f"{label} is required.")
        if len(text) > limit:
            raise ValueError(f"{label} must be at most {limit} characters.")
        return text

    def _create(self, uid: str, body: dict) -> None:
        from plugins.schedules.cron import zone
        from plugins.schedules.planning import NAME_LIMIT, PROMPT_LIMIT, plan_at, plan_cron

        self._require_premium(uid)
        cron = str(body.get("cron") or "").strip()
        at = str(body.get("at") or "").strip()
        if bool(cron) == bool(at):
            raise ValueError("Choose a repeating cron schedule or a one-time date, not both.")
        prompt = self._clean_text(body.get("prompt"), PROMPT_LIMIT, "What Maxwell should do", required=True)
        tz_name = self._clean_text(body.get("timezone") or "UTC", 64, "Time zone", required=True)
        zone(tz_name)
        now = datetime.now(timezone.utc)
        spec, first = plan_cron(cron, tz_name, now) if cron else plan_at(at, tz_name, now)
        name = " ".join(self._clean_text(body.get("name"), NAME_LIMIT, "Name", required=False).split())
        # The web has no channel context, so web-made schedules report by DM.
        self.schedules().add(
            user_id=uid, name=name or prompt[:40], kind="cron" if cron else "once", spec=spec,
            tz=tz_name, prompt=prompt, channel_id="", guild_id="", dm=True, next_run=first,
        )

    def _update(self, uid: str, sid: str, body: dict) -> None:
        from plugins.schedules.cron import zone
        from plugins.schedules.planning import NAME_LIMIT, PROMPT_LIMIT, next_run, plan_at, plan_cron

        store = self.schedules()
        row = store.get(uid, sid)
        if row is None:
            raise web.HTTPNotFound(text="That schedule no longer exists.")
        if not body.keys() <= {"enabled", "name", "prompt", "cron", "at", "timezone"}:
            raise ValueError("Unsupported schedule setting.")
        changes: dict = {}
        now = datetime.now(timezone.utc)
        reviving = body.get("enabled") is True or any(k in body for k in ("cron", "at", "timezone", "prompt"))
        if reviving:
            self._require_premium(uid)
        tz_name = self._clean_text(body.get("timezone") or row["tz"], 64, "Time zone", required=True)
        zone(tz_name)
        cron = str(body.get("cron") or "").strip()
        at = str(body.get("at") or "").strip()
        if cron and at:
            raise ValueError("Choose a repeating cron schedule or a one-time date, not both.")
        if cron:
            changes["kind"], (changes["spec"], changes["next_run"]) = "cron", plan_cron(cron, tz_name, now)
        elif at:
            changes["kind"], (changes["spec"], changes["next_run"]) = "once", plan_at(at, tz_name, now)
        elif "timezone" in body and row["kind"] == "cron":
            changes["spec"], changes["next_run"] = plan_cron(row["spec"], tz_name, now)
        if "timezone" in body:
            changes["tz"] = tz_name
        if "prompt" in body:
            changes["prompt"] = self._clean_text(body["prompt"], PROMPT_LIMIT, "What Maxwell should do", required=True)
        if "name" in body:
            name = " ".join(self._clean_text(body["name"], NAME_LIMIT, "Name", required=False).split())
            changes["name"] = name or row["name"]
        if body.get("enabled") is False:
            changes["enabled"] = False
            changes["last_status"] = "paused from the dashboard"
        elif body.get("enabled") is True or "next_run" in changes:
            first = changes.get("next_run") or next_run({**row, **changes}, now)
            if first is None:
                raise ValueError("That one-time run is in the past. Pick a new date first.")
            changes.update(enabled=True, next_run=first)
            if body.get("enabled") is True and not row["enabled"]:
                changes["last_status"] = "resumed from the dashboard"
        if changes:
            store.update(uid, sid, **changes)

    async def schedules_view(self, request, session):
        from plugins.schedules.cron import CronError
        from plugins.schedules.store import ScheduleLimitError

        uid = self.uid(session)
        try:
            if request.method == "POST":
                body = await self._json(request)
                await asyncio.to_thread(self._create, uid, body)
            elif request.method in {"PUT", "DELETE"}:
                sid = request.match_info.get("schedule_id", "")
                if request.method == "DELETE":
                    if not await asyncio.to_thread(self.schedules().delete, uid, sid):
                        raise web.HTTPNotFound(text="That schedule no longer exists.")
                else:
                    body = await self._json(request)
                    await asyncio.to_thread(self._update, uid, sid, body)
        except (CronError, ScheduleLimitError) as exc:
            raise self._bad(exc) from None
        return self.dashboard.response(await asyncio.to_thread(self._schedule_rows, uid))

    def routes(self):
        return (
            ("GET", "/api/dashboard/plan", self.plan),
            ("GET", "/api/dashboard/files", self.files),
            ("DELETE", "/api/dashboard/files", self.files),
            ("POST", "/api/dashboard/files/upload", self.upload),
            ("POST", "/api/dashboard/files/mkdir", self.mkdir),
            ("POST", "/api/dashboard/files/move", self.move),
            ("GET", "/api/dashboard/files/text", self.text),
            ("POST", "/api/dashboard/files/link", self.link),
            ("PUT", "/api/dashboard/files/handle", self.handle),
            ("GET", "/api/dashboard/schedules", self.schedules_view),
            ("POST", "/api/dashboard/schedules", self.schedules_view),
            ("PUT", "/api/dashboard/schedules/{schedule_id:[0-9a-f]{8}}", self.schedules_view),
            ("DELETE", "/api/dashboard/schedules/{schedule_id:[0-9a-f]{8}}", self.schedules_view),
        )

