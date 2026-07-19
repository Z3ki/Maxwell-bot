# Email: <maxwell@z3ki.dev>

Outbound is sent through Mailgun (HTTP API, no SMTP server). Inbound is
caught by Cloudflare Email Routing and forwarded to the Gmail below;
the bot reads Gmail back over the Gmail REST API.

No IMAP server, no port 25. Works on a Contabo box that blocks inbound
25 by default. Concretely:

```
   bot.py  --HTTP-->  Mailgun          --SMTP-->  recipient
   bot.py  <--HTTPS-- Gmail API  <--forwarded--  CF Email Routing
                                               <--SMTP--  sender
```

## What the bot can do

The four new tools in `bot_tools.py`:

- `email_send` — POST a message to Mailgun. Subject, body, to/cc/bcc.
- `email_read_inbox` — list recent messages forwarded from `maxwell@z3ki.dev`.
- `email_get_message` — fetch a single message body by Gmail id.
- `email_search` — full Gmail search (e.g. `from:github subject:security`).

Send is `is_destructive=True`, so on a tainted turn (one that just
read a fetched URL / web result) the model has to ask the user to
`,confirm` before it goes out. Same gate as `shell` and `sub_agent`.

## One-time setup

### 1. Cloudflare API token with DNS write access

The token you handed over first had read-only access. The DNS records
below need **write**. Make a new one:

1. <https://dash.cloudflare.com/profile/api-tokens>
2. Create Token → Custom token
3. Permissions:
   - Zone / DNS / Edit
   - Zone / Email Routing / Edit
4. Zone Resources: Include → Specific zone → `z3ki.dev`
5. Create → copy the token. Pass it to `setup_dns.py` with `--token` or
   export it as `CF_API_TOKEN`.

### 2. Mailgun sending domain

1. <https://app.mailgun.com> → Sending → Domains → Add `z3ki.dev`
2. Mailgun shows you three DNS records: SPF (TXT), DKIM (TXT at
   `mg._domainkey.z3ki.dev`), and one CNAME for tracking. We set
   the first two; the CNAME is optional (turn it off if you don't
   want opens).
3. After the dashboard accepts the records, copy:
   - The **API key** (top right, "API keys" or "Account settings")
   - The **sending domain** (Mailgun shows this — usually `mg.z3ki.dev`
     or `sandboxXXXX.mailgun.org` for the sandbox tier)

### 3. Gmail OAuth (for reading)

This is the awkward part. Gmail needs a refresh token with
`gmail.readonly` scope, and Google only hands those out to interactive
flows. From your laptop (not the server):

```bash
# Install: pip install google-auth-oauthlib
python3 -c "
from google_auth_oauthlib.flow import InstalledAppFlow
import json

flow = InstalledAppFlow.from_client_secrets_file(
    'client_secret.json',                 # from console.cloud.google.com
    scopes=['https://www.googleapis.com/auth/gmail.readonly'],
)
creds = flow.run_local_server(port=0)
print('CLIENT_ID:', creds.client_id)
print('CLIENT_SECRET:', creds.client_secret)
print('REFRESH_TOKEN:', creds.refresh_token)
"
```

The `client_secret.json` is the OAuth "Desktop app" client from
<https://console.cloud.google.com/apis/credentials> (project: any; enable
the Gmail API first).

### 4. Run the DNS script

```bash
cd /root/maxwell/email_integration
python3 setup_dns.py \
  --token cfat_...your_new_write_token... \
  --mailgun-spf "include:mailgun.org" \
  --dkim "k=rsa; p=MIGfMA0GCSq..."
```

It drops:

- MX `z3ki.dev` → `route1.mx.cloudflare.net` (priority 10)
- TXT `z3ki.dev` SPF (extends if one already exists)
- TXT `_dmarc.z3ki.dev` DMARC (p=none, reporting to your gmail)
- TXT `mg._domainkey.z3ki.dev` DKIM (if you pass `--dkim`)

Idempotent. Re-run with different flags any time.

### 5. CF Email Routing destination (manual)

Cloudflare dashboard → Email → Email Routing → Custom Addresses.

Add: `maxwell@z3ki.dev` → `z3kilol77@gmail.com`

CF emails `z3kilol77@gmail.com` a verification link. Click it. This is
intentionally a manual step — it's a spam-canary check. If a malicious
actor got hold of the zone and added their own forwarding rule, the
verification email would land in your inbox and you'd notice.

### 6. Put the creds in `.env`

Append to `/root/maxwell/.env`:

```
MAILGUN_API_KEY=key-...
MAILGUN_DOMAIN=mg.z3ki.dev
MAILGUN_REGION=us
MAILGUN_FROM_ADDRESS=maxwell@z3ki.dev
MAILGUN_FROM_NAME=Maxwell

GMAIL_CLIENT_ID=...apps.googleusercontent.com
GMAIL_CLIENT_SECRET=GOCSPX-...
GMAIL_REFRESH_TOKEN=1//0g...
GMAIL_USER=z3kilol77@gmail.com
```

Restart `bot.py` and the new tools are live.

## Verifying it works

Send:

- In Discord: `email test from the bot` and ask the bot to send a
  message to `z3kilol77@gmail.com`. Mailgun will queue it; it should
  land in ~30s.

Receive:

- Email `maxwell@z3ki.dev` from any external address.
- CF forwards to `z3kilol77@gmail.com`; the bot reads it back via the
  Gmail API when you ask for `email_read_inbox`.

## What happens if Mailgun flags the bot

Mailgun is strict about domain reputation. The first 100 emails are on
the free tier; if the bot sends a burst, expect a throttling email from
Mailgun. Mitigation: keep the bot from sending the same content to many
recipients in a tight loop. The prompt-injection guard on `email_send`
already prevents a single compromised turn from spamming, but you can
also add a per-hour counter to `bot_tools.EmailSendTool.execute` if
you want belt-and-braces. Not enabled by default.

## What this is NOT

- Not a full mail server. There's no POP/IMAP for arbitrary clients
  (Thunderbird, Apple Mail). If you want that, run Stalwart or
  Mailcow on a host with inbound port 25.
- Not encrypted at rest. Mailgun and Gmail store mail forever; if you
  need E2E, use PGP inline (not implemented).
- Not migrated from existing mail. If `maxwell@z3ki.dev` was on
  another provider, forward from there into Gmail and let the bot see
  the same stream.

## Files

- `setup_dns.py` — DNS drop script (idempotent, see top of file for usage)
- This README
