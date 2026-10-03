#!/usr/bin/env bash
# Validate live host enforcement. Never restart Docker or touch existing sites.
set -euo pipefail
readonly SLICE="maxwell-sites.slice"
readonly READY_DIR="/etc/maxwell-sites"
readonly READY_FILE="${READY_DIR}/resource-pool-ready"
readonly IMAGE="maxwell-site-runtime"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this site resource-pool validation on the Docker host as root." >&2
    exit 1
fi
install -d -m 0755 "${READY_DIR}"
rm -f "${READY_FILE}"
if [[ "$(uname -s)" != "Linux" || ! -f /sys/fs/cgroup/cgroup.controllers ]]; then
    echo "Site backends require Linux with cgroup v2." >&2
    exit 1
fi
if [[ "$(docker info --format '{{.OSType}}')" != "linux" ||
      "$(docker info --format '{{.CgroupDriver}}')" != "systemd" ||
      "$(docker info --format '{{.CgroupVersion}}')" != "2" ]]; then
    echo "Docker must use Linux, cgroup v2 and the systemd cgroup driver. This setup does not restart/reconfigure Docker." >&2
    exit 1
fi
if ! docker info --format '{{json .Runtimes}}' | python3 -c 'import json,sys; sys.exit(0 if "runsc" in json.load(sys.stdin) else 1)'; then
    echo "gVisor runsc must already be installed and registered with Docker; setup does not restart Docker." >&2
    exit 1
fi
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "Build the fixed trusted runtime first: docker build -t ${IMAGE} docker/site-runtime" >&2
    exit 1
fi
systemctl start "${SLICE}"
control_group="$(systemctl show --property=ControlGroup --value "${SLICE}")"
if [[ "${control_group}" != "/maxwell.slice/maxwell-sites.slice" ]]; then
    echo "Unexpected site slice cgroup: ${control_group}." >&2
    exit 1
fi
cgroup="/sys/fs/cgroup${control_group}"
for property in memory.max memory.swap.max cpu.max pids.max; do
    if [[ ! -r "${cgroup}/${property}" ]]; then
        echo "Site slice is missing ${property}; refusing to mark it ready." >&2
        exit 1
    fi
done
read -r memory_max <"${cgroup}/memory.max"
read -r memory_swap_max <"${cgroup}/memory.swap.max"
read -r cpu_quota cpu_period <"${cgroup}/cpu.max"
read -r tasks_max <"${cgroup}/pids.max"
if [[ ! "${memory_max}" =~ ^[0-9]+$ || ! "${memory_swap_max}" =~ ^[0-9]+$ ||
      ! "${cpu_quota}" =~ ^[0-9]+$ || ! "${cpu_period}" =~ ^[0-9]+$ ||
      ! "${tasks_max}" =~ ^[0-9]+$ ]] ||
   (( memory_max <= 0 || memory_max > 4294967296 || memory_swap_max != 0 ||
      cpu_quota <= 0 || cpu_period <= 0 || cpu_quota > cpu_period * 2 ||
      tasks_max <= 0 || tasks_max > 2048 )); then
    echo "Site pool must enforce at most 4GiB RAM, 2 CPUs, zero swap and 2048 tasks." >&2
    exit 1
fi
# Prove runsc launches under the actual slice, rather than trusting registration.
probe=""
cleanup() {
    if [[ -n "${probe}" ]]; then
        docker rm -f "${probe}" >/dev/null || { echo "Could not remove site readiness probe ${probe}." >&2; return 1; }
    fi
}
trap cleanup EXIT
probe="$(docker run -d --runtime runsc --cgroup-parent "${SLICE}" \
    --label maxwell.sites.readiness-probe=true \
    --memory 256m --memory-swap 256m --cpus 0.5 --pids-limit 128 \
    --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 \
    --read-only --network none --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
    --log-driver json-file --log-opt max-size=2m --log-opt max-file=2 \
    --entrypoint python "${IMAGE}" -c 'import os,time; assert os.getuid()==10001; time.sleep(30)')"
pid="$(docker inspect --type container -f '{{.State.Pid}}' "${probe}")"
if [[ ! "${pid}" =~ ^[1-9][0-9]*$ ]] ||
   ! python3 - "${pid}" "${control_group}" <<'PY'
import sys
from pathlib import Path
text = Path(f"/proc/{sys.argv[1]}/cgroup").read_text()
expected = "0::" + sys.argv[2] + "/"
if not any(line.startswith(expected) for line in text.splitlines()):
    raise SystemExit("runsc probe is not actually in the aggregate site slice")
PY
then
    echo "Site runtime launch did not enter its enforced cgroup." >&2
    exit 1
fi
cleanup
probe=""
trap - EXIT
tmp="$(mktemp "${READY_DIR}/.resource-pool-ready.XXXXXX")"
printf '%s\n' 'maxwell-sites-resource-pool-v1' >"${tmp}"
chmod 0444 "${tmp}"
mv -f "${tmp}" "${READY_FILE}"
echo "Site pool ready: ${memory_max} bytes RAM, CPU ${cpu_quota}/${cpu_period}, no swap, ${tasks_max} tasks."
