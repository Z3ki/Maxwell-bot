# Maxwell configuration quick reference

The installer writes `.env` from `.env.example` and updates only the keys it asks about. Keep `.env` private; it is ignored by git.

## Values set by the wizard

| Variable | Required? | Purpose |
|---|---:|---|
| `DISCORD_TOKEN` | Yes | Discord **user** token for the self-bot account. Treat it like a password. |
| `OLLAMA_BASE_URL` | Yes | OpenAI-compatible base URL. A bare host such as `http://localhost:11434` gets `/v1` appended by the provider code. |
| `OLLAMA_MODEL` | Yes | Chat model name served by that endpoint. |
| `OLLAMA_API_KEY` | Sometimes | ****** for hosted providers such as OpenRouter or OpenAI; blank is normal for local Ollama/LM Studio. |
| `MAXWELL_OWNER_IDS` | Strongly recommended | Comma-separated Discord user IDs allowed to run admin commands. Blank means admin commands are denied to everyone. |
| `MAXWELL_ADMIN_USER` | Optional | Admin username for dashboard/API auth (defaults to `admin`). |
| `MAXWELL_ADMIN_PASSWORD` | Strongly recommended | Password for the admin API/dashboard. Blank makes the API return 503. |
| `ENABLE_AUTONOMY` | Optional | Timed self-directed background actions; off by default to avoid surprise token spend. |
| `ENABLE_REM` | Optional | Timed memory consolidation (also accepted as `REM_ENABLED`); off by default to avoid surprise token spend. |
| `ENABLE_SHELL` | Optional | Shell tool. Requires Docker; the installer disables it when Docker is unavailable. |

See [`.env.example`](../.env.example) for the full set of advanced knobs, including embeddings, dashboard host/port, TTS, X/Twitter, email, captcha solving, and tool-specific limits.

## Common provider snippets

```ini
# Local Ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
OLLAMA_API_KEY=
```

```ini
# OpenRouter
OLLAMA_BASE_URL=https://openrouter.ai/api/v1
OLLAMA_MODEL=moonshotai/kimi-k2.6:free
OLLAMA_API_KEY=your-openrouter-key
```

```ini
# OpenAI
OLLAMA_BASE_URL=https://api.openai.com/v1
OLLAMA_MODEL=gpt-4.1-mini
OLLAMA_API_KEY=your-openai-key
```

```ini
# LM Studio
OLLAMA_BASE_URL=http://localhost:1234/v1
OLLAMA_MODEL=the-loaded-model-name
OLLAMA_API_KEY=
```

## Reconfigure

From a cloned checkout:

```bash
./install.sh --local --reconfigure
```

Or, for an existing install made by the one-liner:

```bash
cd ~/maxwell
./install.sh --local --reconfigure
```
