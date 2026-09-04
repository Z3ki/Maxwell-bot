"""Hybrid knowledge graph next to vector RAG.

Vector search is good at conversational vibe and terrible at multi-hop
structure ("who owns this site, which routes does it expose, what does the
frontend call"). This module stores that structure as SQLite nodes+edges in
the same ``maxwell_rag.db`` — no Neo4j, no extra embed calls.

Two writers, both cheap:

* **Site/code graph** — Python ``ast`` + a frontend path scan whenever a
  site is created or edited. Deterministic, zero tokens.
* **Chat triples** — optional ``triples`` on the *existing* context-extractor
  JSON. No extra LLM pass.

Prompt lookup is 1–2 hop expansion from the speaker and any named nodes in
the query.
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_RELS = frozenset(
    {
        "OWNS",
        "USES",
        "DISLIKES",
        "DEPENDS_ON",
        "CONFIGURED_WITH",
        "PREFERS",
        "BUILT",
        "WORKS_ON",
        "EXPOSES",
        "CALLS",
        "HAS_FILE",
    }
)
CHAT_RELS = frozenset(
    {
        "OWNS",
        "USES",
        "DISLIKES",
        "DEPENDS_ON",
        "CONFIGURED_WITH",
        "PREFERS",
        "BUILT",
        "WORKS_ON",
    }
)
ROUTE_ATTRS = frozenset(
    {
        "route",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "websocket",
        "add_url_rule",
    }
)
METHOD_FROM_ATTR = {
    "get": ("GET",),
    "post": ("POST",),
    "put": ("PUT",),
    "patch": ("PATCH",),
    "delete": ("DELETE",),
    "head": ("HEAD",),
    "options": ("OPTIONS",),
    "websocket": ("WS",),
}
API_PATH_RE = re.compile(r"(?:/api/[A-Za-z0-9_./\-?=]+|/bot/[A-Za-z0-9\-]+/api/[A-Za-z0-9_./\-?=]+)")
FETCH_RE = re.compile(
    r"""(?:fetch|axios(?:\.\w+)?|XMLHttpRequest)\s*\(\s*['"`]([^'"`]+)['"`]""",
    re.IGNORECASE,
)
SLUG_IN_TEXT_RE = re.compile(r"\b([a-z0-9][a-z0-9-]{1,28}[a-z0-9])\b")
STOP_NAMES = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "and",
        "or",
        "for",
        "with",
        "from",
        "maxwell",
        "user",
        "site",
        "bot",
        "api",
        "http",
        "https",
        "true",
        "false",
        "none",
        "null",
    }
)
MAX_PROMPT_EDGES = 18
MAX_CHAT_TRIPLES = 12
MAX_SITE_FILES = 16


def _now() -> float:
    return time.time()


def _norm_name(raw: Any, *, limit: int = 80) -> str:
    text = " ".join(str(raw or "").split())[:limit].strip()
    return text


def _slug_id(kind: str, name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:80]
    return f"{kind}:{key}" if key else f"{kind}:unknown"


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def extract_python_routes(source: str) -> list[tuple[str, str]]:
    """Return ``[(METHOD, path), ...]`` from Flask/FastAPI-style Python."""
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return []
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            attr = func.attr.lower()
        elif isinstance(func, ast.Name):
            attr = func.id.lower()
        else:
            continue
        if attr not in ROUTE_ATTRS:
            continue
        path = None
        if node.args:
            path = _const_str(node.args[0])
        if not path:
            for kw in node.keywords:
                if kw.arg in {"path", "rule"}:
                    path = _const_str(kw.value)
        if not path or not str(path).startswith("/"):
            continue
        path = str(path).split("?")[0][:160]
        methods: tuple[str, ...] | None = METHOD_FROM_ATTR.get(attr)
        for kw in node.keywords:
            if kw.arg != "methods":
                continue
            vals: list[str] = []
            if isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                for elt in kw.value.elts:
                    item = _const_str(elt)
                    if item:
                        vals.append(item.upper())
            methods = tuple(vals) if vals else methods
        if not methods:
            methods = ("GET",)
        for method in methods:
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            found.append(key)
    return found


def extract_frontend_api_paths(source: str) -> list[str]:
    """Paths the page actually calls — fetch/axios plus quoted ``/api/...``."""
    text = source or ""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        path = str(raw or "").strip()
        if not path:
            return
        if path.startswith("http"):
            try:
                parsed = urlparse(path)
                path = parsed.path or path
            except Exception:
                return
        if "/api/" not in path and not path.startswith("/api"):
            return
        path = path.split("?")[0][:160]
        if not path.startswith("/"):
            idx = path.find("/api/")
            if idx < 0:
                return
            path = path[idx:]
        if path in seen:
            return
        seen.add(path)
        found.append(path)

    for match in FETCH_RE.finditer(text):
        add(match.group(1))
    for match in API_PATH_RE.finditer(text):
        add(match.group(0))
    return found[:40]


class KnowledgeGraph:
    """SQLite adjacency graph. Shares the RAG connection; does not close it."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_nodes (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                props TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_edges (
                src TEXT NOT NULL,
                rel TEXT NOT NULL,
                dst TEXT NOT NULL,
                props TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL,
                PRIMARY KEY (src, rel, dst)
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_kind ON graph_nodes(kind)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_name ON graph_nodes(name)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst)"
        )

    def upsert_node(
        self, node_id: str, kind: str, name: str, props: dict | None = None
    ) -> str:
        nid = str(node_id or "").strip()[:160]
        if not nid:
            return ""
        payload = json.dumps(props or {}, ensure_ascii=False)[:4000]
        self._db.execute(
            """
            INSERT INTO graph_nodes (id, kind, name, props, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind,
                name=excluded.name,
                props=excluded.props,
                updated_at=excluded.updated_at
            """,
            (nid, str(kind or "thing")[:32], _norm_name(name) or nid, payload, _now()),
        )
        return nid

    def upsert_edge(self, src: str, rel: str, dst: str, props: dict | None = None) -> None:
        src_id, dst_id = str(src or "").strip(), str(dst or "").strip()
        relation = str(rel or "").strip().upper()
        if not src_id or not dst_id or relation not in ALLOWED_RELS:
            return
        payload = json.dumps(props or {}, ensure_ascii=False)[:2000]
        self._db.execute(
            """
            INSERT INTO graph_edges (src, rel, dst, props, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(src, rel, dst) DO UPDATE SET
                props=excluded.props,
                updated_at=excluded.updated_at
            """,
            (src_id, relation, dst_id, payload, _now()),
        )

    def drop_site(self, slug: str) -> None:
        slug = re.sub(r"[^a-z0-9-]", "", str(slug or "").lower())[:30]
        if not slug:
            return
        site_id = f"site:{slug}"
        like = f"route:{slug}:%"
        file_like = f"file:{slug}:%"
        self._db.execute(
            "DELETE FROM graph_edges WHERE src=? OR dst=? OR src LIKE ? OR dst LIKE ? "
            "OR src LIKE ? OR dst LIKE ?",
            (site_id, site_id, like, like, file_like, file_like),
        )
        self._db.execute(
            "DELETE FROM graph_nodes WHERE id=? OR id LIKE ? OR id LIKE ?",
            (site_id, like, file_like),
        )

    def index_site(
        self,
        *,
        slug: str,
        title: str = "",
        owner_id: str = "",
        owner_name: str = "",
        url: str = "",
        site_dir: str | Path | None = None,
        code_dir: str | Path | None = None,
    ) -> str:
        """Rebuild the subgraph for one published site. Returns a one-line summary."""
        slug = re.sub(r"[^a-z0-9-]", "", str(slug or "").lower())[:30].strip("-")
        if not slug:
            return ""
        self.drop_site(slug)
        site_id = f"site:{slug}"
        label = _norm_name(title) or slug
        self.upsert_node(
            site_id,
            "site",
            label,
            {"slug": slug, "url": str(url or ""), "title": label},
        )
        if owner_id:
            user_id = f"user:{owner_id}"
            self.upsert_node(
                user_id, "user", _norm_name(owner_name) or owner_id, {"user_id": owner_id}
            )
            self.upsert_edge(user_id, "OWNS", site_id)

        routes: list[tuple[str, str]] = []
        calls: list[str] = []
        files: list[str] = []
        code_path = Path(code_dir) if code_dir else None
        if code_path and code_path.is_dir():
            for py in sorted(code_path.glob("*.py")):
                if py.name.startswith("_"):
                    continue
                try:
                    source = py.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                files.append(py.name)
                routes.extend(extract_python_routes(source))
        page_dir = Path(site_dir) if site_dir else None
        if page_dir and page_dir.is_dir():
            for rel in ("index.html", "app.js", "main.js", "script.js"):
                target = page_dir / rel
                if not target.is_file():
                    continue
                files.append(rel)
                try:
                    calls.extend(
                        extract_frontend_api_paths(
                            target.read_text(encoding="utf-8", errors="replace")
                        )
                    )
                except OSError:
                    continue
            for html in sorted(page_dir.glob("*.html"))[:8]:
                if html.name == "index.html":
                    continue
                files.append(html.name)
                try:
                    calls.extend(
                        extract_frontend_api_paths(
                            html.read_text(encoding="utf-8", errors="replace")
                        )
                    )
                except OSError:
                    continue

        seen_routes: set[tuple[str, str]] = set()
        for method, path in routes:
            key = (method, path)
            if key in seen_routes:
                continue
            seen_routes.add(key)
            rid = f"route:{slug}:{method}:{path}"[:160]
            self.upsert_node(rid, "route", f"{method} {path}", {"slug": slug})
            self.upsert_edge(site_id, "EXPOSES", rid)

        seen_calls: set[str] = set()
        for path in calls:
            if path in seen_calls:
                continue
            seen_calls.add(path)
            rid = f"route:{slug}:*:{path}"[:160]
            self.upsert_node(rid, "route", path, {"slug": slug, "from": "frontend"})
            self.upsert_edge(site_id, "CALLS", rid)

        for rel in files[:MAX_SITE_FILES]:
            fid = f"file:{slug}:{rel}"[:160]
            self.upsert_node(fid, "file", rel, {"slug": slug})
            self.upsert_edge(site_id, "HAS_FILE", fid)

        return self.summarize_site(slug)

    def summarize_site(self, slug: str) -> str:
        slug = re.sub(r"[^a-z0-9-]", "", str(slug or "").lower())[:30]
        if not slug:
            return ""
        site_id = f"site:{slug}"
        exposed = [
            str(row["name"])
            for row in self._db.execute(
                "SELECT n.name AS name FROM graph_edges e "
                "JOIN graph_nodes n ON n.id = e.dst "
                "WHERE e.src=? AND e.rel='EXPOSES' ORDER BY n.name LIMIT 12",
                (site_id,),
            )
        ]
        called = [
            str(row["name"])
            for row in self._db.execute(
                "SELECT n.name AS name FROM graph_edges e "
                "JOIN graph_nodes n ON n.id = e.dst "
                "WHERE e.src=? AND e.rel='CALLS' ORDER BY n.name LIMIT 8",
                (site_id,),
            )
        ]
        bits: list[str] = []
        if exposed:
            bits.append("routes " + ", ".join(exposed))
        if called:
            bits.append("frontend calls " + ", ".join(called))
        if not bits:
            return ""
        return "; ".join(bits)

    def ingest_triples(
        self,
        triples: Iterable[Any],
        *,
        speaker_id: str = "",
        speaker_name: str = "",
    ) -> int:
        """Store whitelist triples from the context extractor. Returns count."""
        if speaker_id:
            self.upsert_node(
                f"user:{speaker_id}",
                "user",
                _norm_name(speaker_name) or speaker_id,
                {"user_id": speaker_id},
            )
        stored = 0
        speaker_l = str(speaker_name or "").strip().lower()
        for raw in list(triples or [])[:MAX_CHAT_TRIPLES]:
            if not isinstance(raw, dict):
                continue
            rel = str(raw.get("rel") or raw.get("relation") or "").strip().upper()
            if rel not in CHAT_RELS:
                continue
            subj = _norm_name(raw.get("s") or raw.get("subject") or "", limit=60)
            obj = _norm_name(raw.get("o") or raw.get("object") or "", limit=60)
            if not subj or not obj:
                continue
            if subj.lower() in STOP_NAMES or obj.lower() in STOP_NAMES:
                continue
            src = self._node_for_label(subj, speaker_id, speaker_l)
            dst = self._node_for_label(obj, speaker_id, speaker_l)
            if not src or not dst or src == dst:
                continue
            self.upsert_edge(src, rel, dst)
            stored += 1
        return stored

    def _node_for_label(self, label: str, speaker_id: str, speaker_l: str) -> str:
        if speaker_id and label.lower() == speaker_l:
            return f"user:{speaker_id}"
        if label.isdigit() and len(label) >= 15:
            nid = f"user:{label}"
            self.upsert_node(nid, "user", label, {"user_id": label})
            return nid
        slug = re.sub(r"[^a-z0-9-]", "", label.lower())[:30]
        if slug:
            row = self._db.execute(
                "SELECT id FROM graph_nodes WHERE id=? LIMIT 1", (f"site:{slug}",)
            ).fetchone()
            if row:
                return str(row["id"])
        nid = _slug_id("thing", label)
        self.upsert_node(nid, "thing", label)
        return nid

    def find_anchors(self, query: str, user_id: str = "") -> list[str]:
        """Node ids to expand from: the speaker plus anything named in ``query``."""
        ids: list[str] = []
        seen: set[str] = set()

        def add(nid: str) -> None:
            if nid and nid not in seen:
                seen.add(nid)
                ids.append(nid)

        if user_id:
            add(f"user:{user_id}")
        text = str(query or "")
        for match in SLUG_IN_TEXT_RE.finditer(text.lower()):
            token = match.group(1)
            if token in STOP_NAMES:
                continue
            row = self._db.execute(
                "SELECT id FROM graph_nodes WHERE id=? OR lower(name)=? LIMIT 1",
                (f"site:{token}", token),
            ).fetchone()
            if row:
                add(str(row["id"]))
            else:
                row = self._db.execute(
                    "SELECT id FROM graph_nodes WHERE id=? LIMIT 1",
                    (_slug_id("thing", token),),
                ).fetchone()
                if row:
                    add(str(row["id"]))
        # Longer unique names (display names, titles) via substring, bounded.
        tokens = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}", text) if t.lower() not in STOP_NAMES]
        for token in tokens[:8]:
            safe_token = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self._db.execute(
                "SELECT id FROM graph_nodes WHERE name LIKE ? ESCAPE '\\' LIMIT 3",
                (f"%{safe_token}%",),
            ).fetchall()
            for row in rows:
                add(str(row["id"]))
        return ids[:12]

    def neighbors(self, node_ids: list[str], *, hops: int = 2, limit: int = 40) -> list[tuple[str, str, str, str, str]]:
        """Return ``(src_id, src_name, rel, dst_id, dst_name)`` within ``hops``."""
        frontier = [n for n in node_ids if n]
        seen_nodes = set(frontier)
        edges: list[tuple[str, str, str, str, str]] = []
        seen_edges: set[tuple[str, str, str]] = set()
        for _ in range(max(1, min(hops, 3))):
            if not frontier or len(edges) >= limit:
                break
            nxt: list[str] = []
            placeholders = ",".join("?" * len(frontier))
            rows = self._db.execute(
                f"""
                SELECT e.src, e.rel, e.dst, ns.name AS src_name, nd.name AS dst_name
                FROM graph_edges e
                JOIN graph_nodes ns ON ns.id = e.src
                JOIN graph_nodes nd ON nd.id = e.dst
                WHERE e.src IN ({placeholders}) OR e.dst IN ({placeholders})
                """,
                (*frontier, *frontier),
            ).fetchall()
            for row in rows:
                key = (str(row["src"]), str(row["rel"]), str(row["dst"]))
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append(
                    (
                        str(row["src"]),
                        str(row["src_name"]),
                        str(row["rel"]),
                        str(row["dst"]),
                        str(row["dst_name"]),
                    )
                )
                for nid in (str(row["src"]), str(row["dst"])):
                    if nid not in seen_nodes:
                        seen_nodes.add(nid)
                        nxt.append(nid)
                if len(edges) >= limit:
                    break
            frontier = nxt
        return edges

    def prompt_block(self, *, query: str, user_id: str = "", budget: int = 800) -> str:
        """Crisp graph triples for the system prompt, or ``""``."""
        budget = max(0, int(budget or 0))
        if budget < 80:
            return ""
        anchors = self.find_anchors(query, user_id)
        if not anchors:
            return ""
        edges = self.neighbors(anchors, hops=2, limit=MAX_PROMPT_EDGES)
        if not edges:
            return ""
        lines = [
            "Known structure (graph — verified links, not vibe recall; don't recite unless useful):"
        ]
        for _src, src_name, rel, _dst, dst_name in edges:
            lines.append(f"- {src_name} --{rel}--> {dst_name}")
        text = "\n".join(lines)
        if len(text) <= budget:
            return text
        kept = [lines[0]]
        used = len(lines[0]) + 1
        for line in lines[1:]:
            extra = len(line) + 1
            if used + extra > budget:
                break
            kept.append(line)
            used += extra
        return "\n".join(kept) if len(kept) > 1 else ""


def graph_from_bot(bot: Any) -> KnowledgeGraph | None:
    mem = getattr(bot, "memory", None)
    graph = getattr(mem, "graph", None)
    return graph if isinstance(graph, KnowledgeGraph) else None


def graph_enabled(bot: Any) -> bool:
    from control_defaults import parse_bool

    control = getattr(bot, "_control", None) or {}
    return parse_bool(control.get("knowledge_graph_enabled", True), True)


def refresh_site(bot: Any, slug: str) -> str:
    """Re-index one site from disk. Empty string if graph is off or missing."""
    if not graph_enabled(bot):
        return ""
    graph = graph_from_bot(bot)
    if graph is None:
        return ""
    slug = re.sub(r"[^a-z0-9-]", "", str(slug or "").lower())[:30].strip("-")
    if not slug:
        return ""
    if hasattr(bot, "_load_sites"):
        with contextlib.suppress(Exception):
            bot._load_sites(quiet=True)
    entry = ((getattr(bot, "_sites", None) or {}).get(slug) or {})
    cfg = getattr(bot, "config", None)
    site_root = Path(getattr(cfg, "MAXWELL_SITE_DIR", "public/bot") or "public/bot")
    data_dir = getattr(cfg, "DATA_DIR", "data")
    public_base = str(getattr(cfg, "MAXWELL_PUBLIC_BASE_URL", "") or "").rstrip("/")
    url = f"{public_base}/bot/{slug}/" if public_base else f"/bot/{slug}/"
    code_dir = None
    try:
        import site_server

        candidate = site_server.code_dir(data_dir, slug)
        if candidate.is_dir():
            code_dir = candidate
    except Exception:
        code_dir = None
    owner_id = str(entry.get("user_id") or "")
    return graph.index_site(
        slug=slug,
        title=str(entry.get("title") or slug),
        owner_id=owner_id,
        owner_name=str(entry.get("user_name") or entry.get("author") or ""),
        url=url,
        site_dir=site_root / slug,
        code_dir=code_dir,
    )


def drop_site(bot: Any, slug: str) -> None:
    graph = graph_from_bot(bot)
    if graph is None:
        return
    graph.drop_site(slug)
