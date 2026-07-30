# Maxwell Bot — Channel Context & Memory Architecture Analysis

## Executive Summary

The Maxwell bot has a layered context/memory system built entirely on JSON files and in-memory dicts. There is **zero** vector DB, embedding, or RAG code anywhere in the codebase. The system consists of:

1. **Short-term channel memory** (per-channel message ring buffer in `memory.json`)
2. **Long-term memory** (flat text file, `long_term_memory.txt`)
3. **Shared context** (scoped JSON facts, `shared_context.json`)
4. **Media context** (in-memory image cache, never persisted)
5. **REM event log** (ring buffer of recent events, `rem_events.json`)
6. **Context watcher** (LLM-based fact extractor on trigger phrases)
7. **Context cleanup engine** (LLM-based janitor for shared context + LTM)

---

## 1. bot.py — Message Capture & Context Building

### 1A. Message Capture: Listens to ALL messages, not just pings

**Function: `on_message(self, message)` — line 2635**

This is the central message handler. Critically, it **captures and stores ALL messages to channel memory BEFORE the ping check**:

- **Line 2672-2683**: Channel filtering (bot_enabled, stop, blocked/allowed channels)
- **Line 2685-2687**: Check for content, attachments, embeds
- **Line 2703-2737**: Self-message handling — bot's own messages are stored to memory, then returns (no reply to self)
- **Line 2782-2790**: Acquire per-channel lock
- **Line 2792-2861**: **ALL messages are stored to channel memory** (both pinged and non-pinged)
  - Line 2793-2818: Builds `memory_content` — appends `[attachments: ...]` note and `[embeds: ...]` note to text
  - Line 2819-2830: Creates `memory_item` dict with author, content, message_id, timestamp
  - Line 2841-2842: Adds mention rows
  - Line 2843-2859: Adds reply context (reply_to_message_id, reply_to_author, etc.)
  - **Line 2861**: `await self.memory.add_to_channel_memory(channel_id, memory_item)` — this happens for EVERY message
- **Line 2866**: `self._maybe_schedule_context_extraction(message)` — context watcher (see below)
- **Line 2868-2882**: **Background media caching for EVERY message** (not just pings)
  - Comment (line 2869-2872): "Cache media context for EVERY message in an allowed channel, not just pinged ones."
  - Extracts images/embeds and caches them via `_cache_media_context`

**The ping/reply gate (line 2884-2938)**:
- **Line 2884-2885**: Bot messages — return if `reply_to_bots` is off
- **Line 2887-2894**: DMs — always reply if `reply_dms` is on
- **Line 2896-2913**: Group channels — reply if mentioned or reply_to_bot
- **Line 2915-2938**: Guild channels — **only reply if mentioned or reply_to_bot** (line 2924: `if not mentioned and not reply_to_bot: return`)

**Key finding**: The bot stores ALL messages to channel memory regardless of ping status. The ping check only gates whether the bot *generates a reply*. Context capture works for all messages.

### 1B. How Channel Context Is Built

**Function: `_build_messages(self, message, user_message, has_media, media_summary)` — line 8790**

This assembles the full prompt sent to the LLM:

1. **System prompt** (line 8826-8993): Core rules, personality, server prompt, jailbreak, long-term memory, cross-context facts, emoji list, tool prompt, user list
2. **Channel history** (line 8994-9227): 
   - Line 8994: `memory = await self.memory.get_channel_memory(channel_id)` — gets ALL stored messages for this channel
   - Line 9002-9011: Budget (default 200000 chars, max 240000)
   - Line 9012-9021: Count (default 500 messages, max 2000)
   - Line 9023: `recent_memory = memory[-count:]` — takes most recent N messages
   - Line 9032-9040: Also includes tool history messages (up to 20)
   - Line 9041: `context_memory = tool_history + list(recent_memory)`
   - Line 9056-9170: Builds turn sequences with role alternation (user/assistant), author labels, timestamps, reply metadata, mentions
   - Line 9176-9183: Merges consecutive same-role turns
   - Line 9189-9201: Applies budget — trims oldest turns first
   - Line 9212-9227: Wraps history in `<previous_conversation>` tags as a single user message
