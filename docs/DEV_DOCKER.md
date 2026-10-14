# Isolated Maxwell Dev Docker

Production stays on `main` at `/root/maxwell`; development runs from the separate
`dev` worktree at `/root/maxwell-dev`. Dev has a different Discord application,
token, guild allowlist, Compose project, network, data/log/public/shell volumes
and optional model volume. The source bind is read-only and no production data
is mounted. Dev runs with host networking and the same host-tools contract as
Production (read-only Docker socket, shell/site policy mounts), plus its own
operator API on `127.0.0.1:8766`, site-backend ports `8900-8999`, Lavalink port
`2334` and generated-site routes under `https://<host>/dev/`. Production owns
`:8765`, `8800-8899` and `2333`. Music is optional, not globally disabled: its
lazy backend starts on an allowed request.

## One Docker daemon, two controllers

Both bots share one Docker daemon, so Dev sets `MAXWELL_CONTROLLER_ID=dev`
(`docker-compose.dev.yml`). Every container Dev creates — shell sandboxes,
site backends, GitHub workspaces — carries that controller identity in its
name and labels, and startup cleanup only removes its own controller's
containers. Production keeps the unlabeled default identity and adopts only
legacy unlabeled guests. Aggregate shell/site resource limits remain
host-wide: both controllers' guests join the shared `maxwell-shell.slice` /
`maxwell-sites.slice` cgroups, and Dev's `MAXWELL_SHELL_MAX_CONCURRENCY`
keeps its admission share bounded.

## Prepare the worktree and identity

For initial setup only, if the worktree does not already exist:

```sh
cd /root/maxwell
git fetch origin
# Create dev from the reviewed main revision when it does not exist yet.
git worktree add -b dev /root/maxwell-dev origin/main
# If dev already exists locally, use: git worktree add /root/maxwell-dev dev
cd /root/maxwell-dev
cp docker/dev.env.example .env.dev
chmod 600 .env.dev
```

Use Docker with Compose v2, Git, Bash, Python 3 and Make. Configure `.env.dev`
with `MAXWELL_DEV_MODE=true`, a **separate official bot** `DISCORD_BOT_TOKEN`,
and comma-separated numeric `MAXWELL_DEV_GUILD_IDS`. Create/invite the Dev
application only into those test guilds, enable its required gateway intents,
and grant the app command/voice permissions needed for your checks. Commands
register only in allowlisted guilds. Remove any legacy global commands from the
Dev application. Never copy Production's Discord token, guild settings, entire
`.env`, databases, or generated data into Dev.

Only explicitly authorized primary/fallback AI endpoints, model names and
provider keys may be reused. Reused provider keys share billing, quotas and rate
limits: Dev traffic can affect Production. Prefer distinct limited keys where
available. Rotate any exposed token/key, update only its intended env file and
recreate that bot; update the guild allowlist before inviting/registering Dev in
a new test guild. Keep `.env.dev` private and out of Git.

## Lifecycle commands

Always run from `/root/maxwell-dev`, on `dev` or `feature/*`:

```sh
make updev
make restartdev             # rebuild image, recreate Dev bot to load edited code
make statusdev
make logsdev                # Ctrl-C stops following, not the bot
make stopdev                # keeps Dev volumes
```

Equivalent commands are `bash scripts/dev.sh up|restart|status|logs|stop`.
The helper derives its checkout root, refuses `/root/maxwell`, `main`, detached
checkouts and non-development branches, and requires the Dev mode, token and
guild allowlist. It always selects project `maxwell-dev` and uses
`docker compose --env-file .env.dev -f docker-compose.dev.yml`. It creates the
read-only bind's required mountpoint directories. Up/restart stamp the bot image
with the checkout's Git revision, rebuild only the Dev bot and selected audio
node and start required Dev dependencies. With music, they recreate the cipher
in the current node's namespace before recreating the Dev bot; they never operate on the public project. A Dev restart can
interrupt its own conversation/music state. Keep optional flags consistent
across lifecycle commands, especially `stopdev` to stop audio sidecars too.

### Optional local model

There is no mandatory Ollama dependency. To opt into profile `local-ai`, set
`.env.dev`'s AI endpoint to `http://127.0.0.1:11435` (the Dev node's loopback
port) and select the model:

```sh
LOCAL_AI=1 make updev
# Pull once into the separate dev_models volume:
docker compose --project-name maxwell-dev --env-file .env.dev \
  -f docker-compose.dev.yml --profile local-ai exec -T ollama-dev \
  ollama pull qwen2.5:1.5b
LOCAL_AI=1 make restartdev
LOCAL_AI=1 make stopdev
```

This small model is for functional checks, not a production-quality claim.
Alternatively, an existing host Ollama can be selected explicitly using
`http://host.docker.internal:11434` if reachable from the Dev bridge; that shares
inference capacity with the host, not Dev storage. No helper configures/exposes
the host service automatically.

### Optional music

Set **distinct generated** `LAVALINK_PASSWORD` and `YOUTUBE_CIPHER_PASSWORD` in
`.env.dev` (for example, generate each separately with `openssl rand -hex 32`).
Do not reuse Production audio credentials. Any YouTube OAuth token needs
separate authorization for Dev. Enable the isolated node and cipher overlay:

```sh
WITH_MUSIC=1 make updev
WITH_MUSIC=1 make restartdev
WITH_MUSIC=1 make statusdev
WITH_MUSIC=1 make stopdev
# Both options can be combined:
LOCAL_AI=1 WITH_MUSIC=1 make updev
```

