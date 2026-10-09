import asyncio
import json
import shutil
import subprocess

import pytest

import site_routes
import site_server
from tooling.helpers import _site_api_path_warnings, format_site_file_read


@pytest.mark.parametrize("declaration", [
    '@app.get("/bot/demo/api/notes")',
    'app.include_router(router, prefix="/bot/demo/api")',
    'router = APIRouter(prefix="/bot/demo/api")',
    'app.mount("/bot/another/api", other)',
    'app = Flask(__name__)\n@app.route(rule="/bot/demo/api/ws")',
    'from fastapi import APIRouter as Router\nrouter = Router(prefix="/bot/demo/api")',
])
def test_public_mount_is_detected_only_in_framework_declarations(declaration):
    source = "from fastapi import FastAPI, APIRouter\napp = FastAPI()\n" + declaration
    if declaration.startswith("@") or "@app" in declaration:
        source += "\ndef endpoint(): pass\n"
    errors = site_routes.route_mount_errors("demo", {"app.py": source})
    assert errors and "already strips /bot/demo/api" in errors[0]


def test_valid_routes_and_unrelated_mount_strings_are_preserved():
    source = '''from fastapi import FastAPI
app = FastAPI()
PUBLIC_URL = "/bot/demo/api/notes"
@app.get("/notes")
def notes(): return {"url": PUBLIC_URL}
@app.get("/bot/status")
def status(): return {}
client.get("/bot/demo/api/notes")
'''
    assert site_routes.route_mount_errors("demo", {"app.py": source}) == []


@pytest.mark.parametrize("operation", ["deploy", "write", "replace"])
def test_bad_mount_does_not_overwrite_existing_backend(tmp_path, operation):
    source = 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/notes")\ndef notes(): return []\n'
    site_server.write_code(tmp_path, "demo", {"app.py": source, "helper.py": "keep = True"})
    bad = source.replace('"/notes"', '"/bot/demo/api/notes"')
    with pytest.raises(site_server.SiteServerError, match="Invalid backend route mount"):
        if operation == "deploy":
            site_server.write_code(tmp_path, "demo", {"app.py": bad})
        elif operation == "write":
            site_server.merge_code(tmp_path, "demo", {"app.py": bad})
        else:
            site_server.patch_code(tmp_path, "demo", "app.py", '"/notes"', '"/bot/demo/api/notes"')
    assert site_server.read_code(tmp_path, "demo", "app.py") == source
    assert site_server.read_code(tmp_path, "demo", "helper.py") == "keep = True"


def test_client_urls_work_from_nested_pages_and_with_a_base_tag():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    guide = site_routes.client_guide("demo")
    setup = next(line.strip() for line in guide.splitlines() if "const apiBase =" in line)
    setup = setup[setup.index("const apiBase ="):]
    output = subprocess.check_output([node, "-e", """
const location = {origin: 'https://example.com', protocol: 'https:',
                  href: 'https://example.com/bot/demo/pages/nested/index.html'};
const document = {baseURI: 'https://different.example/other/'};
""" + setup + """
const ws = new URL('ws', apiBase);
ws.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
console.log(JSON.stringify([new URL('notes', apiBase).href, ws.href]));
"""], text=True)
    assert json.loads(output) == ["https://example.com/bot/demo/api/notes", "wss://example.com/bot/demo/api/ws"]


def test_frontend_duplicate_prefix_is_reported():
    warnings = _site_api_path_warnings("demo", '<script>fetch("/bot/demo/bot/demo/api/notes")</script>', [])
    assert any("duplicated site prefix" in warning for warning in warnings)
    assert _site_api_path_warnings("demo", '<script>fetch("/bot/demo/api/notes")</script>', []) == []


def test_site_reads_preserve_long_lines_and_explicit_line_offsets():
    source = "HEAD" + "x" * 200_000 + "TAIL"
    assert format_site_file_read("index.html", source).endswith(source)
    source = "skip\n" + source + "\nlast\n"
    assert format_site_file_read("app.py", source, start_line=2).endswith(source[5:])


@pytest.mark.parametrize("lines, tail", [(0, "all"), (500, "500")])
def test_backend_logs_are_complete_and_accept_requested_line_counts(tmp_path, monkeypatch, lines, tail):
    output = "first\n" + "x" * 200_000 + "\nlast"

    async def docker(*args, **kwargs):
        assert args[:3] == ("logs", "--tail", tail)
        return 0, output, ""

    monkeypatch.setattr(site_server, "_docker", docker)
    assert asyncio.run(site_server.logs(tmp_path, "demo", lines)) == output
