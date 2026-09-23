# Isolated Maxwell Dev Docker

This Compose project is for a separate Discord application in designated test
guilds. Its source checkout, token, Ollama model, data, logs, network, and
Compose project are separate from Production. It exposes no host port, mounts
no Docker socket, and disables the web API and voice. Production is not
restarted by Dev Compose commands.

## Prepare

1. Use a separate checkout of `dev` or a reviewed feature branch. Do not run
   Dev Compose from the Production bind-mounted checkout.
2. Copy `docker/dev.env.example` to `.env.dev` in the Dev checkout, set a
   **separate** Discord bot token and numeric test guild IDs, and `chmod 600`
   the file. `.env.dev` is Git-ignored and excluded from Docker build context.
3. Invite the Dev application only to test guilds. Dev rejects DMs and events
   from guilds outside `MAXWELL_DEV_GUILD_IDS`. Its slash commands are synced
   only to those guilds. Disable or remove any old global commands in the
   separate Dev application's Developer Portal if previously published.

## Start

From the Dev checkout. The read-only source bind cannot create nested volume
mountpoints, so those directories must exist before Compose starts:

```sh
mkdir -p data logs public/bot shelldocker
docker compose -f docker-compose.dev.yml config --quiet
docker compose -f docker-compose.dev.yml up -d ollama-dev
docker compose -f docker-compose.dev.yml exec -T ollama-dev ollama pull qwen2.5:1.5b
docker compose -f docker-compose.dev.yml up -d --build maxwell-dev
docker compose -f docker-compose.dev.yml ps
```

The local model is a small functional test model; evaluate quality separately.
The Ollama service has no host port and uses the `maxwell-dev_dev_models`
volume. The bot is capped at one CPU/3 GB RAM; Ollama is capped at one CPU/3 GB.
Neither service uses the Production inference key.

## Verify and stop

Check `docker compose -f docker-compose.dev.yml ps` for a healthy Dev bot.
Use the Dev Discord application in the allowed test guild for a real rendering
and conversation check. Health only proves the bot process is running; it does
not prove Discord delivery or model quality. Check Production's independent
container state before and after Dev maintenance.

```sh
docker compose -f docker-compose.dev.yml stop
```

`stop` retains Dev data, logs, and model. `down` removes Dev containers and
network while retaining named volumes. Never use `down -v` unless Dev data
deletion has been reviewed and backed up. Rebuild Dev with `up -d --build
maxwell-dev`; this does not restart Production. Roll back by checking out the
previous Dev commit in this isolated checkout and rebuilding only the Dev bot.

## Limits

The guard runs inside Maxwell's event handlers. Discord can still deliver
gateway metadata for any guild where the Dev application was invited, so keep
the Dev application out of non-test guilds. This setup does not migrate or
share Production databases. Database changes require a separate Dev migration
test and backup before any Production release.