`WITH_MUSIC=1` adds `docker-compose.dev.music.yml`; its `lavalink-dev` and
`yt-cipher-dev` are private to Dev. The node publishes only
`127.0.0.1:2334` for the host-networked Dev bot, and the cipher shares the
Dev node's network namespace. Voice remains permission-checked and confined to
allowlisted guilds. Without this overlay, non-music testing has no audio service
requirement; voice requests cannot produce playback without a configured node.

For videos requiring YouTube login, run `make youtubeauthdev` from this worktree.
Approve the displayed code at Google's device page with a separate burner
account. The helper handles pending/slow-down/expiry without request flooding
and saves the token only in private `.env.dev`; it never prints the token.
Then run `WITH_MUSIC=1 make restartdev` to apply the new node environment and
recreate its cipher in the correct namespace. Node startup does not create
unattended device codes. Provider restrictions can still reject a video.

### Optional Last.fm

Set `LASTFM_API_KEY` and `LASTFM_API_SECRET` from a Last.fm application, plus a
new Dev-only `LASTFM_ENCRYPTION_KEY` generated with `openssl rand -hex 32`.
Keep `.env.dev` private and back up that encryption key. Authorized application
credentials can be reused for Dev and Production, but share Last.fm's quota;
Discord tokens, encryption keys, linked accounts and data volumes stay separate.
Do not copy production account/session databases.

Leave the Last.fm application's **callback URL blank**. This uses token/browser
approval, not a hosted OAuth callback. Recreate Dev, then use `/lastfm connect`
in its allowlisted guild, approve Maxwell in your browser, and `/lastfm finish`
there. `/lastfm status` reports your private link/pending count; disconnect
removes local consent and pending listening. Asking Maxwell to connect sends a
private DM approval link; in Dev, finish through the slash command in the test
guild because the general Dev scope guard rejects DM interactions. Do not paste
account passwords, approval tokens or session keys into chat. See `MUSIC.md`
for listening eligibility, durable retry and verification limits.

## Real tests and Discord checks

Tests work **before** a Discord token/guild or `.env.dev` has been configured:

```sh
cd /root/maxwell-dev
make testdev                               # full actual pytest suite
make testdev PYTEST_ARGS='tests/test_config_tools.py -q'
bash scripts/dev.sh test -k music -q       # explicit pytest arguments
```

The helper builds the runtime with its test dependencies, then runs pytest in a
separate disposable container: no network, no persistent data volumes, no env
file passed, read-only checkout/root filesystem, writable executable `/tmp`
tmpfs for test fixtures, nonexistent temporary `MAXWELL_ENV_FILE`, and pytest's
cache provider disabled. Executable scratch space is limited to this offline
test container, not added to the live bot.
Existing checkout `.env`/`.env.dev` files are masked with `/dev/null` inside the
test container. Tests do not start Discord, Compose services or Production.
Network-dependent live integrations must instead be checked explicitly in Dev.
The linked worktree's `.git` is masked by disposable empty Git metadata, so
installer subprocesses work without mounting production Git history/config.

For the existing dependency/lint/shell/coverage gates, install
`requirements.txt` and `requirements-dev.txt` in an isolated Python environment
and run `make check`, or rely on the branch CI gates. Container pytest is not a
replacement for those gates. A healthy Dev bot only proves process liveness;
verify real Discord delivery, allowed/denied guild behavior, model responses,
and music queue/play/skip/stop in the Dev guild when promoting music changes.

`stopdev` retains all Dev data. To remove containers/network deliberately, use
the same Dev Compose files/env/project with `down`; never use `down -v` without
reviewed backups and deliberate data deletion. To roll back Dev, select a
reviewed prior commit on a `feature/*` branch and rebuild/recreate its bot.
Production database migration testing requires separate Dev data and a backup.

## Promote reviewed features, then deploy separately

Create `feature/<name>` from `dev` in the Dev worktree. Submit a PR into `dev`,
pass the branch CI checks, and exercise the separate Dev Discord application.
Then submit a reviewed PR from `dev` into `main`, pass main CI, and merge only
after the Dev Discord checks. No helper pushes, resets, rebases or automatically
deploys Production.

Publishing a release by updating `VERSION` and pushing a reviewed release tag
through the existing release workflow publishes artifacts; it does **not** mean
the public bot has been updated. Deployment is a separate operator action.
For this source/worktree installation, after approval and a recommended backup
of `.env`, databases and persistent volumes, run **only in Production**:

```sh
cd /root/maxwell
test "$(pwd -P)" = /root/maxwell
test "$(git branch --show-current)" = main
test -z "$(git status --porcelain)"   # do not overwrite local changes
git fetch origin
docker compose stop maxwell          # source is bind-mounted; drain before changing it
git pull --ff-only origin main       # only the reviewed main revision
docker compose build --build-arg "MAXWELL_BUILD_COMMIT=$(git rev-parse HEAD)" maxwell
docker compose up -d --force-recreate --no-deps maxwell
docker compose ps maxwell
docker compose logs --tail 100 maxwell
```

Run the guards successfully before continuing; commands are shown individually
so any failure can be inspected, not bypassed. Review fetched changes before
pulling. Recreating the public bot interrupts music/queues and in-flight work;
schedule downtime and do not assume queues persist. If a reviewed change also
updates public audio dependencies, follow `docs/MUSIC.md` deliberately rather
than letting Dev commands touch them. Release-installer deployments instead
follow `docs/INSTALL.md`'s pinned version update workflow. Neither path should
reuse Dev's Discord identity or volumes.

The guild guard applies inside event handlers; Discord can still deliver gateway
metadata for every guild the Dev app joins. Keep that application out of
non-test guilds even when the allowlist rejects their events.
