"""Lock in Docker hardening and the slim/multi-stage image contract."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _must_contain(path: Path, *needles: str) -> str:
    text = path.read_text()
    for needle in needles:
        assert needle in text, f"{path.name} missing {needle!r}"
    return text


def test_linux_and_bridge_compose_share_hardening():
    for name in ("docker-compose.yml", "docker-compose.bridge.yml"):
        text = _must_contain(
            ROOT / name,
            "no-new-privileges:true",
            "cap_drop:",
            "mem_limit: 4g",
            "memswap_limit: 4g",
            'cpus: "2.0"',
            "pids_limit: 2048",
            "shm_size: 256mb",
            "init: true",
            "max-size: \"20m\"",
            "/var/run/docker.sock:/var/run/docker.sock:ro",
            "/tmp:rw,nosuid,nodev,size=256m",
        )
        assert "privileged: true" not in text


def test_maxwell_dockerfile_is_multistage_slim():
    text = _must_contain(
        ROOT / "docker" / "maxwell.Dockerfile",
        "python:3.12-slim-bookworm AS deps",
        "FROM python:3.12-slim-bookworm",
        "HEALTHCHECK",
        "build-essential",
        "chromium",
        "syntax=docker/dockerfile:1",
        "--mount=type=cache,target=/root/.cache/pip",
    )
    assert text.count("build-essential") == 1


def test_shell_sandbox_image_has_no_sudo():
    text = _must_contain(
        ROOT / "docker" / "Dockerfile",
        "debian:bookworm-slim",
        "--mount=type=cache,target=/var/cache/apt",
    )
    install = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "sudo" not in install.lower()
    assert "NOPASSWD" not in text


def test_site_runtime_stays_unprivileged():
    text = _must_contain(
        ROOT / "docker" / "site-runtime" / "Dockerfile",
        "USER site",
        "useradd -m -u 10001 site",
        "python:3.12-slim-bookworm",
        "PYTHONDONTWRITEBYTECODE=1",
    )
    assert text.strip().endswith('CMD ["python", "app.py"]')


def test_runtime_flags_disable_swap():
    from plugins.shell.isolation import docker_run_args

    args = docker_run_args(
        container_name="mwsh-" + "a" * 24,
        image="maxwell-shell",
    )
    assert args[args.index("--memory-swap") + 1] == "2g"
    assert args[args.index("--cgroup-parent") + 1] == "maxwell-shell.slice"


def test_shell_requires_explicit_gvisor_platform_and_isolated_network():
    from plugins.shell.isolation import validate_runsc_runtime

    safe, _reason = validate_runsc_runtime(
        {
            "runsc": {
                "path": "/usr/local/bin/runsc",
                "runtimeArgs": ["--platform=systrap", "--network=sandbox"],
            }
        }
    )
    assert safe

    for args in (
        ["--network=sandbox"],
        ["--platform=systrap"],
        ["--platform=systrap", "--network=host"],
        ["--platform=kvm", "--network=sandbox"],
    ):
        safe, _reason = validate_runsc_runtime(
            {"runsc": {"path": "/usr/local/bin/runsc", "runtimeArgs": args}}
        )
        assert not safe, args


