#!/usr/bin/env bash
# startup-script.sh - GCE VM startup script for atomic-agents-stack
#
# NOT YET DOGFOODED against the live GCP platform.
# Claims verified against GCP/GCE documentation as of 2026-06-09.
# See extras/gcp/README.md §External-claim verification status.
#
# Use with:
#   gcloud compute instances create <vm-name> \
#     --metadata-from-file startup-script=startup-script.sh
#
# Or update an existing instance:
#   gcloud compute instances add-metadata <vm-name> \
#     --metadata-from-file startup-script=startup-script.sh
#
# This script runs on every VM boot. It is IDEMPOTENT:
# - mkfs is guarded by a filesystem-type probe (never reformats a live disk).
# - systemd enable/start are idempotent operations.
# - The package is installed into a dedicated venv at /opt/atomic-agents; the
#   venv pip's --upgrade is idempotent (upgrades to latest, or no-ops if already
#   at the target version; pin the version for reproducibility).
#
# WHY A VENV (not a system-level pip install):
#   The selected base image --image-family=debian-12 ships Python 3.11 marked
#   PEP 668 externally-managed (/usr/lib/python3.11/EXTERNALLY-MANAGED present).
#   A system-level `pip install <pkg>` on that interpreter exits non-zero with
#   "error: externally-managed-environment", which under `set -euo pipefail`
#   would abort this script before the systemd unit is written. The base
#   debian-12 cloud image also does not ship python3-pip on PATH. So step 3
#   first `apt-get install -y python3-venv python3-pip`, then installs into a
#   dedicated venv (venv is the PEP 668-blessed bypass; sys.prefix !=
#   sys.base_prefix exempts it from the marker check). The systemd ExecStart
#   points at the venv's console script, NOT /usr/local/bin.
#   Verified: PEP 668 externally-managed semantics + venv exemption at
#   https://peps.python.org/pep-0668/ ; reasoned against debian-12 but NOT yet
#   executed live (see NOT YET DOGFOODED header).
#
# PREREQUISITES (set via instance metadata or inline before running):
#   DISK_DEVICE  - block device path of the attached data disk. Default is the
#                  stable by-id symlink /dev/disk/by-id/google-agent-vault, which
#                  GCE creates from Step 2's --disk device-name=agent-vault. This
#                  is interface-independent: do NOT hardcode /dev/sdb, because on
#                  N2/modern machine types the disk is an NVMe device
#                  (/dev/nvme0n2), and raw /dev/sdX|/dev/nvmeXnY names are not
#                  reboot-stable. Verified: NVMe naming + by-id symlink guidance
#                  at https://cloud.google.com/compute/docs/disks/format-mount-disk-linux
#   MOUNT_POINT  - where the disk mounts, e.g. /app/agents
#   AGENT_NAME   - folder name under MOUNT_POINT, e.g. my-agent
#   PROJECT_ID   - GCP project ID (for Secret Manager / other GCP APIs)
#
# The operator must create and attach the disk BEFORE running this script
# (see extras/gcp/compute-vm/README.md §Create and attach the persistent disk).
#
# MOUNT LAYOUT INVARIANT (from CRITICAL LAYOUT RULE in the Cloud Run version):
#   The persistent disk MUST be mounted at or above ATOMIC_AGENTS_ROOT, and the
#   whole agent vault MUST live on ONE filesystem. Do not split agent state
#   across multiple mount points. Two reasons:
#     1. LAYOUT RULE: a mount AT the agent-folder root shadows the baked-in
#        config (model.md, persona/, tools.md, goal.md, skills/) so the agent
#        looks empty on first start and healthz Check 3 fails. Mount at or above
#        ATOMIC_AGENTS_ROOT (the parent), never at the agent folder itself.
#     2. ONE-FILESYSTEM ASSUMPTION: the framework assumes a single filesystem
#        per vault. atomic_write keeps each temp file BESIDE its target
#        (tempfile.mkstemp(dir=target.parent) + os.replace(); atomic_agents/
#        _io.py ~line 50), so an individual write never crosses devices on its
#        own. But splitting the vault across mounts is unsupported and untested,
#        and any future cross-surface move would risk EXDEV. Keep one filesystem.
#
# ONE-INSTANCE-PER-TENANT INVARIANT:
#   Never run two VMs against the same agent vault root. The default
#   FilesystemLockBackend uses fcntl.flock (single-host advisory locking;
#   atomic_agents/locks/filesystem.py). flock coordinates between processes on
#   the SAME host, but it does NOT coordinate across hosts: two VMs cannot see
#   each other's flock on a shared root, so both could write the same agent and
#   race on atomic_write targets. (A ReadWriteOnce persistent disk also cannot be
#   RW-mounted on two VMs at once, which already blocks shared-root multi-VM.)
#   This is the same single-writer requirement as Cloud Run containerConcurrency=1,
#   but at the VM level. For genuine multi-host coordination use the Redis
#   LockBackend. If you need multi-tenant isolation, use one VM (or one
#   persistent disk) per agent vault.
#
# DO NOT UNMOUNT THE DISK WHILE serve IS RUNNING:
#   If the disk is unmounted while atomic-agents serve is running, atomic_write
#   calls silently write to the rootfs overlay (no error). When the disk is
#   remounted, vault state on disk and in the overlay will be inconsistent.
#   Always: systemctl stop atomic-agents-serve && <disk maintenance> && systemctl start atomic-agents-serve.
#
# CRASH RECOVERY NOTE:
#   If the serve process is killed mid-write (e.g., SIGKILL, OOM kill),
#   atomic_write leaves a .tmp file in the same directory as the target. These
#   orphaned .tmp files are NOT cleaned up automatically at boot. Verified:
#   cleanup_stale_tempfiles / cleanup_stale_tempfiles_for_file
#   (atomic_agents/_io.py) is invoked ONLY inside the MCP-registry
#   install/uninstall path (atomic_agents/mcp_registry/filesystem.py); no boot
#   path, no serve startup, and no AtomicAgent.__init__ calls it. The orphaned
#   .tmp files are HARMLESS - the real target file is either fully written or
#   absent, and .tmp files are ignored on read - but they accumulate on the disk
#   until an operator removes them. To clear them manually, stop the service
#   first to avoid racing a live write:
#     systemctl stop atomic-agents-serve
#     find "$AGENT_ROOT" -name '.*.tmp' -delete
#     systemctl start atomic-agents-serve
#   Cascade work-claims are SEPARATE: they persist until recover_stale_claims()
#   is invoked (by a cascade-processing run or an operator), which is NOT
#   boot-triggered (#386).

