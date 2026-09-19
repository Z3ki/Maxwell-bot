# Email tools: SMTP + IMAP

Maxwell's current email tools use standard SMTP and IMAP settings. The default examples target a local Postfix + Dovecot deployment, but the bot only cares about the configured host/port/account values.

The main tools are:

- `email_send`
- `email_read_inbox`
- `email_get_message`
- `email_search`

They do not require Mailgun or Gmail.

## How the default local setup works

```text
Maxwell --SMTP--> Postfix ----> recipient mail server
Maxwell <--IMAP-- Dovecot <---- local mailbox delivery
```

Blocking SMTP/IMAP operations are moved off the asyncio event loop so mail server timeouts do not freeze the Discord bot.

## Bot configuration

Example `.env` values:

```ini
ENABLE_EMAIL_TOOLS=true

MAXWELL_SMTP_HOST=127.0.0.1
MAXWELL_SMTP_PORT=25
MAXWELL_IMAP_HOST=127.0.0.1
MAXWELL_IMAP_PORT=993
MAXWELL_EMAIL_USER=bot@example.org
MAXWELL_EMAIL_PASSWORD=replace-with-mailbox-password
MAXWELL_EMAIL_FROM=bot@example.org
MAXWELL_EMAIL_FROM_NAME=Maxwell
```

The exact advanced defaults and feature detection are documented in `../.env.example` and `config.py`.

If the required mailbox credentials/configuration are absent, the email tools should fail as unconfigured rather than crashing the bot. To remove the tools entirely from model access, set:

```ini
ENABLE_EMAIL_TOOLS=false
```

## Local Postfix + Dovecot example

A common self-hosted setup is Postfix for SMTP delivery and Dovecot for IMAP access. On Debian/Ubuntu the base packages are typically:

```bash
sudo apt install postfix dovecot-core dovecot-imapd dovecot-lmtpd
```

That package command is only a starting point. Before exposing mail service to the internet, configure authentication, mailbox ownership/permissions, TLS, relay restrictions, spam/abuse controls, and DNS according to the Postfix/Dovecot documentation and your hosting provider.

Many VPS providers restrict outbound port 25. If direct delivery is blocked, use an authenticated smart-host/relay instead of assuming direct SMTP delivery will work.

## DNS: SPF, DKIM, DMARC

Production outbound mail should have correct SPF/DKIM/DMARC alignment for the domain you send from. The exact records depend on the delivery/signing provider.

`setup_dns.py` in this directory is a **current, configurable Cloudflare DNS helper** for Mailgun-style/provider DNS records. It is not hardcoded to a personal domain or zone.

It requires an explicit Cloudflare API token, zone ID, and domain, supplied with arguments or the documented environment variables. It validates that the selected zone matches the supplied domain before writing records. Existing DMARC policy is preserved unless replacement is explicitly requested.

Example shape:

```bash
export CF_API_TOKEN='your-cloudflare-token'
python3 email_integration/setup_dns.py \
  --zone-id YOUR_ZONE_ID \
  --domain example.org \
  --mailgun-spf include:mailgun.org \
  --dkim-selector mg \
  --dkim 'k=rsa; p=YOUR_PROVIDER_PUBLIC_KEY'
```

Use `python3 email_integration/setup_dns.py --help` for the current option list.

Important: this DNS helper does **not** configure Maxwell's SMTP/IMAP transport, Postfix, Dovecot, or mailbox credentials. It only manages the DNS records its command options describe.

## Cloudflare Email Routing

The DNS helper can optionally request Cloudflare Email Routing setup when explicitly enabled. Destination verification and routing rules still require the corresponding Cloudflare configuration; DNS setup alone does not create a working mailbox for Maxwell.

## Legacy Mailgun/Gmail design

[`LEGACY_MAILGUN.md`](LEGACY_MAILGUN.md) documents the older Maxwell mail flow that sent through Mailgun and read forwarded mail through Gmail. That transport is archived and is not the current bot path.

The legacy document remains useful for historical/provider-specific DNS context, but do not copy its old transport assumptions into the current SMTP/IMAP setup.

## TLS and certificates

Remote mail servers should present certificates that validate normally. For a private CA, configure the runtime trust store appropriately (for example through the supported certificate environment configuration).

Do not disable certificate verification globally to make a remote mail server work.

## Operational checklist

Before relying on email tools:

1. Confirm SMTP connectivity/authentication from the Maxwell runtime.
2. Confirm IMAP connectivity/authentication and mailbox visibility.
3. Confirm the sender domain's SPF/DKIM/DMARC setup.
4. Confirm your provider permits outbound delivery or configure a relay.
5. Test delivery to multiple providers and inspect bounces/spam placement.
6. Keep mailbox credentials out of Git and logs.

Mail delivery reputation and recipient-provider policy are external to Maxwell; successful SMTP submission does not guarantee inbox placement.
