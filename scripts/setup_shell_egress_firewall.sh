#!/usr/bin/env bash
set -euo pipefail

# Host-side policy for public shell sandboxes. Run as root after Docker is up.
# This is deliberately outside the sandbox/container and is required before
# Maxwell will start any user shell. Docker's bridge by itself is not an egress
# security policy.

readonly NETWORK="maxwell-shell-egress"
readonly SUBNET="172.30.240.0/20"
readonly BRIDGE="br-maxwell-shell"
readonly READY_DIR="/etc/maxwell-shell"
readonly READY_FILE="${READY_DIR}/egress-ready"
readonly EXTRA_CIDRS="${READY_DIR}/blocked-cidrs"
readonly EGRESS_CHAIN="MAXWELL-SHELL-EGRESS"
readonly INPUT_CHAIN="MAXWELL-SHELL-HOST-IN"

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this host policy setup as root." >&2
    exit 1
fi

install -d -m 0755 "${READY_DIR}"
# An interrupted refresh must immediately make Maxwell fail closed.
rm -f "${READY_FILE}"

if ! docker info >/dev/null 2>&1; then
    echo "Docker Engine is unavailable; refusing to mark shell egress ready." >&2
    exit 1
fi
live_restore="$(docker info --format '{{.LiveRestoreEnabled}}')"
if [[ "${live_restore}" != "false" ]]; then
    echo "Docker live-restore must be disabled so daemon restarts stop shell guests." >&2
    exit 1
fi

