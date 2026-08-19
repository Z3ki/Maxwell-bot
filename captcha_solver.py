"""Discord CAPTCHA solving for the Maxwell self-bot.

Discord challenges certain API actions (most commonly accepting a server
invite, but also login/phone flows) with an hCaptcha or reCAPTCHA
challenge. The library (discord.py-self) surfaces this as
``discord.CaptchaRequired`` on the HTTP layer and, when a
``captcha_handler`` is installed on the client, retries the request with
the solved token in ``X-Captcha-Key`` (plus ``X-Captcha-Rqtoken`` for
enterprise hCaptcha).

This module implements that handler using an external solving service
(CapSolver or 2captcha — the two that reliably support Discord's
enterprise hCaptcha with rqdata). When no solver is configured it falls
back to surfacing the challenge details so the caller (join_server /
leave_server) can report them instead of failing silently.

Env config (see config.py):
    CAPTCHA_SOLVER_SERVICE   "capsolver" | "2captcha" | "" (disabled)
    CAPTCHA_SOLVER_API_KEY   the solver service API key
    CAPTCHA_SOLVER_TIMEOUT   max seconds to wait for a solution (default 180)
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

DISCORD_URL = "https://discord.com"


class CaptchaSolveError(Exception):
    """Raised when the CAPTCHA could not be solved."""


class _BaseSolver:
    service = ""

    def __init__(self, api_key: str, timeout: int = 180) -> None:
        self.api_key = api_key
        self.timeout = timeout

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp,
            ):
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise CaptchaSolveError(f"{self.service}: request failed: {err}") from err
        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as err:
            raise CaptchaSolveError(
                f"solver returned non-JSON ({resp.status}): {text[:200]}"
            ) from err
        if resp.status != 200 or data.get("errorId") not in (None, 0):
            raise CaptchaSolveError(
                f"{self.service} API error {resp.status}: {json.dumps(data)[:300]}"
            )
        return data

    async def _poll(
        self,
        get_result: Callable[[], Awaitable[dict[str, Any]]],
        timeout: int | None = None,
    ) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + (timeout or self.timeout)
        while True:
            data = await get_result()
            status = data.get("status")
            # CapSolver uses "ready"/"success"; 2captcha JSON uses status=1
            # with the token in "request". Integer 1 never matched the string
            # set, so 2captcha polls timed out even after a valid solve.
            if status in (1, "1", "ready", "success", "completed"):
                return data
            request = str(data.get("request") or "")
            if status in (0, "0") and request == "CAPCHA_NOT_READY":
                pass
            elif status in ("failed", "error") or (
                status in (0, "0") and request and request != "CAPCHA_NOT_READY"
            ):
                raise CaptchaSolveError(f"{self.service}: task failed: {data}")
            if asyncio.get_event_loop().time() > deadline:
                raise CaptchaSolveError(
                    f"{self.service}: timed out after {timeout or self.timeout}s"
                )
            await asyncio.sleep(3)

    async def solve(
        self,
        *,
        service: str,
        sitekey: str,
        rqdata: str | None = None,
        invisible: bool = False,
    ) -> str:
        raise NotImplementedError


class CapSolverSolver(_BaseSolver):
    service = "capsolver"
    _BASE = "https://api.capsolver.com"

    async def solve(
        self,
        *,
        service: str,
        sitekey: str,
        rqdata: str | None = None,
        invisible: bool = False,
    ) -> str:
        task: dict[str, Any] = {
            "type": "HCaptchaTaskProxyLess",
            "websiteURL": DISCORD_URL,
            "websiteKey": sitekey,
            "isInvisible": bool(invisible),
        }
        if rqdata:
            task["enterprisePayload"] = {"rqdata": rqdata}
        create = await self._post_json(
            f"{self._BASE}/createTask", {"clientKey": self.api_key, "task": task}
        )
        task_id = create.get("taskId")
        if not task_id:
            raise CaptchaSolveError(f"capsolver: no taskId in {create}")
        logger.info(
            "capsolver task %s created for sitekey=%s invisible=%s",
            task_id,
            sitekey,
            invisible,
        )

        async def get_result() -> dict[str, Any]:
            return await self._post_json(
                f"{self._BASE}/getTaskResult",
                {"clientKey": self.api_key, "taskId": task_id},
            )

        data = await self._poll(get_result)
        solution = data.get("solution") or {}
        token = (
            solution.get("gRecaptchaResponse")
            or solution.get("token")
            or solution.get("captcha_key")
        )
        if not token:
            raise CaptchaSolveError(f"capsolver: no solution token in {data}")
        logger.info("capsolver task %s solved (%d chars)", task_id, len(token))
        return str(token)


class TwoCaptchaSolver(_BaseSolver):
    service = "2captcha"
    _IN = "https://2captcha.com/in.php"
    _RES = "https://2captcha.com/res.php"

    async def _get(self, url: str, params: dict[str, Any]) -> str:
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp,
            ):
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise CaptchaSolveError(f"2captcha: request failed: {err}") from err

    async def solve(
        self,
        *,
        service: str,
        sitekey: str,
        rqdata: str | None = None,
        invisible: bool = False,
    ) -> str:
        params: dict[str, Any] = {
            "key": self.api_key,
            "method": "hcaptcha",
            "sitekey": sitekey,
            "pageurl": DISCORD_URL,
            "json": "1",
            "soft_id": "4001",
        }
        if rqdata:
            params["data"] = rqdata
        if invisible:
            params["is_invisible"] = "1"

        text = await self._get(self._IN, params)
        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as err:
            raise CaptchaSolveError(f"2captcha in.php non-JSON: {text[:200]}") from err
        if data.get("status") != 1:
            raise CaptchaSolveError(f"2captcha in.php error: {data}")
        captcha_id = data.get("request")
        logger.info(
            "2captcha task %s created for sitekey=%s invisible=%s",
            captcha_id,
            sitekey,
            invisible,
        )

        async def get_result() -> dict[str, Any]:
            res_params = {
                "key": self.api_key,
                "action": "get",
                "id": captcha_id,
                "json": "1",
            }
            txt = await self._get(self._RES, res_params)
            try:
                return json.loads(txt)
            except json.JSONDecodeError as err:
                raise CaptchaSolveError(
                    f"2captcha res.php non-JSON: {txt[:200]}"
                ) from err

        data = await self._poll(get_result)
        token = data.get("request")
        if not token or token == "CAPCHA_NOT_READY":
            raise CaptchaSolveError(f"2captcha: no token: {data}")
        logger.info("2captcha task %s solved (%d chars)", captcha_id, len(token))
        return str(token)


def build_solver(
    service: str | None, api_key: str | None, timeout: int = 180
) -> _BaseSolver | None:
    """Return a solver instance or None when not configured."""
    service = (service or "").strip().lower()
    api_key = (api_key or "").strip()
    if not service or not api_key:
        return None
    if service == "capsolver":
        return CapSolverSolver(api_key, timeout)
    if service == "2captcha":
        return TwoCaptchaSolver(api_key, timeout)
    logger.warning(
        "CAPTCHA_SOLVER_SERVICE=%r not supported (capsolver/2captcha)", service
    )
    return None


class HumanCaptchaServer:
    """Host a one-shot hCaptcha solve page and wait for the token.

    Discord's captcha tokens are bound to the sitekey + rqdata, NOT to the
    machine or account that solves them. So the bot can host a page embedding
    Discord's sitekey + the challenge's rqdata, hand the link to anyone
    (owner, friend, server member), and their browser solve produces a token
    that completes the bot's original request. The token is single-use and
    the challenge goes stale in ~2 minutes, so each page is unique and the
    handler waits on it with a hard timeout.

    Serves:
        GET  /captcha/{cid}          — the solve page
        POST /captcha/{cid}/solve    — {token: ...} from the page
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8790,
        public_base: str = "http://127.0.0.1:8790",
        timeout: int = 180,
    ) -> None:
        self.host = host
        self.port = port
        self.public_base = public_base.rstrip("/")
        self.timeout = timeout
        self._app = web.Application()
        self._app.router.add_get("/captcha/{cid}", self._handle_page)
        self._app.router.add_post("/captcha/{cid}/solve", self._handle_solve)
        self._runner: web.AppRunner | None = None
        self._challenges: dict[str, dict[str, Any]] = {}

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> None:
        if self._runner is None:
            self._runner = web.AppRunner(self._app, access_log=None)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.host, self.port)
            await site.start()
            logger.info(
                "Human captcha server listening on http://%s:%s/captcha/<id>",
                self.host,
                self.port,
            )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def create_challenge(self, exception: Any) -> str:
        """Register a pending challenge; returns the public solve URL."""
        await self.start()
        cid = secrets.token_urlsafe(9)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._challenges[cid] = {
            "fut": fut,
            "exc": exception,
            "created": time.time(),
        }
        return f"{self.public_base}/captcha/{cid}"

    async def wait_for_token(self, challenge_url: str) -> str:
        """Block until someone solves the page (or timeout). Returns the token."""
        cid = challenge_url.rsplit("/", 1)[-1]
        entry = self._challenges.get(cid)
        if entry is None:
            raise CaptchaSolveError(f"captcha challenge {cid} not found/expired")
        try:
            return await asyncio.wait_for(entry["fut"], timeout=self.timeout)
        except asyncio.TimeoutError:
            raise CaptchaSolveError(
                f"human captcha solve timed out after {self.timeout}s — "
                "the rqdata/rqtoken challenge has expired"
            ) from None
        finally:
            self._challenges.pop(cid, None)

    # --- HTTP handlers ---

    async def _handle_page(self, request: web.Request) -> web.Response:
        cid = request.match_info.get("cid", "")
        entry = self._challenges.get(cid)
        if entry is None:
            return web.Response(
                text="<h1>Challenge expired or unknown.</h1>",
                content_type="text/html",
                status=404,
            )
        exc = entry["exc"]
        sitekey = getattr(exc, "sitekey", "")
        rqdata = getattr(exc, "rqdata", None) or ""
        invisible = bool(getattr(exc, "should_serve_invisible", False))
        service = getattr(exc, "service", "hcaptcha")
        if str(service or "").lower() not in {"hcaptcha", "h-captcha"}:
            # Only hCaptcha is hosted. Discord reCAPTCHA cannot be solved by
            # this widget; serving hCaptcha for it wastes the human-solve window.
            return web.Response(
                text=f"<h1>Unsupported captcha service: {service}</h1>",
                content_type="text/html",
                status=501,
            )
        html = _build_solve_page(cid, sitekey, rqdata, invisible)
        return web.Response(text=html, content_type="text/html")

    async def _handle_solve(self, request: web.Request) -> web.Response:
        cid = request.match_info.get("cid", "")
        entry = self._challenges.get(cid)
        if entry is None:
            return web.json_response({"ok": False, "error": "expired"}, status=404)
        try:
            body = await request.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError):
            body = {}
        token = (body or {}).get("token") or ""
        if not token:
            return web.json_response(
                {"ok": False, "error": "missing token"}, status=400
            )
        fut = entry["fut"]
        if not fut.done():
            fut.set_result(token)
        logger.info("captcha %s solved (%d chars)", cid, len(token))
        return web.json_response({"ok": True})


