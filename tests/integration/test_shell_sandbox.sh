#!/usr/bin/env bash
set -euo pipefail

# Opt-in, host-level checks for a disposable Linux test deployment. These tests
# launch gVisor containers and intentionally exercise the host firewall.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NETWORK="maxwell-shell-egress"
IMAGE="maxwell-shell"
TEST_TAG="$(python3 -c 'import hashlib,os,time; print(hashlib.sha256(f"{os.getpid()}:{time.time_ns()}".encode()).hexdigest()[:24])')"
C1="mwsh-${TEST_TAG}"
C2="mwsh-$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:24])' "${TEST_TAG}")"
HOST_PROBE_FILE="$(mktemp /tmp/maxwell-shell-host-probe.XXXXXX)"
HOST_PROBE_INFO="$(mktemp /tmp/maxwell-shell-probe-info.XXXXXX)"
HOST_PROBE_PID=""

cleanup() {
    docker rm -f "${C1}" "${C2}" >/dev/null 2>&1 || true
    if [[ -n "${HOST_PROBE_PID}" ]]; then
        kill "${HOST_PROBE_PID}" >/dev/null 2>&1 || true
        wait "${HOST_PROBE_PID}" >/dev/null 2>&1 || true
    fi
    rm -f "${HOST_PROBE_FILE}" "${HOST_PROBE_INFO}"
}
trap cleanup EXIT

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this integration test as root on a disposable Linux test host." >&2
    exit 1
fi
if [[ ! -r /etc/maxwell-shell/egress-ready ]] || \
   [[ "$(cat /etc/maxwell-shell/egress-ready)" != "maxwell-shell-egress-v2" ]]; then
    echo "The host egress policy is not provisioned; run the host setup first." >&2
    exit 1
fi
if [[ ! -r /etc/maxwell-shell/resource-pool-ready ]] || \
   [[ "$(cat /etc/maxwell-shell/resource-pool-ready)" != "maxwell-shell-resource-pool-v2" ]]; then
    echo "The aggregate shell resource pool is not provisioned; run the host setup first." >&2
    exit 1
fi

POOL_GROUP="$(systemctl show --property=ControlGroup --value maxwell-shell.slice)"
if [[ ! "${POOL_GROUP}" =~ ^/[A-Za-z0-9_.@/-]+$ || "${POOL_GROUP}" == *..* ]]; then
    echo "Could not resolve the live aggregate shell cgroup." >&2
    exit 1
fi
POOL_CGROUP="/sys/fs/cgroup${POOL_GROUP}"
read -r POOL_MEMORY <"${POOL_CGROUP}/memory.max"
read -r POOL_SWAP <"${POOL_CGROUP}/memory.swap.max"
read -r POOL_CPU_QUOTA POOL_CPU_PERIOD <"${POOL_CGROUP}/cpu.max"
read -r POOL_TASKS <"${POOL_CGROUP}/pids.max"
HOST_MEMORY_KB="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
[[ "${POOL_MEMORY}" =~ ^[0-9]+$ && "${POOL_SWAP}" == 0 ]]
[[ "${POOL_CPU_QUOTA}" =~ ^[0-9]+$ && "${POOL_CPU_PERIOD}" =~ ^[0-9]+$ ]]
[[ "${POOL_TASKS}" =~ ^[0-9]+$ ]]
((POOL_MEMORY <= HOST_MEMORY_KB * 1024 / 2))
((POOL_CPU_QUOTA > 0 && POOL_CPU_QUOTA <= POOL_CPU_PERIOD * 2))
((POOL_TASKS > 0 && POOL_TASKS <= 1024))

if ! docker info --format '{{json .Runtimes}}' | python3 -c '
import json,sys
runtimes=json.load(sys.stdin)
runsc=runtimes.get("runsc") or {}
args=runsc.get("runtimeArgs") or []
sys.exit(0 if "--platform=systrap" in args and "--network=sandbox" in args else 1)
'; then
    echo "Docker's gVisor runsc runtime must use systrap and its isolated network stack." >&2
    exit 1
fi
if [[ "$(docker info --format '{{.LiveRestoreEnabled}}')" != false ]]; then
    echo "Docker live-restore must be disabled for reliable sandbox cleanup." >&2
    exit 1
fi

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    docker build -t "${IMAGE}" "${ROOT}/docker"
fi

for rule in \
    "-d 169.254.0.0/16 -j DROP" \
    "-d 10.0.0.0/8 -j DROP" \
    "-d 172.30.240.0/20 -j DROP"; do