set -euo pipefail

DISK_DEVICE="${DISK_DEVICE:-/dev/disk/by-id/google-agent-vault}"
MOUNT_POINT="${MOUNT_POINT:-/app/agents}"
AGENT_NAME="${AGENT_NAME:-my-agent}"
ATOMIC_AGENTS_VERSION="${ATOMIC_AGENTS_VERSION:-atomic-agents-stack[serve,redis,vertex]}"

# Install location (single source of truth shared by step 3 install and the
# systemd ExecStart in step 5; the two MUST agree or the unit fails 203/EXEC).
VENV_DIR="${VENV_DIR:-/opt/atomic-agents}"
INSTALL_BIN="${VENV_DIR}/bin/atomic-agents"

# Dedicated service account for the unauthenticated serve workload. Mirrors the
# Dockerfile's non-root hardening (UID 10001): an isolated, no-login, system
# account so a tricked-write or RCE in the serve layer / agent tools / MCP
# subprocesses is confined to this account's files, NOT the shared "nobody"
# privilege domain every other system daemon runs in.
SERVE_USER="${SERVE_USER:-atomic-agents}"
SERVE_UID="${SERVE_UID:-10001}"

LOG_TAG="atomic-agents-startup"
log() { echo "[$LOG_TAG] $*"; logger -t "$LOG_TAG" "$*" || true; }

# ── 1. Detect and conditionally format the persistent disk ───────────────────
# CRITICAL: Only format when no recognized filesystem is present. Reformatting
# destroys all agent state. The blkid guard is the idempotency gate.
#
# Verified: blkid -s TYPE outputs TYPE="ext4" (or similar) if a filesystem
# exists. The grep -q exit code is 0 if found, 1 if not found.
# Source: blkid(8) man page; GCE disk attach docs at
# https://cloud.google.com/compute/docs/disks/format-mount-disk-linux
log "Checking filesystem on $DISK_DEVICE..."
if blkid -s TYPE "$DISK_DEVICE" 2>/dev/null | grep -qE 'TYPE="(ext4|xfs)"'; then
    log "Filesystem already present on $DISK_DEVICE - skipping mkfs."
