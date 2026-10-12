# Natural Discord replies

Maxwell can answer a casual ping, greeting, joke or thanks with a reaction, GIF or sticker. Substantive questions and tasks still call for an answer. These features are available to free and premium users under the same Discord permission rules.

`react(emoji=..., finish=true)` is a complete response by default. It records confirmed delivery and stops both further calls in that batch and automatic prose. Use `finish=false` when the reaction accompanies work or a written answer. An optional `message_id` targets another message in the current channel only.

When Maxwell can read a channel but cannot send, a direct ping receives a mute reaction, or joins an existing Unicode reaction when appropriate, if Discord allows it. Otherwise the request ends silently. This path does not invoke the language model, post typing/progress messages, DM the user, or retry the request. Threads use Send Messages in Threads. Reactions need View Channel and Read Message History; Add Reactions is needed only when creating a new reaction.

## Giphy setup

Create a key at https://developers.giphy.com/ and set this privately in the bot's `.env`:

```dotenv
GIPHY_API_KEY=your_key
```

Restart the bot after changing environment configuration. Without a key, `send_gif` is absent from the model's tools. With a key, Maxwell searches for a short phrase and sends one matching GIF with Powered By GIPHY attribution. It can select another of the first ten search results. Search uses Giphy's PG-13 ceiling; provider quotas still apply. No extra Python dependency is needed. API failures do not expose the key or claim a GIF was delivered.

Official API reference: https://developers.giphy.com/docs/api/endpoint/search/

## Emojis and stickers

- Current-server emojis, including animated emojis and role-based availability, are read from Discord's live guild objects. Server assets stay scoped to that server.
- Application emojis uploaded in Discord Developer Portal are available across Maxwell's chats, including DMs. Discord does not require Use External Emojis for application-owned emojis.
- Official sticker packs, including Wumpus when returned by Discord, are fetched using the official sticker-pack endpoint. Current-server stickers and official stickers are selected with `[STICKER (name)]` in `send_message`. Ordinary words never trigger stickers. Discord controls which native stickers a bot can send; listing a pack is not an entitlement to a Nitro-only sticker.
- `list_expressions(query=...)` searches the full current catalog when the small prompt preview omits a name. The emoji reference image includes current-server and application emojis plus raster stickers when requested.

Server joins, leaves and emoji/sticker updates refresh the server catalog immediately. Reconnects rebuild it. Application emojis and official sticker packs refresh on ready and every five minutes without a restart. Successful refreshes replace the old snapshot, so renames and deletions disappear; transient failures retain the last good snapshot and are retried at the next refresh. Only one refresh worker runs, and shutdown cancels it.

Private/user-installed interaction replies use Discord webhooks. They retain their existing private visibility and do not grant permission to react publicly or access another server.

Official references:

- https://docs.discord.com/developers/resources/emoji
- https://docs.discord.com/developers/resources/sticker
- https://docs.discord.com/developers/resources/message#create-reaction

## Verification

The offline regression suite covers terminal reactions/GIFs, reaction and thread permissions, read-only pings, application emoji scoping, animated expressions, explicit sticker dispatch, periodic refresh/cancellation, optional Giphy registration, Giphy HTTP failures, unsafe URLs, attribution and API-key redaction. Live Giphy delivery and native sticker availability still need a configured key and a Discord test channel.
