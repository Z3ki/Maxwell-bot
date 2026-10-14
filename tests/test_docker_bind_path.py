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


def test_rewrites_paths_under_the_data_volume(monkeypatch, tmp_path: Path):
    data = tmp_path / "app" / "data"
    nested = data / "site_servers" / "demo"
    nested.mkdir(parents=True)
    monkeypatch.setenv("MAXWELL_HOST_BIND", "/host/maxwell")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("MAXWELL_HOST_DATA_BIND", "/host/volumes/dev_data/_data")
    assert (
        docker_bind_path(nested, app_root=tmp_path / "app")
        == "/host/volumes/dev_data/_data/site_servers/demo"
    )
    assert docker_bind_path(data, app_root=tmp_path / "app") == "/host/volumes/dev_data/_data"


def test_data_volume_mapping_wins_over_app_root(monkeypatch, tmp_path: Path):
    data = tmp_path / "app" / "data"
    data.mkdir()
    monkeypatch.setenv("MAXWELL_HOST_BIND", "/host/maxwell")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("MAXWELL_HOST_DATA_BIND", "/host/volumes/dev_data/_data")
    assert docker_bind_path(data / "x", app_root=tmp_path / "app") == "/host/volumes/dev_data/_data/x"
    monkeypatch.delenv("MAXWELL_HOST_DATA_BIND")
    assert docker_bind_path(data / "x", app_root=tmp_path / "app") == "/host/maxwell/data/x"
