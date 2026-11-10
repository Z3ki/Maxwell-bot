"""Offline tests for guarded automatic failover. No network, Discord or systemd."""

from __future__ import annotations

import json
import os
import ssl
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from standby import failover, policy, primary_gate
from standby.failover import Config, Observation, State, decide

CFG = Config()


def obs(now, **kw):
    base = {
        "now": now,
        "self_ok": True,
        "direct_ok": False,
        "heartbeat_age": None,
        "heartbeat_bot_running": None,
        "standby_active": False,
        "lease_mode": None,
        "exit_status": None,
    }
    base.update(kw)
    return Observation(**base)


def run_outage(st, *, seconds, step=15, armed=True, external="down", **kw):
    """Feed continuous 'main down' observations; answer external checks."""
    decisions = []
    t = st.down_since or 1_000_000.0
    end = t + seconds
    while t <= end:
        d = decide(obs(t, **kw), st, CFG, armed)
        if d.action == "external_check":
            st.external_started_at = t
            st.external_result = external
            st.external_result_at = t
            d = decide(obs(t, **kw), st, CFG, armed)
        decisions.append((t, d))
        if d.action == "takeover":
            break
        t += step
    return decisions


def first(decisions, action):
    return next((t for t, d in decisions if d.action == action), None)


def test_takeover_only_after_continuous_threshold_and_external_confirmation():
    st = State()
    decisions = run_outage(st, seconds=600)
    start = decisions[0][0]
    taken = first(decisions, "takeover")
    assert taken is not None
    assert taken - start >= CFG.takeover_after_seconds
    assert all(d.action != "takeover" for t, d in decisions if t - start < CFG.takeover_after_seconds)


def test_manual_mode_never_takes_over():
    st = State()
    decisions = run_outage(st, seconds=3600, armed=False)
    assert first(decisions, "takeover") is None
    assert "manual" in decisions[-1][1].reason


def test_backup_network_blip_never_triggers_and_resets_window():
    st = State()
    t = 1_000_000.0
    # 170 s of apparent outage, then the backup's own internet fails briefly.
    for i in range(12):
        decide(obs(t + i * 15), st, CFG, True)
    d = decide(obs(t + 180, self_ok=False), st, CFG, True)
    assert d.action == "none" and st.down_since is None
    # Even a long period with self_ok False never takes over.
    for i in range(200):
        assert decide(obs(t + 200 + i * 15, self_ok=False), st, CFG, True).action == "none"


def test_any_direct_reachability_or_fresh_heartbeat_resets():
    for kw in ({"direct_ok": True}, {"heartbeat_age": 5.0, "heartbeat_bot_running": False}):
        st = State()
        t = 1_000_000.0
        for i in range(11):
            decide(obs(t + i * 15), st, CFG, True)
        assert decide(obs(t + 165, **kw), st, CFG, True).action == "none"
        assert st.down_since is None
        decisions = run_outage(st, seconds=170)
        assert first(decisions, "takeover") is None


def test_stale_heartbeat_does_not_count_as_alive():
    st = State()
    decisions = run_outage(st, seconds=600, heartbeat_age=CFG.heartbeat_stale_seconds + 1,
                           heartbeat_bot_running=True)
    assert first(decisions, "takeover") is not None


def test_external_observers_reaching_main_veto_takeover_partition():
    st = State()
    decisions = run_outage(st, seconds=600, external="up")
    assert first(decisions, "takeover") is None
    assert st.external_veto_until > 0


def test_external_unknown_never_takes_over_by_default():
    st = State()
    decisions = run_outage(st, seconds=7200, external="unknown")
    assert first(decisions, "takeover") is None


def test_optional_extended_fallback_only_when_configured(monkeypatch):
    monkeypatch.setattr(CFG, "external_fallback_after_seconds", 900.0)
    st = State()
    decisions = run_outage(st, seconds=960, external="unknown")
    taken = first(decisions, "takeover")
    assert taken is not None and taken - decisions[0][0] >= 900


def test_fresh_watchdog_process_waits_full_grace_window():
    st = State(down_since=1.0, external_result="down", external_result_at=2.0)
    d = decide(obs(500, uptime=5.0), st, CFG, True)
    assert d.action == "none" and st.down_since is None and "warming" in d.reason