# A policy refresh invalidates old sandboxes before touching firewall state.
# The one-controller deployment recovers user workspaces from scratch.
mapfile -t stale_ids < <(docker ps -aq --filter label=maxwell.shell.managed=true)
if ((${#stale_ids[@]})); then
    docker rm -f "${stale_ids[@]}" >/dev/null
fi

if ! docker network inspect "${NETWORK}" >/dev/null 2>&1; then
    docker network create \
        --driver bridge \
        --subnet "${SUBNET}" \
        --gateway 172.30.240.1 \
        --ipv6=false \
        --opt "com.docker.network.bridge.name=${BRIDGE}" \
        --opt com.docker.network.bridge.enable_icc=false \
        --opt com.docker.network.bridge.enable_ip_masquerade=true \
        "${NETWORK}" >/dev/null
fi

actual="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}} {{.Internal}} {{.EnableIPv6}} {{index .Options "com.docker.network.bridge.name"}} {{index .Options "com.docker.network.bridge.enable_icc"}} {{index .Options "com.docker.network.bridge.enable_ip_masquerade"}}' "${NETWORK}")"
if [[ "${actual}" != "${SUBNET} false false ${BRIDGE} false true" ]]; then
    echo "Unexpected shell network configuration: ${actual}" >&2
    exit 1
fi

if ! iptables -nL DOCKER-USER >/dev/null 2>&1; then
    echo "Docker's DOCKER-USER chain is unavailable; refusing to enable shell." >&2
    exit 1
fi
if ! iptables -nL INPUT >/dev/null 2>&1; then
    echo "Host INPUT firewall is unavailable; refusing to enable shell." >&2
    exit 1
fi

iptables -N "${EGRESS_CHAIN}" 2>/dev/null || true
iptables -F "${EGRESS_CHAIN}"
iptables -A "${EGRESS_CHAIN}" -d "${SUBNET}" -j DROP
iptables -A "${EGRESS_CHAIN}" -m addrtype --dst-type LOCAL -j DROP

for cidr in \
    0.0.0.0/8 \
    10.0.0.0/8 \
    100.64.0.0/10 \
    127.0.0.0/8 \
    169.254.0.0/16 \
    172.16.0.0/12 \
    192.0.0.0/24 \
    192.0.2.0/24 \
    192.88.99.0/24 \
    192.168.0.0/16 \
    198.18.0.0/15 \
    198.51.100.0/24 \
    203.0.113.0/24 \
    224.0.0.0/4 \
    240.0.0.0/4; do
    iptables -A "${EGRESS_CHAIN}" -d "${cidr}" -j DROP
done

# Add provider/VPC/internal management ranges here. The file is operator-owned
# and each line must be an IPv4 CIDR, optionally prefixed with '#'.
if [[ -f "${EXTRA_CIDRS}" ]]; then
    while IFS= read -r cidr || [[ -n "${cidr}" ]]; do
        cidr="${cidr%%#*}"
        cidr="$(xargs <<<"${cidr}")"
        [[ -z "${cidr}" ]] && continue
        if ! python3 -c 'import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1], strict=False); sys.exit(0 if n.version == 4 else 1)' "${cidr}"; then
            echo "Invalid IPv4 CIDR in ${EXTRA_CIDRS}: ${cidr}" >&2
            exit 1
        fi
        iptables -A "${EGRESS_CHAIN}" -d "${cidr}" -j DROP
    done < "${EXTRA_CIDRS}"
fi

# Public IPv4 is allowed after private, reserved, host-local and tenant-network
# destinations have been rejected. Return to Docker's normal conntrack/NAT path.
iptables -A "${EGRESS_CHAIN}" -j RETURN
iptables -C DOCKER-USER -i "${BRIDGE}" -s "${SUBNET}" -j "${EGRESS_CHAIN}" 2>/dev/null \
    || iptables -I DOCKER-USER 1 -i "${BRIDGE}" -s "${SUBNET}" -j "${EGRESS_CHAIN}"

iptables -N "${INPUT_CHAIN}" 2>/dev/null || true
iptables -F "${INPUT_CHAIN}"
iptables -A "${INPUT_CHAIN}" -j DROP
iptables -C INPUT -i "${BRIDGE}" -s "${SUBNET}" -j "${INPUT_CHAIN}" 2>/dev/null \
    || iptables -I INPUT 1 -i "${BRIDGE}" -s "${SUBNET}" -j "${INPUT_CHAIN}"

# The shell network is created with IPv6 disabled. Require a host-level IPv6
# filter anyway, so a daemon/bridge configuration mistake cannot expose IPv6
# routes. Drop every IPv6 packet entering FORWARD or INPUT from this bridge.
if ! command -v ip6tables >/dev/null 2>&1 || \
   ! ip6tables -nL FORWARD >/dev/null 2>&1 || \
   ! ip6tables -nL INPUT >/dev/null 2>&1; then
    echo "IPv6 host firewall is unavailable; refusing to enable shell egress." >&2
    exit 1
fi
ip6tables -N MAXWELL-SHELL-FORWARD6 2>/dev/null || true
ip6tables -F MAXWELL-SHELL-FORWARD6
ip6tables -A MAXWELL-SHELL-FORWARD6 -j DROP
ip6tables -C FORWARD -i "${BRIDGE}" -j MAXWELL-SHELL-FORWARD6 2>/dev/null \
    || ip6tables -I FORWARD 1 -i "${BRIDGE}" -j MAXWELL-SHELL-FORWARD6
ip6tables -N MAXWELL-SHELL-HOST-IN6 2>/dev/null || true
ip6tables -F MAXWELL-SHELL-HOST-IN6
ip6tables -A MAXWELL-SHELL-HOST-IN6 -j DROP
ip6tables -C INPUT -i "${BRIDGE}" -j MAXWELL-SHELL-HOST-IN6 2>/dev/null \
    || ip6tables -I INPUT 1 -i "${BRIDGE}" -j MAXWELL-SHELL-HOST-IN6

# Compose may have created an empty directory at the bind-mounted marker path
# before this host policy was provisioned. Remove only that empty directory.
if [[ -d "${READY_FILE}" ]]; then
    rmdir "${READY_FILE}"
fi
tmp="$(mktemp "${READY_DIR}/.egress-ready.XXXXXX")"
printf '%s\n' 'maxwell-shell-egress-v2' >"${tmp}"
chmod 0444 "${tmp}"
mv -f "${tmp}" "${READY_FILE}"
echo "Shell egress policy installed for ${NETWORK} (${SUBNET}); public IPv4 only."
