# Custom site backend runtime and migration

Maxwell supports real custom Python backends through `site_server`, in addition
to the built-in JSON/KV backend. New apps use **Python 3.12 + FastAPI/Uvicorn +
stdlib sqlite3**, one Uvicorn worker, no reload, `limit_concurrency=32` and
`backlog=64`. Flask/waitress stay installed so existing apps need not be rewritten.
The shared `maxwell-site-runtime` image is operator-built once and reused.
Generated code cannot request a new package installation or trigger an image
build. Existing registered package lists can be reused unchanged only while
that site's existing `maxwell-siteimg-<slug>` image remains available; changing
to `packages=[]` selects the shared image.

## Supported host and setup

The supported deployment is Linux Docker Engine with **cgroup v2, systemd cgroup
driver and already registered gVisor `runsc`**. macOS/Windows bridge deployments
cannot start custom backends under this host resource model. Setup does not
install/reconfigure/restart Docker, change its default runtime, stop existing
sites, or move/delete application data.

On the Docker host, from the checkout:

```bash
# This builds the fixed, trusted runtime, not user-provided code or packages.
docker build -t maxwell-site-runtime docker/site-runtime
sudo bash scripts/setup_site_host.sh
systemctl status maxwell-sites.slice maxwell-sites-resource-pool.service
```

The installer installs `scripts/maxwell-sites.slice`, the host validator and
`maxwell-sites-resource-pool.service`; it starts/validates the pool and proves a
non-root runsc probe actually enters its cgroup. Readiness is published only
after validation, in `/etc/maxwell-sites/resource-pool-ready`. The service removes
the marker when stopped, follows Docker's service lifecycle and revalidates on
boot. Rerun setup after changing Docker or slice configuration. A missing shared
image or failed probe leaves readiness absent with an actionable error.

The Maxwell controller requires these read-only host mounts (Linux Compose
supplies them):

```yaml
- /etc/maxwell-sites:/run/maxwell-sites-policy:ro
- /sys/fs/cgroup:/run/maxwell-host-cgroup:ro
```

Before every start, Maxwell requires the marker, validates the *live* aggregate
`memory.max`, `memory.swap.max`, `cpu.max`, and `pids.max`, and checks Docker's
cgroup driver/version and runsc registration. Missing, unbounded or oversized
limits fail closed **before removing a live backend**. The controller still
uses the Docker socket for its fixed lifecycle operations; a read-only socket
bind is not a Docker authorization boundary. Generated site containers never
receive that socket, the controller's host cgroup mount or arbitrary host mounts.
Moving lifecycle into a separate authenticated controller would be a separate
security project, not something this resource pool implements.

## Enforced budgets and persistence

| Boundary | Limit |
| --- | --- |
| Each backend | 256MiB RAM, no swap, 0.5 CPU, 128 PIDs, bounded file descriptors |
| `maxwell-sites.slice` aggregate | 2 CPUs, 4GiB RAM, no swap, 2,048 tasks |
| Admission | At most 64 managed containers, including stopped/orphaned containers |
| Temporary writes | 32MiB `/tmp` tmpfs, noexec/nosuid/nodev |
| Docker logs | json-file, 2MiB per file, two files per site |
| API proxy | 32 concurrent requests per site, 128 total, including WebSocket/SSE; busy returns 503 |

Every new/restarted site uses `--runtime runsc`, uid/gid `10001:10001`,
`cap_drop=ALL`, no-new-privileges and a read-only root filesystem. Only its own
source (`/app`, read-only) and persistent state (`/data`, writable) are mounted.
It listens on port 8000, published only on loopback ports 8800–8899. Public
requests reach it at `/bot/<slug>/api/...`, with that prefix stripped. Outbound
network access remains allowed; this pool is a resource/isolation boundary,
not an outbound egress allowlist. Site backend administrative/source operations
are restricted to the site owner or Maxwell admin.

Admission is based on actual Docker containers by `maxwell.site` label or
`maxwell-site-` name, not just `site_servers.json`. Registry locks and a separate
cross-process lifecycle lock serialize operations sharing the same deployment's
`DATA_DIR`. At exact capacity a replacement retains its slot; an overfull pool
rejects starts before replacing anything. Stop removes the container and frees
a slot but keeps source, env and database. Destroy explicitly removes the site;
a failed container teardown must not proceed to deleting its persistent state.