def test_restart_keeps_only_clamped_blockers_never_outage_evidence(tmp_path):
    path = tmp_path / "state.json"
    st = State(down_since=50.0, external_result="down", external_result_at=60.0,
               cooldown_until=1100.0, last_claim_at=990.0, takeovers=[900.0])
    failover.save_state(path, st, mono=1000.0, wall=10_000.0)
    loaded = failover.load_state(path, mono=5.0, wall=10_000.0)
    assert loaded.down_since is None and loaded.external_result is None
    assert loaded.cooldown_until == pytest.approx(105.0)
    assert loaded.last_claim_at == pytest.approx(-5.0)
    assert loaded.takeovers == [pytest.approx(-95.0)]
    # A wall clock jump forward neither erases recent takeovers early beyond 24 h
    # semantics nor stretches cooldowns past their bound.
    far = failover.load_state(path, mono=5.0, wall=10_000.0 - 10**6)
    assert far.cooldown_until <= 5.0 + 3600


def test_cooldown_claim_hold_and_daily_limit_block_takeover():
    now = 1_000_000.0
    for st in (
        State(cooldown_until=now + 10_000),
        State(last_claim_at=now),
        State(takeovers=[now - 10, now - 20, now - 30]),
    ):
        st.down_since = None
        decisions = run_outage(st, seconds=400)
        assert first(decisions, "takeover") is None


def test_active_standby_steps_down_immediately_when_main_bot_runs():
    st = State()
    d = decide(obs(10, standby_active=True, lease_mode="automatic", heartbeat_age=2.0,
                   heartbeat_bot_running=True), st, CFG, True)
    assert d.action == "stepdown"


def test_active_standby_steps_down_after_main_reachable_threshold():
    st = State()
    t = 1000.0
    actions = [
        decide(obs(t + i * 15, standby_active=True, lease_mode="automatic", direct_ok=True),
               st, CFG, True).action
        for i in range(6)
    ]
    assert actions[:4] == ["none"] * 4 and "stepdown" in actions


def test_active_holds_when_main_host_up_but_bot_not_running():
    st = State()
    for i in range(40):
        d = decide(obs(1000 + i * 15, standby_active=True, lease_mode="automatic", direct_ok=True,
                       heartbeat_age=3.0, heartbeat_bot_running=False), st, CFG, True)
        assert d.action == "none"


def test_manual_and_observe_sessions_are_not_managed():
    st = State()
    for mode in (None, "observe_only"):
        d = decide(obs(1, standby_active=True, lease_mode=mode, heartbeat_age=1.0,
                       heartbeat_bot_running=True), st, CFG, True)
        assert d.action == "none"


def test_conflict_exit_cleans_up_with_long_cooldown():
    st = State()
    d = decide(obs(100, lease_mode="automatic", exit_status=failover.CONFLICT_EXIT), st, CFG, True)
    assert d.action == "cleanup"
    assert st.cooldown_until >= 100 + CFG.conflict_cooldown_seconds


def test_disarm_with_mode_file(tmp_path):
    mode = tmp_path / "mode"
    assert not failover.armed(mode)
    mode.write_text("auto\n")
    assert failover.armed(mode)
    for text in ("manual", "AUTO", "auto please", ""):
        mode.write_text(text)
        assert not failover.armed(mode)


def test_config_rejects_unknown_or_unsafe_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"takeover_after_seconds": 240}))
    assert Config.load(path).takeover_after_seconds == 240
    for bad in ({"nope": 1}, {"tick_seconds": -1}, {"tick_seconds": True},
                {"heartbeat_stale_seconds": 10}, {"anchors": []}):
        path.write_text(json.dumps(bad))
        with pytest.raises(policy.PolicyDenied):
            Config.load(path)


@pytest.mark.parametrize(
    "results,expected",
    [
        ([{"a": [{"address": "1", "time": 0.1}], "b": [{"error": "timeout"}]}], "up"),
        ([{"a": [{"error": "timeout"}], "b": [{"error": "No route"}]}], "down"),
        ([{"a": [{"error": "timeout"}], "b": None}], "unknown"),
        ([{"a": [{"error": "t"}], "b": [{"error": "t"}], "c": None}], "unknown"),
        ([{"a": [{"error": "t"}]}], "unknown"),
        ([{"a": None, "b": None}], "unknown"),
        (["garbage", {"a": "x"}], "unknown"),
    ],
)
def test_classify_external(results, expected):
    assert failover.classify_external(results) == expected


