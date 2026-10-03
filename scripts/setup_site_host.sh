#!/usr/bin/env bash
# Install the site pool without restarting Docker, replacing sites or moving data.
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
    echo "Run on the Docker host: sudo bash scripts/setup_site_host.sh" >&2
    exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(uname -s)" != "Linux" ]] || ! command -v systemctl >/dev/null; then
    echo "Site sandbox setup requires a Linux systemd Docker host." >&2
    exit 1
fi
install -d -m 0755 /etc/maxwell-sites
rm -f /etc/maxwell-sites/resource-pool-ready
install -m 0644 "${SCRIPT_DIR}/maxwell-sites.slice" /etc/systemd/system/maxwell-sites.slice
install -m 0755 "${SCRIPT_DIR}/setup_site_resource_pool.sh" /usr/local/sbin/maxwell-sites-resource-pool
install -m 0644 "${SCRIPT_DIR}/maxwell-sites-resource-pool.service" /etc/systemd/system/maxwell-sites-resource-pool.service
systemctl daemon-reload
systemctl start maxwell-sites.slice
# Apply updated bounds to an already active slice too; these are not Docker restarts.
systemctl set-property --runtime maxwell-sites.slice CPUQuota=200% MemoryMax=4G MemorySwapMax=0 TasksMax=2048
systemctl enable maxwell-sites-resource-pool.service
systemctl restart maxwell-sites-resource-pool.service
echo "Mount /etc/maxwell-sites:/run/maxwell-sites-policy:ro and /sys/fs/cgroup:/run/maxwell-host-cgroup:ro in Maxwell."
echo "No existing backend was replaced. Restart each site through site_server one at a time to adopt the pool and non-root runsc runtime."
