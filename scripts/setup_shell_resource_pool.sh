#!/usr/bin/env bash
set -euo pipefail

readonly SLICE="maxwell-shell.slice"
readonly CONFIG="/etc/systemd/system/${SLICE}"
readonly READY_DIR="/etc/maxwell-shell"
readonly READY_FILE="${READY_DIR}/resource-pool-ready"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this host resource-pool setup as root." >&2
    exit 1
fi

install -d -m 0755 "${READY_DIR}"
rm -f "${READY_FILE}"
if [[ ! -f "${CONFIG}" ]]; then
    echo "${CONFIG} is missing; install scripts/maxwell-shell.slice first." >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "Docker Engine is unavailable; refusing to mark the resource pool ready." >&2
    exit 1
fi
driver="$(docker info --format '{{.CgroupDriver}}')"
if [[ "${driver}" != "systemd" ]]; then
    echo "Docker must use the systemd cgroup driver; found ${driver}." >&2
    exit 1
fi
live_restore="$(docker info --format '{{.LiveRestoreEnabled}}')"
if [[ "${live_restore}" != "false" ]]; then
    echo "Docker live-restore must be disabled so daemon restarts stop shell guests." >&2
    exit 1
fi
docker_root="$(docker info --format '{{.DockerRootDir}}')"
available_kb="$(df -Pk "${docker_root}" | awk 'NR == 2 {print $4}')"
if [[ ! "${available_kb}" =~ ^[0-9]+$ ]] || ((available_kb < 30000000)); then
    echo "Docker needs at least 30 GB free for the bounded shell container layers." >&2
    exit 1
fi

systemctl daemon-reload
systemctl start "${SLICE}"
control_group="$(systemctl show --property=ControlGroup --value "${SLICE}")"
if [[ ! "${control_group}" =~ ^/[A-Za-z0-9_.@/-]+$ || "${control_group}" == *..* ]]; then
    echo "Could not resolve the shell slice's cgroup v2 path." >&2
    exit 1
fi
cgroup="/sys/fs/cgroup${control_group}"
for property in memory.max memory.swap.max cpu.max pids.max; do
    if [[ ! -r "${cgroup}/${property}" ]]; then
        echo "The shell slice is missing cgroup v2 ${property}; refusing shell startup." >&2
        exit 1
    fi
done

read -r memory_max <"${cgroup}/memory.max"
read -r memory_swap_max <"${cgroup}/memory.swap.max"
read -r cpu_quota cpu_period <"${cgroup}/cpu.max"
read -r tasks_max <"${cgroup}/pids.max"
host_memory_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
if [[ ! "${memory_max}" =~ ^[0-9]+$ ||
      ! "${memory_swap_max}" =~ ^[0-9]+$ ||
      ! "${cpu_quota}" =~ ^[0-9]+$ ||
      ! "${cpu_period}" =~ ^[0-9]+$ ||
      ! "${tasks_max}" =~ ^[0-9]+$ ||
      ! "${host_memory_kb}" =~ ^[0-9]+$ ]]; then
    echo "The shell slice has an unbounded or malformed cgroup v2 limit." >&2
    exit 1
fi
host_memory_bytes=$((host_memory_kb * 1024))
if ((memory_max <= 0 || memory_max > host_memory_bytes / 2 ||
     memory_swap_max != 0 || cpu_quota <= 0 || cpu_period <= 0 ||
     cpu_quota > cpu_period * 2 || tasks_max <= 0 || tasks_max > 1024)); then
    echo "The shell slice exceeds its required aggregate limits (50% RAM, 2 CPUs, no swap, 1,024 tasks)." >&2
    exit 1
fi

tmp="$(mktemp "${READY_DIR}/.resource-pool-ready.XXXXXX")"
printf '%s\n' 'maxwell-shell-resource-pool-v2' >"${tmp}"
chmod 0444 "${tmp}"
mv -f "${tmp}" "${READY_FILE}"
echo "Shell resource pool ready: MemoryMax=${memory_max}, CPUQuota=${cpu_quota}/${cpu_period}, MemorySwapMax=${memory_swap_max}, TasksMax=${tasks_max}."
