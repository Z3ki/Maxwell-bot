from pathlib import Path

from bot_tools import _is_path_allowed, _safe_attachment_filename, ShellTool


class TestIsPathAllowed:
    def test_allows_file_under_base(self, tmp_path: Path):
        base = tmp_path / "base"
        base.mkdir()
        file = base / "img.png"
        file.write_text("x")
        assert _is_path_allowed(str(file), str(base)) is True

    def test_rejects_file_outside_base(self, tmp_path: Path):
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside.png"
        outside.write_text("x")
        assert _is_path_allowed(str(outside), str(base)) is False

    def test_rejects_traversal(self, tmp_path: Path):
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "secret.png"
        outside.write_text("x")
        assert _is_path_allowed(str(base / ".." / "secret.png"), str(base)) is False

    def test_rejects_symlink_outside_base(self, tmp_path: Path):
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "secret.png"
        outside.write_text("x")
        link = base / "link.png"
        link.symlink_to(outside)
        assert _is_path_allowed(str(link), str(base)) is False

    def test_rejects_missing_file(self, tmp_path: Path):
        base = tmp_path / "base"
        base.mkdir()
        assert _is_path_allowed(str(base / "nope.png"), str(base)) is False


class TestSafeAttachmentFilename:
    def test_strips_path_components(self):
        assert _safe_attachment_filename("/etc/passwd") == "passwd"

    def test_replaces_unsafe_chars(self):
        assert _safe_attachment_filename("hello<world>.txt") == "hello_world_txt"

    def test_removes_leading_dots(self):
        assert _safe_attachment_filename(".hidden.exe") == "hidden.exe"

    def test_uses_default_for_empty(self):
        assert _safe_attachment_filename("") == "attachment"
        assert _safe_attachment_filename(None, default="file") == "file"  # type: ignore[arg-type]

    def test_truncates_long_names(self):
        long_name = "a" * 200 + ".txt"
        result = _safe_attachment_filename(long_name)
        assert len(result) <= 80
        assert result.endswith(".txt")


class TestShellToolValidation:
    def test_accepts_simple_command(self):
        tool = ShellTool(None)  # type: ignore[arg-type]
        assert tool._validate_command("ls -la") is None

    def test_rejects_newlines(self):
        tool = ShellTool(None)  # type: ignore[arg-type]
        assert tool._validate_command("ls\nrm -rf /") is not None

    def test_rejects_control_chars(self):
        tool = ShellTool(None)  # type: ignore[arg-type]
        assert tool._validate_command("ls\x00") is not None

    def test_rejects_privileged_flag(self):
        tool = ShellTool(None)  # type: ignore[arg-type]
        assert tool._validate_command("docker run --privileged ubuntu") is not None

    def test_rejects_bind_mount(self):
        tool = ShellTool(None)  # type: ignore[arg-type]
        assert tool._validate_command("docker run -v /:/host ubuntu") is not None

    def test_rejects_docker_socket(self):
        tool = ShellTool(None)  # type: ignore[arg-type]
        assert tool._validate_command("cat /var/run/docker.sock") is not None

    def test_rejects_long_command(self):
        tool = ShellTool(None)  # type: ignore[arg-type]
        assert tool._validate_command("x" * 5000) is not None