def test_external_check_failures_are_unknown_never_down():
    def broken(url):
        raise OSError("no observer")

    assert failover.external_check(CFG, fetch=broken, poll=0) == "unknown"

    def fake(url):
        if "check-tcp" in url:
            return {"request_id": "r1"}
        return {n: [{"error": "Connection timed out"}] for n in CFG.external_nodes}

    assert failover.external_check(CFG, fetch=fake, poll=0) == "down"


# ---------------------------------------------------------------- actuator


class FakeSystemd:
    def __init__(self, active=False, start_ok=True):
        self.active, self.start_ok, self.calls = active, start_ok, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1])
        if argv[1] == "show":
            out = (f"ActiveState={'active' if self.active else 'inactive'}\n"
                   f"MainPID={'42' if self.active else '0'}\nExecMainStatus=0\n")
            return SimpleNamespace(returncode=0, stdout=out)
        if argv[1] == "start":
            self.active = self.start_ok
            return SimpleNamespace(returncode=0 if self.start_ok else 1, stdout="")
        if argv[1] == "stop":
            self.active = False
        return SimpleNamespace(returncode=0, stdout="")


def paths(tmp_path, armed=True):
    p = failover.ActuatorPaths(
        root_run=tmp_path / "root", watch_run=tmp_path / "watch", state=tmp_path / "state",
        standby_run=tmp_path / "standby-run", standby_etc=tmp_path / "etc",
        mode=tmp_path / "mode", config=tmp_path / "config.json",
    )
    for d in (p.root_run, p.watch_run):
        d.mkdir()
    if armed:
        p.mode.write_text("auto")
    return p


def request(action="takeover", **kw):
    import time
    import uuid

    return {"id": uuid.uuid4().hex, "action": action, "created_at": time.time(), "reason": "t", **kw}


def test_actuator_takeover_writes_valid_lease_and_starts(tmp_path):
    p = paths(tmp_path)
    systemd = FakeSystemd()
    result = failover.handle_request(p, request(), systemd, __import__("time").time())
    # Inject a passing offline validation for the real takeover call.
    assert result["ok"] is False  # default preflight fails without credentials
    result = failover.takeover(p, request(), systemd, validate=lambda root: None, settle=lambda s: None)
    assert "automatic" in result and systemd.active
    record = json.loads((p.standby_run / "activation.json").read_text())
    assert policy.auto_lease_record(record)["mode"] == "automatic"
    assert json.loads((p.root_run / "lease.json").read_text())["lease_id"] == record["lease_id"]
    assert len(json.loads((p.state / "history.json").read_text())) == 1


def test_actuator_refuses_when_disarmed_running_or_leased(tmp_path):
    p = paths(tmp_path, armed=False)
    with pytest.raises(policy.PolicyDenied, match="off"):
        failover.takeover(p, request(), FakeSystemd(), validate=lambda root: None, settle=lambda s: None)
    p.mode.write_text("auto")
    with pytest.raises(policy.PolicyDenied, match="running"):
        failover.takeover(p, request(), FakeSystemd(active=True), validate=lambda root: None, settle=lambda s: None)
    (p.root_run / "lease.json").write_text("{}")
    with pytest.raises(policy.PolicyDenied, match="lease"):
        failover.takeover(p, request(), FakeSystemd(), validate=lambda root: None, settle=lambda s: None)


def test_actuator_daily_limit_is_enforced_root_side(tmp_path):
    import time

    p = paths(tmp_path)
    p.state.mkdir()
    (p.state / "history.json").write_text(json.dumps([time.time() - 5] * 3))
    with pytest.raises(policy.PolicyDenied, match="limit"):
        failover.takeover(p, request(), FakeSystemd(), validate=lambda root: None, settle=lambda s: None)
    # Observe-only tests never send, so they are not limited and need no arming.
    p.mode.write_text("manual")
    assert "observe_only" in failover.takeover(p, request(observe_only=True), FakeSystemd(),
                                               validate=lambda root: None, settle=lambda s: None)


def test_failed_start_clears_lease(tmp_path):
    p = paths(tmp_path)
    with pytest.raises(policy.PolicyDenied, match="failed to start"):
        failover.takeover(p, request(), FakeSystemd(start_ok=False), validate=lambda root: None, settle=lambda s: None)
    assert not (p.root_run / "lease.json").exists()
    assert not (p.standby_run / "activation.json").exists()


