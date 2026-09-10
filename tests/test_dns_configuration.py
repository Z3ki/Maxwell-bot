"""DNS changes must target only the operator's explicitly configured zone."""

import subprocess
import sys
from pathlib import Path

import pytest

from email_integration import setup_dns as dns

ROOT = Path(__file__).resolve().parents[1]
ZONE = "a" * 32
ARGS = ["--token", "unused", "--zone-id", ZONE, "--domain", "example.org"]


def test_dns_requires_explicit_zone_and_domain_before_requests(monkeypatch):
    for name in ("CF_API_TOKEN", "CF_ZONE_ID", "MAXWELL_EMAIL_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    monkeypatch.setattr(dns, "_cf_request", lambda *a, **kw: calls.append(a))
    with pytest.raises(SystemExit) as exc:
        dns.main(["--token", "unused"])
    assert exc.value.code == 2
    assert calls == []


def test_dns_refuses_mismatched_domain_before_writes(monkeypatch):
    calls = []

    def request(token, method, path, body=None):
        calls.append((method, path))
        return {"result": {"name": "another.example"}}

    monkeypatch.setattr(dns, "_cf_request", request)
    with pytest.raises(SystemExit):
        dns.main(ARGS)
    assert calls == [("GET", f"/zones/{ZONE}")]


def test_dns_custom_domain_selector_and_policy_preservation(monkeypatch):
    writes = []

    def request(token, method, path, body=None):
        assert f"/zones/{ZONE}" in path
        if method == "GET":
            if "dns_records" not in path:
                return {"result": {"name": "example.org"}}
            if "_dmarc" in path:
                return {
                    "result": [
                        {
                            "id": "policy",
                            "name": "_dmarc.example.org",
                            "type": "TXT",
                            "content": "v=DMARC1; p=reject",
                        }
                    ]
                }
            return {"result": []}
        writes.append((method, path, body))
        return {"success": True}

    monkeypatch.setattr(dns, "_cf_request", request)
    assert (
        dns.main(
            ARGS
            + [
                "--dkim",
                "k=rsa; p=test",
                "--dkim-selector",
                "custom",
                "--dmarc-email",
                "reports@example.org",
            ]
        )
        == 0
    )
    assert {body["name"] for _, _, body in writes} == {
        "example.org",
        "custom._domainkey.example.org",
    }
    assert all("z3ki" not in str(write) for write in writes)
    assert all(body["type"] == "TXT" for _, _, body in writes)


def test_routing_uses_cloudflare_dns_endpoint_without_guessing_mx(monkeypatch):
    calls = []

    def request(token, method, path, body=None):
        calls.append((method, path))
        return {"result": {"name": "example.org"}}

    monkeypatch.setattr(dns, "_cf_request", request)
    assert dns.main(ARGS + ["--enable-routing", "--mailgun-spf", ""]) == 0
    assert calls == [
        ("GET", f"/zones/{ZONE}"),
        ("POST", f"/zones/{ZONE}/email/routing/dns"),
    ]


@pytest.mark.parametrize("entrypoint", ["setup_dns.py", "setup_dns_legacy.py"])
def test_dns_entrypoints_explain_required_configuration(entrypoint):
    result = subprocess.run(
        [sys.executable, str(ROOT / "email_integration" / entrypoint), "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "--zone-id" in result.stdout
    assert "--domain" in result.stdout