3. **Live message** (line 9228-9301): Appended as final user turn with `[RESPOND TO THIS]` tag

### 1C. Image/Media Handling

**Images are NOT saved to channel memory (JSON). They are kept in a separate in-memory cache.**

**Media extraction: `_extract_media(self, message)` — line 6060**
- Downloads all attachments, classifies as image/video/audio/text
- Returns `(images: list[str], media: list[dict])` where images are base64 strings
- Handles GIF normalization (→ JPEG contact sheet), video frame extraction, audio track extraction

**Media context cache: `_cache_media_context(self, channel_id, media)` — line 6628**
- Only caches image items (`is_image: True`)
- Stored in `self._media_context: dict[str, list[dict]]` (line 1929) — **in-memory only, never persisted**
- Each item: `{b64, mime_type, filename, message_id, uses_left}`
- `MAX_VISUAL_MEMORY_IMAGES = 5` (line 296) — cap per channel
- `MEDIA_CONTEXT_USES = 2` (line 299) — decremented each handled message, expires at 0
- Dedup by (message_id, filename) — bumps `uses_left` instead of appending duplicates

**Media context retrieval: `_get_media_context(self, channel_id, message_id)` — line 6676**
- Returns cached images for a channel, optionally filtered by message_id

**When cached media is attached: `_handle_message` — line 6931**
- Line 6961-6963: Extracts current media (images, embeds, gif links)
- Line 6967-6980: Decides which media to attach:
  - Current attachments always go through
  - Cached images only if user references "image/picture/screenshot" or replies to a media message
  - `_should_use_cached_media_context` (line 6694) — regex match on visual keywords
  - `_should_mix_cached_with_current` (line 6714) — only when user says "previous/prior/earlier/last/old/recent"

**Media context tick: `_tick_media_context(self, channel_id)` — line 6793**
- Called after each handled message (line 7797)
- Decrements `uses_left` on all cached items, expires at 0

**Key finding**: Images ARE cached for non-pinged messages (line 2868-2882 in `on_message`), but the cache is **in-memory only, lost on restart, and expires after 2 uses**. Images are never stored in the JSON channel memory — only an `[attachments: filename.png (image/png)]` text note is stored.

### 1D. Context Extraction (the "context watcher")

**`_should_extract_context(self, message)` — line 5176**
- Only triggers on messages containing keywords: "important", "remember", "don't forget", "call me", "my name is", "i prefer", "i hate", "i like", "this is my", etc.
- Or admin DMs with ≥12 chars

**`_maybe_schedule_context_extraction(self, message)` — line 5214**
- Fires as async task if `_should_extract_context` returns True
- Caps at 20 concurrent tasks

**`_extract_shared_context_fact(self, message)` — line 5372**
- Sends message to LLM (aux provider) with extraction prompt
- LLM returns JSON: `{should_store, importance, scope, visibility, summary, tags, expires_in_hours}`
- `_normalize_context_entry` (line 5267) validates/sanitizes — enforces scope/visibility rules
- Stores via `memory.add_shared_context(entry)`

### 1E. Reply Context

**`_get_reply_context(self, message)` — line 4236**
- If message is a reply, fetches referenced message content
- Returns `[Latest message replies to Name(id): content]` string
- Appends `[media attached]` note if reference has attachments (line 4255) — but does NOT include the actual image

### 1F. Shared Fact Relevance Filtering

**`_shared_fact_relevant(cls, latest, fact)` — line 8700**
- User/channel/dm scoped facts: always relevant
- Guild/global facts: only if latest message tokens overlap with fact tokens
- `_topic_tokens(text)` — line 8586: extracts 4+ char tokens, minus stop words

---

## 2. memory.py — The Full Memory System

### 2A. MemoryManager class (line 220)

**Data structures (3 separate stores):**

1. **`self.memory: dict[str, list[dict]]`** — Short-term channel memory
   - Key: channel_id, Value: list of message dicts
   - Each message: `{author, author_id, author_is_bot, content, message_id, timestamp, mentions?, reply_to_*?, is_tool?, tool_name?, tool_params?, tool_result?}`
   - Persisted to: `data/memory.json`
   - Cap: `max_messages` (default 2000, max 10000) per channel
   - Channel cap: `MAX_CHANNELS = 25` (oldest channels evicted by latest timestamp)