def test_stepdown_stops_first_and_never_touches_manual_fence(tmp_path):
    p = paths(tmp_path)
    systemd = FakeSystemd()
    failover.takeover(p, request(), systemd, validate=lambda root: None, settle=lambda s: None)
    assert failover.stepdown(p, systemd).startswith("standby stopped")
    assert systemd.calls.index("stop") > systemd.calls.index("start")
    assert not (p.root_run / "lease.json").exists()
    manual = p.standby_run / "owner.json"
    manual.write_text(json.dumps({"state": "manually_fenced"}))
    manual.chmod(0o600)
    systemd = FakeSystemd(active=True)
    with pytest.raises(policy.PolicyDenied, match="manual"):
        failover.stepdown(p, systemd)
    assert "stop" not in systemd.calls and systemd.active


def test_stale_or_malformed_requests_rejected(tmp_path):
    p = paths(tmp_path)
    old = request()
    old["created_at"] -= 600
    assert failover.handle_request(p, old, FakeSystemd(), __import__("time").time())["ok"] is False
    bad = request(action="shell")
    assert failover.handle_request(p, bad, FakeSystemd(), __import__("time").time())["ok"] is False
    bad = request()
    bad["id"] = "../../etc"
    assert failover.handle_request(p, bad, FakeSystemd(), __import__("time").time())["ok"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"lease_id": "x" * 32},
        {"mode": "full"},
        {"probation_seconds": 500},
        {"observe_seconds": 5},
        {"takeover_at": "now"},
    ],
)
def test_malformed_auto_lease_never_activates(tmp_path, change):
    record = {"state": "auto_lease", "lease_id": "a" * 32, "mode": "automatic",
              "takeover_at": 1.0, "probation_seconds": 30, "observe_seconds": 90, **change}
    path = tmp_path / "activation"
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    with pytest.raises(policy.PolicyDenied):
        policy.activation_record(path)


def test_preflight_auto_mode_keeps_last_good_snapshot(tmp_path, monkeypatch):
    from standby import preflight, sync

    controls = {"bot_enabled": True, "reply_mentions": True, "ignore_users": [],
                "allowed_channels": [], "blocked_channels": [], "guild_solo_channel": {},
                "base_personality": ""}
    files = {
        "provider.json": {"base_url": "https://openrouter.ai/api/v1", "model": "m", "api_key": "k"},
        "discord-token": "t",
        "chat-config.json": {"schema": 1, "revision": "r", "exported_at": 1000,
                             "sha256": sync.digest(controls), "controls": controls},
        "audience.env": "MAXWELL_STANDBY_CHANNEL_IDS=10",
    }
    for name, value in files.items():
        (tmp_path / name).write_text(value if isinstance(value, str) else json.dumps(value))
        (tmp_path / name).chmod(0o600)
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda name: object())
    with pytest.raises(policy.PolicyDenied, match="15 minute"):
        preflight.validate(tmp_path, now=100_000)
    preflight.validate(tmp_path, now=100_000, max_age=None)


# ----------------------------------------------------------- primary gate


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def call(self, op, bot_running, timeout=45):
        self.calls.append((op, bot_running))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def gate(tmp_path, client, **kw):
    d = tmp_path / "standby-failover"
    d.mkdir(exist_ok=True)
    (d / "client.json").write_text("{}")
    clock = iter(range(0, 10_000, 3))
    return primary_gate.startup_gate(tmp_path, client_factory=lambda _: client, sleep=lambda s: None,
                                     clock=lambda: next(clock), **kw)


def test_gate_noop_when_not_configured(tmp_path):
    assert primary_gate.startup_gate(tmp_path) == "not configured"


def test_gate_waits_for_backup_to_confirm_stepdown(tmp_path):
    client = FakeClient([
        {"ok": True, "standby_active": True, "stepped_down": False},
        {"ok": True, "standby_active": False, "stepped_down": True},
    ])
    assert gate(tmp_path, client) == "backup inactive"
    assert client.calls == [("handback", True), ("handback", True)]


def test_gate_unreachable_backup_never_blocks_primary_long(tmp_path):
    client = FakeClient([OSError("down"), TimeoutError("slow"), OSError("x")])
    assert "unreachable" in gate(tmp_path, client)
    assert len(client.calls) == primary_gate.UNREACHABLE_ATTEMPTS


