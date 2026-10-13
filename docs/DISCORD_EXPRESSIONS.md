# Natural Discord replies

Maxwell can answer a casual ping, greeting, joke or thanks with a reaction, GIF or sticker. Substantive questions and tasks still call for an answer. These features are available to free and premium users under the same Discord permission rules.

`react(emoji=..., finish=true)` is a complete response by default. It records confirmed delivery and stops both further calls in that batch and automatic prose. Use `finish=false` when the reaction accompanies work or a written answer. An optional `message_id` targets another message in the current channel only.

When Maxwell can read a channel but cannot send, a direct ping receives a mute reaction, or joins an existing Unicode reaction when appropriate, if Discord allows it. Otherwise the request ends silently. This path does not invoke the language model, post typing/progress messages, DM the user, or retry the request. Threads use Send Messages in Threads. Reactions need View Channel and Read Message History; Add Reactions is needed only when creating a new reaction.

## GIF replies

`send_gif` is always available. The Discord message is the GIF file itself: no caption, embed, or attribution line. A reaction phrase such as hug, laugh, wave, or thumbs up uses the keyless [nekos.best](https://nekos.best/) catalog (200 requests per minute per category, no API key). Other short phrases use Giphy when `GIPHY_API_KEY` is set in `.env`. `index` selects another of the first ten results. Search stays at Giphy's PG-13 ceiling. API failures do not expose the key or claim a GIF was delivered.

```dotenv
GIPHY_API_KEY=your_key
```

Restart the bot after changing environment configuration. No extra Python dependency is needed.

Official references: https://docs.nekos.best/getting-started/api-endpoints.html and https://developers.giphy.com/docs/api/endpoint/search/

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

The offline regression suite covers terminal reactions/GIFs, reaction and thread permissions, read-only pings, application emoji scoping, animated expressions, explicit sticker dispatch, periodic refresh/cancellation, keyless reaction GIFs, the Giphy fallback, file-only delivery, unsafe URLs, and API-key redaction. Live GIF delivery and native sticker availability still need a Discord test channel.
