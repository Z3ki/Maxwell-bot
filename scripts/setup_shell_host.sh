#!/usr/bin/env bash
# Prepare the Docker host for Maxwell's public shell.
# Installs gVisor runsc, the cgroup and egress units, and an XFS project
# quota for overlay2 when the current filesystem cannot enforce
# --storage-opt size. Does not change Docker's default runtime.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this on the Docker host as root." >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SNAP_DIR="/var/lib/maxwell-shell-setup"
SNAP_FILE="${SNAP_DIR}/running-containers.txt"
IMG="/var/lib/docker-overlay-quota.img"
MOUNT_POINT="/var/lib/docker/overlay2"
MOUNT_UNIT="var-lib-docker-overlay2.mount"
DROPIN_DIR="/etc/systemd/system/docker.service.d"
DROPIN="${DROPIN_DIR}/maxwell-overlay-quota.conf"

log() { printf '==> %s\n' "$*"; }

snapshot_containers() {
    install -d -m 0755 "${SNAP_DIR}"
    if docker info >/dev/null 2>&1; then
        docker ps --filter status=running --format '{{.Names}}' | sort -u >"${SNAP_FILE}"
    fi
}

restore_containers() {
    [[ -f "${SNAP_FILE}" ]] || return 0
    local name running
    while IFS= read -r name; do
        [[ -n "${name}" ]] || continue
        running="$(docker inspect -f '{{.State.Running}}' "${name}" 2>/dev/null || echo false)"
        if [[ "${running}" != "true" ]]; then
            docker start "${name}" >/dev/null || echo "Could not start ${name}." >&2
        fi
    done <"${SNAP_FILE}"
}

runsc_path() {
    command -v runsc
}

install_runsc_binary() {
    local arch url dir
    arch="$(uname -m)"
    url="https://storage.googleapis.com/gvisor/releases/release/latest/${arch}"
    dir="$(mktemp -d)"
    curl -fsSL "${url}/runsc" -o "${dir}/runsc"
    curl -fsSL "${url}/runsc.sha512" -o "${dir}/runsc.sha512"
    (
        cd "${dir}"
        sha512sum -c runsc.sha512
    )
    install -m 0755 "${dir}/runsc" /usr/local/bin/runsc
    rm -rf "${dir}"
}

install_runsc() {
    if command -v runsc >/dev/null 2>&1; then
        log "runsc already installed at $(runsc_path)"
        return 0
    fi
    log "Installing gVisor runsc"
    apt-get update
    apt-get install -y apt-transport-https ca-certificates curl gnupg
    curl -fsSL https://gvisor.dev/archive.key | gpg --batch --yes --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
        >/etc/apt/sources.list.d/gvisor.list
    apt-get update
    if ! apt-get install -y runsc; then
        log "gVisor apt package failed; installing the release binary"
        install_runsc_binary
    fi
    command -v runsc >/dev/null 2>&1
}

write_daemon_json() {
    local bin
    bin="$(runsc_path)"
    python3 - "${bin}" <<'PY'
import json, sys
from pathlib import Path
path = Path("/etc/docker/daemon.json")
data = {}
if path.exists() and path.read_text().strip():
    data = json.loads(path.read_text())
if not isinstance(data, dict):
    raise SystemExit("daemon.json is not an object")
runtimes = data.get("runtimes")
if not isinstance(runtimes, dict):
    runtimes = {}
current = runtimes.get("runsc")
if not isinstance(current, dict):
    current = {}
current["path"] = sys.argv[1]
args = [item for item in (current.get("runtimeArgs") or []) if isinstance(item, str)]
args = [
    item for item in args
    if not item.startswith("--platform") and not item.startswith("--network")
]
args.extend(["--platform=systrap", "--network=sandbox"])
current["runtimeArgs"] = args
runtimes["runsc"] = current
data["runtimes"] = runtimes
# A daemon restart must stop shell guests. Never turn live-restore on.
data["live-restore"] = False
path.write_text(json.dumps(data, indent=2) + "\n")
path.chmod(0o644)
print(path.read_text())
PY
}

