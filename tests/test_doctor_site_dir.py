"""doctor.site_dir_is_host_visible: create_site vs Caddy bind-mount."""

from pathlib import Path

from doctor import site_dir_is_host_visible


def _mountinfo(*rows: str) -> str:
    return "\n".join(rows) + "\n"


def test_outside_docker_is_always_visible(tmp_path: Path):
    site = tmp_path / "bot"
    site.mkdir()
    assert site_dir_is_host_visible(
        site, in_docker=False, app_root=tmp_path / "app", mountinfo=""
    )


def test_site_dir_under_app_bind_is_visible(tmp_path: Path):
    app = tmp_path / "app"
    site = app / "public" / "bot"
    site.mkdir(parents=True)
    assert site_dir_is_host_visible(site, in_docker=True, app_root=app, mountinfo="")


def test_overlay_site_dir_is_not_visible(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    site = tmp_path / "www" / "bot"
    site.mkdir(parents=True)
    mountinfo = _mountinfo(
        "1 0 0:0 / / rw - overlay overlay rw",
        "2 1 8:1 /root/maxwell /app rw - ext4 /dev/sda1 rw",
    )
    assert not site_dir_is_host_visible(
        site, in_docker=True, app_root=app, mountinfo=mountinfo
    )


def test_bind_mounted_site_dir_is_visible(tmp_path: Path):
    app = tmp_path / "app"
    app.mkdir()
    site = tmp_path / "www" / "bot"
    site.mkdir(parents=True)
    mountinfo = _mountinfo(
        "1 0 0:0 / / rw - overlay overlay rw",
        f"2 1 8:1 {site} {site} rw - ext4 /dev/sda1 rw",
    )
    assert site_dir_is_host_visible(
        site, in_docker=True, app_root=app, mountinfo=mountinfo
    )
