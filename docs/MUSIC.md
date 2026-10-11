# Maxwell music

Music is a bundled plugin, not a second bot. `music_info` reads live state/search/voice channels; `music_control` performs validated actions. `/music` and the Now Playing controls call the same `MusicService`. The static prompt describes capabilities without changing on each song. No microphone access, voice transcription or recurring AI polling is involved.

## Audio backend and deployment

Pinned dependencies:

- Lavalink server **4.2.2**: DAVE E2EE support, introduced in 4.2.0.
- Java **21.0.12.1 LTS** (Temurin JRE, digest-pinned): correct cgroup v2 resource detection on newer Linux kernels.
- Python `lavalink==5.11.0`: sends the mandatory DAVE `channelId` voice field.
- YouTube Source plugin snapshot `2be8e54` (September 2026, after 1.18.2): iOS playback and the cipher fetch fix. 1.18.2 answers many videos with "requires login" and cannot extract the WEB signature on this host.

References: [Lavalink changelog](https://lavalink.dev/changelog/v4), [Python client releases](https://github.com/Devoxin/Lavalink.py/releases), [YouTube plugin configuration](https://github.com/lavalink-devs/youtube-source/blob/2be8e542d3f6f178e048dca565892684c2e40177/README.md#plugin).

For the Linux host-network Compose deployment, set a **strong random `LAVALINK_PASSWORD`** in `.env` and run:

```sh
docker compose -f docker-compose.yml -f docker-compose.music.yml up -d --build
```

The audio REST/WebSocket port binds only to host loopback `127.0.0.1:2333`. No public reverse proxy is needed. The node has authenticated health checks, bounded logs, 1 GiB RAM and 1.5 CPU limits. Maxwell waits for the node health check at startup. The audio container needs outbound HTTPS and Discord voice UDP; do not block its outbound audio traffic. Do not publish its port to the internet. Generic HTTP/local audio sources are disabled at the node to prevent URL-fetch abuse.

The image copies the unchanged pinned Lavalink artifacts onto the maintained Java runtime. Java 18 incorrectly detects the CPU/memory controllers on this host's newer kernel ([OpenJDK JDK-8347811](https://bugs.openjdk.org/browse/JDK-8347811)), sizing GC workers for the host rather than the container quota. Java 21 detects the 1.5-CPU/1-GiB limits; `JAVA_TOOL_OPTIONS` explicitly retains G1 GC instead of switching to serial GC for the small container. Under these limits, G1 uses two parallel workers and one concurrent worker.

When replacing the audio node, also recreate `yt-cipher`: it shares Lavalink's network namespace. To apply the runtime change and restart the bot:

```sh
docker compose -f docker-compose.yml -f docker-compose.music.yml up -d --build --force-recreate --wait lavalink yt-cipher maxwell
```

For macOS/Windows or the bridge deployment, combine `docker-compose.bridge.yml` with `docker-compose.music.yml` and set `LAVALINK_HOST=lavalink` in `.env`. Both services must share the Compose default network. The audio port remains bound to host loopback.

For a separately operated node, set `LAVALINK_HOST`, `LAVALINK_PORT`, `LAVALINK_PASSWORD`, and optionally `LAVALINK_TLS=true` in the bot environment. Use a private network and authentication. Blank password keeps Maxwell running without an audio connection and returns an actionable music configuration error.

The bot must be server-installed, with the voice-state intent (already enabled) and View Channel, Connect and Speak permissions. Normal voice channels are supported; stage channels are deliberately rejected. Slash commands are registered by Maxwell's existing application-command sync on readiness. Audio processing remains on Lavalink rather than the AI process.

## Usage and permissions

Examples:

- “Maxwell, join my VC and play Megalovania.”
- “Queue Tenebre Rosso Sangue.”
- “Play this instead.”
- “Add five Undertale songs.”
- “What is playing?” / “Lower the volume to 25.”
- “Queue these three songs next: Megalovania, Tenebre Rosso Sangue, and VERITY PHONK.”
- “Enable autoplay and keep related songs going.” / “Turn autoplay off.”

The model chooses songs and actions. Code selects the requester's voice channel, checks membership and permissions, orders queues and serializes changes. Search yields public track metadata only. `mode=queue` preserves current playback; `next` adds at the front; `now` replaces only on explicit request. Playback confirmation waits for a real Lavalink TrackStart event. Failed operations return safe errors, never stream URLs, encoded tracks, audio-node errors or credentials.

Listeners can request songs and remove their own queued songs. Ordinary pause/resume/skip/seek/shuffle/repeat/volume/autoplay controls require presence in the active voice channel and enabled listener controls. DJs, server managers and the server owner can manage playback remotely; session moves, stop-and-clear, queue clear and leave require these privileges. Only server managers/owners can configure behavior. An ordinary requester cannot take over another active session. All commands and tools resolve the guild and actor from Discord, never model-supplied IDs.

`music_control` remains available after web search or URL fetches, so Maxwell can identify a track and play it in the same turn. Music mutations are serialized side effects, not destructive tools for the fetched-content taint gate. Guild membership, voice-channel access, DJ/manager permissions and the YouTube-only source allowlist still apply; destructive tools such as shell remain blocked on a tainted turn.

`/music` includes play/batch/autoplay/join/move/search/status/queue/pause/resume/skip/stop/leave/volume/seek/repeat/shuffle/clear/remove/configure. A meaningful playback or queue change posts a fresh public Now Playing embed with artwork, linked track/artist, requester or autoplay origin, duration, voice channel, settings and the next three songs. Previous panels lose their controls; stale or cross-server buttons cannot change playback. No-op updates do not repost, and there is no periodic panel polling. Pause/resume, skip, queue, shuffle, repeat, autoplay and stop buttons recheck current permissions.

`/music status` publishes a fresh panel in the invoking text channel when Maxwell can view, send and embed there; otherwise it returns a private status embed. Queue/search results and action/error feedback are private. Failed public publication does not turn a confirmed music operation into a failure.

### Multiple songs and autoplay

For AI requests, `music_control(action="play", queries=[...], mode="queue")` loads one to ten selections atomically in the supplied order. Use `query` for one selection or `queries` for a batch, never both. `mode="next"` inserts the whole batch ahead of the existing queue without reversing it; `now` replaces the current song only on explicit request. `/music batch input:` accepts one title or YouTube link per line, with the same queue/next/now modes.

Autoplay is **off by default**, scoped to the current session. Enable it with `/music autoplay enabled:true`, the Autoplay button, or `music_control(action="autoplay", value=true)`. Once the requested queue is exhausted, Maxwell loads a real YouTube radio mix seeded by the last song and starts one new related track. Requested songs and repeat modes take precedence. The last 50 played URLs are excluded to avoid immediate cycling; automatically chosen songs are labeled in the embed.

Autoplay does not continue in an empty voice channel or after a failed source with no queued successor. Exhausted/unavailable recommendations leave playback idle with an actionable notice, not an infinite retry loop. Disabling autoplay prevents future automatic selections; it does not interrupt the current song. Stop clears the queue and disables autoplay. Leaving or restarting Maxwell discards session autoplay state.

`/music configure` shows or edits:

| Setting | Default | Allowed |
|---|---|---|
| Fallback voice channel | none | A voice channel in this server; server manager/DJ can initiate remote playback |
| DJ role | none | A role in this server |
| Listener controls | enabled | Enabled/disabled |
| Idle/empty departure | 180 seconds | 30–3600 seconds |
| Queued songs | 100 | 1–100 |
| Per-listener queued songs | 20 | 1–100; DJs/managers bypass this allowance |

Settings are saved atomically under the music plugin data directory, independently per guild. `/config` plugin enable/disable and optional-plugin capability controls also apply to manual music requests.

## Reliability and limits

One playback session per guild, with one guild lock shared by actions and events. Independent guilds can play concurrently. Admission is bounded to 250 sessions, eight concurrent source loads, eight outstanding actions per session and ten mutating/search requests per user/server per ten seconds. Playlist loading is limited to the first 100 tracks (one source page); overly large queue additions fail atomically rather than partly inserting songs. Queue listings paginate 25 tracks. Volume is 0–100.

The Lavalink client reconnects with exponential backoff. Session resuming is requested for 120 seconds; its node manager restores current tracks, seek positions, pause state and volume after a fresh node session. Recovery uses the same guild lock. In-memory queues survive transient node disconnects; a failed transition retains upcoming songs for a later skip. Failed-source end events advance to the next track; repeated source/node failures are surfaced without a tight retry loop. Unexpected Discord disconnects/guild removal destroy the session. Idle/paused/empty sessions leave automatically; shutdown leaves voice and closes the audio client. Node/source failures are logged by exception type without including source tokens or private messages.

Queue and autoplay-session persistence across a complete Maxwell process restart are **not implemented**. The separate Dev/production bots need separate Discord identities and may share an audio node with its capacity adjusted. The supported discord.py VoiceProtocol forwards voice gateway state by guild ID (including channel moves that keep the same Discord session ID), so no audio passes through Discord shards. Gateway connections may be sharded. Guild locks and music sessions stay keyed by guild id, and a shard ready or resume only resyncs that shard. Redis or cross-process session ownership is not claimed.

YouTube/YouTube Music HTTPS video and playlist links and title searches are supported. No arbitrary audio URL, Spotify DRM, stage support or other audio providers are claimed. Anonymous iOS playback covers videos YouTube still serves without a login. Videos that return "requires login" need the one-time burner-account device code from `https://www.google.com/device`. Store that refresh token in `YOUTUBE_OAUTH_REFRESH_TOKEN` and recreate the audio node; do not commit it. Signature decoding uses the local `yt-cipher` sidecar (`YOUTUBE_CIPHER_PASSWORD`), which shares the audio node's network namespace, not the public cipher host. Ordinary YouTube title search is often empty on this plugin, so a title with no YouTube results is loaded again with YouTube Music search. Age/region/removed videos and source failures cannot be guaranteed playable. Do not share extractor tokens with the model. Live playback must be tested on the operator's VPS before public rollout.

## TTS reference URLs

`tts(text=..., reference_audio_url="https://example.com/voice.wav")` or a URL in `reference` downloads a public direct audio/video file without requiring it to be attached. The model can discover a permitted media reference online. Attachment filenames and `reference="attachment"` still work. HTTP(S) public hosts and safe redirects are accepted; private/loopback/metadata addresses, private DNS answers, credentials in URLs and non-HTTP schemes are rejected.

There are **no local reference-byte, synthesized-audio-byte or sample-duration caps**. The complete reference is converted to mono MP3, base64-encoded and supplied to Mistral, which validates it. Network/process timeouts still prevent stuck operations. A page URL (including YouTube watch pages) is not a direct media file; HTML responses are rejected. Decoder protocol/format restrictions prevent downloaded media from opening additional network or local-file playlists. Reference clips are temporary and not kept as saved voices. Use voices you own or have permission to clone.

## Verification

Automated tests cover per-guild concurrency, queue/playlist transitions, repeat/autoplay precedence, recent-track exclusion, autoplay opt-out/exhaustion, atomic batches, permissions, admission, failed sources/nodes, disconnect cleanup, settings persistence, manual/AI consistency, public panel replacement, stale buttons and aggregate embed-message limits. A real Lavalink.py client is exercised against a local v4 REST/WebSocket fixture: authenticated connection, actual discord.py voice-state/server parser dispatch through VoiceProtocol, DAVE channelId voice handoff and same-session channel moves, TrackStart acknowledgment, next-track events and cleanup. URL TTS tests cover unattached references, redirects, private DNS/IPs, format errors, downloads/outputs exceeding the former 8 MiB cap, and full samples exceeding the former 20-second trim.

Live verification checklist for an operator with credentials:

1. Start the audio node; check its authenticated health status and source-plugin logs.
2. Join a VC and request a public YouTube song through conversation; verify audible playback and Now Playing.
3. Queue, pause/resume, seek, skip, repeat, and leave through both AI and `/music`; test a second guild concurrently.
4. Test another requester outside the VC, a DJ, default-channel selection and channel permission revocation.
5. Restart the audio node, test restored playback/queue, and let an empty session reach its idle timeout.
6. Supply a permitted direct audio URL to TTS and verify Mistral generation and voice-message delivery.

Local verification exercises live Lavalink title/radio resolution and music dispatch with simulated Discord voice transport. The Java runtime cutover additionally verifies authenticated node health, unchanged server/plugin versions, native buffered UDP sender startup, cgroup CPU/memory detection and quota-sized G1 workers. Embed payloads are checked against Discord limits and rendered in a desktop/mobile preview. These checks do not establish audible live Discord playback, elimination of reported mid-song dropouts, or exact Discord-client rendering; the operator checklist above covers those surfaces and live provider cloning. CI additionally validates the audio image, JVM resource detection and composed deployment on GitHub's Docker-enabled runner.