restart_docker() {
    log "Restarting Docker so runsc is registered"
    systemctl restart docker
    restore_containers
}

quota_works() {
    docker run --rm --storage-opt size=64m hello-world >/dev/null 2>&1
}

ensure_overlay_quota() {
    if quota_works; then
        log "Docker already accepts per-container storage limits"
        return 0
    fi
    local avail_kb used_kb img_bytes
    avail_kb="$(df -Pk /var/lib | awk 'NR==2 {print $4}')"
    used_kb="$(du -sk "${MOUNT_POINT}" | awk '{print $1}')"
    # Keep the current overlay layers, 40 GiB of headroom, and 20 GiB free on the host.
    img_bytes=$(( (used_kb + 40 * 1024 * 1024) * 1024 ))
    if (( img_bytes < 80 * 1024 * 1024 * 1024 )); then
        img_bytes=$((80 * 1024 * 1024 * 1024))
    fi
    if (( avail_kb * 1024 < img_bytes + 20 * 1024 * 1024 * 1024 )); then
        echo "Not enough free disk to put overlay2 on XFS (need the image plus 20 GiB free)." >&2
        exit 1
    fi
    if [[ -e "${MOUNT_POINT}.ext4-bak" ]]; then
        echo "${MOUNT_POINT}.ext4-bak already exists. Move it aside before retrying." >&2
        exit 1
    fi
    log "Docker storage is $(findmnt -T "${MOUNT_POINT}" -no FSTYPE). Creating an XFS quota filesystem."
    if [[ ! -f "${IMG}" ]]; then
        truncate -s "${img_bytes}" "${IMG}"
        mkfs.xfs -f "${IMG}"
    fi
    mkdir -p /mnt/maxwell-overlay-new
    if ! findmnt -T /mnt/maxwell-overlay-new >/dev/null || [[ "$(findmnt -T /mnt/maxwell-overlay-new -no FSTYPE)" != "xfs" ]]; then
        mount -o loop,pquota "${IMG}" /mnt/maxwell-overlay-new
    fi
    log "Copying overlay2 (this is the long part)"
    # Live containers can unlink files mid-copy. The stopped pass below is the consistent one.
    set +e
    rsync -aHAX --numeric-ids "${MOUNT_POINT}/" /mnt/maxwell-overlay-new/
    live_rc=$?
    set -e
    if [[ "${live_rc}" -ne 0 && "${live_rc}" -ne 24 ]]; then
        echo "overlay2 copy failed (${live_rc})." >&2
        exit "${live_rc}"
    fi
    log "Stopping Docker for the final overlay copy"
    systemctl stop docker.service docker.socket
    rsync -aHAX --numeric-ids --delete "${MOUNT_POINT}/" /mnt/maxwell-overlay-new/
    umount /mnt/maxwell-overlay-new
    mv "${MOUNT_POINT}" "${MOUNT_POINT}.ext4-bak"
    mkdir "${MOUNT_POINT}"
    chmod --reference="${MOUNT_POINT}.ext4-bak" "${MOUNT_POINT}"
    cat >"/etc/systemd/system/${MOUNT_UNIT}" <<EOF
[Unit]
Description=XFS project quotas for Docker overlay2
DefaultDependencies=no
Before=docker.service
After=local-fs.target

[Mount]
What=${IMG}
Where=${MOUNT_POINT}
Type=xfs
Options=loop,pquota

[Install]
WantedBy=docker.service
EOF
    systemctl daemon-reload
    systemctl start "${MOUNT_UNIT}"
    if ! findmnt -T "${MOUNT_POINT}" -no OPTIONS | grep -Eq 'pquota|prjquota'; then
        echo "XFS mount is missing project quotas. Restoring the old overlay2." >&2
        systemctl stop "${MOUNT_UNIT}" || true
        rmdir "${MOUNT_POINT}" || true
        mv "${MOUNT_POINT}.ext4-bak" "${MOUNT_POINT}"
        systemctl start docker
        restore_containers
        exit 1
    fi
    install -d -m 0755 "${DROPIN_DIR}"
    cat >"${DROPIN}" <<EOF
[Unit]
Requires=${MOUNT_UNIT}
After=${MOUNT_UNIT}
EOF
    systemctl daemon-reload
    systemctl enable "${MOUNT_UNIT}"
    systemctl start docker
    restore_containers
    MIGRATED=1
    if ! quota_works; then
        echo "Storage limits still fail after the XFS move." >&2
        exit 1
    fi
    log "Per-container storage limits work. The old layers are in ${MOUNT_POINT}.ext4-bak"
}

