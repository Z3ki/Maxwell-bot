"""docker_bind_path: rewrite in-container paths for the host daemon."""

from pathlib import Path

from utils import docker_bind_path


def test_passthrough_without_host_bind(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MAXWELL_HOST_BIND", raising=False)
    target = tmp_path / "shelldocker"
    target.mkdir()
    assert docker_bind_path(target) == str(target.resolve())


def test_rewrites_paths_under_app_root(monkeypatch, tmp_path: Path):
    app = tmp_path / "app"
    nested = app / "shelldocker" / "out.png"
    nested.parent.mkdir(parents=True)
    nested.write_text("x")
    monkeypatch.setenv("MAXWELL_HOST_BIND", "/host/maxwell")
    got = docker_bind_path(nested, app_root=app)
    assert got == "/host/maxwell/shelldocker/out.png"


def test_app_root_itself_maps_to_host_bind(monkeypatch, tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setenv("MAXWELL_HOST_BIND", "/host/maxwell")
    assert docker_bind_path(app, app_root=app) == "/host/maxwell"


def test_paths_outside_app_root_are_unchanged(monkeypatch, tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    other = tmp_path / "elsewhere" / "file"
    other.parent.mkdir()
    other.write_text("x")
    monkeypatch.setenv("MAXWELL_HOST_BIND", "/host/maxwell")
    assert docker_bind_path(other, app_root=app) == str(other.resolve())
