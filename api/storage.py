"""Pure storage helpers for the Maxwell API: env loading, safe I/O, and path resolvers.

No route handlers, no auth logic. Path resolvers look up DATA_DIR lazily from
`api.api_server` so monkeypatching `api.api_server.DATA_DIR` (used by
test_api_corrupt_writes) and `setenv("DATA_DIR")` + reload (used by
test_api_rem) both keep working.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = Path(os.getenv("MAXWELL_APP_ROOT", ROOT))
ENV_FILE = Path(os.getenv("MAXWELL_ENV_FILE", APP_ROOT / ".env"))


def _data_dir() -> Path:
    """Resolve the current DATA_DIR from api.api_server at call time.

    Tests monkeypatch `api.api_server.DATA_DIR` directly, so the lookup must
    be lazy. Importing inside the function also avoids a circular import
    (api_server imports this module at load time).
    """
    from api import api_server

    return api_server.DATA_DIR


def _load_env_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(ENV_FILE)

if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from utils import _atomic_json_write_sync, _atomic_text_write_sync  # noqa: E402

MAX_ID_CHARS = 64


def _int_env_safe(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _safe_int(val, default: int) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _safe_float(val, default: float) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _safe_list(value):
    return value if isinstance(value, list) else []


def _safe_object(value):
    return value if isinstance(value, dict) else {}


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _load_for_write(path, expected_type, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise ValueError(f"refusing to overwrite corrupt {path.name}: {exc}") from exc
    if not isinstance(data, expected_type):
        raise ValueError(f"refusing to overwrite malformed {path.name}")
    return data


def _clean_id(value: str) -> str:
    return str(value or "").strip()[:MAX_ID_CHARS]


async def atomic_json_write(path: Path, data) -> None:
    await asyncio.to_thread(_atomic_json_write_sync, path, data)


async def atomic_text_write(path: Path, text: str) -> None:
    await asyncio.to_thread(_atomic_text_write_sync, path, text)


def _control_path() -> Path:
    return _data_dir() / "bot_control.json"


def _rem_state_path() -> Path:
    return _data_dir() / "rem_state.json"


def _rem_runs_path() -> Path:
    return _data_dir() / "rem_runs.json"


def _rem_events_path() -> Path:
    return _data_dir() / "rem_events.json"


def _rem_control_path() -> Path:
    return _data_dir() / "rem_control.json"


def _autonomy_state_path() -> Path:
    return _data_dir() / "autonomy_state.json"


def _autonomy_goals_path() -> Path:
    return _data_dir() / "autonomy_goals.json"


def _autonomy_log_path() -> Path:
    return _data_dir() / "autonomy_log.json"


def _context_cleanup_state_path() -> Path:
    return _data_dir() / "context_cleanup_state.json"


def _context_cleanup_control_path() -> Path:
    return _data_dir() / "context_cleanup_control.json"


def _context_cleanup_log_path() -> Path:
    return _data_dir() / "context_cleanup_log.json"


def _commands_path() -> Path:
    return _data_dir() / "bot_commands.json"


def _memory_text_path() -> Path:
    return _data_dir() / "long_term_memory.txt"


def _context_path() -> Path:
    return _data_dir() / "shared_context.json"


def _llm_traces_path() -> Path:
    return _data_dir() / "llm_traces.json"
