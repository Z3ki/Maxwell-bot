"""Public user settings API. Never grants access to the operator API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web

from api.dashboard_auth import DashboardConfig, DashboardSessions
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


class Dashboard:
    def __init__(self, data_dir, config=None):
        self.data_dir = data_dir
        self.config = config
        self.sessions = self.preferences = self.vault = self.http = None
        self.login_limit = RateLimiter(rate=1 / 60, burst=5)
        self.test_limit = RateLimiter(rate=1 / 30, burst=2)

    async def start(self, app):
        try:
            self.config = self.config or DashboardConfig.from_env()
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

    async def authorize_guild(self, session, guild_id):
        if not guild_id.isdigit():
            raise web.HTTPBadRequest(text="Invalid server.")
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
                    self.sessions.get, request.cookies.get(self.config.cookie, "")
                )
                if not session:
                    raise web.HTTPUnauthorized(
                        text="Your session expired. Sign in with Discord again."
                    )
            if request.method not in {"GET", "HEAD"}:
                if (
                    request.headers.get("Origin") != self.config.origin
                    or not session
                    or not hmac.compare_digest(
                        request.headers.get("X-CSRF-Token", ""), session["csrf"]
                    )
                ):
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
        session = await asyncio.to_thread(
            self.sessions.get, request.cookies.get(self.config.cookie, "")
        )
        return self.response(
            {
                "configured": True,
                "user": session["user"] if session else None,
                "csrf": session["csrf"] if session else None,
            }
        )

    async def logout(self, request, session):
        await asyncio.to_thread(
            self.sessions.delete, request.cookies.get(self.config.cookie, "")
        )
        response = self.response({"ok": True})
        self.cookie(response, self.config.cookie, "", 0)
        return response

    async def personal(self, request, session):
        user_id = session["user"]["id"]
        if request.method == "PUT":
            defaults, personality = validate_personal(await self.body(request))
            await asyncio.to_thread(
                self.preferences.update, user_id, defaults, personality
            )
        from user_install import USER_INSTALL_CONTEXT_COUNTS

        result = await asyncio.to_thread(self.preferences.get, user_id)
        result["context_counts"] = USER_INSTALL_CONTEXT_COUNTS
        return self.response(result)

    async def servers(self, request, session):
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
            for path, manifest, error in discover_plugin_manifests()
            if manifest and not error and not manifest.protected
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

    async def connection(self, request, session):
        user_id = session["user"]["id"]
        if request.method == "DELETE":
            await asyncio.to_thread(self.vault.delete, user_id)
        elif request.method == "PUT":
            if not self.vault.enabled:
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
            existing = await asyncio.to_thread(self.vault.get, user_id)
            same = (
                existing
                if existing and existing["provider"] == body.get("provider")
                else None
            )
            secret = body.get("api_key") or (same or {}).get("api_key", "")
            settings = (
                parse_model_support(body["modalities"], body["generation"])
                if "modalities" in body and "generation" in body
                else effective_settings(same.get("settings"))
                if same
                else parse_model_support("text, tools", "")
            )
            await asyncio.to_thread(
                self.vault.save,
                user_id,
                body.get("provider", ""),
                body.get("model", ""),
                secret,
                base_url=body.get("base_url", (same or {}).get("base_url", "")),
                settings=settings,
            )
        status, unreadable = None, False
        present = await asyncio.to_thread(self.vault.has_credential, user_id)
        if present and self.vault.enabled:
            try:
                status = await asyncio.to_thread(self.vault.status, user_id)
            except Exception:
                unreadable = True
        return self.response(
            {
                "enabled": self.vault.enabled,
                "present": present,
                "unreadable": unreadable,
                "connection": status,
                "providers": PROVIDERS,
            }
        )

    async def test_connection(self, request, session):
        from maxwell_core.providers.factory import openai_compat_provider

        user_id = session["user"]["id"]
        if not self.test_limit.allow(user_id):
            raise web.HTTPTooManyRequests(text="Wait 30 seconds before testing again.")
        credential = await asyncio.to_thread(self.vault.get, user_id)
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
                "message": "Connection works. Your messages will use this AI.",
            }
        )


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
        ("POST", "/api/dashboard/logout", dashboard.logout, False),
        ("GET", "/api/dashboard/personal", dashboard.personal, False),
        ("PUT", "/api/dashboard/personal", dashboard.personal, False),
        ("GET", "/api/dashboard/servers", dashboard.servers, False),
        ("GET", "/api/dashboard/servers/{guild_id}", dashboard.server, False),
        ("PUT", "/api/dashboard/servers/{guild_id}", dashboard.server, False),
        ("GET", "/api/dashboard/connection", dashboard.connection, False),
        ("PUT", "/api/dashboard/connection", dashboard.connection, False),
        ("DELETE", "/api/dashboard/connection", dashboard.connection, False),
        ("POST", "/api/dashboard/connection/test", dashboard.test_connection, False),
    ):

        async def route(request, handler=handler, public=public):
            return await dashboard.dispatch(request, handler, public=public)

        app.router.add_route(method, path, route)
    return dashboard
