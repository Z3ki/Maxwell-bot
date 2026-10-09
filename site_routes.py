"""Shared public routing contract, independent of framework implementation."""

from __future__ import annotations

import ast
import re

SITE_MOUNT = "/bot"


def page_path(slug: str) -> str:
    return f"{SITE_MOUNT}/{slug}/"


def server_path(slug: str) -> str:
    return page_path(slug) + "api"


def store_path(slug: str) -> str:
    return f"/api/site/{slug}"


def client_guide(slug: str) -> str:
    base = server_path(slug)
    return (
        f"  Page mount  : {page_path(slug)}\n"
        f"  Server mount: {base}/ -> local routes, stripping {base} exactly once.\n"
        f"                Define /notes once; its public URL is {base}/notes.\n"
        "                Do not add /bot/<slug>/api to Python routes or router prefixes.\n"
        f"  Frontend    : const apiBase = new URL('{base}/', location.origin);\n"
        "                fetch(new URL('notes', apiBase));\n"
        "                Endpoint names have no leading slash. This works on nested\n"
        "                pages and ignores HTML <base> tags. Bare fetch('api/notes')\n"
        "                only works at the site's root page.\n"
        "  WebSocket   : const ws = new URL('ws', apiBase);\n"
        "                ws.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';\n"
        "                new WebSocket(ws);\n"
        f"  KV store    : {store_path(slug)}/kv is a separate built-in service.\n"
        "                backend=true enables KV only; site_server runs Python."
    )


def route_mount_errors(slug: str, files: dict[str, str]) -> list[str]:
    """Inspect known framework route declarations without executing user code."""
    errors = []
    constructors = {"FastAPI", "APIRouter", "Flask", "Blueprint"}
    methods = {"get", "post", "put", "patch", "delete", "head", "options",
               "route", "api_route", "websocket", "websocket_route"}
    for filename, source in files.items():
        if not filename.endswith(".py"):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        aliases = set(constructors)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                aliases.update(entry.asname or entry.name for entry in node.names
                               if entry.name in constructors)
        receivers = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                func = node.value.func
                if getattr(func, "id", getattr(func, "attr", "")) in aliases:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    receivers.update(t.id for t in targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", getattr(func, "attr", ""))
            values = []
            if name in aliases:
                values = [kw.value for kw in node.keywords if kw.arg in {"prefix", "url_prefix"}]
            elif (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                  and func.value.id in receivers):
                if name in methods:
                    values = node.args[:1] + [kw.value for kw in node.keywords if kw.arg in {"path", "rule"}]
                elif name in {"include_router", "register_blueprint", "mount"}:
                    values = [kw.value for kw in node.keywords if kw.arg in {"prefix", "url_prefix", "path"}]
                    if name == "mount":
                        values += node.args[:1]
            errors.extend(
                f"{filename}:{node.lineno}: {value.value!r} includes the public site mount. "
                f"The proxy already strips {server_path(slug)}; define a local route "
                "such as '/notes' and use the public mount only in the frontend."
                for value in values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
                and re.match(r"^/bot/[^/]+/api(?:/|$)", value.value)
            )
    return errors
