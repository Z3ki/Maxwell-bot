"""Bounded decoding and recognition of textual Discord attachments."""

from pathlib import Path


TEXT_ATTACHMENT_MAX_CHARS = 50_000


TEXT_MIME_TYPES = {
    "application/json",
    "application/javascript",
    "application/typescript",
    "application/xml",
    "application/x-httpd-php",
    "application/x-sh",
    "application/x-shellscript",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
    "application/sql",
    "application/rtf",
}


TEXT_ATTACHMENT_EXTS = {
    ".1",
    ".2",
    ".3",
    ".4",
    ".5",
    ".6",
    ".7",
    ".8",
    ".9",
    ".asm",
    ".bat",
    ".c",
    ".cfg",
    ".clj",
    ".cmake",
    ".cmd",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".cxx",
    ".diff",
    ".dockerfile",
    ".erl",
    ".ex",
    ".exs",
    ".fish",
    ".go",
    ".h",
    ".hpp",
    ".hrl",
    ".hs",
    ".htm",
    ".html",
    ".inc",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".less",
    ".lisp",
    ".log",
    ".lua",
    ".m",
    ".make",
    ".markdown",
    ".md",
    ".ml",
    ".mli",
    ".nasm",
    ".patch",
    ".php",
    ".pl",
    ".pm",
    ".ps1",
    ".py",
    ".r",
    ".rb",
    ".rs",
    ".sass",
    ".scala",
    ".scss",
    ".sh",
    ".s",
    ".sql",
    ".svelte",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vim",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
    ".zig",
}


def _looks_like_text(blob: bytes) -> bool:
    if not blob:
        return True
    sample = blob[:4096]
    if b"\x00" in sample:
        return False
    control = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13))
    return control / max(1, len(sample)) < 0.05


def _decoded_looks_readable(text: str) -> bool:
    if not text:
        return True
    sample = text[:4096]
    control = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\n\r\f")
    replacement = sample.count("\ufffd")
    return (control + replacement) / max(1, len(sample)) < 0.05


def _decode_readable_text(blob: bytes) -> str:
    # Unmarked Latin-1 can decode as arbitrary, apparently readable UTF-16.
    # A byte-order mark is the reliable signal to try UTF-16 before Latin-1.
    encodings = (
        ("utf-16", "utf-8-sig", "latin-1")
        if blob.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ("utf-8-sig", "latin-1")
    )
    for encoding in encodings:
        try:
            text = blob.decode(encoding)
            if _decoded_looks_readable(text):
                if len(text) > TEXT_ATTACHMENT_MAX_CHARS:
                    # Huge logs in prompts are context-window napalm. Keep enough
                    # to be useful and make the truncation explicit.
                    head = TEXT_ATTACHMENT_MAX_CHARS // 2
                    tail = TEXT_ATTACHMENT_MAX_CHARS - head
                    omitted = len(text) - TEXT_ATTACHMENT_MAX_CHARS
                    return (
                        text[:head]
                        + f"\n\n[... truncated {omitted} chars from middle ...]\n\n"
                        + text[-tail:]
                    )
                return text
        except UnicodeError:
            continue
    return ""


def _is_text_attachment(
    filename: str, content_type: str, blob: bytes | None = None
) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    ext = Path(filename).suffix.lower()
    if mime.startswith("text/") or mime in TEXT_MIME_TYPES:
        return True
    if ext in TEXT_ATTACHMENT_EXTS:
        return True
    if blob is not None:
        return _looks_like_text(blob)
    return False
