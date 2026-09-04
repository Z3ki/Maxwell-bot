"""site_test must not lie about a page, and must not leave a browser behind.

Two production failures drive these tests:

  - Sites were listed as live while serving nothing but "Initializing…". A 200
    with a clean console was reported as a pass, so the bot told people the
    site worked.
  - Browser profiles accumulated to 68 MB because the snap Chromium remaps
    ``/tmp``: the profile was written somewhere else and the cleanup deleted an
    empty directory.
"""

import asyncio
import os

import pytest

import site_test


# --------------------------------------------------------------------------
# stub detection
# --------------------------------------------------------------------------


def _probe(**over):
    base = {
        "url": "https://example.test/bot/x/",
        "browser": "chromium-browser",
        "http_status": 200,
        "console_errors": [],
        "page_errors": [],
        "failed_requests": [],
        "asset_errors": [],
        "visible_text": "A real page with plenty of readable prose on it.",
        "rendered_nodes": 40,
        "has_canvas_or_media": False,
    }
    base.update(over)
    return base


def test_a_real_page_is_not_called_a_stub():
    assert site_test.describe_stub(_probe()) == ""


def test_a_loading_placeholder_is_a_stub():
    why = site_test.describe_stub(
        _probe(visible_text="Loading SLAM '88...", rendered_nodes=1)
    )
    assert "loading" in why
    assert "never rendered" in why


def test_an_initializing_shell_is_a_stub():
    why = site_test.describe_stub(_probe(visible_text="Initializing", rendered_nodes=1))
    assert "initializing" in why


def test_an_empty_body_is_a_stub():
    why = site_test.describe_stub(_probe(visible_text="", rendered_nodes=0))
    assert "no visible text" in why


def test_a_canvas_game_with_little_text_is_not_a_stub():
    """A drawn game legitimately has almost no prose."""
    assert (
        site_test.describe_stub(
            _probe(visible_text="Score 0", rendered_nodes=3, has_canvas_or_media=True)
        )
        == ""
    )


def test_a_page_with_few_nodes_but_real_text_is_not_a_stub():
    assert (
        site_test.describe_stub(
            _probe(
                visible_text=(
                    "This is a short single-paragraph page, which is a legitimate "
                    "thing to publish and must not be flagged as broken."
                ),
                rendered_nodes=2,
            )
        )
        == ""
    )


def test_no_browser_means_no_stub_verdict():
    """Without a render there is nothing to judge; guessing would flag every SPA."""
    assert site_test.describe_stub(_probe(browser=None, visible_text="")) == ""
    assert (
        site_test.describe_stub(_probe(browser_error="not installed", visible_text=""))
        == ""
    )


def test_missing_render_data_means_no_stub_verdict():
    probe = _probe()
    probe.pop("visible_text")
    assert site_test.describe_stub(probe) == ""


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def test_report_counts_a_stub_as_a_problem():
    report = site_test.format_report(
        _probe(visible_text="Initializing", rendered_nodes=1)
    )
    assert "NOT ACTUALLY RENDERED" in report
    assert "1 problem(s)" in report
    assert "Do not tell the user it works" in report


def test_report_passes_a_healthy_page():
    report = site_test.format_report(_probe())
    assert "RESULT: page loaded with no console errors" in report
    assert "NOT ACTUALLY RENDERED" not in report


def test_report_still_counts_console_errors():
    report = site_test.format_report(_probe(console_errors=["boom", "bang"]))
    assert "Console errors:" in report
    assert "2 problem(s)" in report


def test_report_adds_the_stub_on_top_of_other_problems():
    report = site_test.format_report(
        _probe(
            console_errors=["boom"],
            visible_text="Loading...",
            rendered_nodes=1,
        )
    )
    assert "3 problem(s)" in report or "2 problem(s)" in report
    assert "NOT ACTUALLY RENDERED" in report


# --------------------------------------------------------------------------
# browser profiles
# --------------------------------------------------------------------------


def test_profiles_live_outside_tmp():
    """Snap Chromium remaps /tmp, so a profile there is written elsewhere and
    the cleanup silently deletes an empty directory."""
    if os.path.abspath(os.path.dirname(site_test.__file__)).startswith("/tmp/"):
        pytest.skip("repository itself is checked out under /tmp")
    root = site_test.profile_root()
    assert not root.startswith("/tmp/")
    assert site_test._PROFILE_DIRNAME in root


