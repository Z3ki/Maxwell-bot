#!/usr/bin/env python3
"""Configure Mailgun DNS records for an explicitly selected Cloudflare zone.

Set CF_API_TOKEN, CF_ZONE_ID and MAXWELL_EMAIL_DOMAIN, or pass --token,
--zone-id and --domain. This script does not configure the local SMTP/IMAP
transport. Email Routing is optional (--enable-routing); destinations must
still be verified in the Cloudflare dashboard. No personal domain or mailbox
is selected by default. Existing DMARC policy is preserved unless requested.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Any

CF_API = "https://api.cloudflare.com/client/v4"


def _cf_request(
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tiny CF API helper. We don't add a dependency for one-off DNS work.

    Raises on any non-2xx with the response body inline. Errors from CF
    are usually one-liners in the `errors[].message` field; surfacing the
    whole JSON is more useful than a bare exception.
    """
    url = f"{CF_API}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"Cloudflare API {method} {path} -> HTTP {e.code}\n{body_text}"
        ) from None
    if not payload.get("success", False):
        raise SystemExit(
            f"Cloudflare API {method} {path} returned success=false:\n"
            f"{json.dumps(payload, indent=2)}"
        )
    return payload


def _existing_record(
    token: str, fqdn: str, rtype: str, *, zone_id: str, content_prefix: str = ""
) -> dict[str, Any] | None:
    """Look up a record by exact name + type, return first match or None.

    CF's `match=any` lets name be a substring; we want exact, so we walk
    the results and filter ourselves. Returns None if no match.
    """
    name = fqdn.rstrip(".")
    page = 1
    while True:
        qs = urllib.parse.urlencode({"type": rtype, "name": name, "page": page})
        payload = _cf_request(token, "GET", f"/zones/{zone_id}/dns_records?{qs}")
        for rec in payload.get("result", []):
            content = str(rec.get("content", "")).strip('" ')
            if (
                rec.get("name", "").rstrip(".") == name
                and rec.get("type") == rtype
                and (not content_prefix or content.split()[:1] == [content_prefix])
            ):
                return rec
        if page >= (payload.get("result_info", {}).get("total_pages") or 1):
            return None
        page += 1


