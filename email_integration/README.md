# Email tools (local Postfix + Dovecot)

The four email tools in `bot_tools.py` — `email_send`, `email_read_inbox`,
`email_get_message`, `email_search` — talk to a **local Postfix + Dovecot
mail server** on `127.0.0.1` by default. They do **not** use Mailgun, Gmail,
or any other third-party service. There is nothing to sign up for.

This file is the install guide. The legacy Mailgun/Gmail flow and the
`setup_dns.py` Cloudflare script in this directory are **archived** —
see [`LEGACY_MAILGUN.md`](LEGACY_MAILGUN.md) if you want the old design
back. They are not wired into the bot.

## How it works

```
   bot.py  --SMTP/25-->  Postfix   --SMTP/25-->  recipient MX
   bot.py  <--IMAPS/993--  Dovecot  <--delivered--  Postfix local
```

- **Outbound**: bot connects to `127.0.0.1:25`, `STARTTLS`, `SASL PLAIN`,
  `MAIL FROM`/`RCPT TO`/`DATA`. Postfix handles all DNS, queueing, retry.
  We never talk to recipient MXes directly.
- **Inbound**: bot connects to `127.0.0.1:993` (IMAPS), `SELECT INBOX`,
  `FETCH`. Postfix's `virtual(5)` transport delivers local mail to
  `/var/mail/vmail/<your-domain>/<mailbox>/Maildir`; Dovecot serves it
  over IMAP.

The blocking I/O (`smtplib`, `imaplib`) runs through `asyncio.to_thread`
so the bot's event loop isn't held up by a 30-second SMTP timeout.

## What you need to install

A local Postfix + Dovecot with virtual mailboxes. Any setup that exposes
SMTP on port 25 and IMAPS on port 993 will work — the bot only cares
about the host/port/user/password. The defaults match a typical
`postfix` + `dovecot-core` + `dovecot-imapd` install on Debian/Ubuntu.

Quick-and-dirty Debian/Ubuntu install (NOT a hardened setup — read the
Postfix/Dovecot docs before exposing this to the internet):

```bash
sudo apt install postfix dovecot-core dovecot-imapd dovecot-lmtpd
# pick "Internet Site" or "Local only" during the postfix install
```

Then:

1. Configure Postfix for virtual mailboxes under `/var/mail/vmail/<domain>/`.
   Add your domain to `virtual_mailbox_domains`, set `virtual_mailbox_maps`
   to a file mapping `user@domain` → `vmail/domain/user/`, and turn on
   SASL auth via Dovecot.
2. Configure Dovecot with the same `vmail` UID/GID, IMAPS on 993, and a
   self-signed cert (or a real one). Set `auth_mechanisms = plain login`
   and point `auth-passwd-file` at `/etc/dovecot/users` with one line
   per mailbox: `user@domain:{PLAIN}password:5000:5000::/var/mail/vmail/domain/user::user@domain`.
3. Make sure port 25 is open outbound — many VPS providers (Contabo,
   Hetzner, etc.) block it by default. If your provider blocks port 25,
   you'll need a smart-host relay.
4. Set the SPF and DKIM TXT records for your domain in DNS. Without
   them, mail you send to Gmail/Outlook/Yahoo will land in spam or get
   rejected outright (Google returns 550 5.7.26 "your email has been
   blocked because the sender is unauthenticated" when neither is
   present). The `LEGACY_MAILGUN.md` file has DKIM setup notes you can
   adapt for any DKIM signer (OpenDKIM, Rspamd, etc.).

## Bot config

Put the following in `.env`:

```ini
ENABLE_EMAIL_TOOLS=true

MAXWELL_SMTP_HOST=127.0.0.1
MAXWELL_SMTP_PORT=25
MAXWELL_IMAP_HOST=127.0.0.1
MAXWELL_IMAP_PORT=993
MAXWELL_EMAIL_USER=bot@yourdomain.example
MAXWELL_EMAIL_PASSWORD=replace-with-dovecot-password
MAXWELL_EMAIL_FROM=bot@yourdomain.example
MAXWELL_EMAIL_FROM_NAME=Maxwell
```

If `MAXWELL_EMAIL_PASSWORD` is empty, the four email tools return
"local mail is not configured" at call time without crashing the bot.

## To disable entirely

Set `ENABLE_EMAIL_TOOLS=false` in `.env` and the four tools are not
registered with the model. No Postfix/Dovecot required.

## What this is NOT

- Not a full mail server. There's no POP/IMAP for arbitrary clients
  (Thunderbird, Apple Mail) out of the box. You can add it by
  configuring Dovecot to publish the same mailbox over POP3, but that's
  outside this README.
- Not encrypted at rest. Mail sits in `Maildir` on disk unencrypted; if
  you need E2E, use PGP inline (not implemented).
- Not migrated from existing mail. If `bot@yourdomain.example` was on
  another provider, forward from there into your local mailbox.

## Files

- `LEGACY_MAILGUN.md` — the old Mailgun + Gmail + Cloudflare design.
  Archived. The bot doesn't use any of it; keep it only as a reference
  for DKIM/SPF setup notes.
- `setup_dns.py` — the legacy Cloudflare DNS script. Also archived.
  Hardcoded for the `z3ki.dev` zone and not generic.
- This README — the current install guide.
