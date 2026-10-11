"""Real HTTP OAuth error bodies, finite polling and private credential persistence."""
import json
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from scripts import youtube_auth as auth


@pytest.fixture
def authorization_server(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(auth, "time", SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))
    state = SimpleNamespace(replies=[], token_requests=[], requests=[], device_lifetime=30, redirect=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            state.requests.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            if state.redirect:
                self.send_response(307)
                self.send_header("Location", "/credential-sink")
                self.end_headers()
                return
            if self.path == "/device":
                response = {"device_code": "private-device-grant", "user_code": "TEST-CODE",
                            "expires_in": state.device_lifetime, "interval": 5}
                status = 200
            else:
                state.token_requests.append(clock.now)
                response = state.replies.pop(0) if state.replies else {"error": "authorization_pending"}
                status = 400 if "error" in response else 200
            payload = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(auth, "DEVICE_URL", endpoint + "/device")
    monkeypatch.setattr(auth, "TOKEN_URL", endpoint + "/token")
    try:
        yield state, clock
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def private_env(tmp_path, monkeypatch):
    path = tmp_path / ".env.dev"
    path.write_text("# keep this\nDISCORD_BOT_TOKEN=unchanged\nYOUTUBE_OAUTH_REFRESH_TOKEN=old\n")
    path.chmod(0o600)
    monkeypatch.setattr(sys, "argv", ["youtube_auth", "--env-file", str(path)])
    return path


def test_pending_and_slow_down_save_only_approved_token_privately(
    authorization_server, tmp_path, monkeypatch, capsys,
):
    state, _ = authorization_server
    state.replies = [{"error": "authorization_pending"}, {"error": "slow_down"},
                     {"refresh_token": "new-private-refresh-credential"}]
    path = private_env(tmp_path, monkeypatch)
    assert auth.main() == 0
    assert state.token_requests == [5, 10, 20]
    assert path.read_text() == (
        "# keep this\nDISCORD_BOT_TOKEN=unchanged\n"
        "YOUTUBE_OAUTH_REFRESH_TOKEN=new-private-refresh-credential\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    output = capsys.readouterr()
    assert "new-private-refresh-credential" not in output.out + output.err
    assert "private-device-grant" not in output.out + output.err


@pytest.mark.parametrize("failure", ["access_denied", "expired_token"])
def test_terminal_google_errors_do_not_retry_or_replace_existing_credentials(
    authorization_server, tmp_path, monkeypatch, capsys, failure,
):
    state, _ = authorization_server
    state.replies = [{"error": failure}]
    path = private_env(tmp_path, monkeypatch)
    original = path.read_bytes()
    assert auth.main() == 1
    assert state.token_requests == [5]
    assert path.read_bytes() == original
    expected = 'denied' if failure == 'access_denied' else 'expired'
    assert expected in capsys.readouterr().err


def test_unapproved_code_stops_at_deadline_without_flooding(
    authorization_server, tmp_path, monkeypatch, capsys,
):
    state, clock = authorization_server
    state.device_lifetime = 12
    path = private_env(tmp_path, monkeypatch)
    original = path.read_bytes()
    assert auth.main() == 1
    assert state.token_requests == [5, 10]
    assert path.read_bytes() == original
    assert clock.now == 12
    assert "expired" in capsys.readouterr().err


def test_credentials_are_not_forwarded_on_redirect(
    authorization_server, tmp_path, monkeypatch, capsys,
):
    state, _ = authorization_server
    state.redirect = True
    path = private_env(tmp_path, monkeypatch)
    original = path.read_bytes()
    assert auth.main() == 1
    assert '/credential-sink' not in state.requests
    assert path.read_bytes() == original
    assert "redirected" in capsys.readouterr().err


def test_insecure_env_is_rejected_before_contacting_google(
    authorization_server, tmp_path, monkeypatch, capsys,
):
    state, _ = authorization_server
    path = private_env(tmp_path, monkeypatch)
    path.chmod(0o644)
    original = path.read_bytes()
    assert auth.main() == 1
    assert not state.requests
    assert path.read_bytes() == original
    assert "chmod 600" in capsys.readouterr().err
