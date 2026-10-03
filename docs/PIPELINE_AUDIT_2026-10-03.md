# Pipeline audit — 2026-10-03

Reviewed `main` at `5a6ee06fd389c1da5253c1682a3e26fc17709b71`, tracing Discord
gateway events and personal app interactions through admission, journaling,
queueing, history and media extraction, prompt construction, provider routing,
tool dispatch, follow-ups, visible delivery, and retry decisions. The review
also followed the plugin hooks that replace the core app-history reader and
the mailbox notices appended during prompt construction.

## Findings repaired

| Boundary | Failure and correction | Regression coverage |
| --- | --- | --- |
| Discord media links | Markdown wrappers and HTML ampersands could corrupt a signed URL. Failed CDN requests had no refresh path, and message links were treated as web pages. Normalize wrappers without decoding signed query values; on CDN 403/404, refresh once using actual source-message IDs or up to 25 recent messages. Resolve message links only in the request's source channel. | `test_discord_media.py`: URL preservation, actual message ID versus attachment ID, 404 then fresh 200, private app source channel, cross-channel refusal, malformed links, pixels and caption from message links. |
| Attachment and tool media | Snapshot attachments without `read()` used an unchecked redirect path. Tool images lost their MIME type and were subsequently labeled PNG even for JPEG bytes. Use the shared checked, bounded downloader; retain MIME with typed image markers, strip the binary marker from text, and bound accumulated follow-up media. | `test_forwarded_messages.py`, `test_media_payloads.py`. |
| History merge | `fresh + memory` put unseen live rows before every stored row regardless of time. Stored versions also defeated current Discord edits. Merge copies by message ID, let live payloads override old fields, then sort chronologically while retaining synthetic tool rows. | `test_user_install.py`: interleaved stored/live rows, edits, tool timestamps, original data unchanged. |
| Selected context length | Admission stored the current request before prompt construction; slicing the merged history to N included it, then skipped it during rendering, leaving N−1 history rows. Remove the current request before applying the selected count. | `test_prompt_cache.py`: all ten selected rows survive alongside the live request. |
| History collection | An unbounded API wait delayed personal app requests. List-comprehension collection also discarded already received rows on errors. Bound the entire read to ten seconds, collect incrementally, retain partial results, and use the same implementation through the extras plugin. | `test_discord_media.py`: history yields one row then stalls; the row survives the deadline. Existing app and extras tests cover preferences and count limits. |
| App retries and policy | The retry sweep fetched an interaction snowflake as a channel message, producing a permanent fetch failure. App IDs also polluted gateway replay watermarks, and the queued app shortcut preceded ignored-user checks. Retain a bounded live adapter for in-process retries, release terminal/expired adapters, exclude interactions from gateway watermarks, and recheck ignored users first. | `test_inbound_reliability.py`: timeout followed by successful delivery through the same adapter, no message fetch, no watermark, blacklist recheck. Media refresh also refuses to fetch interaction IDs. |
| Tool ordering | All helper tools ran concurrently before all visible-output tools. A create/edit pair could race, and an interleaved send ran at the wrong time. This also let a write begin before a fetched-content security flag was applied. Walk emission order, allow adjacent explicitly read-only calls to overlap, and make side effects/terminal tools sequencing barriers. Preserve per-call error isolation without replaying completed effects. | `test_tool_batch_order.py`: create → send → edit and fetched-content flag before shell. Existing progress tests cover read concurrency, exceptions, cancellation, and exactly-once dispatch. |
| Provider routing and media fallback | The natural retry schedule selected only its first two routes, skipping primary when vision and fallback both failed. Removing media kwargs for a text-only retry supplied no explanation of the missing pixels. Budget attempts for every configured route and explicitly tell the model when no media was delivered. | `test_provider_architecture.py`: three-route transient failures, text-only omission notice, existing two-route budgets and concurrent result isolation. |
| Operator mailbox | Global mailbox notices entered ordinary/public prompts; mailbox tools were also offered without operator authorization. A shared notice-ID list let overlapping requests mark each other's notices. Gate both discovery and execution, including stale plugin metadata; inject automatic notices only for private operator replies/direct DMs; keep notice IDs in request context and mark them after delivery. | `test_private_inbox_context.py`, `test_dispatch_authorization.py`, `test_token_trim.py`: public/non-operator absence, concurrent notice ownership, all six stale-metadata tool denials, operator/non-operator catalogs. |

## Discord diagnosis and limits

Discord's [API reference](https://github.com/discord/discord-api-docs/blob/main/developers/reference.mdx)
documents that attachment CDN URLs are signed and expire, while fetching the
message returns a current URL. An image remaining visible in the Discord
client therefore does not guarantee that a copied CDN signature remains valid.
The tests reproduce stale-link recovery through mocked Discord/HTTP I/O; no
live failing link, production logs, or test-bot credentials were provided.

Refresh work is limited to the request's source channel, two known-message
fetches, 25 recent history rows, four seconds, and one CDN retry. An inaccessible
or older source may still require Ask Maxwell on that message or reattaching
the image. A failed CDN fetch no longer establishes that the underlying image
was deleted. Existing public-URL checks, DNS protection, redirect caps, media
size caps, and independent image/audio/video settings remain enforced.

Interaction adapters are retained only in memory for bounded retries during
their 15-minute response lifetime. Restart recovery keeps the existing rule
that offline app requests are not replayed; webhook tokens are not persisted.

The screenshot also mentions a generated site's 404. Repository tests cannot
determine whether that particular site expired, was removed, or is missing from
the running deployment. These changes do not claim to restore that site.

## Verification

The unchanged baseline completed with 2,112 passing tests, two skips, and one
filesystem-dependent test failure: this workspace cannot map the site's
container UID for `chown`. The source-replacement test now uses the current
mapped UID/GID; the production ownership enforcement is unchanged and the
separate ownership/permission-repair tests remain in place.

Validation includes the full pytest suite, Ruff, `pip check`, installer and
sandbox-script syntax checks, and the existing memory-scope, bounded-admission,
and embedding-backlog benchmarks. The benchmarks exercise local data/queue
behavior, not live Discord or paid-provider throughput.

The final local Python 3.12 run completed with **2,149 passed and 2 skipped**
in 70.23 seconds. Ruff, dependency consistency, shell syntax, diff whitespace,
and all three benchmark commands passed.

Browser site rendering and the real-provider progress test require Chromium
and `AI_BASE_URL` respectively and remain conditional integration checks.