# Each rule must exist in the externally enforced egress chain.
    read -r -a rule_args <<<"${rule}"
    iptables -C MAXWELL-SHELL-EGRESS "${rule_args[@]}"
done
ip6tables -C FORWARD -i br-maxwell-sh -j MAXWELL-SHELL-FORWARD6
ip6tables -C INPUT -i br-maxwell-sh -j MAXWELL-SHELL-HOST-IN6

python3 - "${HOST_PROBE_FILE}" "${HOST_PROBE_INFO}" <<'PY' &
import http.server
import pathlib
import socketserver
import sys

probe = pathlib.Path(sys.argv[1])
info = pathlib.Path(sys.argv[2])
class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        probe.write_text("host-service-reached", encoding="ascii")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"host-service")
    def log_message(self, *_args):
        pass

server = socketserver.TCPServer(("0.0.0.0", 0), QuietHandler)
info.write_text(str(server.server_address[1]), encoding="ascii")
server.serve_forever()
PY
HOST_PROBE_PID=$!
for _ in $(seq 1 50); do
    [[ -s "${HOST_PROBE_INFO}" ]] && break
    sleep 0.1
done
if [[ ! -s "${HOST_PROBE_INFO}" ]]; then
    echo "Could not start the host service probe." >&2
    exit 1
fi
HOST_PROBE_PORT="$(cat "${HOST_PROBE_INFO}")"
GATEWAY="$(docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' "${NETWORK}")"

common=(
    --runtime runsc
    --init
    --cgroup-parent maxwell-shell.slice
    --cap-drop ALL
    --security-opt no-new-privileges:true
    --memory 2g
    --memory-swap 2g
    --cpus 1.0
    --pids-limit 256
    --ulimit nofile=1024:2048
    --storage-opt size=6G
    --network "${NETWORK}"
    --tmpfs /workspace:rw,exec,nosuid,nodev,size=512m
    --tmpfs /tmp:rw,exec,nosuid,nodev,size=256m
)

docker run -d "${common[@]}" --name "${C1}" "${IMAGE}" >/dev/null
docker run -d "${common[@]}" --name "${C2}" "${IMAGE}" >/dev/null

read -r runtime memory memory_swap nano_cpus pids privileged network_mode cgroup_parent < <(
    docker inspect --format '{{.HostConfig.Runtime}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}} {{.HostConfig.Privileged}} {{.HostConfig.NetworkMode}} {{.HostConfig.CgroupParent}}' "${C1}"
)
[[ "${runtime}" == runsc ]]
[[ "${memory}" == 2147483648 ]]
[[ "${memory_swap}" == 2147483648 ]]
[[ "${nano_cpus}" == 1000000000 ]]
[[ "${pids}" == 256 ]]
[[ "${privileged}" == false ]]
[[ "${network_mode}" == "${NETWORK}" ]]
[[ "${cgroup_parent}" == "maxwell-shell.slice" ]]
docker inspect --format '{{json .HostConfig}}' "${C1}" | python3 -c '
import json,sys
c=json.load(sys.stdin)
assert not c.get("Binds")
assert c.get("CapDrop") == ["ALL"]
assert set(c.get("CapAdd") or []) == {
    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_KILL",
    "CAP_SETGID", "CAP_SETUID", "CAP_NET_BIND_SERVICE", "CAP_SYS_CHROOT",
    "CAP_MKNOD", "CAP_SETFCAP",
}
assert "no-new-privileges:true" in (c.get("SecurityOpt") or [])
assert c.get("PidMode") not in ("host", "container:")
assert set((c.get("Tmpfs") or {}).keys()) == {"/workspace", "/tmp"}
assert (c.get("StorageOpt") or {}).get("size") == "6G"
'