install_policy_units() {
    install -d -m 0755 /etc/maxwell-shell
    if [[ ! -e /etc/maxwell-shell/blocked-cidrs ]]; then
        python3 - <<'PY'
import ipaddress, subprocess
from pathlib import Path
route = subprocess.check_output(["ip", "-4", "route", "show", "default"], text=True)
dev = ""
for line in route.splitlines():
    parts = line.split()
    if "dev" in parts:
        dev = parts[parts.index("dev") + 1]
        break
cidrs = []
if dev:
    link = subprocess.check_output(["ip", "-4", "route", "show", "dev", dev, "scope", "link"], text=True)
    for line in link.splitlines():
        token = line.split()[0] if line.split() else ""
        try:
            net = ipaddress.ip_network(token, strict=False)
        except ValueError:
            continue
        if net.version == 4 and not net.is_private and net.prefixlen >= 8:
            cidrs.append(str(net))
text = ""
if cidrs:
    text = "# On-link public subnet. Stops shell guests from scanning neighbor hosts.\n" + "\n".join(cidrs) + "\n"
Path("/etc/maxwell-shell/blocked-cidrs").write_text(text)
Path("/etc/maxwell-shell/blocked-cidrs").chmod(0o644)
PY
    fi
    install -m 0644 "${REPO_DIR}/scripts/maxwell-shell.slice" /etc/systemd/system/maxwell-shell.slice
    install -m 0755 "${REPO_DIR}/scripts/setup_shell_resource_pool.sh" /usr/local/sbin/maxwell-shell-resource-pool
    install -m 0644 "${REPO_DIR}/scripts/maxwell-shell-resource-pool.service" /etc/systemd/system/maxwell-shell-resource-pool.service
    install -m 0755 "${REPO_DIR}/scripts/setup_shell_egress_firewall.sh" /usr/local/sbin/maxwell-shell-egress
    install -m 0644 "${REPO_DIR}/scripts/maxwell-shell-egress.service" /etc/systemd/system/maxwell-shell-egress.service
    systemctl daemon-reload
    systemctl enable --now maxwell-shell-resource-pool.service
    systemctl enable --now maxwell-shell-egress.service
}

verify() {
    python3 - <<'PY'
import json, subprocess
info = json.loads(subprocess.check_output(["docker", "info", "--format", "{{json .Runtimes}}"], text=True))
runsc = info.get("runsc") or {}
args = runsc.get("runtimeArgs") or []
if "--platform=systrap" not in args or "--network=sandbox" not in args:
    raise SystemExit(f"runsc runtimeArgs are not visible to Docker: {runsc}")
print("runsc runtimeArgs:", args)
PY
    docker run --rm --runtime=runsc hello-world >/dev/null
    docker run --rm --storage-opt size=64m hello-world >/dev/null
    [[ "$(cat /etc/maxwell-shell/egress-ready)" == "maxwell-shell-egress-v2" ]]
    [[ "$(cat /etc/maxwell-shell/resource-pool-ready)" == "maxwell-shell-resource-pool-v2" ]]
    log "Shell host is ready"
}

MIGRATED=0
snapshot_containers
install_runsc
write_daemon_json
ensure_overlay_quota
if [[ "${MIGRATED}" != 1 ]]; then
    restart_docker
fi
install_policy_units
verify
