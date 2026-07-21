# LEGACY: Mailgun + Gmail + Cloudflare (archived 2026-07-21)

This file documents the **old** mail flow that Maxwell used before
switching to local Postfix + Dovecot. The bot no longer reads
`MAILGUN_*` or `GMAIL_*` env vars. This file is kept only because:

1. The DKIM/SPF/DMARC setup notes are still useful for any local mail
   server (Postfix, OpenSMTPD, Rspamd, OpenDKIM, etc.) — strip the
   Mailgun-specific bits and the rest is generic.
2. The `setup_dns.py` script is a working Cloudflare API tool that
   some operators may want to reuse for other zones.

For the current bot flow, read [`README.md`](README.md) in this
directory.

---

## Old design (pre-2026)

Outbound was sent through Mailgun (HTTP API, no SMTP server). Inbound
was caught by Cloudflare Email Routing and forwarded to a Gmail inbox;
the bot read Gmail back over the Gmail REST API.

```
   bot.py  --HTTP-->  Mailgun          --SMTP-->  recipient
   bot.py  <--HTTPS-- Gmail API  <--forwarded--  CF Email Routing
                                                <--SMTP--  sender
```

## Files

- `setup_dns.py` — DNS drop script (idempotent). Currently hardcoded for
  the `z3ki.dev` Cloudflare zone; edit the `ZONE` constant at the top
  to use it for another zone. Re-run with different flags any time.

## SPF / DKIM / DMARC notes (still useful)

The `setup_dns.py` script drops three records:

- `MX <your-domain>` → `route1.mx.cloudflare.net` (priority 10) — for
  Cloudflare Email Routing.
- `TXT <your-domain>` SPF chain — extends if one already exists; the
  script's `--mailgun-spf` flag adds the Mailgun include, but you can
  substitute any ESP's SPF include (`include:_spf.google.com`,
  `include:amazonses.com`, etc.).
- `TXT _dmarc.<your-domain>` DMARC — defaults to `p=none` with
  reporting to a Gmail address. The DMARC record is independent of the
  transport.
- `TXT <selector>._domainkey.<your-domain>` DKIM — for any DKIM
  signer. With Mailgun the selector is usually `mg`; with OpenDKIM it's
  whatever you configured in `opendkim.conf`.

Without SPF + DKIM, mail you send from a fresh VPS to Gmail/Outlook/
Yahoo will land in spam or get rejected outright. Google returns
`550 5.7.26 — your email has been blocked because the sender is
unauthenticated` when neither is present. Set them up before sending
anything important.