# Verify the live host cgroup assigned to the container, not just Docker's
# stored configuration. The process cgroup must sit beneath the aggregate
# slice and enforce the per-guest memory, CPU, process and swap limits.
C1_PID="$(docker inspect --format '{{.State.Pid}}' "${C1}")"
C1_GROUP="$(awk -F: '$1 == "0" {print $3}' "/proc/${C1_PID}/cgroup")"
[[ "${C1_GROUP}" == "${POOL_GROUP}/"* ]]
C1_CGROUP="/sys/fs/cgroup${C1_GROUP}"
read -r C1_MEMORY <"${C1_CGROUP}/memory.max"
read -r C1_SWAP <"${C1_CGROUP}/memory.swap.max"
read -r C1_CPU_QUOTA C1_CPU_PERIOD <"${C1_CGROUP}/cpu.max"
read -r C1_TASKS <"${C1_CGROUP}/pids.max"
[[ "${C1_MEMORY}" == 2147483648 && "${C1_SWAP}" == 0 ]]
[[ "${C1_CPU_QUOTA}" =~ ^[0-9]+$ && "${C1_CPU_PERIOD}" =~ ^[0-9]+$ ]]
((C1_CPU_QUOTA > 0 && C1_CPU_QUOTA <= C1_CPU_PERIOD))
[[ "${C1_TASKS}" == 256 ]]

root_probe="$(docker exec "${C1}" sh -lc 'id -u; awk "/^CapEff:/ {print \$2}" /proc/self/status; test ! -S /var/run/docker.sock && test ! -e /host && test ! -e /app && echo clean')"
EXPECTED_GUEST_CAPS="$(python3 -c 'print(f"{sum(1 << i for i in (0, 1, 3, 4, 5, 6, 7, 10, 18, 27, 31)):016x}")')"
[[ "${root_probe}" == $'0\n'"${EXPECTED_GUEST_CAPS}"$'\nclean' ]]

docker exec "${C1}" sh -lc 'printf tenant-one >/workspace/tenant.txt'
if docker exec "${C2}" sh -lc 'test -e /workspace/tenant.txt'; then
    echo "Tenant workspaces are shared." >&2
    exit 1
fi

# Root can use ordinary package tools inside the guest; package installation
# remains bounded by the guest and aggregate host cgroups.
docker exec "${C1}" sh -lc 'apt-get update -qq && apt-get install -y --no-install-recommends sl >/dev/null && command -v sl >/dev/null'

# Test that a process in one guest cannot connect to a service in another guest
# attached to the same bridge.
docker exec -d "${C2}" sh -lc 'python3 -m http.server 18991 --bind 0.0.0.0 >/tmp/http.log 2>&1'
C2_IP="$(docker inspect --format "{{(index .NetworkSettings.Networks \"${NETWORK}\").IPAddress}}" "${C2}")"
if docker exec "${C1}" sh -lc "curl -fsS --connect-timeout 2 http://${C2_IP}:18991/ >/dev/null"; then
    echo "A sandbox reached a peer sandbox." >&2
    exit 1
fi

# Public IPv4 is available for package repositories and development tools.
docker exec "${C1}" sh -lc 'curl -fsSI --connect-timeout 5 --max-time 15 https://deb.debian.org >/dev/null'

# The filter must block host-local services, metadata, and private addresses.
for target in \
    "http://${GATEWAY}:${HOST_PROBE_PORT}/" \
    "http://169.254.169.254/latest/meta-data/" \
    "http://10.0.0.1/"; do
    if docker exec "${C1}" sh -lc "curl -fsS --connect-timeout 2 --max-time 3 '${target}' >/dev/null"; then
        echo "A sandbox reached a blocked destination: ${target}" >&2
        exit 1
    fi
done
for target in \
    "http://[fd00:ec2::254]/latest/meta-data/" \
    "http://[fe80::a9fe:a9fe]/"; do
    if docker exec "${C1}" sh -lc "curl -g -fsS --connect-timeout 2 --max-time 3 '${target}' >/dev/null"; then
        echo "A sandbox reached an IPv6 link-local or metadata destination: ${target}" >&2
        exit 1
    fi
done
if docker exec "${C1}" sh -lc 'curl -g -fsS --connect-timeout 2 --max-time 3 "https://[2606:4700:4700::1111]/" >/dev/null'; then
    echo "A sandbox reached public IPv6 despite the IPv6 egress deny policy." >&2
    exit 1
fi
if [[ -s "${HOST_PROBE_FILE}" ]]; then
    echo "A sandbox reached the host service probe." >&2
    exit 1
fi

# Force cleanup while a child process is running; container removal must kill
# the guest process and leave the peer container intact.
docker exec -d "${C1}" sh -lc 'setsid sh -c "sleep 600" >/tmp/child.log 2>&1'
docker rm -f "${C1}" >/dev/null
if docker inspect "${C1}" >/dev/null 2>&1; then
    echo "Cancelled sandbox container still exists." >&2
    exit 1
fi
docker inspect "${C2}" >/dev/null

echo "Shell sandbox integration checks passed."