2. **`self.long_term_memory: list[dict]`** — Long-term memory
   - Each entry: `{id: int, content: str}`
   - Persisted to: `data/long_term_memory.txt` (one line per entry)
   - Cap: `MAX_LTM_LINES = 999`
   - `MAX_MEMORY_CHARS = 1000` per line

3. **`self.shared_context: list[dict]`** — Shared context (scoped facts)
   - Each entry: `{id, scope, visibility, importance, content, source_user_id, source_channel_id, source_guild_id, source_kind, tags, created_at, last_seen_at, expires_at}`
   - Persisted to: `data/shared_context.json`
   - Cap: `MAX_SHARED_CONTEXT = 1000` entries, `MAX_SHARED_CONTEXT_CHARS = 1200` per entry
   - Scopes: `global`, `user:<id>`, `guild:<id>`, `channel:<id>`, `dm:<id>`
   - Visibility: `shared`, `private`, `admin_only`, `public_hint`
   - Dedup: Jaccard similarity >0.8 in same scope → merge (line 474-492)
   - LRU eviction: importance <3 not seen in 7+ days → removed (line 493-503)

### 2B. Key Methods

**Short-term channel memory:**
- `get_channel_memory(channel_id)` — line 706: Returns list of messages for channel
- `add_to_channel_memory(channel_id, message)` — line 710: Adds message, dedup by message_id, prunes to max_messages
- `clear_channel_memory(channel_id)` — line 754: Deletes channel from memory

**Long-term memory:**
- `get_long_term_memory()` — line 854: Returns list, reloads from disk if file changed
- `add_long_term_memory(content)` — line 761: Appends, saves
- `edit_long_term_memory(memory_id, content)` — line 778
- `remove_long_term_memory(memory_id)` — line 790
- `apply_ltm_batch(edits, deletes)` — line 803: Batch edit/delete in one locked pass

**Shared context:**
- `add_shared_context(entry)` — line 537: Adds with dedup, merge, sanitize
- `remove_shared_context(context_id)` — line 576
- `update_shared_context(context_id, updates)` — line 589
- `list_shared_context(limit)` — line 616
- `get_relevant_shared_context(user_id, guild_id, channel_id, is_dm, is_admin, max_items)` — line 627:
  - Filters by scope match + visibility rules
  - Scores by scope specificity (user=4, channel=3, guild=2, global=1) + importance + recency
  - Returns top N entries

**Server prompts:**
- `get_server_prompt(server_id)` — line 858
- `set_server_prompt(server_id, prompt)` — line 869
- `clear_server_prompt(server_id)` — line 883

### 2C. RemEventLog class (line 75)

- JSON-backed ring buffer of recent visible events (user/assistant messages)
- Persisted to: `data/rem_events.json`
- Cap: `DEFAULT_REM_EVENT_BUFFER_MAX = 500`
- Used by REM assimilation to review recent conversation

### 2D. Persistence

All saves are debounced (5s) and atomic (via `_atomic_json_write_sync` from utils.py). Uses `asyncio.Lock` for concurrency safety.

---

## 3. context_cleanup.py — The Context Cleanup Engine

### 3A. What it is

This is **NOT the "context engine"** — it's a **janitor/maintenance agent** that runs on a schedule (default every 1800s / 30min) to clean up the shared context and long-term memory stores.

### 3B. ContextCleanupStore (line 131)

JSON-backed state + audit log:
- `context_cleanup_state.json` — running state, counters
- `context_cleanup_log.json` — audit log (last 50 passes)
- `context_cleanup_control.json` — enabled/interval config

### 3C. ContextCleanupEngine (line 217)

**Main loop: `_loop()` — line 326**
- Runs on interval (default 1800s, min 300s)
- Exponential backoff on failures (max 6x)

