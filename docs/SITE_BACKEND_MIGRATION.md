# Legacy site backend migration plan

**Status: planning only. No production Docker state or site data was inspected,
backed up, moved, or deleted.** The current Linux Compose service still mounts
`/var/run/docker.sock`; the read-only bind flag does not restrict Docker API
operations. Do not remove that mount until the site lifecycle and dashboard
paths have been migrated and verified.

## Inventory

`site_server.py` labels managed containers with `maxwell.site=<slug>`, names
them `maxwell-site-<slug>`, and builds images named
`maxwell-siteimg-<slug>`. Per-site code and backend data live below
`DATA_DIR/site_servers/<slug>/`; `DATA_DIR/site_servers.json` is the registry
and may contain backend environment secrets.

On the operator host, run the read-only inventory with a private output path:

```bash
python scripts/inventory_site_backends.py \
  --data-dir "$DATA_DIR" \
  --output /secure/operator-only/site-backend-inventory.json
```

The report lists container state, restart policy, resource limits, host mounts,
published ports, networks, image references, and aggregate site file counts and
sizes. It deliberately excludes environment variables, logs, and file
contents. The output still contains host paths and must remain private. Review
Docker containers by both name and label, then reconcile them with the registry
and site directories; do not assume that the registry lists every container or
that every container is still referenced.

## Backup and target boundary

Before migration, take a consistent backup of the site registry, per-site code,
backend data, published static-site files, and any operator-managed config that
controls these routes. Prefer a provider-level filesystem snapshot or a planned
maintenance window so registry rows and databases are not copied mid-write.
The backup may include credentials from `site_servers.json`; store and handle
it as a secret. Verify restoreability without overwriting the live data.

Move Docker lifecycle operations into a dedicated site-controller service. The
Maxwell bot and dashboard should call an authenticated, narrow API for named
site operations such as build, start, stop, health, and bounded logs. The
controller must validate the site slug, use fixed host data roots and network
settings, enforce resource limits, and reject arbitrary Docker arguments,
mounts, images, hosts, or commands. Only the controller should mount the Docker
socket. Do not expose an unrestricted Docker TCP endpoint or proxy.

## Migration and release gates

1. Map every dashboard/API/tool caller of `site_server.py` and define the
   authenticated site-controller contract.
2. Implement the controller and a client with scoped operations. Preserve the
   existing host paths and site URLs during the first migration.
3. On the isolated dev deployment, migrate a copy of the inventory and data.
   Verify site updates, start/stop/restart, health checks, logs, deletion,
   dashboard operations, and rollback from the backup.
4. Confirm the Maxwell bot container has no Docker socket mount and cannot
   reach the controller from an untrusted shell guest. Test the controller's
   authorization and rejected arbitrary-operation cases.
5. Schedule a production maintenance window, take and verify a fresh backup,
   migrate site state, and only then remove Docker socket mounts from production
   Compose. Verify site URLs and administrative operations after restart.

Do not prune images or containers as part of inventory. Do not delete old site
containers, images, registry rows, or data until the migrated service has been
verified and the rollback window has closed.
