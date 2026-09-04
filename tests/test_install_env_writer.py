from pathlib import Path

import pytest

from scripts.set_env import set_env


def test_set_env_replaces_values_with_special_characters(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_TOKEN=old\n"
        "OLLAMA_BASE_URL=http://localhost:11434\n"
        "KEEP=this stays\n",
        encoding="utf-8",
    )

    set_env(env_file, "OLLAMA_BASE_URL", "https://example.com/a/b?x=1&y=two")
    set_env(env_file, "DASHBOARD_PASSWORD", "abc=123 with spaces & symbols")

    assert env_file.read_text(encoding="utf-8") == (
        "DISCORD_TOKEN=old\n"
        "OLLAMA_BASE_URL=https://example.com/a/b?x=1&y=two\n"
        "KEEP=this stays\n"
        "DASHBOARD_PASSWORD=abc=123 with spaces & symbols\n"
    )


def test_set_env_replaces_exported_key_and_rejects_multiline(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("export MAXWELL_OWNER_IDS=1\n", encoding="utf-8")

    set_env(env_file, "MAXWELL_OWNER_IDS", "123, 456")

    assert env_file.read_text(encoding="utf-8") == "MAXWELL_OWNER_IDS=123, 456\n"
    with pytest.raises(ValueError):
        set_env(env_file, "BAD", "line1\nline2")


def test_set_env_handles_backreferences_and_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_TOKEN=initial\n", encoding="utf-8")

    special_secret = r"token\1\g<test>with\"quote and #comment"
    set_env(env_file, "DISCORD_TOKEN", special_secret)

    import dotenv
    values = dotenv.dotenv_values(env_file)
    assert values["DISCORD_TOKEN"] == special_secret