def _build_solve_page(cid: str, sitekey: str, rqdata: str, invisible: bool) -> str:
    """Render the hCaptcha solve page (hCaptcha enterprise, rqdata-aware)."""
    rqdata_js = json.dumps(rqdata) if rqdata else "null"
    sitekey_js = json.dumps(sitekey)
    extra = ""
    if invisible:
        extra = (
            '<p><button id="solve-btn" class="btn">Click to solve captcha</button></p>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verify — Maxwell</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #1e1f22; color: #eee; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #2b2d31; border-radius: 12px; padding: 32px; max-width: 420px; width: 90%; text-align: center; box-shadow: 0 8px 24px rgba(0,0,0,.4); }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  p {{ color: #b5bac1; font-size: 14px; line-height: 1.5; }}
  .btn {{ background: #5865f2; color: #fff; border: 0; border-radius: 8px; padding: 10px 18px; font-size: 14px; cursor: pointer; margin-top: 12px; }}
  .btn:hover {{ background: #4752c4; }}
  #status {{ margin-top: 14px; font-size: 13px; min-height: 18px; }}
  .ok {{ color: #57f287; }} .err {{ color: #ed4245; }}
</style>
</head>
<body>
<div class="card">
  <h1>Human verification</h1>
  <p>Complete the captcha to finish this action. This is a one-time, expiring challenge.</p>
  {extra}
  <div id="hcaptcha-widget" style="margin: 14px auto 0; display: inline-block;"></div>
  <div id="status"></div>
</div>
<script src="https://hcaptcha.com/1/api.js?render=explicit&onload=hcaptchaOnLoad" async defer></script>
<script>
  var SITEKEY = {sitekey_js};
  var RQDATA = {rqdata_js};
  var INVISIBLE = {json.dumps(bool(invisible))};
  var CID = {json.dumps(cid)};
  var widgetId = null;

  function setStatus(msg, cls) {{
    var el = document.getElementById('status');
    el.textContent = msg;
    el.className = cls || '';
  }}

  function onSolved(token) {{
    setStatus('Submitting…');
    fetch('/captcha/' + CID + '/solve', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ token: token }})
    }}).then(function (r) {{ return r.json(); }}).then(function (j) {{
      if (j.ok) {{
        setStatus('Done. You can close this page.', 'ok');
      }} else {{
        setStatus('Error: ' + (j.error || 'unknown'), 'err');
      }}
    }}).catch(function (e) {{
      setStatus('Network error: ' + e, 'err');
    }});
  }}

  window.hcaptchaOnLoad = function () {{
    var config = {{
      sitekey: SITEKEY,
      theme: 'dark',
      size: INVISIBLE ? 'invisible' : 'normal',
      callback: onSolved,
      'expired-callback': function () {{ setStatus('Challenge expired — please refresh.', 'err'); }}
    }};
    if (RQDATA) {{ config.rqdata = RQDATA; }}
    widgetId = hcaptcha.render('hcaptcha-widget', config);
    if (INVISIBLE) {{
      document.getElementById('solve-btn').addEventListener('click', function () {{
        hcaptcha.execute(widgetId);
      }});
    }}
  }};
</script>
</body>
</html>"""
