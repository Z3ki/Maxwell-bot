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

## Configurable DNS helper

`setup_dns.py` and its compatibility entry point `setup_dns_legacy.py` now
require an explicit domain and zone ID. They have no personal domain, zone,
reporting mailbox, or host path defaults.

```bash
export CF_API_TOKEN='your-cloudflare-token'
python3 email_integration/setup_dns.py \
  --zone-id YOUR_32_CHARACTER_ZONE_ID \
  --domain example.org \
  --mailgun-spf include:mailgun.org \
  --dkim-selector mg \
  --dkim 'k=rsa; p=YOUR_PROVIDER_PUBLIC_KEY' \
  --dmarc-email reports@example.org
```

`CF_ZONE_ID` and `MAXWELL_EMAIL_DOMAIN` are environment alternatives. The token
needs permission to read the zone and edit its DNS. The helper checks that the
zone ID belongs to the supplied domain before writing anything.

- SPF extends the existing policy without replacing unrelated TXT records.
- DKIM uses the selector supplied by your provider.
- DMARC is untouched without `--dmarc-email`. An existing policy is preserved
  unless you explicitly pass `--replace-dmarc`; replacement creates `p=none`.
- MX and Email Routing are untouched by default. For routing-only setup, use
  `--enable-routing --mailgun-spf ''`. Cloudflare's
  [DNS setup endpoint](https://developers.cloudflare.com/api/resources/email_routing/subresources/dns/methods/create/)
  selects and locks the necessary MX and SPF records. Configure a combined
  forwarding/sending SPF policy in Cloudflare rather than trying to overwrite
  its managed record. Destination verification and forwarding rules remain
  separate steps in the dashboard.

This helper does not set up Maxwell's current SMTP/IMAP transport; see the
[current mail guide](README.md).

Without SPF + DKIM, mail you send from a fresh VPS to Gmail/Outlook/
Yahoo will land in spam or get rejected outright. Google returns
`550 5.7.26 — your email has been blocked because the sender is
unauthenticated` when neither is present. Set them up before sending
anything important.
