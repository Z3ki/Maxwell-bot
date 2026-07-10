# New Plan: Make Pi the Brain of Maxwell (Discord Bot)

**Date:** 2026-07-10
**Context:** User clarification - "I WANTED PI TO BE THE BRAIN". Previous work made Pi a dev harness + tool port for coding Maxwell. Now: Pi (pi.dev agent) *is* the core agentic brain for the Discord self-bot. The Python Discord client becomes a thin adapter. All decisions, responses, tool use, memory, autonomy driven by Pi.

## Reread of Pi (from pi.dev docs, SDK, RPC, extensions, GitHub)
- Pi is a *minimal extensible agent harness*, not just a coding CLI. Core: LLM + tools + memory (sessions) + events + extensions/skills.
- **SDK (Node/TS)**: `createAgentSession()` - embed full agent. Custom tools via `defineTool` or extensions (`pi.registerTool`), event subscription (message_update, tool_execution_*, agent_start, etc.), prompt(), custom providers, skills, context files (AGENTS.md).
- **RPC mode** (key for Python): `pi --mode rpc` - JSONL over stdin/stdout. Commands: `prompt`, `steer`, `follow_up`, `get_state`, etc. Events stream back (text deltas, tool calls, results). Perfect bridge for non-Node apps. Examples show Python clients.
- **Extensions**: TS modules for custom tools (LLM-callable), event hooks (block/modify tools, inject context), commands, UI. Can do *anything* (Discord API calls, external services).
- **Custom tools**: Full control - define schema, execute fn that can call Discord.py, write sites, web search (via our ports or native), etc. Tools can be stateful.
- **General agent, not just code**: Used for UIs, automation, sub-agents, custom loops. Override tools/prompts/skills to make it a "Discord brain".
- **Providers**: Same as Maxwell - env keys (NVIDIA_API_KEY, OPENROUTER_*, etc.) or auth.json. Pi discovers them.
- **Sessions/memory**: Built-in tree, compaction, persistence. Can drive "autonomy/REM" via background prompts/skills.
- **Docker fit**: Run in our Debian container (Node for Pi + Python for Discord bridge). Full internet for tools/LLM.
- **Website creator**: Implement as custom Pi tool (exact parity with old CreateSiteTool so Caddy works).
- Sources: pi.dev/docs (SDK, extensions, RPC), GitHub examples (sdk/, extensions/, rpc-client), OpenClaw (real embedding example).

Pi can replace Maxwell's custom Python LLM loop (providers.py + bot.py tool orchestration) while keeping Discord.py for the client layer.

## High-Level Architecture (Pi as Brain)
- **Thin Python Discord layer** (bot.py minimal changes or new bridge):
  - Uses discord.py-self for connection, message parsing (multimodal: images/audio/video/embeds), VC, admin checks, command queue.
  - On relevant events (on_message, etc.): Serialize context (history, user, attachments, memory facts) + send as "prompt" to Pi (via RPC subprocess or HTTP/IPC bridge).
  - Subscribe to Pi events: On text deltas -> send to Discord channel. On tool calls -> map to actions (Discord send/edit, site create, web fetch, etc.) and feed results back to Pi.
  - No more custom `generate_response` + XML tool parsing in Python; Pi handles the agent loop.