def _upsert(
    token: str,
    *,
    zone_id: str,
    fqdn: str,
    rtype: str,
    content: str,
    priority: int | None = None,
    proxied: bool = False,
) -> None:
    """Create-or-update a record. Idempotent on (name, type)."""
    name = fqdn.rstrip(".")
    body: dict[str, Any] = {"type": rtype, "name": name, "content": content}
    if rtype == "MX":
        body["priority"] = int(priority if priority is not None else 10)
    if rtype in {"A", "AAAA", "CNAME"}:
        body["proxied"] = proxied
    existing = _existing_record(
        token,
        name,
        rtype,
        zone_id=zone_id,
        content_prefix="v=spf1"
        if rtype == "TXT" and content.startswith("v=spf1 ")
        else "",
    )
    if existing:
        # If the value is already what we want, skip the write. Re-PUTting
        # a record with the same content produces a `success: true` and
        # bumps the modified_on timestamp for no real reason.
        same_content = existing.get("content") == content
        same_prio = rtype != "MX" or existing.get("priority") == body.get("priority")
        if same_content and same_prio:
            print(f"  = {rtype} {name} (unchanged)")
            return
        rec_id = existing["id"]
        _cf_request(token, "PUT", f"/zones/{zone_id}/dns_records/{rec_id}", body)
        print(f"  ~ {rtype} {name} (updated)")
        return
    _cf_request(token, "POST", f"/zones/{zone_id}/dns_records", body)
    print(f"  + {rtype} {name} (created)")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument(
        "--token", default=os.getenv("CF_API_TOKEN", ""), help="Cloudflare API token"
    )
    p.add_argument(
        "--zone-id",
        default=os.getenv("CF_ZONE_ID", ""),
        help="Cloudflare zone ID (required)",
    )
    p.add_argument(
        "--domain",
        default=os.getenv("MAXWELL_EMAIL_DOMAIN", ""),
        help="Domain belonging to that zone (required)",
    )
    p.add_argument(
        "--mailgun-spf",
        default="include:mailgun.org",
        help="SPF include clause; pass an empty string to skip",
    )
    p.add_argument(
        "--dkim", default="", help="Full DKIM TXT value from your mail provider"
    )
    p.add_argument(
        "--dkim-selector", default="mg", help="DKIM selector supplied by your provider"
    )
    p.add_argument(
        "--dmarc-email",
        default=os.getenv("MAXWELL_DMARC_EMAIL", ""),
        help="Reporting address; omitted means leave DMARC unchanged",
    )
    p.add_argument(
        "--replace-dmarc",
        action="store_true",
        help="Explicitly replace an existing DMARC policy with p=none",
    )
    p.add_argument(
        "--enable-routing",
        action="store_true",
        help="Ask Cloudflare to configure its Email Routing DNS records",
    )
    args = p.parse_args(argv)
    domain = args.domain.strip().lower().rstrip(".")
    zone_id = args.zone_id.strip()
    if not args.token or not zone_id or not domain:
        p.error(
            "--token/CF_API_TOKEN, --zone-id/CF_ZONE_ID and --domain/MAXWELL_EMAIL_DOMAIN are required"
        )
    if not re.fullmatch(r"[a-fA-F0-9]{32}", zone_id):
        p.error("--zone-id must be a 32-character Cloudflare zone ID")
    if not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        domain,
    ):
        p.error(
            "--domain must be a DNS domain name (use punycode for international domains)"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", args.dkim_selector):
        p.error("--dkim-selector must be a DNS selector, not a full domain or URL")
    if args.dmarc_email and not re.fullmatch(
        r"[^\s@;,]+@[^\s@;,]+\.[^\s@;,]+", args.dmarc_email
    ):
        p.error("--dmarc-email must be one email address")
    clause = args.mailgun_spf.strip()
    if clause and not re.fullmatch(r"include:[A-Za-z0-9.-]+", clause):
        p.error("--mailgun-spf must be one include:domain clause, or empty to skip")
    if args.enable_routing and clause:
        p.error(
            "Email Routing locks its SPF record; use --mailgun-spf '' with --enable-routing and configure any combined SPF policy in Cloudflare"
        )

    # Verify the ID/name pair before any write, so a pasted zone ID cannot
    # accidentally direct DNS changes at another domain.
    zone = _cf_request(args.token, "GET", f"/zones/{zone_id}").get("result", {})
    if str(zone.get("name", "")).lower().rstrip(".") != domain:
        p.error("--domain does not match the selected Cloudflare zone")

    if args.enable_routing:
        # Cloudflare selects the MX hosts and priorities for this zone.
        # https://developers.cloudflare.com/api/resources/email_routing/subresources/dns/methods/create/
        _cf_request(args.token, "POST", f"/zones/{zone_id}/email/routing/dns")
        print(f"Email Routing DNS configured for {domain}")

    if clause:
        existing = _existing_record(
            args.token, domain, "TXT", zone_id=zone_id, content_prefix="v=spf1"
        )
        if existing:
            current = existing["content"].strip('" ').split()
            if clause not in current:
                current.insert(1, clause)
                _cf_request(
                    args.token,
                    "PUT",
                    f"/zones/{zone_id}/dns_records/{existing['id']}",
                    {"type": "TXT", "name": domain, "content": " ".join(current)},
                )
                print(f"SPF include added for {domain}")
            else:
                print(f"SPF unchanged for {domain}")
        else:
            _upsert(
                args.token,
                zone_id=zone_id,
                fqdn=domain,
                rtype="TXT",
                content=f"v=spf1 {clause} -all",
            )

    if args.dmarc_email:
        name = f"_dmarc.{domain}"
        existing = _existing_record(args.token, name, "TXT", zone_id=zone_id)
        if existing and not args.replace_dmarc:
            print(
                f"Existing DMARC policy preserved for {domain}; use --replace-dmarc to change it"
            )
        else:
            _upsert(
                args.token,
                zone_id=zone_id,
                fqdn=name,
                rtype="TXT",
                content=f"v=DMARC1; p=none; rua=mailto:{args.dmarc_email}",
            )

    if args.dkim:
        _upsert(
            args.token,
            zone_id=zone_id,
            fqdn=f"{args.dkim_selector}._domainkey.{domain}",
            rtype="TXT",
            content=args.dkim,
        )

    print(f"Done. Verify SPF and DKIM for {domain} in your mail provider dashboard.")
    if args.enable_routing:
        print(
            "Verify your destination address and create a routing rule in Cloudflare."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
