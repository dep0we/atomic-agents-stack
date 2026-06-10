# GCP Compute Engine VM - atomic-agents-stack v0 stateful reference

NOT YET DOGFOODED against the live GCP platform.
Claims verified against GCP/GCE documentation as of 2026-06-09.
See `extras/gcp/README.md §External-claim verification status`.

This directory is the **v0 stateful-today** deployment reference for
atomic-agents-stack on GCP. Use this when you need durable filesystem state
(memory, goals, outcomes, journal, cascade queue) that survives container
restarts.

The sister artifact, `extras/gcp/cloudrun-service.yaml`, is the **v0
stateless** Cloud Run reference. Cloud Run does not support GCE persistent
disk volumes (the original defect in issue #395). The stateless Cloud Run
shape is the target topology once all mutable state moves to managed backends
(Phases 2-3 of the scale-out path). The VM is the stateful-today bridge.

---

## Why a VM, not Cloud Run, for stateful v0

Cloud Run v2 does not support GCE block persistent disk volumes. The only
supported Cloud Run volume types are: `secret`, `cloudSqlInstance`, `emptyDir`,
`nfs`, and `gcs` (verified: Cloud Run v2 Volume schema at
https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.services).

More importantly, even NFS and GCS FUSE (which Cloud Run does support) are
incompatible with the framework's `atomic_write` primitive. `atomic_write`
uses `tempfile.mkstemp(dir=target.parent)` + `os.replace()` (POSIX `rename(2)`),
which requires the temp file and target to live on the same filesystem. NFS and
GCS FUSE do not guarantee POSIX `rename(2)` atomicity. A crash mid-write on a
non-POSIX mount leaves the target file absent rather than leaving a recoverable
`.tmp` file. See TENSIONS.md T4.

A GCE VM with an ext4 or xfs persistent disk attached directly is the only
Cloud Run-adjacent topology that satisfies POSIX `rename(2)` atomicity today.

---

## Architecture

```
  operator / Cloud Scheduler
        |
        v (IAP TCP tunnel or VPC-internal HTTPS)
  GCE VM
    |- /app/agents/         <- MOUNT POINT: persistent disk (= ATOMIC_AGENTS_ROOT, ext4/xfs)
    |     |- <name>/             <- agent folder (on disk, survives reboots)
    |     |     |- persona/      <- config (baked in by operator)
    |     |     |- model.md      <- config (baked in by operator)
    |     |     |- tools.md      <- config (baked in by operator)
    |     |     |- goal.md       <- config (baked in by operator)
    |     |     |- memory/       <- state (durable on disk; #382 seam shipped, #258 Postgres adapter moves it off disk)
    |     |     |- log/          <- state (durable on disk; use PostgresLogBackend for query)
    |     |     |- journal/      <- state (durable on disk, no Protocol yet; #383)
    |     |     |- goals/        <- state (durable on disk, no Protocol yet; #383)
    |     |     |- outcomes/     <- state (durable on disk, no Protocol yet; #383)
    |- atomic-agents serve  <- systemd unit (atomic-agents-serve.service)
    |     listening :8080
    |- Memorystore/Redis    <- LockBackend (recommended; filesystem fallback ok for single-VM)
```

**One instance per tenant.** Never run two VMs against the same agent vault root.
The default FilesystemLockBackend uses `fcntl.flock` (single-host advisory
locking - see atomic_agents/locks/filesystem.py). `flock` coordinates correctly
between processes ON THE SAME HOST, but it does NOT coordinate across hosts: two
VMs cannot see each other's `flock` on a shared root, so both could write the
same agent and race on `atomic_write` targets. (A ReadWriteOnce persistent disk
also cannot be RW-mounted on two VMs at once, which already prevents shared-root
multi-VM in this topology.) For genuine multi-host coordination, use the Redis
LockBackend. See the startup-script.sh ONE-INSTANCE-PER-TENANT comment.

---

## CRITICAL LAYOUT RULE

Mount the persistent disk at `ATOMIC_AGENTS_ROOT` (the directory that holds agent
folders), never at the agent folder root itself (`/app/agents/<name>`).

The agent folder root holds config files (persona/, model.md, tools.md, goal.md,
skills/) that are baked in by the operator during provisioning. Mounting a volume
at the agent folder root would shadow those files, leaving the folder empty on
first start. `healthz` Check 3 (model.md present and parseable) would then return
503 per request - the serve process stays up (a missing model.md does not exit
the process; see Step 4), so on the VM there is no systemd crash loop, but every
`/call` against the shadowed folder fails until config is restored.

Instead, mount the disk at `/app/agents` (or wherever `ATOMIC_AGENTS_ROOT` points),
and deploy agent config into a subdirectory on the disk after the first mount:

```
/app/agents/                  <- disk mount point (= ATOMIC_AGENTS_ROOT)
  <agent-name>/               <- agent folder (on disk, survives reboots)
    persona/IDENTITY.md       <- config (deploy once after first mount)
    model.md                  <- config
    tools.md                  <- config
    goal.md                   <- config
    memory/                   <- state (written by the framework)
    log/                      <- state (written by the framework)
    journal/                  <- state (written by the framework)
```

Do NOT split the agent folder across two mount points, and never mount a volume
AT the agent folder root (that shadows the baked-in config and fails healthz
Check 3 - the LAYOUT RULE above). Keep the whole vault on ONE filesystem: the
framework assumes one filesystem per vault. Note that `atomic_write` itself does
NOT raise EXDEV here - it keeps each temp file beside its target
(`tempfile.mkstemp(dir=target.parent)` + `os.replace()`), so an individual write
never crosses devices. The reason to keep one filesystem is that a split layout
is unsupported and untested, and any future cross-surface rename would risk
`OSError: [Errno 18] Invalid cross-device link`.

---

## Prerequisites

- GCP project with billing enabled
- `gcloud` CLI authenticated: `gcloud auth login && gcloud config set project <PROJECT_ID>`
- Verified: gcloud CLI prerequisites at https://cloud.google.com/sdk/docs/install

---

## Step 1. Create a persistent disk

Create the disk before the VM so you can attach it at instance-create time.
(Verified: `gcloud compute disks create` flags at
https://cloud.google.com/sdk/gcloud/reference/compute/disks/create)

```bash
gcloud compute disks create <agent-name>-vault \
  --project=<PROJECT_ID> \
  --zone=<ZONE> \
  --type=pd-balanced \
  --size=20GB \
  --labels=agent=<agent-name>,env=production
```

`pd-balanced` (SSD-backed, balanced performance and cost) is the recommended
disk type for `atomic_write` workloads. `pd-balanced` provisions 6 write IOPS
per GiB plus a 3,000-IOPS per-instance baseline (the 3,000 figure is the
baseline floor, not a per-instance maximum - the per-instance ceiling is far
higher), which is well above the sequential-append pattern of JSONL log writes
and the atomic-replace pattern of memory writes. `pd-standard` (HDD) is a
lower-cost option if write latency is not a concern.
(Verified: disk type comparison at
https://cloud.google.com/compute/docs/disks/performance)

---

## Step 2. Create the VM and attach the disk

> **SECURITY - the serve endpoint is unauthenticated.** The systemd unit runs
> `atomic-agents serve --host 0.0.0.0 --allow-no-auth` and is enabled+started on
> first boot. Anything that can route to port 8080 can trigger `agent.call()`
> and incur LLM spend. The command below uses `--no-address` so the VM has NO
> external IP - operator access is via the IAP TCP tunnel (Step 6), which needs
> no external IP. Create the scoped firewall rule (Step 6) BEFORE or alongside
> instance creation; do NOT expose port 8080 to `0.0.0.0/0`.
>
> **AGGREGATE SPEND.** The framework bounds PER-CALL spend via cost guardrails
> (`model.md`, `_check_cost_guardrails`), but AGGREGATE `agent.call()` spend
> across all requests is bounded ONLY by a GCP billing budget alert - set one up
> before exposing the endpoint; see `extras/gcp/README.md §"Cost controls and
> billing budget"`. The IAP-TCP-tunnel access path has NO Cloud Armor
> rate-limiting layer (unlike the Cloud Run path), so on the VM the billing
> budget is the SOLE aggregate ceiling.

```bash
gcloud compute instances create <agent-name>-vm \
  --project=<PROJECT_ID> \
  --zone=<ZONE> \
  --machine-type=n2-standard-2 \
  --no-address \
  --disk=name=<agent-name>-vault,device-name=agent-vault,auto-delete=no,mode=rw \
  --boot-disk-size=20GB \
  --boot-disk-type=pd-standard \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --scopes=cloud-platform \
  --metadata-from-file startup-script=startup-script.sh \
  --tags=atomic-agents-serve
```

`--no-address`: the VM receives NO ephemeral external IP. Without this flag,
gcloud assigns a default ephemeral PUBLIC IP, and the unauthenticated serve
endpoint would be reachable from the internet for any 8080 ingress the network
permits - before Step 6's firewall scoping is applied. IAP TCP tunneling
(Step 6, Option A) reaches the VM over its internal interface and needs no
external IP. If you genuinely need an external IP, you MUST apply the Step 6
IAP-scoped firewall rule first and never open 8080 to `0.0.0.0/0`.
(Verified: `--no-address` / default external IP behavior at
https://cloud.google.com/sdk/gcloud/reference/compute/instances/create)

`--disk=auto-delete=no`: the data disk is NOT deleted when the VM is deleted.
This is the key difference from the boot disk: you can recreate the VM and
reattach the same data disk to recover all agent state.
(Verified: `--disk` flag semantics at
https://cloud.google.com/sdk/gcloud/reference/compute/instances/create)

`--machine-type=n2-standard-2`: 2 vCPU, 8 GB RAM. Sufficient for a single
agent with moderate LLM call frequency. Resize if needed:
`gcloud compute instances set-machine-type <vm-name> --machine-type=n2-standard-4`

`--scopes=cloud-platform`: grants the VM's default service account access to
GCP APIs (Secret Manager, Vertex AI, Cloud SQL). Prefer a custom service account
with minimal permissions for production:
```bash
gcloud compute instances create ... \
  --service-account=<SA>@<PROJECT>.iam.gserviceaccount.com \
  --scopes=cloud-platform
```

---

## Step 3. Configure the startup script variables

Edit `startup-script.sh` to set the correct values for your deployment before
uploading. Key variables:

| Variable | Default | Description |
|---|---|---|
| `DISK_DEVICE` | `/dev/disk/by-id/google-agent-vault` | Stable symlink to the attached data disk. The Step 2 `--disk` flag sets `device-name=agent-vault`, so GCE creates this `/dev/disk/by-id/google-<device-name>` symlink deterministically, independent of the underlying interface. Do NOT default to `/dev/sdb`: on N2 (the prescribed machine type) and other modern families, GCE attaches disks over NVMe, so the data disk surfaces as `/dev/nvme0n2`, not `/dev/sdb`. The `/dev/sdX` and `/dev/nvmeXnY` names are not stable across reboots; the by-id symlink is. |
| `MOUNT_POINT` | `/app/agents` | Where the disk mounts (= ATOMIC_AGENTS_ROOT) |
| `AGENT_NAME` | `my-agent` | Folder name under MOUNT_POINT |
| `ATOMIC_AGENTS_VERSION` | `atomic-agents-stack[serve,redis,vertex]` | Package spec; pin version for reproducibility |
| `VENV_DIR` | `/opt/atomic-agents` | Dedicated venv install location (debian-12 is PEP 668 externally-managed, so a system `pip install` is blocked; the package installs into this venv). The systemd `ExecStart` derives the console-script path (`$VENV_DIR/bin/atomic-agents`) from this same variable, so the two cannot drift |
| `SERVE_USER` / `SERVE_UID` | `atomic-agents` / `10001` | Dedicated, no-login system account the serve process runs as (mirrors the Dockerfile's UID 10001 hardening; not the shared `nobody`) |

> **Install path.** The script installs into a venv at `$VENV_DIR` because the
> selected `debian-12` image ships PEP 668 externally-managed Python 3.11 (a
> bare system `pip install` exits with `externally-managed-environment`) and the
> base cloud image has no `python3-pip` on PATH. The systemd `ExecStart` points
> at `$VENV_DIR/bin/atomic-agents`, NOT `/usr/local/bin/atomic-agents`. If you
> change the install method, keep `ExecStart` and the install step pointing at
> the same binary. (Verified: PEP 668 venv exemption at
> https://peps.python.org/pep-0668/; reasoned against debian-12 but not yet run
> live.)

The default `DISK_DEVICE` (`/dev/disk/by-id/google-agent-vault`) works as-is when
Step 2's `--disk` flag sets `device-name=agent-vault` (as shown). That symlink is
interface-independent and reboot-stable, so you normally do not need to change it.

To confirm the disk after the VM starts (and to see the underlying raw device,
which on N2/modern machine types is an NVMe path like `/dev/nvme0n2`, NOT
`/dev/sdb`):
```bash
gcloud compute ssh <vm-name> --zone=<ZONE> -- lsblk -o NAME,SIZE,TYPE,MOUNTPOINT
gcloud compute ssh <vm-name> --zone=<ZONE> -- ls -l /dev/disk/by-id/google-*
```
Match the data disk (not the 20GB boot disk) by size. Prefer the
`/dev/disk/by-id/google-*` symlink over the raw `/dev/sdX` or `/dev/nvmeXnY`
name, which is not stable across reboots.

---

## Step 4. Deploy agent config

After the first boot and disk format, deploy your agent's config files to the
disk. The startup script creates the directory skeleton; you supply the content:

```bash
# Copy your agent folder from local development
gcloud compute scp --recurse ./agents/<agent-name>/ \
  <vm-name>:/app/agents/<agent-name>/ \
  --zone=<ZONE>
```

Config files that must exist before first serve start:
- `persona/IDENTITY.md` (or equivalent persona file)
- `model.md` (required; healthz Check 3 validates this)
- `tools.md`
- `goal.md`

**Expect 503s on first boot until config is deployed.** The startup script
enables and starts the service unconditionally on first boot, but config does not
exist yet (you deploy it here, in Step 4). Until `model.md` is present and
parseable, the serve process stays UP and `healthz` Check 3 returns 503 per
request. The systemd unit does NOT restart on this: `run_server` only exits
non-zero on a missing agent folder, malformed `serve.md`, or non-loopback
without `--allow-no-auth` - a missing `model.md` does not exit the process, so
`Restart=on-failure` (which fires only on process exit) never triggers and there
is no crash loop on the VM. (Verified: `atomic_agents/serve/_server.py`
`run_server` exit conditions; the `healthz` Check 3 503 is per-request in
`atomic_agents/serve/_app.py`.) Note: a `/call` against an agent folder that
exists but is missing `model.md` may still attempt to run, so deploy config
before sending real calls. The crash-loop framing applies only to the Cloud Run
topology, where a failing startup/liveness probe causes Cloud Run to replace the
container.

**Re-apply ownership after copying config.** The startup script chowns
`$AGENT_ROOT` to the dedicated `atomic-agents` service account on first boot,
BEFORE this `scp` step, so it does not cover the files you copy here - and `scp`
lands them owned by the SSH user (root), while the service runs as
`User=atomic-agents`. The framework can read root-owned config, but cannot
rewrite/rotate any file it treats as writable, and a framework-created subdir
under a root-owned path would fail. After copying, re-chown to the same
dedicated account the script created and restart:
```bash
gcloud compute ssh <vm-name> --zone=<ZONE> -- \
  "sudo chown -R atomic-agents:atomic-agents /app/agents/<agent-name> && \
   sudo systemctl restart atomic-agents-serve"
```

---

## Step 5. Verify the service is running

```bash
# SSH to the VM
gcloud compute ssh <vm-name> --zone=<ZONE>

# Check service status
systemctl status atomic-agents-serve

# Tail service logs
journalctl -u atomic-agents-serve -f

# Confirm the healthz endpoint responds
curl http://localhost:8080/agents/<agent-name>/healthz
```

---

## Step 6. Firewall and access

> **REQUIRED PREREQUISITE, not optional.** The serve endpoint is unauthenticated
> (`--allow-no-auth`). It must NOT be reachable except through an enforced
> perimeter. The scoped firewall rule below (IAP range 35.235.240.0/20) is the
> perimeter for the IAP-TCP-tunnel access pattern. Apply it before (or as part
> of) instance creation in Step 2. Until a scoped rule is in place and any
> external IP is removed (`--no-address`), treat the service as live-but-must-be-
> unreachable. Never open port 8080 to `0.0.0.0/0`.

**Option A: IAP TCP tunneling (the access path for this reference)**

IAP TCP tunneling lets you access the VM without a public IP. It is a separate
GCP product from IAP for HTTPS (which secures end-user access to web apps).
(Verified: IAP TCP tunneling docs at
https://cloud.google.com/iap/docs/using-tcp-forwarding)

```bash
# Allow IAP TCP traffic through the firewall (35.235.240.0/20 is GCP's IAP range)
# Apply this BEFORE first boot - the service starts unauthenticated on first boot.
gcloud compute firewall-rules create allow-iap-tcp \
  --network=default \
  --allow=tcp:8080 \
  --source-ranges=35.235.240.0/20 \
  --target-tags=atomic-agents-serve \
  --description="Allow IAP TCP tunnel to atomic-agents serve"

# Open a tunnel (binds localhost:8080 to the VM's :8080)
gcloud compute start-iap-tunnel <vm-name> 8080 \
  --local-host-port=localhost:8080 \
  --zone=<ZONE>

# Hit the health endpoint through the tunnel
curl http://localhost:8080/agents/<agent-name>/healthz
```

**Option B: VPC-internal HTTPS (for production workloads behind a load balancer)**

For production, place the VM behind a load balancer with IAP enabled (same
topology as the Cloud Run reference, but targeting the VM's backend service).
See `extras/gcp/iap-setup.md` for the load-balancer + IAP setup steps; replace
the serverless NEG with an instance group NEG pointing at this VM.

---

## Disaster recovery

### Crash recovery

The persistent disk survives VM crashes and container replacements. When the
VM reboots (or `systemctl start atomic-agents-serve` is run), the startup script
mounts the disk and starts the service against the same vault root as before.

Important: `recover_stale_claims` is NOT invoked automatically at service start.
If the serve process was killed mid-cascade-operation, orphaned work-claim files
persist on the disk until a cascade-processing run calls `recover_stale_claims()`
or an operator clears them. Automatic boot-time cascade recovery is tracked in
issue #386.

The `lease_seconds` parameter (default 3600s) determines how long a claim is
considered live before recovery reclaims it.

### SIGTERM and graceful shutdown

The systemd unit's `TimeoutStopSec=30` gives the serve process 30 seconds to
finish in-flight requests before systemd escalates SIGTERM to SIGKILL. The
framework registers no signal handler directly, but `uvicorn`'s SIGTERM handling
triggers the Starlette lifespan shutdown, which calls `shutdown_executor()`
(`atomic_agents/serve/_runner.py`; the lifespan call site is `_app.py`). That
function internally calls the serve thread pool's `shutdown(wait=True)`
(`_runner.py`), so it waits for the thread pool to drain in-flight
`agent.call()` work up to the grace window. So a clean
`systemctl stop` (or SIGTERM within `TimeoutStopSec`) DOES drain in-flight calls.
What is not durable is a hard SIGKILL (OOM kill, or TimeoutStopSec exceeded): a
SIGKILL between the cascade work-claim `rename` and the sidecar write leaves a
claimed file with NO sidecar at all (the claim is taken by `src.rename(dst)`
BEFORE `_write_sidecar` runs - `atomic_agents/_cascade.py`). `recover_stale_claims`
handles exactly that case via its "legacy claim (no sidecar) - use mtime" branch,
comparing file mtime against `lease_seconds`; recovery is NOT boot-triggered
(#386). A sidecar that DOES exist always carries `lease_expires_at` +
`lease_seconds`, so the failure mode is "no sidecar," never "sidecar without
lease metadata." Set `TimeoutStopSec` to at least 2x the expected max
`agent.call()` runtime so SIGTERM drains rather than escalating to SIGKILL.

**Before any disk maintenance:**
```bash
systemctl stop atomic-agents-serve
# ... perform disk maintenance ...
systemctl start atomic-agents-serve
```
Do not unmount the disk while the serve process is running (see
startup-script.sh DO NOT UNMOUNT note).

### Disk backup

Snapshot the persistent disk before major updates or migrations:
```bash
gcloud compute snapshots create <agent-name>-vault-$(date +%Y%m%d) \
  --project=<PROJECT_ID> \
  --source-disk=<agent-name>-vault \
  --source-disk-zone=<ZONE>
```
(Uses the currently recommended `gcloud compute snapshots create` form rather
than the legacy `gcloud compute disks snapshot`. Verified:
https://cloud.google.com/compute/docs/disks/create-snapshots)

---

## One-instance-per-tenant monitoring

The instance count alert for the VM topology is: one instance per agent vault.
Unlike the stateless Cloud Run shape (where containerConcurrency caps concurrency
within an instance), the VM constraint is at the VM level: two VMs sharing the
same disk root would race on `atomic_write` targets.

Alert recommendation: monitor for a second VM with the same `agent=<name>` label
using a Cloud Monitoring custom metric or a simple scheduled health check.

The equivalent of the Cloud Run "instance count != 1" alert does not map directly
to Compute Engine instance metrics because the constraint is per-vault, not
per-service. Use VM labels and a resource policy to enforce one-instance-per-tenant.

---

## Scale-out path

This VM is the v0 stateful-today bridge. As backend protocols ship:

- **Logs now (shipped):** activate `PostgresLogBackend` (`ATOMIC_AGENTS_LOG_BACKEND=postgres`) to move run logs off the disk to Cloud SQL.
- **Memory (#382 + #258):** the operator override seam already shipped (#382 PR 1: set `ATOMIC_AGENTS_MEMORY_BACKEND` or `AtomicAgent(memory_backend=...)`), but only `filesystem` is registered today. Once the Postgres MemoryBackend adapter ships (#258), `ATOMIC_AGENTS_MEMORY_BACKEND=postgres` moves memory off the disk; until then that value fails fast with `BackendNotRegistered`.
- **Goals, outcomes, journal, cascade (#383):** when Protocols ship for these surfaces, cloud adapters can be written.
- **After all surfaces move:** the VM's disk is empty; the stateless Cloud Run manifest (`extras/gcp/cloudrun-service.yaml`) becomes the correct topology.

See `extras/gcp/README.md §Scale-out path` for the full phase table.
