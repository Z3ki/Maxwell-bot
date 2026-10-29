"""Public user settings API. Never grants access to the operator API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web

from api.dashboard_auth import LINK_SESSION_TTL, DashboardConfig, DashboardSessions
from api.dashboard_panels import Panels
from control_defaults import GUILD_CAPABILITIES
from plugins.maxwell_extras.byok import (
    PROVIDERS,
    CredentialVault,
    effective_settings,
    make_request_provider,
    parse_model_support,
)
from plugins.maxwell_extras.dashboard_settings import (
    read_json,
    save_server_settings,
    server_settings,
)
from plugins.maxwell_extras.user_preferences import UserPreferenceStore
from site_backend import RateLimiter

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parents[1] / "web" / "dashboard"
API = "https://discord.com/api/v10"


def can_manage(guild):
    try:
        return guild.get("owner") is True or bool(
            int(guild.get("permissions", 0)) & (8 | 32)
        )
    except (ValueError, TypeError):
        return False


def member_can_manage(guild, member, user_id):
    """Owner, Administrator or Manage Server, from bot-token REST payloads.

    Link sign-ins carry no OAuth token, so this is the live check: the
    member's roles OR'd with @everyone (whose role id is the guild id).
    """
    try:
        if str(guild.get("owner_id")) == str(user_id):
            return True
        roles = {str(row["id"]): int(row.get("permissions", 0)) for row in guild.get("roles", [])}
        bits = roles.get(str(guild.get("id")), 0)
        for role in member.get("roles", []):
            bits |= roles.get(str(role), 0)
        return bool(bits & (8 | 32))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def dev_guild_allowed(guild_id):
    """The dev bot only manages the guilds it's scoped to, web included."""
    if os.getenv("MAXWELL_DEV_MODE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return True
    allowed = {g.strip() for g in os.getenv("MAXWELL_DEV_GUILD_IDS", "").split(",") if g.strip()}
    return not allowed or str(guild_id) in allowed


def validate_personal(body):
    from plugins.maxwell_extras.command_suite import _PERSONAL_DEFAULT_VALUES

    if not body.keys() <= {"defaults", "personality"}:
        raise ValueError("Unsupported personal setting.")
    defaults = body.get("defaults", {})
    personality = body.get("personality", "")
    if (
        not isinstance(defaults, dict)
        or not defaults.keys() <= _PERSONAL_DEFAULT_VALUES.keys()
    ):
        raise ValueError("Choose supported reply settings.")
    for key, value in defaults.items():
        if key == "language":
            if (
                not isinstance(value, str)
                or len(value) > 80
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Language must be at most 80 characters.")
        elif key == "context":
            if (
                type(value) is not int
                or str(value) not in _PERSONAL_DEFAULT_VALUES[key]
            ):
                raise ValueError("Choose a recent-message count from the list.")
        elif not isinstance(value, str) or value not in _PERSONAL_DEFAULT_VALUES[key]:
            raise ValueError("Choose a valid reply setting.")
    if not isinstance(personality, str) or len(personality) > 800:
        raise ValueError("Personality must be at most 800 characters.")
    return defaults, personality


_PRIVACY_STARTED = web.RequestKey("privacy_started_at", float)


class Dashboard:
    def __init__(self, data_dir, config=None):
        self.data_dir = data_dir
        self.config = config
        self.sessions = self.preferences = self.vault = self.worker_vault = self.http = None
        self.login_limit = RateLimiter(rate=1 / 60, burst=5)
        self.redeem_limit = RateLimiter(rate=1 / 6, burst=10)
        self.test_limit = RateLimiter(rate=1 / 30, burst=2)
        self.panels = Panels(self)

    async def start(self, app):
        try:
            self.config = self.config or await asyncio.to_thread(
                DashboardConfig.from_env, Path(self.data_dir())
            )
        except ValueError:
            logger.error(
                "Dashboard configuration is invalid; sign-in is disabled. See docs/DASHBOARD.md."
            )
            self.config = None
        if not self.config:
            return

        def stores():
            root = Path(self.data_dir())
            self.sessions = DashboardSessions(
                root / "dashboard" / "sessions.sqlite3", self.config.key
            )
            extra = root / "plugins" / "maxwell_extras"
            self.preferences = UserPreferenceStore(extra / "user_preferences.json")
            self.vault = CredentialVault(extra / "byok.sqlite3")
            self.worker_vault = CredentialVault(extra / "worker_byok.sqlite3")

        await asyncio.to_thread(stores)
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def stop(self, app):
        if self.http:
            await self.http.close()

    async def discord(self, path, *, token=None, form=None):
        headers = {
            "Authorization": ("Bearer " + token)
            if token
            else "Bot " + self.config.bot_token
        }
        method = "POST" if form is not None else "GET"
        async with self.http.request(
            method,
            API + path,
            headers=headers if form is None else {},
            data=form,
            allow_redirects=False,
        ) as response:
            if response.status == 429:
                raise web.HTTPTooManyRequests(
                    text="Discord is busy. Try again shortly."
                )
            if response.status in {401, 403, 404}:
                raise web.HTTPForbidden(
                    text="Discord access changed. Sign in again or check your server permissions."
                )
            if response.status != 200:
                raise web.HTTPBadGateway(
                    text="Could not reach Discord. Try again shortly."
                )
            return await response.json()

    async def guilds(self, token=None):
        result, after = [], "0"
        for _ in range(100):
            page = await self.discord(
                "/users/@me/guilds?limit=200&after=" + after, token=token
            )
            result.extend(page)
            if len(page) < 200:
                return result
            after = max((str(row["id"]) for row in page), key=int)
        raise web.HTTPBadGateway(text="Could not load the server list.")

    def session_id(self, request):
        if self.config.link_mode:
            value = request.headers.get("Authorization", "")
            return value[7:].strip() if value.startswith("Bearer ") else ""
        return request.cookies.get(self.config.cookie, "")

    async def authorize_guild(self, session, guild_id):
        if not guild_id.isdigit():
            raise web.HTTPBadRequest(text="Invalid server.")
        if not dev_guild_allowed(guild_id):
            raise web.HTTPForbidden(text="This test bot only manages its development servers.")
        if self.config.link_mode:
            uid = session["user"]["id"]
            guild = await self.discord("/guilds/" + guild_id)
            member = await self.discord("/guilds/" + guild_id + "/members/" + uid)
            if not member_can_manage(guild, member, uid):
                raise web.HTTPForbidden(
                    text="You need to own this server or have Administrator / Manage Server."
                )
            return {"id": guild_id, "name": str(guild.get("name", ""))}
        # Never trust IDs or permission flags submitted by the browser. Check
        # live OAuth permissions on every read and write, including revocations.
        guild = next(
            (
                row
                for row in await self.guilds(session["token"])
                if str(row["id"]) == guild_id and can_manage(row)
            ),
            None,
        )
        if guild is None:
            raise web.HTTPForbidden(
                text="You need to own this server or have Administrator / Manage Server."
            )
        await self.discord("/guilds/" + guild_id)
        return guild

    async def body(self, request):
        if request.content_length and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(
                max_size=16384, actual_size=request.content_length
            )
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="Send a JSON settings form.")
        value = await request.clone(client_max_size=16384).json()
        if not isinstance(value, dict):
            raise TypeError("A settings form is required.")
        return value

    async def dispatch(self, request, handler, *, public=False):
        request[_PRIVACY_STARTED] = time.time()
        try:
            if self.config and request.host != urlsplit(self.config.origin).netloc:
                raise web.HTTPForbidden(text="Use the dashboard address.")
            if request.path.startswith("/api/dashboard/") and not self.config:
                if request.path == "/api/dashboard/session":
                    return self.response({"configured": False, "user": None})
                raise web.HTTPServiceUnavailable(
                    text="Discord sign-in is not configured yet. Use /config in Discord."
                )
            session = None
            if not public:
                session = await asyncio.to_thread(
                    self.sessions.get, self.session_id(request)
                )
                if not session:
                    raise web.HTTPUnauthorized(
                        text="Your session ended. Run /dashboard in Discord for a new sign-in link."
                        if self.config.link_mode
                        else "Your session expired. Sign in with Discord again."
                    )
            if request.method not in {"GET", "HEAD"}:
                origin_ok = request.headers.get("Origin") == self.config.origin
                if self.config.link_mode:
                    # A bearer header is never attached by the browser on its
                    # own, so it is the CSRF token. Origin is belt and braces.
                    allowed = origin_ok and (public or session)
                else:
                    allowed = origin_ok and session and hmac.compare_digest(
                        request.headers.get("X-CSRF-Token", ""), session["csrf"]
                    )
                if not allowed:
                    raise web.HTTPForbidden(text="Refresh the dashboard before saving.")
            response = await handler(request, session)
        except web.HTTPException as exc:
            response = self.response({"error": exc.text}, exc.status)
        except json.JSONDecodeError:
            response = self.response({"error": "Send a valid JSON settings form."}, 400)
        except ValueError as exc:
            response = self.response({"error": str(exc)[:240]}, 400)
        except TypeError:
            response = self.response(
                {
                    "error": "Invalid or unavailable settings. Check your choices and try again."
                },
                400,
            )
        except (aiohttp.ClientError, TimeoutError):
            response = self.response(
                {"error": "Could not reach Discord. Try again shortly."}, 502
            )
        except Exception as exc:
            logger.warning("Dashboard action failed (%s)", type(exc).__name__)
            response = self.response(
                {
                    "error": "Could not load or save settings. Contact the operator if this continues."
                },
                503,
            )
        if (
            request.path == "/api/dashboard/callback"
            and response.status >= 400
            and self.config
        ):
            response = web.HTTPFound("/dashboard/?login=failed")
            self.cookie(response, self.config.flow_cookie, "", 0)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @staticmethod
    def response(value, status=200):
        return web.json_response(
            value, status=status, headers={"Cache-Control": "no-store"}
        )

    def cookie(self, response, name, value, lifetime):
        response.set_cookie(
            name,
            value,
            max_age=lifetime,
            httponly=True,
            secure=self.config.secure,
            samesite="Lax",
            path="/",
        )

    async def static(self, request, session):
        if request.path == "/dashboard":
            # Relative redirect: behind /dev the proxy strips the prefix, and
            # the page's relative asset URLs only work with the trailing slash.
            return web.HTTPFound("dashboard/" + (f"?{request.query_string}" if request.query_string else ""))
        name = request.match_info.get("asset", "index.html")
        if name not in {"index.html", "dashboard.js", "dashboard.css"}:
            raise web.HTTPNotFound(text="Page not found.")
        return web.Response(
            body=await asyncio.to_thread((WEB_DIR / name).read_bytes),
            content_type={
                "index.html": "text/html",
                "dashboard.js": "text/javascript",
                "dashboard.css": "text/css",
            }[name],
        )

    async def login(self, request, session):
        from api.auth import _get_client_ip

        if not self.login_limit.allow(_get_client_ip(request)):
            raise web.HTTPTooManyRequests(
                text="Too many sign-ins. Try again in a minute."
            )
        state, browser = await asyncio.to_thread(self.sessions.begin)
        response = web.HTTPFound(
            "https://discord.com/oauth2/authorize?"
            + urlencode(
                {
                    "client_id": self.config.client_id,
                    "redirect_uri": self.config.callback,
                    "response_type": "code",
                    "scope": "identify guilds",
                    "state": state,
                }
            )
        )
        self.cookie(response, self.config.flow_cookie, browser, 300)
        return response

    async def callback(self, request, session):
        state, code = request.query.get("state", ""), request.query.get("code", "")
        browser = request.cookies.get(self.config.flow_cookie, "")
        valid = bool(state and browser) and await asyncio.to_thread(
            self.sessions.consume, state, browser
        )
        if not valid or not code or "error" in request.query:
            response = web.HTTPFound("/dashboard/?login=cancelled")
        else:
            token = await self.discord(
                "/oauth2/token",
                form={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.config.callback,
                },
            )
            if not {"identify", "guilds"} <= set(token.get("scope", "").split()):
                raise web.HTTPForbidden(
                    text="Discord sign-in did not grant the required permissions."
                )
            user = await self.discord("/users/@me", token=token["access_token"])
            profile = {
                "id": str(user["id"]),
                "name": str(user.get("global_name") or user["username"])[:100],
            }
            old_cookie = request.cookies.get(self.config.cookie, "")
            await asyncio.to_thread(self.sessions.delete, old_cookie)
            cookie = await asyncio.to_thread(
                self.sessions.create,
                profile,
                token["access_token"],
                int(token["expires_in"]),
            )
            response = web.HTTPFound("/dashboard/")
            self.cookie(
                response,
                self.config.cookie,
                cookie,
                min(int(token["expires_in"]), 8 * 3600),
            )
        self.cookie(response, self.config.flow_cookie, "", 0)
        return response

    async def session(self, request, unused):
        session = await asyncio.to_thread(self.sessions.get, self.session_id(request))
        return self.response(
            {
                "configured": True,
                "mode": self.config.mode,
                "user": session["user"] if session else None,
                "csrf": session["csrf"] if session else None,
            }
        )

    async def redeem(self, request, unused):
        """Trade a one-time /dashboard link for an in-memory bearer session."""
        from api.auth import _get_client_ip

        if not self.config.link_mode:
            raise web.HTTPNotFound(text="Sign in with Discord instead.")
        if not self.redeem_limit.allow(_get_client_ip(request)):
            raise web.HTTPTooManyRequests(text="Too many sign-ins. Try again in a minute.")
        body = await self.body(request)
        payload = await asyncio.to_thread(self.sessions.redeem_link, str(body.get("token", "")))
        if not payload or not str(payload.get("user", {}).get("id", "")).isdigit():
            raise web.HTTPUnauthorized(
                text="This sign-in link expired or was already used. Run /dashboard in Discord for a new one."
            )
        guilds = [
            {"id": str(row["id"]), "name": str(row.get("name", ""))[:100]}
            for row in payload.get("guilds", [])
            if str(row.get("id", "")).isdigit()
        ][:200]
        token = await asyncio.to_thread(
            self.sessions.create, payload["user"], "", LINK_SESSION_TTL, guilds=guilds, mode="link"
        )
        return self.response(
            {"token": token, "user": payload["user"], "expires_in": LINK_SESSION_TTL}
        )

    async def logout(self, request, session):
        await asyncio.to_thread(self.sessions.delete, self.session_id(request))
        response = self.response({"ok": True})
        if not self.config.link_mode:
            self.cookie(response, self.config.cookie, "", 0)
        return response

    async def personal(self, request, session):
        user_id = session["user"]["id"]
        if request.method == "PUT":
            defaults, personality = validate_personal(await self.body(request))
            await asyncio.to_thread(
                self.preferences.update, user_id, defaults, personality
            )
            if defaults.get("tracking") == "off":
                from conversation_memory import delete_author_content_file

                try:
                    await asyncio.to_thread(
                        delete_author_content_file,
                        Path(self.data_dir()) / "maxwell_rag.db",
                        user_id,
                    )
                except Exception:
                    logger.warning("Could not delete stored message content", exc_info=False)
                    raise web.HTTPBadRequest(
                        text="Message storage is off, but stored messages could not be deleted. Try again."
                    ) from None
        from user_install import USER_INSTALL_CONTEXT_COUNTS

        result = await asyncio.to_thread(self.preferences.get, user_id)
        result["context_counts"] = USER_INSTALL_CONTEXT_COUNTS
        return self.response(result)

    async def privacy(self, request, session):
        from privacy import policy_urls
        from privacy_data import delete_data
        uid = session["user"]["id"]
        if request.method == "DELETE":
            result = await asyncio.to_thread(delete_data, self.data_dir(), uid, self.preferences, sessions=self.sessions)
            return self.response(result, 207 if result["failed"] else 200)
        return self.response({"policies": policy_urls(), "consents": await asyncio.to_thread(self.preferences.consents.review, uid),
            "storage": (await asyncio.to_thread(self.preferences.get, uid))["defaults"]["tracking"],
            "processing": "Requests and necessary context go to selected AI providers, including fallbacks and BYOK. Disabling history does not change provider processing or provider retention.",
            "support": policy_urls().get("contact", "Use /config → Privacy → Request private support in Discord."),
            "persistent_memory": "Automatic facts and embeddings are retired."})

    async def servers(self, request, session):
        if self.config.link_mode:
            # Snapshot from the bot's member cache when the link was minted;
            # authorize_guild re-checks live on every open and save.
            return self.response(
                {
                    "servers": [
                        {**row, "installed": True}
                        for row in session.get("guilds", [])
                        if dev_guild_allowed(row["id"])
                    ],
                    "client_id": self.config.client_id,
                    "mode": "link",
                }
            )
        users, bots = await asyncio.gather(self.guilds(session["token"]), self.guilds())
        bot_ids = {str(row["id"]) for row in bots}
        return self.response(
            {
                "servers": [
                    {
                        "id": str(row["id"]),
                        "name": row["name"],
                        "installed": str(row["id"]) in bot_ids,
                    }
                    for row in users
                    if can_manage(row)
                ],
                "client_id": self.config.client_id,
            }
        )

    def plugins(self):
        from maxwell_core.plugins.catalog import discover_plugin_manifests

        state = read_json(Path(self.data_dir()) / "plugins.json", dict, {}).get(
            "plugins", {}
        )
        return [
            {
                "id": path.name,
                "name": manifest.name,
                "default": bool(
                    state.get(path.name, {}).get(
                        "enabled_globally", manifest.enabled_by_default
                    )
                ),
            }
            # Tool-less plugins (web_dashboard) have nothing to switch off per server.
            for path, manifest, error in discover_plugin_manifests()
            if manifest and not error and not manifest.protected and manifest.tools
        ]

    async def server(self, request, session):
        guild_id = request.match_info["guild_id"]
        guild = await self.authorize_guild(session, guild_id)
        channels = await self.discord("/guilds/" + guild_id + "/channels")
        channels = [
            {"id": str(row["id"]), "name": row["name"]}
            for row in channels
            if row.get("type") in {0, 5}
        ]
        plugins = await asyncio.to_thread(self.plugins)
        if request.method == "PUT":
            body = await self.body(request)
            if body.get("channel") and body["channel"] not in {
                row["id"] for row in channels
            }:
                raise ValueError("Choose a channel in this server.")
            if "plugins" in body and (
                not isinstance(body["plugins"], dict)
                or not body["plugins"].keys() <= {row["id"] for row in plugins}
            ):
                raise ValueError("Choose installed extra tools.")
            await asyncio.to_thread(
                save_server_settings, Path(self.data_dir()), guild_id, body
            )
        settings = await asyncio.to_thread(
            server_settings, Path(self.data_dir()), guild_id
        )
        return self.response(
            {
                "name": guild["name"],
                "settings": settings,
                "channels": channels,
                "capabilities": GUILD_CAPABILITIES,
                "plugins": plugins,
            }
        )

    async def connection(self, request, session, *, worker=False):
        vault = self.worker_vault if worker else self.vault
        user_id = session["user"]["id"]
        if request.method == "DELETE":
            await asyncio.to_thread(vault.delete, user_id)
        elif request.method == "PUT":
            if not vault.enabled:
                raise web.HTTPServiceUnavailable(
                    text="Personal AI connections are unavailable. Ask the operator."
                )
            body = await self.body(request)
            if not body.keys() <= {
                "provider",
                "model",
                "api_key",
                "base_url",
                "modalities",
                "generation",
            }:
                raise ValueError("Unsupported connection setting.")
            existing = await asyncio.to_thread(vault.get, user_id)
            same = (
                existing
                if existing and existing["provider"] == body.get("provider")
                else None
            )
            if same and not body.get("api_key"):
                from plugins.maxwell_extras.byok import validate_public_endpoint
                endpoint = validate_public_endpoint(body.get("base_url", same["base_url"]), required=True)
                if endpoint != same["base_url"]:
                    raise ValueError("Paste an API key when changing the endpoint.")
            secret = body.get("api_key") or (same or {}).get("api_key", "")
            settings = (
                parse_model_support(body["modalities"], body["generation"])
                if "modalities" in body and "generation" in body
                else effective_settings(same.get("settings"))
                if same
                else parse_model_support("text, vision, tools", "")
            )
            await asyncio.to_thread(
                vault.save,
                user_id,
                body.get("provider", "custom"),
                body.get("model", ""),
                secret,
                base_url=body.get("base_url", (same or {}).get("base_url", "")),
                settings=settings,
                started_at=request.get(_PRIVACY_STARTED, time.time()),
            )
        status, unreadable = None, False
        present = await asyncio.to_thread(vault.has_credential, user_id)
        if present and vault.enabled:
            try:
                status = await asyncio.to_thread(vault.status, user_id)
            except Exception:
                unreadable = True
        return self.response(
            {
                "enabled": vault.enabled,
                "present": present,
                "unreadable": unreadable,
                "connection": status,
                "providers": PROVIDERS,
                "scope": "worker" if worker else "chat",
            }
        )

    async def test_connection(self, request, session, *, worker=False):
        vault = self.worker_vault if worker else self.vault
        from maxwell_core.providers.factory import openai_compat_provider

        user_id = session["user"]["id"]
        if not self.test_limit.allow(user_id):
            raise web.HTTPTooManyRequests(text="Wait 30 seconds before testing again.")
        credential = await asyncio.to_thread(vault.get, user_id)
        if not credential:
            raise web.HTTPBadRequest(text="Save a connection first.")
        provider = make_request_provider(
            SimpleNamespace(_make_chat_provider=openai_compat_provider), credential
        )
        try:
            await asyncio.wait_for(
                provider.generate_response(
                    [{"role": "user", "content": "Reply with OK."}], max_tokens=16
                ),
                15,
            )
        except Exception:
            raise web.HTTPBadRequest(
                text="Connection failed. Check the model, API key and provider credit."
            ) from None
        finally:
            await provider.close()
        return self.response(
            {
                "ok": True,
                "message": "Connection works. Subagents will use this AI." if worker else "Connection works. Your messages will use this AI.",
            }
        )


    async def worker_connection(self, request, session):
        return await self.connection(request, session, worker=True)

    async def test_worker_connection(self, request, session):
        return await self.test_connection(request, session, worker=True)


def register_dashboard(app, data_dir, config=None):
    dashboard = Dashboard(data_dir, config)
    app.on_startup.append(dashboard.start)
    app.on_cleanup.append(dashboard.stop)
    for method, path, handler, public in (
        ("GET", "/dashboard", dashboard.static, True),
        ("GET", "/dashboard/", dashboard.static, True),
        ("GET", "/dashboard/{asset}", dashboard.static, True),
        ("GET", "/api/dashboard/login", dashboard.login, True),
        ("GET", "/api/dashboard/callback", dashboard.callback, True),
        ("GET", "/api/dashboard/session", dashboard.session, True),
        ("POST", "/api/dashboard/redeem", dashboard.redeem, True),
        ("POST", "/api/dashboard/logout", dashboard.logout, False),
        ("GET", "/api/dashboard/personal", dashboard.personal, False),
        ("GET", "/api/dashboard/privacy", dashboard.privacy, False),
        ("DELETE", "/api/dashboard/privacy", dashboard.privacy, False),
        ("PUT", "/api/dashboard/personal", dashboard.personal, False),
        ("GET", "/api/dashboard/servers", dashboard.servers, False),
        ("GET", "/api/dashboard/servers/{guild_id}", dashboard.server, False),
        ("PUT", "/api/dashboard/servers/{guild_id}", dashboard.server, False),
        ("GET", "/api/dashboard/connection", dashboard.connection, False),
        ("PUT", "/api/dashboard/connection", dashboard.connection, False),
        ("DELETE", "/api/dashboard/connection", dashboard.connection, False),
        ("POST", "/api/dashboard/connection/test", dashboard.test_connection, False),
        ("GET", "/api/dashboard/worker-connection", dashboard.worker_connection, False),
        ("PUT", "/api/dashboard/worker-connection", dashboard.worker_connection, False),
        ("DELETE", "/api/dashboard/worker-connection", dashboard.worker_connection, False),
        ("POST", "/api/dashboard/worker-connection/test", dashboard.test_worker_connection, False),
        *((method, path, handler, False) for method, path, handler in dashboard.panels.routes()),
    ):

        async def route(request, handler=handler, public=public):
            return await dashboard.dispatch(request, handler, public=public)

        app.router.add_route(method, path, route)
    return dashboard