**Single pass: `run_once()` — line 360**
- Two sub-passes per tick:
  1. **Shared context pass** (line 400-406): Loads up to 200 entries, asks LLM to produce cleanup ops (delete/edit/merge/add)
  2. **LTM pass** (line 409-415): Loads LTM entries, asks LLM to clean (delete/edit/merge, no add)

**Planning: `plan(entries)` — line 511**
- Sends entry digests to LLM with cleanup rules
- LLM returns JSON: `{audit, ops: [{kind, id, ...}]}`
- Valid ops: delete, edit, merge, add
- Max 60 ops per pass (`MAX_OPS_PER_PASS = 60`)

**Applying: `apply(plan)` — line 735**
- Executes ops through MemoryManager API (never writes files directly)
- delete → `memory.remove_shared_context()`
- edit → `memory.update_shared_context()`
- merge → update keep_id + delete others
- add → `memory.add_shared_context()`

**LTM pass: `plan_ltm(entries)` / `apply_ltm(plan)` — lines 790-1034**
- Similar but for long_term_memory
- Uses `memory.apply_ltm_batch()` for atomic batch apply
- No "add" op (Intel/bot manage additions)

---

## 4. config.py — Context/Memory/Embedding Config

**No embedding/vector/RAG config exists.** Relevant config:

- `MEMORY_MESSAGE_LIMIT = 2000` (line 174) — max messages per channel (env: `MEMORY_MESSAGE_LIMIT`)
- `REM_ENABLED = True` (line 177)
- `REM_INTERVAL_SECONDS = 600` (line 178)
- `AUX_BASE_URL`, `AUX_API_KEY`, `AUX_MODEL` (lines 135-140) — provider for background agents (REM, context-cleanup, context-watcher)
- `ENABLE_IMAGE_INPUT = True` (line 89)
- `ENABLE_VIDEO_INPUT = True` (line 90)
- `OLLAMA_BASE_URL` (line 60) — could be used for embeddings

**Control defaults** (from `control_defaults.py`):
- `store_memory: True` (line 29)
- `long_term_memory_enabled: True` (line 30)
- `cross_context_enabled: True` (line 31)
- `cross_context_extract_enabled: True` (line 32)
- `cross_context_max_items: 10` (line 33)
- `memory_history_messages: 800` (line 70)
- `memory_context_budget: 200000` (line 71)
- `prompt_context_budget: 240000` (line 73)
- `context_cleanup_enabled: True` (line 136)
- `context_cleanup_interval_seconds: 1800` (line 137)
- `process_images: True` (line 58)

---

## 5. Existing Vector DB / Embedding / RAG Code

**There is NONE.** Comprehensive search across all `.py` files, `requirements.txt`, and `.env.example` found:
- No `chromadb`, `qdrant`, `faiss`, `pinecone`, `weaviate`, `lancedb`, `milvus` imports or references
- No `embedding`, `vector_db`, `vector_store` references
- No `cosine_similarity`, `numpy.dot`, or similarity search code (beyond Jaccard in `_text_similarity`)
- No `qwen3` or `ollama.*embed` references
- `requirements.txt` has no vector DB or embedding libraries
- The only "similarity" code is `_text_similarity()` in memory.py (line 54) — a simple Jaccard token overlap used for shared context dedup

---

## 6. What Needs to Be Removed/Replaced for RAG Migration

### To Remove or Replace:

1. **`memory.py` — `MemoryManager` class (line 220)**
   - Short-term channel memory (`self.memory`, `get/add/clear_channel_memory`) → Replace with RAG vector store for message history
   - Long-term memory (`self.long_term_memory`, `get/add/edit/remove_long_term_memory`, `apply_ltm_batch`) → Replace with RAG vector store
   - Shared context (`self.shared_context`, `add/remove/update/list/get_relevant_shared_context`) → Replace with RAG vector store
   - The Jaccard similarity dedup (`_text_similarity`) → Replace with embedding similarity

2. **`context_cleanup.py` — `ContextCleanupEngine` (line 217)**
   - Entire LLM-based cleanup engine → May become unnecessary if RAG handles dedup via similarity search
   - Or repurpose to clean the vector store instead

