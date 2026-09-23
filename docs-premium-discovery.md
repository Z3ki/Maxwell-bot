# Premium discovery preparation

Maxwell's software and existing free model behavior remain free and unchanged. This change does not implement billing, entitlements, subscription transfers, or model routing.

## Existing command paths

- `bot.py` syncs `USER_INSTALL_COMMANDS` globally on ready. `user_install.py` routes interactions through registered handlers before AI turns. The bundled extras plugin adds `/owner` and extended personal commands before sync.
- `,help` is a static prefix-command listing in `bot.py`. There was no public `/help` or `/usage` slash command. Owner `/owner quota` reads `DailyTokens`; user AI requests are charged by `bot.py` into `daily_tokens.sqlite3`.
- Quota exceeded messages originate from `DailyTokens` and are sent without upgrade links. No paid model routing or paid quota exists.

## Launch control and commands

`premium_discovery_state` in `bot_control.json` is the only discovery control. Its default is `off`. Values: `off` hides `/premium`; `preview` registers an ephemeral, explicitly unavailable proposal; `launched` is reserved for integration with verified plan data and currently reports subscriptions unavailable. Reconnect/restart triggers slash command reconciliation after a state change. This control cannot enable payment, entitlements, or routing. Keep it `off` until explicitly approved.

`/help` lists `/premium` only in preview or launched state. The prefix `,help` does the same. `/usage` reports the requesting user's existing UTC token accounting and has an optional `notices` argument (`on`/`off`). That preference persists in `premium_discovery.sqlite3`. There is no promotional usage line until a verified entitlement catalog and actual launch integration exist. Ordinary messages and AI prompts are untouched.

The static `web/pricing/index.html` is an unpublished proposal page for Maxwell's website (`maxwell.z3ki.dev`); the separate `z3ki.dev` homepage is outside this repository. It has no checkout and is not linked from the home page. Before publishing, replace proposed prices and benefits with verified values from the future centralized plan service and gate the route as needed.

## Announcements

`DiscoveryStore` persists an administrator-authorized channel per guild and an atomic receipt per `(launch_id, guild_id)`. No scheduler, command, or sending code is connected. A future owner-approved launch process must verify administrator authority before calling `opt_in_channel`, verify the payment service and approved content, and use `claim_announcement` immediately before sending. The durable claim prevents repeats after restarts; a crash after claim may leave an announcement unsent and requires manual review. Never broadcast across all guilds or send user DMs.

## Future integration gates

Before showing live plan details or a `/usage` notice, add a verified central plan catalog, actual entitlement accounting, safe model fallback, eligibility and transfer handling, and separate tests for those systems. Do not infer a paid state from the discovery flag. Confirm launch authorization and website publication separately.
