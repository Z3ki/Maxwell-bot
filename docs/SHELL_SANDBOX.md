# Public shell sandbox

Maxwell exposes arbitrary Linux commands only when the required isolated
backend is ready. Root is available inside each user's guest so package
installation and ordinary development tools work. Root inside the guest does
not carry host capabilities or Maxwell, Discord, provider-key, or application
owner permissions.

## Backend choice for the current VPS deployment

The deployment described for this bot is a Contabo Cloud VPS. Contabo's current
product documentation says nested virtualization is not supported on Cloud
VPS. Firecracker creates microVMs through KVM, so it cannot be used on that VPS
without moving the workload to a provider plan that exposes nested KVM. gVisor's
`runsc` runtime is the practical supported choice: its `systrap` platform is
intended for running inside VMs or systems without usable virtualization
extensions. Configure the Docker runtime explicitly with `--platform=systrap`.

This is a userspace application kernel, not a hardware virtual machine. It
reduces direct host-kernel exposure but still depends on the host kernel, Docker,
gVisor, and host firewall. Ordinary Docker containers share the host kernel and
are not accepted as a fallback. If the deployment moves to a host with KVM
available, evaluate a microVM backend such as Firecracker before enabling shell
there.

References: [Contabo nested virtualization support](https://help.contabo.com/en/support/solutions/articles/103000271595-can-i-setup-nested-virtualization-on-my-server-),
[gVisor platforms](https://gvisor.dev/docs/architecture_guide/platforms/),
[gVisor networking](https://gvisor.dev/docs/user_guide/networking/),
[gVisor Docker install](https://gvisor.dev/docs/user_guide/install/), and
[Firecracker](https://firecracker-microvm.github.io/).

## Host setup

On the Linux Docker host (not inside the Maxwell container):

```bash
sudo bash scripts/setup_shell_host.sh
```

`install.sh --with-shell` runs that same script after the container starts.
It installs gVisor `runsc`, registers it without changing Docker's default
runtime, installs the cgroup and egress units, and—only when the current
storage driver rejects `--storage-opt size`—moves `overlay2` onto an XFS
filesystem mounted with project quotas. That storage step restarts Docker.
Shell stays disabled until both readiness markers exist. Do not create the
marker files by hand.

The steps below are what the script performs. Use them when you need to
provision a host without the script.

1. Install gVisor using its official installation guide. Merge this runtime
   into `/etc/docker/daemon.json`, preserving any existing configuration:

   ```json
   {
     "runtimes": {
       "runsc": {
         "path": "/usr/local/bin/runsc",
         "runtimeArgs": ["--platform=systrap", "--network=sandbox"]
       }
     }
   }
   ```

   Restart Docker and confirm `docker info --format '{{json .Runtimes}}'`
   contains `runsc` with the `systrap` platform and isolated sandbox network
   stack. Host network passthrough is rejected because it weakens isolation.
   Docker live-restore must remain disabled so a daemon restart stops all shell
   guests; both host setup and application startup check this setting.

2. Install the aggregate systemd cgroup. It bounds the whole shell pool to half
   of host RAM, two CPU cores, no swap, and 1,024 tasks. Maxwell requires Docker's
   systemd cgroup driver and fails closed without the readiness marker:

   ```bash
   sudo install -m 0644 scripts/maxwell-shell.slice \
     /etc/systemd/system/maxwell-shell.slice
   sudo install -m 0755 scripts/setup_shell_resource_pool.sh \
     /usr/local/sbin/maxwell-shell-resource-pool
   sudo install -m 0644 scripts/maxwell-shell-resource-pool.service \
     /etc/systemd/system/maxwell-shell-resource-pool.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now maxwell-shell-resource-pool.service
   ```

   The resource setup requires at least 30 GB free on Docker's data filesystem.
   This reserves room for four bounded writable layers and headroom for image
   and Docker metadata growth. Confirm Docker's storage driver supports the
   per-container writable-layer limit (`--storage-opt size=6G`); Maxwell does
   not start a shell if Docker rejects the limit. The workspace is a 512 MiB
   container-local tmpfs; `/tmp` is 256 MiB. Each guest memory limit is 2 GiB.

3. Install the host egress policy. The script creates an isolated IPv4 bridge
   (`172.30.240.0/20`, ICC disabled) and filters egress in `DOCKER-USER`, outside
   the guest. It blocks host-local services, the sandbox subnet, private and
   reserved IPv4, and the metadata link-local range; the host `INPUT` rule blocks
   bridge access to host services. IPv6 is disabled for this network and dropped
   by the host filter when Docker's IPv6 chains are available. Public IPv4
   egress remains available for package repositories and developer tools.

   Add provider/VPC-specific internal or management CIDRs to
   `/etc/maxwell-shell/blocked-cidrs` before running the setup script. If that
   network has globally routed internal addresses, add those too. The policy
   cannot infer private address ranges that only the operator knows.

   ```bash
   sudo install -m 0755 scripts/setup_shell_egress_firewall.sh \
     /usr/local/sbin/maxwell-shell-egress
   sudo install -m 0644 scripts/maxwell-shell-egress.service \
     /etc/systemd/system/maxwell-shell-egress.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now maxwell-shell-egress.service
   ```

   The readiness marker is stored in `/etc/maxwell-shell/egress-ready` and is
   mounted read-only into Maxwell. The systemd unit removes it when Docker stops
   so the application fails closed during a daemon restart. Do not copy or set
   the marker manually.

4. Configure the shown `MAXWELL_SHELL_*` values in `.env` and restart Maxwell.
   On platforms without runsc and the host policy, shell reports that isolation
   is unavailable and never launches `runc` or a host process.

## Runtime boundaries and limits

- A sandbox name comes from the authenticated user and guild/private scope;
  model arguments cannot choose a container, workspace, host path, or other
  tenant. Different users in one guild receive different containers. A user's
  private and guild workspaces are separate.
- The guest gets root UID with all capabilities dropped followed by a small
  allow-list of in-guest capabilities needed for package installation,
  ownership changes, and development workflows. It has no host capabilities,
  `no-new-privileges`, Docker's default seccomp filter, gVisor syscall
  mediation, a dedicated bridge, no
  published ports, no host namespaces, no bind mounts, and no Docker socket.
- Each guest is limited to 2 GiB RAM, 1 CPU, 256 processes, 6 GiB writable
  container layer (where supported by the storage driver), 512 MiB workspace,
  256 MiB `/tmp`, 1,024 open files, 15 minutes per command, and bounded output.
  The host cgroup limits all shell guests together to 50% of host RAM, two CPU
  cores, no swap, and 1,024 tasks. A user may have one running command and two
  cached workspaces; the host has at most four cached guests, bounding shell
  writable layers to 24 GiB. The Maxwell process admits at most four shell
  commands at once (hard ceiling eight).
- Workspace and packages persist between calls in the same active container.
  Timeout, cancellation, idle expiry, and Maxwell restart destroy that user's
  container, which kills all its descendants. The workspace and packages are
  ephemeral and are lost on that reset. Other users' containers are untouched.
- Shell-generated files can be attached only from that user's active
  `/workspace`, with traversal rejected and a bounded size. Shell exports are
  not copied into shared static hosting or a shared exports directory.
- The Maxwell controller still has access to the Docker daemon and must be
  treated as host-root trusted. Untrusted shell code never receives that socket.
  gVisor and the external firewall reduce exposure; neither is an absolute
  security boundary against a runtime, kernel, daemon, or firewall defect.

The current backend assumes one Maxwell controller process per Docker daemon.
If multiple controller replicas are introduced, replace the in-process global
admission semaphore with a host-wide admission service before enabling shell.

## Integration verification

Run the host-level integration script only on a disposable Linux test host that
has gVisor and the egress firewall configured:

```bash
bash tests/integration/test_shell_sandbox.sh
```

It launches isolated test containers and checks root-in-guest, dropped
capabilities, runtime selection, resource settings, no host mounts/socket,
tenant filesystem separation, container-to-container isolation, public egress,
host/private/metadata blocking, and cancellation cleanup. It does not target
production data or users.
