"""Generated attachment bytes accessible only inside their originating turn.

Delivery never reads an arbitrary host path: a path is a lookup key for bytes
already produced by a tool in this request. Parallel turns get separate maps.
"""

from contextvars import ContextVar, Token
from pathlib import Path
from uuid import uuid4

_files: ContextVar[dict[str, tuple[bytes, str]] | None] = ContextVar(
    "generated_attachment_files", default=None
)


def begin_generated_files() -> Token:
    return _files.set({})


def reset_generated_files(token: Token) -> None:
    files = _files.get()
    if files is not None:
        files.clear()
    _files.reset(token)


def register_generated_file(blob: bytes, filename: str) -> str | None:
    files = _files.get()
    if files is None:
        return None
    name = Path(filename).name
    path = f"generated:{uuid4().hex}/{name}"
    files[path] = (blob, name)
    return path


def generated_file(path: str) -> tuple[bytes, str] | None:
    return (_files.get() or {}).get(str(path))