def test_new_profile_dir_is_created_under_the_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = site_test._new_profile_dir()
    assert os.path.isdir(path)
    assert path.startswith(str(tmp_path))
    assert os.path.basename(path).startswith("probe-")


def test_sweep_removes_stale_profiles_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    old = site_test._new_profile_dir()
    # Distinct name: _new_profile_dir keys on pid+ms, so two calls in the same
    # millisecond would otherwise be the same directory.
    fresh = os.path.join(site_test.profile_root(), "probe-fresh")
    os.makedirs(fresh, exist_ok=True)
    os.utime(old, (0, 0))  # ancient
    removed = site_test.sweep_browser_profiles(max_age=60.0)
    assert removed == 1
    assert not os.path.isdir(old)
    assert os.path.isdir(fresh)


def test_sweep_ignores_unrelated_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    root = site_test.profile_root()
    os.makedirs(root, exist_ok=True)
    keep = os.path.join(root, "not-a-probe")
    os.makedirs(keep, exist_ok=True)
    os.utime(keep, (0, 0))
    assert site_test.sweep_browser_profiles(max_age=0.0) == 0
    assert os.path.isdir(keep)


def test_sweep_on_a_missing_root_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "nope"))
    assert site_test.sweep_browser_profiles() == 0


def test_kill_proc_tolerates_a_dead_process():
    async def run():
        await site_test._kill_proc(None)

    asyncio.run(run())


# --------------------------------------------------------------------------
# engine discovery
# --------------------------------------------------------------------------


def test_find_obscura_honours_the_env_override(tmp_path, monkeypatch):
    binary = tmp_path / "obscura"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("MAXWELL_OBSCURA_BIN", str(binary))
    assert site_test.find_obscura() == str(binary)


def test_find_obscura_ignores_a_bad_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MAXWELL_OBSCURA_BIN", str(tmp_path / "missing"))
    # Falls through to PATH, which on a box without obscura is None.
    assert site_test.find_obscura() in (
        None,
        site_test._which_first(site_test.OBSCURA_CANDIDATES),
    )


def test_probe_reports_both_engines_when_neither_exists(monkeypatch):
    monkeypatch.setattr(site_test, "find_chrome", lambda: None)
    monkeypatch.setattr(site_test, "find_obscura", lambda: None)

    async def run():
        return await site_test.probe_browser("https://example.test/", screenshot=False)

    result = asyncio.run(run())
    assert "chromium" in result["browser_error"]
    assert "obscura" in result["browser_error"]


def test_probe_falls_back_to_obscura_when_chromium_fails(monkeypatch):
    monkeypatch.setattr(site_test, "find_chrome", lambda: "/bin/false")
    monkeypatch.setattr(site_test, "find_obscura", lambda: "/bin/obscura")

    async def fake_chromium(url, *, binary, wait, screenshot):
        return {"browser": "chromium", "browser_error": "would not start"}

    async def fake_obscura(url, *, binary, wait, screenshot):
        return {"browser": "obscura", "title": "worked"}

    monkeypatch.setattr(site_test, "_probe_with_chromium", fake_chromium)
    monkeypatch.setattr(site_test, "_probe_with_obscura", fake_obscura)

    async def run():
        return await site_test.probe_browser("https://example.test/", screenshot=False)

    result = asyncio.run(run())
    assert result["browser"] == "obscura"
    assert "browser_error" not in result


def test_probe_prefers_chromium_when_it_works(monkeypatch):
    monkeypatch.setattr(site_test, "find_chrome", lambda: "/bin/chromium")
    called = []

    async def fake_chromium(url, *, binary, wait, screenshot):
        return {"browser": "chromium", "title": "fine"}

    async def fake_obscura(url, *, binary, wait, screenshot):
        called.append(url)
        return {"browser": "obscura"}

    monkeypatch.setattr(site_test, "_probe_with_chromium", fake_chromium)
    monkeypatch.setattr(site_test, "_probe_with_obscura", fake_obscura)

    async def run():
        return await site_test.probe_browser("https://example.test/", screenshot=False)

    result = asyncio.run(run())
    assert result["browser"] == "chromium"
    assert called == []


@pytest.mark.parametrize("engine", ["chromium", "obscura"])
def test_console_shim_and_render_probe_are_valid_javascript_shape(engine):
    """Cheap guard: the injected sources must at least be balanced."""
    for source in (site_test._CONSOLE_SHIM, site_test._RENDER_PROBE):
        assert source.count("(") == source.count(")")
        assert source.count("{") == source.count("}")
        assert "__maxwellProbe" in source