- **Pi as core brain** (in Node, driven by same providers):
  - Run via `pi --mode rpc` (subprocess from Python) or Node SDK in a bridge process.
  - Custom system prompt: Maxwell personality + tool guidelines.
  - All tools as Pi custom tools/extensions: port every Maxwell tool (send_message, create_site with CSP, web_search, fetch_url, youtube, image_gen, shell via sandbox, polls, channel ops, memory ops, etc.).
  - Memory/REM/autonomy/intel: Use Pi sessions for state + skills/background prompts (e.g., periodic "REM consolidation" prompt to Pi).
  - Site creator: `maxwell_create_site` tool writes to MAXWELL_SITE_DIR (Caddy serves /bot/* as before).
  - Sub-agents: Pi's built-in or custom for autonomy.
- **Providers**: Identical. Export from .env (NVIDIA, OpenRouter fallbacks, etc.). Pi reads them natively. `tools/setup-pi-providers.sh` helps.
- **Docker (Debian full, as before)**: One container. Installs Node + `pi`, Python + Maxwell reqs. Runs Python bridge + Pi RPC (or co-process). Volumes: /workspace (code), public/bot (sites for Caddy), ~/.pi (Pi state/sessions). Full net (no --network none). `CMD` or entrypoint starts the integrated bot. Pre-runs setup.
- **Integration bridge**: Python side uses Pi's RPC protocol (JSONL stdin/stdout) or spawns a Node bridge using SDK. On Discord msg -> `{"type":"prompt", "message": "...", "images": [...]}`. Stream events back.
- **Website creator & Caddy**: Unchanged output format. Tool in Pi produces same HTML/CSP. Served from volume.
- **Everything ported**: Tools, systems (autonomy as Pi loop, REM as skill, intel as background, memory via sessions), providers, multimodal.
- **Keep working**: Original Python bot.py can fallback if needed. Tests, lint, API, etc.
- **Security**: Self-bot risks same. Pi tools can have gates (admin checks via ctx or Python bridge). Full access in Docker as specified.

This makes Pi the "brain" (agent loop, decisions, tool orchestration) while Python handles Discord I/O.

## Phased Plan (on experimental-pi-port branch)
Do research first (done), then implement in Docker. Log everything to progress.md. Use Pi tools where possible (once integrated). Run `python -m pytest -q`, `ruff check . --fix` at end of phases. Verify site creator + Caddy in Docker.

1. **Finalize research & this plan** (this doc + progress.md update). Reread Pi SDK/RPC/extensions for exact integration. Explore current Maxwell brain (bot.py loop, providers, tools).

2. **Docker (optional/legacy)**:
   - Pi-specific pi-debian + entrypoint removed (not needed for core).
   - Main is Python bot + `pi --mode rpc` subprocess (direct, restricted flags).
   - General Docker may still exist for other use.

3. **Implement Python <-> Pi bridge** (new `pi_bridge.py` or integrate in bot.py):
   - Use Pi RPC: spawn `pi --mode rpc --no-session` (or with model/provider flags from env).
   - Send prompts: serialize Discord context (history slice, attachments as base64 if supported, memory facts, current prompt).
   - Handle events: text deltas -> `channel.send()`, tool calls -> execute (map to Discord ops or ported tools), feed results back.
   - Multimodal: forward images/audio/video to Pi prompts.
   - Keep admin checks, rate limits, etc.
   - Make thin: no more custom LLM loop in Python.

4. **Port ALL tools/systems as Pi custom tools/extensions/skills**:
   - Create/enhance `.pi/extensions/maxwell-brain/` (or use SDK defineTool in bridge).
   - Port every tool: send/edit/delete/forward messages, create_site (exact HTML + CSP + images + quota), web_search/fetch/youtube (use existing or Pi native), image_gen (NVIDIA/Pollinations), shell (via sandbox), polls, invites, avatar, channels (with gates), memory ops, etc.
   - Site creator: must produce Caddy-served HTML (test in Docker).
   - Autonomy: Pi skill or background loop that periodically prompts Pi for self-directed actions (post, tool call).
   - REM: Skill for "dream" consolidation on events buffer.
   - Intel: Background skill using web tools to gather/inject facts to memory.
   - Memory: Use Pi sessions + custom tools for long_term, shared_context, rem_events.
   - Register via extension (for hot-reload) or inline in SDK.
   - Same providers in Pi (env keys).

5. **Wire Discord bot to use Pi as brain**:
   - On message/reaction/etc.: build context -> send to Pi.
   - Pi decides everything (text response + tools).
   - Handle streaming (deltas to Discord).
   - Preserve features: visual memory, custom prompts per server, drug mode, blacklist, etc. as Pi context or tools.
   - Command queue from API still works (feed as prompts).

6. **Providers & config**:
   - Read .env exactly as before.
   - `tools/setup-pi-providers.sh` or direct env for Pi (NVIDIA_API_KEY, OPENROUTER_API_KEY, etc.).
   - Pi uses same for its LLM calls (the brain).
   - Fallbacks, reasoning disable, etc. mapped.

7. **Ensure website creator & Caddy work in Docker**:
   - Pi tool writes to MAXWELL_SITE_DIR (volume).
   - Test: use Pi tool (via prompt or direct) to create site -> verify HTML/CSP/index.html -> Caddy would serve.
   - Same for Python fallback if needed.

8. **Testing & validation (intensive, inside Docker)**:
   - Build/run Docker.
   - Start integrated bot (needs DISCORD_TOKEN + keys; use test server).
   - Send messages -> Pi brain responds via tools.
   - Test tools (web, image, create_site -> check public/bot).
   - Autonomy/REM/Intel loops.
   - Multimodal attachments.
   - `python -m pytest -q` (update tests if bridge changes core).
   - `ruff check . --fix`.
   - Manual: site gen, Caddy compat, providers same.
   - Full access/internet verified (tools call out).

9. **Polish, docs, cleanup**:
   - Update README, AGENTS.md, progress.md with "Pi is now the brain".
   - Keep original code working as fallback.
   - Subagents/docs port concepts to Pi skills.
   - Commit on branch.
   - "Review your own work".

## Risks & Mitigations
- Python <-> Node bridge complexity: Use RPC (simplest, from Pi docs). Start with subprocess.
- Discord rate limits/state: Keep in Python thin layer.
- Multimodal in Pi: Map attachments; Pi SDK/RPC supports.
- Performance: Pi compaction/sessions handle long context.
- "My code was buggy": Pi's harness + clean extensions should make it solid.
- Docker: legacy pi-specific removed; use direct subprocess for Pi brain (with --no-builtin-tools to restrict fs perms).
- Website creator: Parity test in plan.

## Immediate Next Actions (start here)
- Update this plan + progress.md.
- Explore Pi RPC with a small test (in Docker).
- Skeleton Python RPC client.
- Basic Pi extension for 1-2 tools (e.g., create_site).
- Iterate in Docker.

This makes Pi the brain while preserving Maxwell's Discord features, tools, and Docker/Caddy setup. Use Pi extensively (as per AGENTS.md).

**Status: Plan created. Ready for phase 2 implementation.**