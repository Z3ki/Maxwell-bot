# Maxwell overview

Maxwell is a Discord self-bot powered by any OpenAI-compatible chat API. It can read Discord text plus images, audio, video, files, and embeds, then answer with an LLM that can call tools such as web search, URL fetch, YouTube extraction, image generation, chess, memory, shell-in-Docker, and site generation.

> **Discord Terms of Service warning:** Maxwell uses `discord.py-self` with a user account token. Self-bots may violate Discord's Terms of Service and can put the account at risk. Use a dedicated account only if you accept that risk.

## How the pieces fit

```text
Discord account/session
        │
        ▼
     bot.py ────────────────┐
        │                   │
        ▼                   ▼
   providers.py       bot_tools.py / tool_registry.py
        │                   │
        ▼                   ├── docker/ shell sandbox
OpenAI-compatible LLM       ├── rag_memory.py + SQLite memory
(Ollama, OpenRouter,        ├── autonomy.py / rem.py background loops
 OpenAI, LM Studio, etc.)   └── api/api_server.py + web/ dashboard
```

## Main modules

| Path | What it does |
|---|---|
| `bot.py` | Discord client, message ingestion, multimodal handling, tool-call loop, chat commands. |
| `config.py` | Loads `.env`, validates core settings, resolves optional feature flags. |
| `providers.py` | OpenAI-compatible chat/streaming provider client and base URL normalization. |
| `bot_tools.py`, `tool_registry.py`, `tool_schemas.py`, `tools.py` | Tool implementations, registration, and LLM schemas. |
| `rag_memory.py` | SQLite-backed semantic memory using an OpenAI-compatible embeddings endpoint. |
| `autonomy.py`, `rem.py` | Optional timed background reasoning and REM memory consolidation. |
| `api/api_server.py`, `web/` | Local admin API and dashboard. |
| `docker/` | Container image used by the shell tool sandbox. |
| `doctor.py` | Installation/configuration report; `--probe` calls the configured endpoints. |
| `ecosystem.config.js` | PM2 process definitions for the bot, API, and optional Ollama process. |
| `requirements.txt` | Core Python packages required to start Maxwell. |
| `requirements-optional.txt` | Optional packages for web search, YouTube, voice, and TTS features. |

## Feature flags

Most optional features are controlled by `ENABLE_*` variables in `.env`. They are tri-state:

- `auto` (or unset): enable the feature only when its dependency is present.
- `true`: force the feature on.
- `false`: force the feature off.

This lets a basic install work with only the core dependencies. For example, `ENABLE_WEB_SEARCH=auto` turns on only when `ddgs` is installed, `ENABLE_VIDEO_INPUT=auto` needs `ffmpeg`, and `ENABLE_VC=auto` needs the voice Python packages plus system audio libraries. `ENABLE_REM` and `ENABLE_AUTONOMY` default to `false` because they spend model tokens on a timer.

`ENABLE_SHELL=true` is the default in `.env.example`, but the shell tool requires a reachable Docker daemon. The installer sets `ENABLE_SHELL=false` when Docker is not available or not selected.

## Checking an install

Run:

```bash
python3 doctor.py
python3 doctor.py --probe
```

`doctor.py` reports Python, core packages, required `.env` values, optional system tools, Docker reachability, X/Twitter status, and resolved feature flags. `--probe` additionally calls the chat `/models` endpoint and the embedding endpoint when RAG is enabled.
