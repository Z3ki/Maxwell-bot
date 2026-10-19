"""Hybrid knowledge graph: site routes + 1-hop facts."""

import sqlite3

from knowledge_graph import (
    KnowledgeGraph,
    extract_frontend_api_paths,
    extract_python_routes,
)
from maxwell_core.memory.scope import MemoryRequester


FLASK_APP = """
from flask import Flask
app = Flask(__name__)

@app.route("/api/health")
def health():
    return "ok"

@app.route("/api/items/<id>", methods=["GET", "POST"])
def items(id):
    return id

@app.post("/api/submit")
def submit():
    return "ok"
"""

FASTAPI_APP = """
from fastapi import FastAPI
app = FastAPI()

@app.get("/api/score")
def score():
    return {}

@app.websocket("/ws")
def ws():
    pass
"""

PAGE = """
<!doctype html>
<script>
fetch('/api/score')
axios.get('/api/items')
const other = '/bot/demo/api/telemetry'
</script>
"""


def _admin():
    return MemoryRequester(
        user_id="owner", channel_id="admin-channel", guild_id="g1",
        is_dm=False, is_admin=True, channel_is_public=True,
    )


def _graph(tmp_path):
    db = sqlite3.connect(tmp_path / "graph.db")
    db.row_factory = sqlite3.Row
    return KnowledgeGraph(db), db


def test_extracts_flask_and_fastapi_routes():
    flask = extract_python_routes(FLASK_APP)
    assert ("GET", "/api/health") in flask
    assert ("GET", "/api/items/<id>") in flask
    assert ("POST", "/api/items/<id>") in flask
    assert ("POST", "/api/submit") in flask
    fastapi = extract_python_routes(FASTAPI_APP)
    assert ("GET", "/api/score") in fastapi
    assert ("WS", "/ws") in fastapi


def test_broken_python_is_empty_not_an_exception():
    assert extract_python_routes("def oops(") == []


def test_extracts_frontend_api_paths():
    paths = extract_frontend_api_paths(PAGE)
    assert "/api/score" in paths
    assert "/api/items" in paths
    assert any(p.endswith("/api/telemetry") for p in paths)


def test_site_index_is_queryable_without_embeddings(tmp_path):
    graph, _db = _graph(tmp_path)
    site_dir = tmp_path / "public" / "bot" / "demo"
    code_dir = tmp_path / "site_servers" / "demo"
    site_dir.mkdir(parents=True)
    code_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text(PAGE, encoding="utf-8")
    (code_dir / "app.py").write_text(FASTAPI_APP, encoding="utf-8")
    note = graph.index_site(
        slug="demo",
        title="Demo",
        owner_id="111",
        owner_name="Z3ki",
        url="https://example/bot/demo/",
        site_dir=site_dir,
        code_dir=code_dir,
    )
    assert "GET /api/score" in note
    assert "frontend calls" in note
    block = graph.prompt_block(
        query="fix demo telemetry", user_id="111", requester=_admin()
    )
    assert "Z3ki" in block
    assert "OWNS" in block
    assert "EXPOSES" in block or "CALLS" in block
    graph.drop_site("demo")
    assert graph.summarize_site("demo") == ""


def test_chat_triples_link_a_person_to_a_project(tmp_path):
    graph, _db = _graph(tmp_path)
    n = graph.ingest_triples(
        [
            {"s": "Z3ki", "rel": "OWNS", "o": "LeakBot"},
            {"s": "LeakBot", "rel": "USES", "o": "hybra.onyx"},
            {"s": "nope", "rel": "EATS", "o": "pizza"},
        ],
        speaker_id="111",
        speaker_name="Z3ki",
        requester=_admin(),
    )
    assert n == 2
    block = graph.prompt_block(
        query="LeakBot config", user_id="111", requester=_admin()
    )
    assert "OWNS" in block
    assert "USES" in block
    assert "EATS" not in block


def test_chat_triples_are_limited_to_their_source_channel(tmp_path):
    graph, _db = _graph(tmp_path)
    source = MemoryRequester(
        user_id="owner", channel_id="private-one", guild_id="g1",
        is_dm=False, is_admin=True, channel_is_public=False,
    )
    graph.ingest_triples(
        [{"s": "Owner", "rel": "WORKS_ON", "o": "SecretProject"}],
        speaker_id="owner",
        speaker_name="Owner",
        requester=source,
    )
    same_channel = graph.prompt_block(
        query="SecretProject", user_id="owner", requester=source
    )
    assert "SecretProject" in same_channel

    other_channel = MemoryRequester(
        user_id="owner", channel_id="public", guild_id="g1",
        is_dm=False, is_admin=True, channel_is_public=True,
    )
    other_guild = MemoryRequester(
        user_id="owner", channel_id="same-id", guild_id="g2",
        is_dm=False, is_admin=True, channel_is_public=True,
    )
    assert graph.prompt_block(
        query="SecretProject", user_id="owner", requester=other_channel
    ) == ""
    assert graph.prompt_block(
        query="SecretProject", user_id="owner", requester=other_guild
    ) == ""


def test_dm_graph_facts_do_not_reach_a_guild_prompt(tmp_path):
    graph, _db = _graph(tmp_path)
    dm = MemoryRequester(
        user_id="owner", channel_id="dm-owner", guild_id="",
        is_dm=True, is_admin=True, channel_is_public=False,
    )
    graph.ingest_triples(
        [{"s": "Owner", "rel": "PREFERS", "o": "PrivateAlias"}],
        speaker_id="owner",
        speaker_name="Owner",
        requester=dm,
    )
    assert "PrivateAlias" in graph.prompt_block(
        query="PrivateAlias", user_id="owner", requester=dm
    )
    guild = MemoryRequester(
        user_id="owner", channel_id="guild-channel", guild_id="g1",
        is_dm=False, is_admin=True, channel_is_public=True,
    )
    assert graph.prompt_block(
        query="PrivateAlias", user_id="owner", requester=guild
    ) == ""


def test_legacy_unscoped_graph_edges_stay_hidden(tmp_path):
    graph, db = _graph(tmp_path)
    graph.upsert_node("user:old", "user", "OldUser")
    graph.upsert_node("thing:legacy", "thing", "LegacySecret")
    db.execute(
        "INSERT INTO graph_edges (src, rel, dst, props, updated_at, scope) "
        "VALUES ('user:old', 'OWNS', 'thing:legacy', '{}', 0, '')"
    )
    assert graph.prompt_block(
        query="LegacySecret", user_id="old", requester=_admin()
    ) == ""


def test_prompt_block_respects_budget(tmp_path):
    graph, _db = _graph(tmp_path)
    graph.ingest_triples(
        [{"s": "Z3ki", "rel": "OWNS", "o": "Thing"}],
        speaker_id="1",
        speaker_name="Z3ki",
        requester=_admin(),
    )
    assert graph.prompt_block(
        query="Thing", user_id="1", budget=20, requester=_admin()
    ) == ""
    assert "OWNS" in graph.prompt_block(
        query="Thing", user_id="1", budget=400, requester=_admin()
    )


def test_refresh_site_noops_without_memory():
    from types import SimpleNamespace

    from knowledge_graph import refresh_site

    bot = SimpleNamespace(_sites={}, config=SimpleNamespace())
    assert refresh_site(bot, "demo") == ""
