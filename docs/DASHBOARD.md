# Discord settings dashboard

The dashboard at `/dashboard/` lets Discord users edit their own reply preferences and personal AI connection, and manage servers they own or where they have **Administrator** or **Manage Server**. `/config` remains available in Discord. Its first screen now has language, answer length and visibility controls, plus a dashboard link when configured.

Both interfaces use the same personal preference file, encrypted BYOK vault and server settings. Personal changes apply on the next request. Server controls reload within five seconds. The dashboard never grants application-owner or operator privileges.

## Enable it on your host

Use the **same Discord application as the bot**. In [Discord Developer Portal](https://discord.com/developers/applications), open OAuth2 and add this exact redirect URI, replacing the hostname:

```
https://dashboard.example.com/api/dashboard/callback
```

Add these values to the bot's `.env` (the API reads the same file):

```dotenv
MAXWELL_DASHBOARD_URL=https://dashboard.example.com
DISCORD_CLIENT_ID=your_application_id
DISCORD_CLIENT_SECRET=your_oauth_client_secret
MAXWELL_DASHBOARD_SECRET=your_64_character_hex_secret
# Existing bot token is also used to find servers where Maxwell is installed.
DISCORD_BOT_TOKEN=your_existing_bot_token
# Optional: use the existing BYOK encryption key to enable personal AI connections.
MAXWELL_BYOK_ENCRYPTION_KEY=your_existing_64_character_hex_key
```

Generate a new dashboard secret with `python -c 'import secrets; print(secrets.token_hex(32))'`. Keep it private and stable across restarts. Rotating it invalidates existing browser sessions. Do not replace an existing BYOK key: that would make saved provider credentials unreadable.

Configure DNS for the dashboard subdomain and use the dedicated dashboard block in [`examples/Caddyfile.example`](../examples/Caddyfile.example). It proxies only `/dashboard`, `/dashboard/*` and `/api/dashboard/*` to the existing API on `127.0.0.1:8765`. The API serves the dashboard assets directly from the checkout, so no frontend build or separate service is needed. Keep the operator API on its own origin and keep it bound to loopback. Restart the API and bot using your existing supervisor after changing `.env`, then reload Caddy.

**Use a different hostname from generated sites.** User-created pages must never share the dashboard origin. `MAXWELL_DASHBOARD_URL` is a bare HTTPS origin, without `/dashboard/`. Configuration rejects the generated-site hostname. If you serve additional user content on another hostname, ensure that hostname also differs from the dashboard. HTTP is allowed only for localhost development; production cookies always require HTTPS.

The public website's `/dashboard/` link redirects to the dedicated dashboard hostname in the Caddy example. Replace all example hostnames before use. The dashboard also appears directly in Discord `/config` when `MAXWELL_DASHBOARD_URL` is set.

## Login and access

- Login requests only `identify` and `guilds`, without email or message-reading scopes.
- A browser-bound, single-use state nonce expires after five minutes. Login rotates any old browser session.
- Session cookies are opaque, Secure, HttpOnly, SameSite=Lax and host-only. Discord access tokens are encrypted in a private server-side SQLite database. They never enter JavaScript or local storage.
- Sessions expire after eight hours or when the Discord token expires, whichever comes first. Sign out deletes the server session. Refresh tokens are not retained.
- Every server read and save fetches current user permissions from Discord and verifies that Maxwell is still a member. Ordinary members cannot read or edit server settings by changing a URL.
- Saves require a session, the exact dashboard Origin and a session-specific CSRF token. API keys are encrypted by the existing vault; only redacted connection metadata is returned.
- The bot token, OAuth secret, provider keys, bot-wide controls, diagnostics, messages and other users' preferences are never returned by this API.
- A missing or invalid dashboard configuration disables login while Discord configuration remains usable. Provider setup is optional; Maxwell's included AI remains the default.

Server settings cover response channels, allowed tool groups (including moderation), progress messages, ticket greetings, and optional plugin tools. A channel must belong to the selected server; Maxwell also needs Discord channel permissions to respond there. Changing a response-channel restriction keeps the existing autonomy block behavior and preserves manual operator blocks.

Settings writers lock read-modify-write operations and merge only the selected guild or user, preserving other accounts and servers. Corrupt JSON is left intact and writes fail closed. Only one live bot deployment should use a data directory, as with the existing bot deployment model.

## Verify after deployment

Open the dashboard, sign in, save a personal language preference, and confirm it in `/config`. Use an administrator account to save a server setting and confirm it in Discord. Check that a normal member cannot access that server's settings, that logging out requires a new sign-in, and that `/api/control` on the dashboard origin returns 404 from Caddy. Actual OAuth sign-in requires your configured application credentials and registered redirect URI; automated tests use mocked Discord responses.
