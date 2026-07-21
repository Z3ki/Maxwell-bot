#!/usr/bin/env python3
"""LEGACY — Drop the DNS records for maxwell@z3ki.dev in one shot.

Archived 2026-07-21: Maxwell switched to local Postfix + Dovecot and
no longer uses Mailgun/Gmail. This script is kept as a working
Cloudflare DNS tool for the `z3ki.dev` zone (or any zone — edit the
ZONE constant). See LEGACY_MAILGUN.md for context and SPF/DKIM setup
notes that are still useful for any local mail server.

What this does (idempotent — safe to re-run):

  1. Enables Cloudflare Email Routing for the zone (z3ki.dev).
  2. Adds an MX record pointing to the CF Email Routing target so mail for
     maxwell@z3ki.dev is actually accepted by CF and forwarded onward.
  3. Adds an SPF TXT record that includes Mailgun's send infrastructure so
     mail we send from maxwell@z3ki.dev passes SPF checks at the receiver.
  4. Adds a DMARC TXT record at _dmarc.z3ki.dev with a permissive policy
     (none) and a reporting address. Tighten this later once you trust the
     deliverability.
  5. Adds the DKIM TXT record that Mailgun gives you after you verify
     domain ownership in their dashboard. Pass it via --dkim "k=rsa; p=..."
     (the full TXT value), NOT just the public key.

Run order:

  1. Cloudflare dashboard -> My Profile -> API Tokens -> Create Token ->
     Custom token. Permissions: Zone / DNS / Edit + Zone / Email Routing /
     Edit. Zone Resources: Include / Specific zone / z3ki.dev. Save the
     token somewhere safe; it's the one with the real powers (the one you
     pasted earlier only had read access, hence the auth errors).
  2. Mailgun dashboard -> Sending -> Domains -> Add z3ki.dev. They hand
     you a set of DNS records; the only ones this script sets are the
     Mailgun-specific SPF (we extend the existing v=spf1) and DKIM.
     Mailgun will check and tick them green on its end.
  3. Run this script:

       python3 email/setup_dns.py --token cfat_... \\
           --mailgun-spf "include:mailgun.org" \\
           --dkim "k=rsa; p=MIGfMA0GCSq..."

     If you skip --mailgun-spf or --dkim, the script logs a warning and
     continues. You can re-run with the missing piece without breaking
     anything that's already in place.

  4. Mailgun -> Domain settings -> Verify DNS. If anything's red, fix and
     re-run this script.

  5. Cloudflare dashboard -> Email -> Email Routing -> Routes. Create a
     route for maxwell@z3ki.dev -> z3kilol77@gmail.com. CF will email
     that address a verification link; click it. THIS STEP IS MANUAL on
     purpose: it's the spam-canary check and you should see the email
     land before trusting forwarding.

This script DOES NOT create the CF Email Routing rule (the destination
address). That has to happen in the dashboard or via a separate API call
after the destination is verified, because CF will email z3kilol77@gmail.com
and we shouldn't programmatically dismiss that handshake.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any

CF_API = "https://api.cloudflare.com/client/v4"
ZONE = "da3a6ecd035d0925aad967b4db3fe14d"  # z3ki.dev

# CF Email Routing target hostname. This is the same for every zone on the
# free plan; do not invent your own.
CF_EMAIL_ROUTING_TARGET = "route1.mx.cloudflare.net"


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


def _existing_record(token: str, fqdn: str, rtype: str) -> dict[str, Any] | None:
    """Look up a record by exact name + type, return first match or None.

    CF's `match=any` lets name be a substring; we want exact, so we walk
    the results and filter ourselves. Returns None if no match.
    """
    name = fqdn.rstrip(".")
    qs = urllib.parse.urlencode({"type": rtype, "name": name})
    payload = _cf_request(token, "GET", f"/zones/{ZONE}/dns_records?{qs}")
    for rec in payload.get("result", []):
        if rec.get("name", "").rstrip(".") == name and rec.get("type") == rtype:
            return rec
    return None


def _upsert(
    token: str,
    *,
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
    existing = _existing_record(token, name, rtype)
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
        _cf_request(token, "PUT", f"/zones/{ZONE}/dns_records/{rec_id}", body)
        print(f"  ~ {rtype} {name} (updated)")
        return
    _cf_request(token, "POST", f"/zones/{ZONE}/dns_records", body)
    print(f"  + {rtype} {name} (created)")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument(
        "--token",
        default=os.environ.get("CF_API_TOKEN", ""),
        help="Cloudflare API token with Zone DNS Edit + Email Routing Edit on z3ki.dev",
    )
    p.add_argument(
        "--mailgun-spf",
        default="include:mailgun.org",
        help=(
            "SPF include clause for Mailgun. Default is fine for the US region. "
            "EU uses include:eu.mailgun.org."
        ),
    )
    p.add_argument(
        "--dkim",
        default="",
        help=(
            "Full DKIM TXT value from Mailgun dashboard (starts with k=rsa;). "
            "Required if you want the bot to send. Skip for receive-only."
        ),
    )
    p.add_argument(
        "--dmarc-email",
        default="z3kilol77@gmail.com",
        help="Where to send DMARC aggregate reports. Default z3kilol77@gmail.com.",
    )
    args = p.parse_args(argv)

    if not args.token:
        print(
            "Error: --token (or CF_API_TOKEN env) is required.\n"
            "Generate one at https://dash.cloudflare.com/profile/api-tokens with\n"
            "Zone DNS Edit + Email Routing Edit on z3ki.dev.",
            file=sys.stderr,
        )
        return 2

    # Step 1: enable email routing for the zone. The endpoint is idempotent:
    # calling it on an already-enabled zone returns success=true with the
    # current state, no harm done.
    print("1. Enabling Cloudflare Email Routing for z3ki.dev ...")
    _cf_request(
        args.token, "POST", f"/zones/{ZONE}/email/routing/enable", {"enabled": True}
    )

    # Step 2: MX record. CF gives every zone the same routing target so we
    # don't need to discover it; route1 is the canonical one.
    print("2. Setting MX record for z3ki.dev ...")
    _upsert(
        args.token,
        fqdn="z3ki.dev",
        rtype="MX",
        content=CF_EMAIL_ROUTING_TARGET,
        priority=10,
    )

    # Step 3: SPF TXT. We extend the existing v=spf1 chain (or create one)
    # with the Mailgun include. If you also use, say, Google Workspace for
    # this domain later, add "include:_spf.google.com" to the same list.
    print("3. Setting SPF TXT record for z3ki.dev ...")
    spf_target = f"v=spf1 {args.mailgun_spf} -all"
    existing_spf = _existing_record(args.token, "z3ki.dev", "TXT")
    if existing_spf and "v=spf1" in existing_spf.get("content", ""):
        # Already have an SPF chain. Splice the Mailgun include into the
        # existing record (idempotent splice — if it's already there, noop).
        current = existing_spf["content"]
        clause = args.mailgun_spf.strip()
        # Cloudflare TXT content is normally a single string but the
        # underlying API stores it as-is; if someone added a multi-string
        # SPF we would have a different problem. Assume single string here.
        if clause in current:
            print(f"  = TXT z3ki.dev (SPF already includes {clause})")
        else:
            new_content = current.replace(
                "v=spf1 ", f"v=spf1 {clause} ", 1
            )
            _cf_request(
                args.token,
                "PUT",
                f"/zones/{ZONE}/dns_records/{existing_spf['id']}",
                {"type": "TXT", "name": "z3ki.dev", "content": new_content},
            )
            print(f"  ~ TXT z3ki.dev (added {clause} to SPF chain)")
    else:
        _upsert(args.token, fqdn="z3ki.dev", rtype="TXT", content=spf_target)

    # Step 4: DMARC. Permissive policy (p=none) with aggregate reports.
    # Move this to p=quarantine or p=reject once you have 30 days of clean
    # reports and trust the deliverability.
    print("4. Setting DMARC TXT record at _dmarc.z3ki.dev ...")
    dmarc_value = (
        f"v=DMARC1; p=none; rua=mailto:{args.dmarc_email}; "
        f"ruf=mailto:{args.dmarc_email}; fo=1; adkim=s; aspf=s"
    )
    _upsert(args.token, fqdn="_dmarc.z3ki.dev", rtype="TXT", content=dmarc_value)

    # Step 5: DKIM. Mailgun gives you a single TXT record to drop at
    # <selector>._domainkey.z3ki.dev. Default selector for Mailgun is
    # "mg" but they tell you exactly what it is on the dashboard; you can
    # override with --dkim-host if you use a different provider later.
    if args.dkim:
        print("5. Setting DKIM TXT record at mg._domainkey.z3ki.dev ...")
        _upsert(
            args.token,
            fqdn="mg._domainkey.z3ki.dev",
            rtype="TXT",
            content=args.dkim,
        )
    else:
        print(
            "5. DKIM skipped (no --dkim value). Mailgun will refuse to send "
            "until DKIM is in place. Re-run with --dkim to add it."
        )

    print()
    print("Done. Next manual steps:")
    print("  - Cloudflare -> Email -> Email Routing -> Custom Addresses")
    print("    Add maxwell@z3ki.dev -> z3kilol77@gmail.com (CF will email")
    print("    z3kilol77@gmail.com a verification link; click it).")
    print("  - Mailgun -> Sending -> Domains -> z3ki.dev -> DNS records.")
    print("    Click 'Verify DNS records' and confirm SPF + DKIM are green.")
    print("  - Then drop the Mailgun API key + Gmail OAuth creds into /root/maxwell/.env")
    print("    and restart the bot.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