SQLite belongs at `/data/app.db`. Source, registry env secrets and `_data` are
outside the public static web root. Restart and code replacement do not delete
`_data`. Permission preparation supports old root-owned databases, including
SQLite journal/WAL files: it grants uid 10001 access without copying/truncating
files. With a cap-dropped controller, files it owns may require mode 0666 and
its private directory mode 1777 because chown is unavailable; these are private
per-site mounts, not shared between backends. Symlinks/special files are rejected,
and inaccessible state yields an explicit repair error rather than pretending
persistence is writable. Unexpected permissions require host operator repair.

**Persistent SQLite storage is not disk-quota enforced.** The tmpfs/log limits
and admission count do not bound database growth. Monitor host disk use and
maintain backups; this change does not claim a per-site persistent disk quota.
The aggregate budget applies to site runtime processes, not the trusted
operator's shared-image build or the Maxwell controller/API service itself.

## Existing backend migration

Installing the pool **does not automatically move old root/runc containers**.
An existing running container retains its old settings until recreated through
`site_server.start` / the owner/admin `restart` action. Existing Flask code,
registered secrets, package metadata and databases are preserved. Each site has
brief downtime during its own replacement; Docker and the host do not need a
shutdown. Do not run Docker prune, destroy sites or replace source as migration.

Before replacing anything, take a consistent backup/snapshot of
`DATA_DIR/site_servers.json`, `DATA_DIR/site_servers/`, published static sites and
operator route configuration. The registry contains secrets; backups/inventory
must remain private. Inventory actual Docker names/labels as well as registry:

```bash
python scripts/inventory_site_backends.py \
  --data-dir "$DATA_DIR" \
  --output /secure/operator-only/site-backend-inventory.json
```

Map orphaned managed containers to their existing source/registry before
migration; do not assume every container has a registry row. Do not delete
unmatched source or databases. Bring an overfull deployment below 64 containers
through intentional stops before migrating. Preserve any legacy dependency
images; a missing legacy image fails before replacement instead of silently
rebuilding packages outside the runtime budget.

After setup and controller redeployment with both read-only mounts, migrate
**one site at a time**, then verify it before continuing. The owner/admin can
use `site_server(action="restart", name="<slug>")`. An operator can invoke the
same lifecycle inside Linux Compose, without editing source or env:

```bash
docker compose exec -T maxwell python - demo <<'PY'
import asyncio, sys
from config import Config
import site_server
entry = asyncio.run(site_server.start(Config.DATA_DIR, sys.argv[1]))
print({'running': entry['running'], 'health': entry['health']})
PY
```

Replace `demo` with the inventoried slug. Check backend-specific read/write
routes, SQLite row preservation and frontend/API functionality before migrating
the next site. Repeating restart with omitted env/packages reuses registry
configuration. Never print registry env values into deployment logs.

## Verification commands

```bash
docker info --format '{{.OSType}} {{.CgroupDriver}} {{.CgroupVersion}} {{json .Runtimes}}'
systemctl show maxwell-sites.slice -p ControlGroup -p CPUQuotaPerSecUSec \
  -p MemoryMax -p MemorySwapMax -p TasksMax
cat /sys/fs/cgroup/maxwell.slice/maxwell-sites.slice/memory.max
cat /sys/fs/cgroup/maxwell.slice/maxwell-sites.slice/memory.swap.max
cat /sys/fs/cgroup/maxwell.slice/maxwell-sites.slice/cpu.max
cat /sys/fs/cgroup/maxwell.slice/maxwell-sites.slice/pids.max
docker inspect --type container --format \
  '{{.HostConfig.Runtime}} {{.HostConfig.CgroupParent}} {{.Config.User}} {{json .HostConfig.CapDrop}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}} {{json .HostConfig.LogConfig}}' \
  maxwell-site-demo
curl --fail http://127.0.0.1:8800/notes
python -m pytest tests/test_site_server.py -q
```

Use the site's actual registered loopback port and real route instead of assuming
8800/notes. For a new notes app, POST a unique note through the public proxy,
restart the backend through the lifecycle, and GET that note again. Confirm
missing readiness/altered live limits reject a deployment without removing the
old container. Resource settings on old containers must be checked individually;
a working marker alone is not evidence that legacy sites were migrated.