def test_gate_bounded_when_backup_refuses(tmp_path):
    client = FakeClient([{"ok": True, "standby_active": True, "stepped_down": False}] * 200)
    assert "deadline" in gate(tmp_path, client)


def test_gate_requires_explicit_stepdown_confirmation(tmp_path):
    client = FakeClient([{"ok": True, "standby_active": False}] * 200)
    assert "deadline" in gate(tmp_path, client)


def test_gate_fifo_certificate_cannot_block(tmp_path):
    d = tmp_path / "standby-failover"
    d.mkdir()
    for name, value in (("client.json", '{"host": "127.0.0.1", "port": 9}'), ("key", "k")):
        (d / name).write_text(value)
        (d / name).chmod(0o600)
    os.mkfifo(d / "server.crt", 0o600)
    assert primary_gate.startup_gate(tmp_path, deadline=1.0) == "misconfigured"


def test_gate_misconfigured_never_raises(tmp_path):
    d = tmp_path / "standby-failover"
    d.mkdir()
    (d / "client.json").write_text("not json")
    (d / "client.json").chmod(0o600)
    assert primary_gate.startup_gate(tmp_path) == "misconfigured"


def test_guard_requests_handback_only_for_live_automatic_overlap():
    client = FakeClient([{"ok": True, "standby_active": True, "lease_mode": "automatic"},
                         {"ok": True, "stepped_down": True}])
    out = primary_gate.guard_tick(client, bot_running=lambda: True)
    assert out["handback"]["stepped_down"] and [c[0] for c in client.calls] == ["heartbeat", "handback"]
    for running, mode in ((False, "automatic"), (True, "observe_only"), (True, None)):
        client = FakeClient([{"ok": True, "standby_active": True, "lease_mode": mode}])
        assert primary_gate.guard_tick(client, bot_running=lambda r=running: r)["handback"] is None


def test_container_state_parsing():
    def runner(state, code=0):
        return lambda *a, **k: SimpleNamespace(returncode=code, stdout=json.dumps(state))

    assert primary_gate.container_bot_running(runner({"Running": True, "Health": {"Status": "healthy"}}))
    assert not primary_gate.container_bot_running(runner({"Running": True, "Health": {"Status": "unhealthy"}}))
    assert not primary_gate.container_bot_running(runner({"Running": False}))
    assert not primary_gate.container_bot_running(runner({}, code=1))


def test_primary_awareness_mentions_automatic_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_STANDBY_CONFIGURED", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert "Automatic failover is NOT enabled" in policy.primary_context()
    (tmp_path / "standby-failover").mkdir()
    (tmp_path / "standby-failover" / "client.json").write_text("{}")
    text = policy.primary_context()
    assert "automatic takeover" in text and "Do not claim that standby is active" in text


# ------------------------------------------------- real TLS listener roundtrip


def _make_cert(tmp_path):
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
         str(cert), "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )
    return cert, key


def test_listener_drops_non_primary_peers(tmp_path):
    cert, key = _make_cert(tmp_path)
    watchdog = failover.Watchdog(CFG, key="k", prober=SimpleNamespace(), watch_run=tmp_path,
                                 root_run=tmp_path, state_path=tmp_path / "s.json",
                                 mode_path=tmp_path / "mode", runner=FakeSystemd())
    server = failover.serve(watchdog, cert, key, 0, allowed=("192.0.2.9",))
    try:
        import socket

        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=3) as sock:
            sock.settimeout(3)
            assert sock.recv(1) == b""  # closed before TLS
    finally:
        server.shutdown()


