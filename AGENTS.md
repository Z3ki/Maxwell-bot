# AGENTS.md for Maxwell (Pi Coding Agent + human devs)

This project is Maxwell: a feature-rich Discord self-bot with LLM tool-calling (OpenAI-compatible providers), multimodal (image/audio/video), autonomy engine, REM memory consolidation, intel gatherer, web dashboard/API, temporary site generator ("website creator"), voice, sub-agents, and more.

## Key Principles
- **Use Pi extensively here.** Run `pi` in /workspace. The project is set up with `.pi/extensions/maxwell-tools` for parity with bot tools.
- Prefer editing via Pi tools (read/write/edit). Use `maxwell_*` tools from the extension for web, images, site creation (exact parity with bot's CreateSiteTool so Caddy-served sites work).
- Same providers: export the keys from .env (NVIDIA_API_KEY, OPENROUTER_API_KEY or the OLLAMA_FALLBACK keys map to it, etc). Pi discovers them.
- All changes must keep website creator, API, bot tools, and Docker working.
- Tests: `python -m pytest -q`. Lint: `ruff check . --fix`.
- Docker: optional/general (legacy files removed; main integration is direct Python + `pi --mode rpc` subprocess).
- Security: self-bot (risk), admin-only for dangerous (shell, site, some channel ops). Autonomy can be powerful/dangerous — review before enabling.

## Project Layout (Pi-relevant)
- bot.py, bot_tools.py, providers.py : core bot + tools + LLM (rewrite target for cleanliness)
- api/api_server.py + web/ : dashboard
- docker/ : legacy (pi-specific removed; Pi brain runs via subprocess, not containerized by default).
- .pi/extensions/maxwell-tools/ : ported tools for Pi (web, fetch, image, yt, create_site, list_sites)
- examples/Caddyfile.example : serves generated sites under /bot/* + API reverse proxy. Use separate origin for sites if possible.
- data/ (gitignored): state, sites.json
- public/bot/ (gitignored): generated sites served by Caddy
- subagents/*.md : architecture notes (port concepts to Pi skills if useful)
- requirements*.txt, config.py, control_defaults.py, rem.py, autonomy.py, intel.py, memory.py, utils.py

## Running
- Python venv + pip -r requirements.txt
- `python bot.py` (needs DISCORD_TOKEN + LLM keys)
- `python api/api_server.py`
- Or PM2 (ecosystem.config.js)
- In Pi container: `pi` then use tools or /maxwell-status

## Porting Notes (experimental pi port)
- Tools ported as Pi extensions for parity (see maxwell-tools).
- Providers identical via env.
- Rewrite goal: cleaner modular Python, fix known issues (autonomy safety, gates, shutdowns), make subagent use Pi.
- Site creation must continue to produce valid served HTML even from inside Debian Docker.

## When using LLM (Pi or Maxwell)
- Be explicit with paths, avoid destructive without confirm.
- For site work: use maxwell_create_site or direct write to public/bot/<slug>/index.html
- Verify with `maxwell_list_sites`

See README.md, subagents/*.md, progress.md (full history), pi.dev docs.

This file is for both humans and agents (Pi bakes docs into context).