3. **`bot.py` — Context building logic**
   - `_build_messages` (line 8790) — The channel history section (lines 8994-9227) → Replace with RAG retrieval of relevant messages
   - `_should_extract_context` (line 5176) / `_maybe_schedule_context_extraction` (line 5214) / `_extract_shared_context_fact` (line 5372) → Replace with automatic embedding of all messages
   - `_shared_fact_relevant` (line 8700) / `_topic_tokens` (line 8586) → Replace with vector similarity search
   - Long-term memory injection (lines 8887-8914) → Replace with RAG retrieval
   - Cross-context facts injection (lines 8915-8948) → Replace with RAG retrieval

4. **`rem.py` — REM assimilation**
   - `_apply_audit_actions` (line 188) → LTM/shared context writes → Replace with RAG storage
   - `run_rem` (line 271) → May become unnecessary or repurposed

5. **`autonomy.py`**
   - Line 1395: `memory.get_channel_memory(cid)` → Replace with RAG retrieval
   - Line 1613: `memory.get_long_term_memory()` → Replace with RAG retrieval
   - Line 1647: `memory.get_relevant_shared_context(...)` → Replace with RAG retrieval
   - Line 2938: `memory.add_to_channel_memory(...)` → Replace with RAG storage
   - Line 3194: `memory.add_long_term_memory(...)` → Replace with RAG storage

6. **`bot_tools.py`**
   - Lines 531, 690: `memory.add_to_channel_memory(...)` → Replace with RAG storage
   - Lines 733, 740, 748: LTM add/edit/remove → Replace with RAG operations

### To Keep (or minimally modify):

- **`on_message` (line 2635)** — Keep the message capture logic, but route storage to RAG instead of JSON
- **`_extract_media` (line 6060)** — Keep media extraction, but store image embeddings in RAG
- **`_cache_media_context` (line 6628)** — Could be replaced by RAG image retrieval or kept as-is
- **`render_discord_context_text`** (utils.py line 127) — Keep, still needed for rendering
- **`RemEventLog`** (memory.py line 75) — Could keep as a simple event buffer or replace
- **Server prompts** (`get/set/clear_server_prompt`) — Unrelated to RAG, keep

### New Code Needed:

- Embedding generation via `qwen3-embedding:0.6b` through Ollama (`OLLAMA_BASE_URL + /api/embeddings`)
- Vector store (e.g., ChromaDB, Qdrant, or simple numpy-based store)
- RAG retrieval function to replace `get_channel_memory` + `get_relevant_shared_context` + `get_long_term_memory`
- Automatic embedding pipeline for all incoming messages (text + image descriptions)
- Image description generation (using vision model) before embedding, since `qwen3-embedding` is text-only

### Data Files to Migrate:

- `data/memory.json` (412KB, 17 channels, ~500 msgs/channel) → Embed and store in vector DB
- `data/shared_context.json` (1.7KB, 3 entries) → Embed and store in vector DB
- `data/long_term_memory.txt` → Not currently present, but would need migration if populated
- `data/rem_events.json` (279KB, up to 500 events) → Could embed or discard

---

## 7. Architecture Diagram (Current System)

```
on_message (line 2635)
  │
  ├── ALL messages → memory.add_to_channel_memory() → memory.json
  │                  (text + [attachments: note] + [embeds: note])
  │
  ├── ALL messages with media → _extract_media() → _cache_media_context()
  │                              (base64 images in memory, 5 per channel, 2 uses)
  │
  ├── Trigger phrases → _maybe_schedule_context_extraction()
  │                      → LLM extracts fact → memory.add_shared_context()
  │                                                      → shared_context.json
  │
  └── Ping/reply only → _handle_message()
                          → _build_messages()
                              ├── System prompt (rules, personality, LTM, shared context, emojis)
                              ├── Channel history (from memory.json, up to 500 msgs, 200k chars)
                              └── Live message with [RESPOND TO THIS]

Background agents:
  ├── REM (rem.py) — reviews rem_events, writes to LTM/shared_context
  ├── ContextCleanup (context_cleanup.py) — cleans shared_context + LTM via LLM
  └── Autonomy (autonomy.py) — reads channel memory + LTM + shared context for goals
```