def test_listener_auth_heartbeat_and_handback_roundtrip(tmp_path):
    cert, key = _make_cert(tmp_path)
    (tmp_path / "root").mkdir()
    (tmp_path / "watch").mkdir()
    watchdog = failover.Watchdog(
        CFG, key="secret-key", prober=SimpleNamespace(), watch_run=tmp_path / "watch",
        root_run=tmp_path / "root", state_path=tmp_path / "state.json", mode_path=tmp_path / "mode",
        runner=FakeSystemd(),
    )
    server = failover.serve(watchdog, cert, key, 0, allowed=("127.0.0.1",))
    port = server.server_address[1]
    client_dir = tmp_path / "client"
    client_dir.mkdir()
    (client_dir / "client.json").write_text(json.dumps({"host": "127.0.0.1", "port": port}))
    (client_dir / "key").write_text("secret-key")
    (client_dir / "server.crt").write_text(cert.read_text())
    for name in ("client.json", "key", "server.crt"):
        (client_dir / name).chmod(0o600)
    try:
        client = primary_gate.FailoverClient(client_dir)
        answer = client.call("heartbeat", True, timeout=5)
        assert answer["standby_active"] is False and watchdog.heartbeat_bot_running is True
        assert client.call("handback", True, timeout=5)["stepped_down"] is True
        assert watchdog.state.last_claim_at > 0
        (client_dir / "key").write_text("wrong")
        with pytest.raises(primary_gate.GateError):
            client2 = primary_gate.FailoverClient(client_dir)
            client2.call("status", True, timeout=5)
        # A different certificate is rejected even if it were trusted.
        (tmp_path / "o").mkdir()
        other, _ = _make_cert(tmp_path / "o")
        (client_dir / "key").write_text("secret-key")
        (client_dir / "server.crt").write_text(other.read_text())
        (client_dir / "server.crt").chmod(0o600)
        with pytest.raises((primary_gate.GateError, ssl.SSLError)):
            primary_gate.FailoverClient(client_dir).call("status", True, timeout=5)
    finally:
        server.shutdown()


def test_units_keep_watchdog_unprivileged_and_standby_static():
    root = Path(__file__).resolve().parents[1] / "standby"
    watch = (root / "maxwell-failover.service").read_text()
    assert "User=maxwell-failover" in watch and "CapabilityBoundingSet=\n" in watch
    standby = (root / "maxwell-standby.service").read_text()
    assert "Restart=no" in standby and "[Install]" not in standby
    assert "MemoryMax=512M" in standby and "User=maxwell-standby" in standby
    actuator = (root / "maxwell-failover-actuator.service").read_text()
    assert "[Install]" not in actuator  # only the path unit starts it


# ------------------------------------------------------ runtime split-brain


def _runtime_env(tmp_path, monkeypatch, lease):
    import sys  # noqa: F401
    from standby import sync

    def private(name, value):
        (tmp_path / name).write_text(value if isinstance(value, str) else json.dumps(value))
        (tmp_path / name).chmod(0o600)

    controls = {"bot_enabled": True, "reply_mentions": True, "ignore_users": [],
                "allowed_channels": [], "blocked_channels": [], "guild_solo_channel": {},
                "base_personality": ""}
    private("activation", lease)
    private("discord-token", "offline-token")
    private("provider", {"base_url": "https://openrouter.ai/api/v1", "api_key": "k", "model": "m"})
    private("chat-config", {"schema": 1, "revision": "t", "exported_at": 1000,
                            "sha256": sync.digest(controls), "controls": controls})
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("MAXWELL_DEPLOYMENT_MODE", "standby")
    monkeypatch.setenv("MAXWELL_STANDBY_CHANNEL_IDS", "10")


def _run_fake(script, monkeypatch):
    """Run runtime.run() with fake discord/aiohttp; ``script(client)`` drives events."""
    import asyncio
    import sys
    from unittest.mock import patch

    from standby import runtime

    sends, posts = [], []
    me = SimpleNamespace(id=555)

    class Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session(Ctx):
        def __init__(self, **kw):
            pass

        def post(self, url, **kw):
            posts.append(url)
            raise AssertionError("provider must not be called in these tests")

    class Client(Ctx):
        def __init__(self, **kw):
            self.user = me
            self.http = SimpleNamespace(request=self.request)
            self.closed = False

        async def request(self, route, **kw):
            sends.append(kw["json"])
            return {"id": str(9000 + len(sends))}

        async def close(self):
            self.closed = True

        async def start(self, token, **kw):
            assert kw == {"reconnect": False}
            await self.setup_hook()
            await script(self)

    discord = SimpleNamespace(
        Client=Client, Intents=SimpleNamespace(none=lambda: SimpleNamespace()),
        AllowedMentions=SimpleNamespace(none=lambda: None),
        http=SimpleNamespace(Route=lambda *a, **kw: (*a, kw)),
    )
    aiohttp = SimpleNamespace(ClientSession=Session, ClientTimeout=lambda **kw: None,
                              TCPConnector=lambda **kw: None)
    with patch.dict(sys.modules, {"discord": discord, "aiohttp": aiohttp}):
        code = asyncio.run(runtime.run())
    return code, sends, posts, me