else
    log "No recognized filesystem on $DISK_DEVICE - formatting ext4."
    # -F: non-interactive (do not prompt); -L: label for /etc/fstab
    # Verified: mkfs.ext4 -F -L flag usage at
    # https://cloud.google.com/compute/docs/disks/format-mount-disk-linux
    mkfs.ext4 -F -L agent-vault "$DISK_DEVICE"
    log "Formatted $DISK_DEVICE as ext4."
fi

# ── 2. Create mount point and mount the disk ─────────────────────────────────
log "Mounting $DISK_DEVICE at $MOUNT_POINT..."
mkdir -p "$MOUNT_POINT"

# Add /etc/fstab entry if not already present, using the disk label.
# nofail: do not prevent boot if disk is absent (safety net; do not rely on it
# to run serve without the disk - see DO NOT UNMOUNT note above).
# Verified: /etc/fstab nofail option at
# https://cloud.google.com/compute/docs/disks/format-mount-disk-linux#fstab
if ! grep -qF "agent-vault" /etc/fstab; then
    log "Adding /etc/fstab entry for agent-vault..."
    echo "LABEL=agent-vault  $MOUNT_POINT  ext4  defaults,nofail  0  2" >> /etc/fstab
fi

# Mount all filesystems from fstab (idempotent - already-mounted entries are
# skipped). Prefer mount -a over direct mount so fstab is the single source.
mount -a
log "Mount complete. Disk mounted at $MOUNT_POINT."

# ── 3. Install atomic-agents-stack into a dedicated venv ─────────────────────
# debian-12 ships PEP 668 externally-managed Python 3.11 and the base cloud
# image has no python3-pip on PATH. Install the apt prerequisites, then install
# the package into a venv (the PEP 668-blessed bypass). See WHY A VENV header.
# Verified: PEP 668 venv exemption at https://peps.python.org/pep-0668/
log "Installing apt prerequisites (python3-venv, python3-pip)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip

log "Creating venv at $VENV_DIR and installing $ATOMIC_AGENTS_VERSION..."
# Create the venv if absent (idempotent on reboot); upgrade pip inside it.
if [ ! -x "${VENV_DIR}/bin/pip" ]; then
    python3 -m venv "$VENV_DIR"
fi
"${VENV_DIR}/bin/pip" install --upgrade --no-cache-dir pip
# Pin version in production: replace the package spec with a pinned version,
# e.g. "atomic-agents-stack[serve,redis,vertex]==1.0.0" for reproducible builds.
# venv pip's --upgrade is idempotent.
# Verified: pip install --upgrade semantics at https://pip.pypa.io/en/stable/
"${VENV_DIR}/bin/pip" install --upgrade --no-cache-dir "$ATOMIC_AGENTS_VERSION"
log "Installation complete. Console script at $INSTALL_BIN."

# ── 4. Ensure agent vault directory exists ───────────────────────────────────
# The agent folder must exist before serve starts. Config files (persona/,
# model.md, tools.md, goal.md, skills/) should be deployed here via your
# provisioning pipeline (gcloud compute scp, Cloud Build, etc.) before running
# serve. This script creates the directory skeleton only.
AGENT_ROOT="$MOUNT_POINT/$AGENT_NAME"
mkdir -p "$AGENT_ROOT"/{memory,log,journal,goals,outcomes}
# Create a dedicated, isolated, no-login system account for the serve workload
# rather than reusing the shared "nobody" catch-all. This mirrors the
# Dockerfile's UID 10001 hardening: the serve endpoint is an unauthenticated,
# high-blast-radius RCE surface (agent tools, MCP subprocesses), so it gets its
# OWN privilege domain - a tricked-write file lands owned by atomic-agents, not
# writable by every other nobody-running daemon on the host.
#   -r: system account   -s /bin/false: no interactive shell
#   useradd is idempotent-guarded so reboots do not error.
if ! id -u "$SERVE_USER" >/dev/null 2>&1; then
    log "Creating dedicated service account $SERVE_USER (uid $SERVE_UID)..."
    useradd -u "$SERVE_UID" -r -s /bin/false "$SERVE_USER"
