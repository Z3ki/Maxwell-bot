# Maxwell music

Music is a bundled plugin, not a second bot. `music_info` reads live state/search/voice channels; `music_control` performs validated actions. `/music` and the four Now Playing buttons call the same `MusicService`. The static prompt describes capabilities without changing on each song. No microphone access, voice transcription or recurring AI polling is involved.

## Audio backend and deployment

Pinned dependencies:

- Lavalink server **4.2.2**: DAVE E2EE support, introduced in 4.2.0.
- Python `lavalink==5.11.0`: sends the mandatory DAVE `channelId` voice field.
- YouTube Source plugin **1.18.2**: current source clients and playlist support.

References: [Lavalink changelog](https://lavalink.dev/changelog/v4), [Python client releases](https://github.com/Devoxin/Lavalink.py/releases), [YouTube plugin configuration](https://github.com/lavalink-devs/youtube-source/tree/1.18.2#plugin).

For the Linux host-network Compose deployment, set a **strong random `LAVALINK_PASSWORD`** in `.env` and run:

```sh
docker compose -f docker-compose.yml -f docker-compose.music.yml up -d --build
```

The audio REST/WebSocket port binds only to host loopback `127.0.0.1:2333`. No public reverse proxy is needed. The node has authenticated health checks, bounded logs, 1 GiB RAM and 1.5 CPU limits. Maxwell waits for the node health check at startup. The audio container needs outbound HTTPS and Discord voice UDP; do not block its outbound audio traffic. Do not publish its port to the internet. Generic HTTP/local audio sources are disabled at the node to prevent URL-fetch abuse.

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

The model chooses songs and actions. Code selects the requester's voice channel, checks membership and permissions, orders queues and serializes changes. Search yields public track metadata only. `mode=queue` preserves current playback; `next` adds at the front; `now` replaces only on explicit request. Playback confirmation waits for a real Lavalink TrackStart event. Failed operations return safe errors, never stream URLs, encoded tracks, audio-node errors or credentials.

Listeners can request songs and remove their own queued songs. Ordinary pause/resume/skip/seek/shuffle/repeat/volume controls require presence in the active voice channel and enabled listener controls. DJs, server managers and the server owner can manage playback remotely; session moves, stop-and-clear, queue clear and leave require these privileges. Only server managers/owners can configure behavior. An ordinary requester cannot take over another active session. All commands and tools resolve the guild and actor from Discord, never model-supplied IDs.

`/music` includes play/join/move/search/status/queue/pause/resume/skip/stop/leave/volume/seek/repeat/shuffle/clear/remove/configure. The Now Playing panel is edited as tracks and controls change; it is not reposted for every change. Buttons recheck current permissions and reject old or cross-server panels. Queue and errors are ephemeral; successful playback is shown in the shared panel.

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

Queue persistence across a complete Maxwell process restart is **not implemented**. The separate Dev/production bots need separate Discord identities and may share an audio node with its capacity adjusted. The supported discord.py VoiceProtocol forwards voice gateway state by guild ID (including channel moves that keep the same Discord session ID), so no audio passes through Discord shards; this repository currently uses a single gateway client. Redis or cross-process session ownership is not claimed.

YouTube/YouTube Music HTTPS video and playlist links and title searches are supported. No arbitrary audio URL, Spotify DRM, stage support or other audio providers are claimed. YouTube can block VPS addresses or require source-plugin OAuth/cipher configuration; see the plugin's operator documentation. Age/region/removed videos and source failures cannot be guaranteed playable. Do not share extractor tokens with the model. Live playback must be tested on the operator's VPS before public rollout.

## TTS reference URLs

`tts(text=..., reference_audio_url="https://example.com/voice.wav")` or a URL in `reference` downloads a public direct audio/video file without requiring it to be attached. The model can discover a permitted media reference online. Attachment filenames and `reference="attachment"` still work. HTTP(S) public hosts and safe redirects are accepted; private/loopback/metadata addresses, private DNS answers, credentials in URLs and non-HTTP schemes are rejected.

There are **no local reference-byte, synthesized-audio-byte or sample-duration caps**. The complete reference is converted to mono MP3, base64-encoded and supplied to Mistral, which validates it. Network/process timeouts still prevent stuck operations. A page URL (including YouTube watch pages) is not a direct media file; HTML responses are rejected. Decoder protocol/format restrictions prevent downloaded media from opening additional network or local-file playlists. Reference clips are temporary and not kept as saved voices. Use voices you own or have permission to clone.

## Verification

Automated tests cover per-guild concurrency, queue/playlist transitions, repeat, selection and permissions, admission, failed sources/nodes, disconnect cleanup, settings persistence, manual/AI consistency and stale buttons. A real Lavalink.py client is exercised against a local v4 REST/WebSocket fixture: authenticated connection, actual discord.py voice-state/server parser dispatch through VoiceProtocol, DAVE channelId voice handoff and same-session channel moves, TrackStart acknowledgment, next-track events and cleanup. URL TTS tests cover unattached references, redirects, private DNS/IPs, format errors, downloads/outputs exceeding the former 8 MiB cap, and full samples exceeding the former 20-second trim.

Live verification checklist for an operator with credentials:

1. Start the audio node; check its authenticated health status and source-plugin logs.
2. Join a VC and request a public YouTube song through conversation; verify audible playback and Now Playing.
3. Queue, pause/resume, seek, skip, repeat, and leave through both AI and `/music`; test a second guild concurrently.
4. Test another requester outside the VC, a DJ, default-channel selection and channel permission revocation.
5. Restart the audio node, test restored playback/queue, and let an empty session reach its idle timeout.
6. Supply a permitted direct audio URL to TTS and verify Mistral generation and voice-message delivery.

No live Discord/Mistral credentials or Docker daemon are available in the development environment; audible playback and live provider cloning are therefore pending operator verification. CI additionally validates the audio image and composed deployment on GitHub's Docker-enabled runner.