def _msg(author_id, *, bot=False, nonce=None, mid=1, mention=None):
    return SimpleNamespace(
        id=mid, nonce=nonce, author=SimpleNamespace(id=author_id, bot=bot),
        channel=SimpleNamespace(id=10), guild=SimpleNamespace(id=7),
        mentions=[mention] if mention else [], attachments=[], stickers=[], content="hello",
    )


LEASE = {"state": "auto_lease", "lease_id": "b" * 32, "mode": "automatic", "takeover_at": 1.0,
         "probation_seconds": 0, "observe_seconds": 90}


def test_runtime_exits_with_conflict_when_primary_message_seen(tmp_path, monkeypatch):
    import asyncio

    _runtime_env(tmp_path, monkeypatch, LEASE)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep(asyncio.sleep))

    async def script(client):
        await client.on_ready()
        # A message from our own bot account that we never sent => live primary.
        await client.on_message(_msg(555, bot=True, nonce="123", mid=77))
        await _drain()

    code, sends, _, _ = _run_fake(script, monkeypatch)
    assert code == failover.CONFLICT_EXIT and not sends


def test_runtime_recognises_its_own_messages_by_nonce(tmp_path, monkeypatch):
    import asyncio

    _runtime_env(tmp_path, monkeypatch, LEASE)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep(asyncio.sleep))

    async def script(client):
        await client.on_ready()
        await client.send_text(10, "hi")
        nonce = sends_holder[0]["nonce"]
        await client.on_message(_msg(555, bot=True, nonce=nonce, mid=12345))
        await _drain()

    sends_holder = []
    original = _run_fake

    def run(script_, mp):
        return original(script_, mp)

    # Capture sends via closure on the fake client's request list.
    async def wrapped(client):
        orig = client.http.request

        async def req(route, **kw):
            sends_holder.append(kw["json"])
            return await orig(route, **kw)

        client.http.request = req
        await script(client)

    code, sends, _, _ = run(wrapped, monkeypatch)
    assert code == 0 and len(sends) == 1


def test_probation_and_observe_only_never_reply(tmp_path, monkeypatch):
    for lease in ({**LEASE, "probation_seconds": 60},
                  {**LEASE, "mode": "observe_only", "observe_seconds": 10}):
        _runtime_env(tmp_path, monkeypatch, lease)
        state: dict = {}

        async def script(client, lease=lease, state=state):
            await client.on_ready()
            await client.on_message(_msg(1, mention=client.user))
            interaction = SimpleNamespace(guild_id=7, channel_id=10, user=SimpleNamespace(id=1),
                                          response=SimpleNamespace(is_done=lambda: False,
                                                                   send_message=_fail))
            await client.on_interaction(interaction)
            if lease["mode"] == "observe_only":
                await client.on_message(_msg(555, bot=True, mid=5))
                with pytest.raises(policy.PolicyDenied):
                    await client.send_text(10, "x")
            state["ok"] = True

        code, sends, posts, _ = _run_fake(script, monkeypatch)
        assert state["ok"] and code == 0 and not sends and not posts


async def _fail(*a, **k):
    raise AssertionError("must not respond")


async def _drain():
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)


def _instant_sleep(real):
    async def sleep(delay, *a, **k):
        return await real(0)

    return sleep


def test_systemd_credential_mode_0440_only_inside_credentials_directory(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    for directory in (creds, tmp_path):
        (directory / "c").write_text("v")
        (directory / "c").chmod(0o440)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    assert policy.read_credential(creds / "c") == "v"
    with pytest.raises(policy.PolicyDenied):
        policy.read_credential(tmp_path / "c")
    for mode in (0o460, 0o444, 0o442):
        (creds / "c").chmod(mode)
        with pytest.raises(policy.PolicyDenied):
            policy.read_credential(creds / "c")


def test_suspected_primary_message_silences_standby_immediately(tmp_path, monkeypatch):
    import asyncio

    _runtime_env(tmp_path, monkeypatch, LEASE)
    seen = {}

    async def script(client):
        await client.on_ready()
        await client.on_message(_msg(555, bot=True, nonce="1", mid=99))
        await asyncio.sleep(0)  # detector task starts; confirmation still pending
        seen["quiet"] = client.quiet()
        with pytest.raises(policy.PolicyDenied):
            await client.send_text(10, "x")

    code, sends, _, _ = _run_fake(script, monkeypatch)
    assert seen["quiet"] is True and not sends