fi
# Resolve the account's primary group dynamically for the systemd Group= below.
SERVE_GROUP="$(id -gn "$SERVE_USER")"
# Do NOT swallow a chown failure: if $AGENT_ROOT stays root-owned, serve (running
# as $SERVE_USER) cannot write its state and would crash. Surface it loudly.
chown -R "${SERVE_USER}:${SERVE_GROUP}" "$AGENT_ROOT"
log "Agent vault directory skeleton created at $AGENT_ROOT (owned by ${SERVE_USER}:${SERVE_GROUP})."

# ── 5. Write systemd unit for atomic-agents serve ────────────────────────────
# After= local-fs.target: waits for all local filesystems (including the
# persistent disk) to be mounted before starting the service.
# RequiresMountsFor=: explicit dependency on the disk mount path, preventing
# the race between systemd service start and disk mount.
# Verified: systemd After= and RequiresMountsFor= semantics at
# https://www.freedesktop.org/software/systemd/man/systemd.unit.html
log "Writing systemd unit file..."
cat > /etc/systemd/system/atomic-agents-serve.service << EOF
[Unit]
Description=atomic-agents serve - agent HTTP service
After=network.target local-fs.target
RequiresMountsFor=$MOUNT_POINT
# Do not start if the disk is not mounted (prevents silent writes to rootfs).
ConditionPathIsMountPoint=$MOUNT_POINT

[Service]
Type=simple
# Dedicated, isolated service account (created above), NOT the shared "nobody".
# Group resolved from that account so it matches the chown'd ownership of
# $AGENT_ROOT and the service can write its state.
User=$SERVE_USER
Group=$SERVE_GROUP
WorkingDirectory=$AGENT_ROOT
Environment=ATOMIC_AGENTS_ROOT=$MOUNT_POINT
# Lock backend: set ATOMIC_AGENTS_LOCK_BACKEND=redis and provide the URL
# to use Redis for distributed locking (recommended for durability).
# Default (unset) uses FilesystemLockBackend.
# Environment=ATOMIC_AGENTS_LOCK_BACKEND=redis
# Environment=ATOMIC_AGENTS_LOCK_BACKEND_URL=redis://...
#
# SECURITY - UNAUTHENTICATED ENDPOINT:
#   --host 0.0.0.0 --allow-no-auth binds the /call endpoint on ALL interfaces
#   with NO authentication. Anything that can route to this VM's port 8080 can
#   trigger agent.call() and incur unbounded LLM spend. This is ONLY safe behind
#   an enforced perimeter:
#     - the VM has NO external IP (create with --no-address; see README Step 2), AND
#     - the IAP-TCP firewall rule scoped to 35.235.240.0/20 is in place
#       (README Step 6) BEFORE the service first listens, OR
#     - the interface is otherwise VPC-internal-only (LB + IAP).
#   The framework default port is 8000; Cloud Run uses 8080 - this VM reference
#   pins 8080 to match. To bind loopback-only instead, change --host to 127.0.0.1.
#
#   AGGREGATE SPEND: --allow-no-auth bounds nothing at the project level. The
#   framework's _check_cost_guardrails (model.md) caps PER-CALL spend, but
#   AGGREGATE agent.call() spend across all requests is bounded ONLY by a GCP
#   billing budget alert. Unlike the Cloud Run path, the IAP-TCP-tunnel access
#   path here has NO Cloud Armor rate-limiting layer, so the billing budget is
#   the SOLE aggregate ceiling. Set one up before exposing the endpoint - see
#   extras/gcp/README.md §"Cost controls and billing budget".
#
# ExecStart points at the venv console script (INSTALL_BIN), NOT /usr/local/bin:
# the package is installed into $VENV_DIR (step 3), so /usr/local/bin/atomic-agents
# does not exist and would fail 203/EXEC. INSTALL_BIN is the single source of
# truth shared with the install step.
ExecStart=$INSTALL_BIN serve $AGENT_NAME \
  --host 0.0.0.0 \
  --port 8080 \
  --allow-no-auth
# Restart on failure, but not on clean stop (systemctl stop).
# Verified: systemd Restart=on-failure semantics at
# https://www.freedesktop.org/software/systemd/man/systemd.service.html
Restart=on-failure
RestartSec=5s
# Allow 30s for graceful shutdown (set to >= 2x max agent.call() runtime).
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
log "Systemd unit written."

# ── 6. Enable and start the service ──────────────────────────────────────────
systemctl daemon-reload
systemctl enable atomic-agents-serve
systemctl start atomic-agents-serve
log "atomic-agents-serve enabled and started."
log "Startup script complete. Check service status with: systemctl status atomic-agents-serve"
