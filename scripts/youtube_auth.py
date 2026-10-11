#!/usr/bin/env python3
"""Authorize a separate YouTube account without an unattended node-side poller."""
from __future__ import annotations

import argparse
import json
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.set_env import set_env

# Public YouTube client identity used by the pinned youtube-source plugin.
# https://github.com/lavalink-devs/youtube-source/blob/2be8e542d3f6f178e048dca565892684c2e40177/common/src/main/java/dev/lavalink/youtube/http/YoutubeOauth2Handler.java
CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
DEVICE_URL = "https://www.youtube.com/o/oauth2/device/code"
TOKEN_URL = "https://www.youtube.com/o/oauth2/token"


class AuthorizationError(Exception):
    """Safe operator-facing failure, without credential-bearing response bodies."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuthorizationError("YouTube authorization unexpectedly redirected; no credentials were forwarded.")


def _post_json(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        response = urllib.request.build_opener(_NoRedirects()).open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            exc.close()
            raise AuthorizationError(f"YouTube authorization failed (HTTP {exc.code}).") from None
        # Google uses HTTP 400 for pending, slow_down, expiry and denial.
        response = exc
    except (urllib.error.URLError, TimeoutError):
        raise AuthorizationError("Could not reach YouTube authorization securely.") from None
    with response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise AuthorizationError("YouTube returned an oversized authorization response.")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise AuthorizationError("YouTube returned an invalid authorization response.") from None
    if not isinstance(result, dict):
        raise AuthorizationError("YouTube returned an invalid authorization response.")
    return result


def request_device_code():
    grant = _post_json(DEVICE_URL, {
        "client_id": CLIENT_ID,
        "scope": "http://gdata.youtube.com https://www.googleapis.com/auth/youtube",
        "device_id": uuid.uuid4().hex,
        "device_model": "ytlr::",
    })
    if not all(grant.get(key) for key in ("device_code", "user_code", "expires_in")):
        raise AuthorizationError("YouTube did not issue an approval code.")
    return grant


def wait_for_token(grant, deadline):
    interval = max(5, float(grant.get("interval", 5)))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AuthorizationError('The approval code expired. Run authorization again for a fresh code.')
        time.sleep(min(interval, remaining))
        if time.monotonic() >= deadline:
            raise AuthorizationError("The approval code expired. Run authorization again for a fresh code.")
        response = _post_json(TOKEN_URL, {
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "code": grant["device_code"], "grant_type": "http://oauth.net/grant_type/device/1.0",
        })
        error = response.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "expired_token":
            raise AuthorizationError("The approval code expired. Run authorization again for a fresh code.")
        if error == "access_denied":
            raise AuthorizationError("YouTube account approval was denied.")
        if error:
            raise AuthorizationError("YouTube rejected account authorization; request a fresh approval code.")
        token = response.get("refresh_token")
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise AuthorizationError("YouTube did not return a usable refresh token.")
        return token


def save_token(env_file, token):
    # Recheck after browser approval: do not follow a replaced path into another env.
    if env_file.is_symlink() or not env_file.is_file():
        raise AuthorizationError("Select an existing regular private env file, not a symlink.")
    if stat.S_IMODE(env_file.stat().st_mode) & 0o077:
        raise AuthorizationError("Protect the selected env file with chmod 600 before authorization.")
    set_env(env_file, "YOUTUBE_OAUTH_REFRESH_TOKEN", token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path, help="Existing private .env.dev or .env to update")
    args = parser.parse_args()
    env_file = args.env_file
    try:
        if env_file.is_symlink() or not env_file.is_file():
            raise AuthorizationError("Select an existing regular private env file, not a symlink.")
        if stat.S_IMODE(env_file.stat().st_mode) & 0o077:
            raise AuthorizationError("Protect the selected env file with chmod 600 before authorization.")
        grant = request_device_code()
        deadline = time.monotonic() + float(grant["expires_in"])
        print("Use a separate burner YouTube account, NOT your main account; provider bans are possible.", flush=True)
        print(f"Open https://www.google.com/device and enter {grant['user_code']}", flush=True)
        print("Waiting for browser approval. Do not paste passwords or tokens here.", flush=True)
        token = wait_for_token(grant, deadline)
        save_token(env_file, token)
        print("Refresh token saved privately. Recreate the audio node and cipher together to apply it.")
        return 0
    except (AuthorizationError, OSError) as exc:
        print(f"Authorization failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Authorization cancelled; no token was saved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
