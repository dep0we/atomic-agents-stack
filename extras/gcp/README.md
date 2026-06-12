# GCP deployment - Cloud Run (stateless) + Compute Engine VM (stateful v0) + IAP + Cloud Scheduler

NOT YET DOGFOODED against the live GCP platform.
Claims verified against GCP provider docs as of 2026-06-09.
See §External-claim verification status below.

Reference deployment for atomic-agents-stack on Google Cloud. Two reference
topologies:

**(a) Cloud Run (stateless v0)** - one Cloud Run service per customer, IAP
perimeter for auth, Cloud Scheduler for scheduled triggers, Redis for locks,
Cloud SQL for run logs. Config baked into the image; every not-yet-cloud-backed
state surface is ephemeral (lost on container replacement). The target topology
once all mutable state moves to managed backends (Phases 2-3).

**(b) Compute Engine VM (stateful v0)** - one VM per tenant, persistent ext4/xfs
disk, POSIX `rename()` atomicity, one-instance-per-tenant pinning. The stateful
bridge for today: use this when you need durable filesystem state (memory,
goals, outcomes, journal) before the managed backend protocols ship. See
`extras/gcp/compute-vm/` for the full guide.

**This is reference scaffolding, not a one-command provisioner.** `atomic-agents serve`
(issue #342) is shipped. `PostgresLogBackend` (issue #258 PR 1) is also shipped.
The remaining gate on a runnable path is an operator's real agent folder plus live
credentials.

---

## External-claim verification status

| Claim | Source | Assurance |
|---|---|---|
| Cloud Run v2 supported volume types (secret, cloudSqlInstance, emptyDir, nfs, gcs - no GCE block PD) | [Cloud Run v2 Volume schema](https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.services) | Verified against docs |
| internal-and-cloud-load-balancing = "resources allowed by the more restrictive internal setting" + external ALB; the internal-source list (Cloud Scheduler, Pub/Sub, Eventarc, ...) is defined under `internal` and inherited. Doc-literal: same-project Scheduler-to-run.app is admitted; confirm at deploy (recognition is documented most crisply under bare `internal`) | [Cloud Run ingress settings](https://cloud.google.com/run/docs/securing/ingress) | Verified against docs (doc-literal); confirm Scheduler-to-run.app at deploy time, or route via LB/IAP |
| Cloud Armor enforceOnKey: HTTP_HEADER runs on Cloud Armor Standard (default tier for any project with an external load balancer; no Enterprise subscription required; there is no "free"/"base" tier) | [Standard vs Enterprise tiers](https://cloud.google.com/armor/docs/armor-enterprise-overview) + [rate limiting overview](https://cloud.google.com/armor/docs/rate-limiting-overview) | Verified against docs |
| Cloud Armor evaluatePreconfiguredWaf current stable XSS rule set is xss-v422-stable (OWASP CRS 4.22); xss-v33-stable is the legacy CRS 3.x set | [Cloud Armor rule tuning](https://cloud.google.com/armor/docs/rule-tuning) | Verified against docs (confirm latest stable at deploy time) |
| Secret Manager idempotent create + version add via gcloud | [Secret Manager API](https://cloud.google.com/secret-manager/docs/creating-and-accessing-secrets) | Verified against docs |
| IAP injects X-Goog-IAP-JWT-Assertion on authenticated requests | [IAP docs](https://cloud.google.com/iap/docs/signed-headers-howto) | Verified against docs |
| IAP injects X-Goog-Authenticated-User-Email on authenticated requests | [IAP JWT header docs](https://cloud.google.com/iap/docs/signed-headers-howto) | Verified against docs |
| Cloud Armor vs IAP evaluation order: on a GLOBAL EXTERNAL ALB Cloud Armor runs FIRST then IAP (so the IAP identity header is ABSENT when Cloud Armor evaluates - Rule 900 per-identity keying is a no-op there); on a CLASSIC ALB IAP runs first then Cloud Armor (header present, Rule 900 works) | [Integrating Cloud Armor with IAP](https://cloud.google.com/armor/docs/integrating-cloud-armor) | Verified against docs |
| GCE disk attach auto-delete=no behavior | [gcloud compute instances create](https://cloud.google.com/sdk/gcloud/reference/compute/instances/create) | Verified against docs |
| GCE blkid filesystem detection + mkfs.ext4 idempotency | [GCE format-mount-disk guide](https://cloud.google.com/compute/docs/disks/format-mount-disk-linux) | Verified against docs |
| systemd After=local-fs.target + RequiresMountsFor= | [systemd.unit man page](https://www.freedesktop.org/software/systemd/man/systemd.unit.html) | Verified against docs |
| IAP TCP tunneling source range (35.235.240.0/20) | [IAP TCP forwarding docs](https://cloud.google.com/iap/docs/using-tcp-forwarding) | Verified against docs |
| Cloud Billing Budgets API gcloud billing budgets create (incl. --threshold-rule basis enum: current-spend / forecasted-spend, lowercase-hyphen) | [gcloud billing budgets create](https://cloud.google.com/sdk/gcloud/reference/billing/budgets/create) | Verified against docs |
| NFS is a supported Cloud Run v2 volume type | [Cloud Run NFS volume mounts](https://cloud.google.com/run/docs/configuring/services/nfs-volume-mounts) | Verified against docs |
| GCSFuse is a supported Cloud Run v2 volume type | [Cloud Run storage considerations](https://cloud.google.com/run/docs/configuring/services/storage-considerations) | Verified against docs |

---

## What is in this directory vs. what is deferred

| Item | Status | Notes |
|---|---|---|
| `Dockerfile` | here | CMD runs `atomic-agents serve` (shipped, #342); non-root user (UID 10001) |
| `cloudrun-service.yaml` | here | Stateless Cloud Run service, ephemeral state annotations |
| `cloudscheduler-jobs.yaml.example` | here | OIDC auth, retry config, ingress topology options |
| `secret-manager-bootstrap.sh` | here | Idempotent provisioning |
| `cloud-logging-config.md` | here | Signal streams + audit trail reality |
| `cloud-monitoring-policies.yaml` | here | HTTP 402/500/503 + instance floor alert |
| `iap-setup.md` | here | Perimeter setup, Cloud Armor pointer |
| `cloud-armor-rules.yaml.example` | here | Rate limit (IP + identity) + body-size + WAF starter |
| `compute-vm/` | here | Compute Engine VM with persistent disk, v0 stateful reference (one instance per tenant) |
| Terraform module | deferred | Revisit on real community demand; gcloud upsert semantics cover the reference case without a maintainer-owned HCL module that drifts with GCP API changes |
| `cloudsql-bootstrap.sql` | deferred | PostgresLogBackend (#258 PR 1, shipped) self-provisions its schema on first connect - no manual DDL needed. |
| Full elastic scale-out | deferred | 4-phase path; see §Scale-out path |

---

## v0 deployment topology

**Cloud Run (stateless v0):**
```
   Cloud Scheduler ──OIDC──┐ (same-project: direct run.app URL)
                           ▼
   user → Cloud Armor → IAP → Cloud Run (containerConcurrency=1, min-instances=1)
                               │  image = framework + baked config (immutable)
                               │  atomic-agents serve (non-root, UID 10001)
                               ├→ Vertex AI / Anthropic (LLM)   see §LLM backend
                               ├→ Cloud SQL                      run logs (activate PostgresLogBackend, #258)
                               ├→ Secret Manager                 secrets (active via env vars today; GCPSecretBackend at #340 PR 2)
                               └→ Memorystore/Redis              locks (#60, shipped)

                  State surfaces: EPHEMERAL (container writable layer, lost on replacement)
                  - memory, goals, outcomes, journal, cascade: annotated in cloudrun-service.yaml
                  Cloud Logging + Cloud Monitoring (HTTP access logs + alerts)
```

**Compute Engine VM (stateful v0) - see extras/gcp/compute-vm/:**
```
   operator / Cloud Scheduler ──IAP TCP tunnel / VPC-internal──┐
                                                               ▼
                                               GCE VM (1 per tenant)
                                                 │  atomic-agents serve (systemd)
                                                 ├→ /app/agents/   <- MOUNT POINT: persistent disk (= ATOMIC_AGENTS_ROOT, ext4/xfs)
                                                 │    └ <name>/     <- agent folder (config + state on disk)
                                                 │        memory/, log/, journal/, goals/, outcomes/
                                                 ├→ Memorystore/Redis    locks (#60)
                                                 └→ Cloud SQL            run logs (PostgresLogBackend recommended)
```

In the Cloud Run shape, every not-yet-cloud-backed state surface is ephemeral and
annotated in `cloudrun-service.yaml`. The VM shape provides durable filesystem
state today. Full elastic scale-out = emptying the VM's disk surface by surface
as managed backends ship. See §Scale-out path.

---

## Why no disk on v0 Cloud Run

Cloud Run v2 does not support GCE block persistent disk volumes. The original
v0 blueprint (issue #339) included `gce-persistent-disk` volume blocks in
`cloudrun-service.yaml`. These do not exist in the Cloud Run v2 Volume schema:
the valid types are `secret`, `cloudSqlInstance`, `emptyDir`, `nfs`, and `gcs`.
(Verified: https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.services)

Even the Cloud Run-supported volume types (NFS, GCS FUSE) are incompatible with
the framework's `atomic_write` primitive. `atomic_write` uses
`tempfile.mkstemp(dir=target.parent)` + `os.replace()` (POSIX `rename(2)`),
which requires the temp file and target to live on the same filesystem. NFS and
GCS FUSE do not guarantee POSIX `rename(2)` atomicity. A crash mid-write on a
non-POSIX mount leaves the target file absent rather than leaving a recoverable
`.tmp` file. See TENSIONS.md T4.

**v0 Cloud Run is therefore explicitly stateless.** Every not-yet-cloud-backed
state surface (memory, goals, outcomes, journal, cascade, mandate, policy,
profile, tool-registry, corpus) is ephemeral: it lives in the container's
writable layer and is LOST on container replacement. Each surface is annotated
individually in `cloudrun-service.yaml`.

For a stateful-today deployment, use the Compute Engine VM reference
(`extras/gcp/compute-vm/`). The VM has a real ext4/xfs disk satisfying POSIX
`rename(2)` atomicity.

---

## Per-layer state mapping

**Cloud Run v0 (stateless):**

Issue #258 below is the Postgres-adapter-family umbrella: it scopes the Log /
Memory / Profile / ToolRegistry / Mandate / Lock adapters by name, and the
remaining filesystem-default surfaces (Policy, Corpus, Persona storage) roll up
under the same Phase-3 Postgres-adapter work. If those surfaces later get their
own tracking issues, re-point the rows below.

| Layer | v0 Cloud Run backend | Path to cloud backend |
|---|---|---|
| Config (persona, model.md, tools.md, goal.md, skills) | Baked into container image | Never moves - config is immutable per deployment |
| Run logs | Ephemeral (container writable layer) unless PostgresLogBackend activated; activate with `ATOMIC_AGENTS_LOG_BACKEND=postgres` + `[postgres]` extra (#258, shipped) | Backend shipped; activate PostgresLogBackend to make durable |
| Locks | Redis `ATOMIC_AGENTS_LOCK_BACKEND=redis` (#60, shipped) | Already shipped |
| Memory | Ephemeral (container writable layer) - the MemoryBackend override seam shipped (#382 PR 1) but only `filesystem` is registered today; durable once the Postgres MemoryBackend adapter ships (#258, Phase 2). Until then `ATOMIC_AGENTS_MEMORY_BACKEND=postgres` fails fast with `BackendNotRegistered`. | Issue #382 (T5 wiring seam, shipped) AND issue #258 (Postgres memory adapter) |
| Goals | Ephemeral (container writable layer) - GoalBackend Protocol shipped (DRAFT spec/41, FilesystemGoalBackend, #425); only the filesystem impl is registered today, so durable once a cloud adapter ships | Issue #425 (Protocol shipped); cloud adapter pending |
| Outcomes | Ephemeral (container writable layer) - OutcomeBackend Protocol shipped (DRAFT spec/42, FilesystemOutcomeBackend, #426; write-path wiring deferred to #448); durable once write-path wiring lands AND a cloud adapter ships | Issue #426 (Protocol shipped) + #448 (write-path); cloud adapter pending |
| Dreams | Ephemeral - operator-triggered dream artifacts at `<agent_root>/dreams/` (`atomic_agents/dream.py`); no Protocol yet; #383 (Phase 3) | Issue #383 |
| Journal | Ephemeral (container writable layer) - JournalBackend Protocol shipped (DRAFT spec/43, FilesystemJournalBackend, #427); only the filesystem impl is registered today, so durable once a cloud adapter ships | Issue #427 (Protocol shipped); cloud adapter pending |
| Cascade queue | Ephemeral - no Protocol yet; #383 + TENSIONS.md T4 (Phase 3) | Issue #383 |
| Mandate, Policy | Ephemeral - Postgres adapters tracked in #258 (Phase 3) | Issue #258 |
| Profile, tool-registry, corpus | Ephemeral - Postgres adapters tracked in #258 (Phase 3) | Issue #258 |
| Persona storage (`.personas/`, FilesystemPersonaBackend) | Ephemeral - gated on the PersonaBackend cloud adapter, #258 (Phase 3). This is persona-backend storage, NOT the cascade work-queue (that is the `Cascade queue` row above). | Issue #258 |

**Compute Engine VM v0 (stateful) - see extras/gcp/compute-vm/:**

| Layer | v0 VM backend | Path to cloud backend |
|---|---|---|
| Config | Baked on disk (operator-deployed) | Never moves |
| Run logs | Persistent disk (FilesystemLogBackend) or PostgresLogBackend | Activate PostgresLogBackend for query |
| Locks | Redis (recommended) or FilesystemLockBackend | Redis already shipped |
| Memory | Persistent disk (FilesystemMemoryBackend) | Issue #382 (T5 wiring seam, shipped) AND issue #258 (Postgres memory adapter, Phase 2) |
| Goals, Outcomes, Journal | Persistent disk - GoalBackend (#425, DRAFT spec/41) + OutcomeBackend (#426, DRAFT spec/42) + JournalBackend (#427, DRAFT spec/43) Protocols shipped, filesystem reference impls | #425 + #426 + #427 (Protocols shipped); cloud adapters pending |
| Dreams | Persistent disk (no Protocol yet; dreams at `<agent_root>/dreams/`) | Issue #383 (Phase 3) |
| Cascade queue | Persistent disk (POSIX rename claim) | Issue #383 + TENSIONS.md T4 |
| Mandate, Policy, Profile, tool-registry, corpus | Persistent disk (Filesystem/SQLite backends) | Issue #258 (Phase 3) |

**Two groups (same for both topologies):**

- **Group A - Protocols exist, cloud adapter can be written once it ships:**
  logs (#258, shipped), memory (#382 override seam shipped; #258 Postgres
  adapter pending), goals (#425, DRAFT spec/41), outcomes (#426, DRAFT spec/42;
  write-path wiring deferred to #448), journal (#427, DRAFT spec/43), mandate,
  policy, profile, tool-registry, corpus, persona (#258).
- **Group B - No Protocol yet; Protocol must be authored before any adapter:**
  cascade queue. Tracked in #383. The home user
  (filesystem-only) is unaffected; this gate only matters for org-scale
  elastic deployments.

To activate Redis locks today:

```bash
gcloud run services update <your-agent-name> \
  --set-env-vars ATOMIC_AGENTS_LOCK_BACKEND=redis \
  --set-secrets ATOMIC_AGENTS_LOCK_BACKEND_URL=redis-url:latest
```

Include the `redis` extra in the `Dockerfile` pip line (the shipped Dockerfile
installs `[serve,redis,vertex]`).

---

## Config layer

Config (persona, model.md, tools.md, goal.md, skills) is baked into the
container image at build time. It is never in a database.

**Changing agent config requires a new image build and re-deploy.** This is
intentional: config is immutable across a deployment, which eliminates
config-state races. To update config:

1. Edit the file in your agent folder.
2. Build a new image: `docker build -t gcr.io/<project>/<agent>:<tag> .`
3. Push: `docker push gcr.io/<project>/<agent>:<tag>`
4. Deploy the new revision: `gcloud run deploy ...`

On Cloud Run v0 (stateless), there is no disk overlay. State directories
(`memory/`, `log/`, `journal/`, etc.) in the baked image exist only in the
container's ephemeral writable layer - they are not backed by any volume mount.
State written there is lost on container replacement. Each surface is annotated
in `cloudrun-service.yaml` with its durability status and the issue that gates
the cloud backend.

`goal.md` is a config file (a file, not a directory) and is baked into the
image. To change it, rebuild the image.

Do not mount a volume at `/app/agents/<name>` itself (the agent folder root) in
Phase 4 or the VM topology - that would shadow the baked-in config files and
the agent folder would look empty on first start, failing `healthz` Check 3.
Mount at the state subdirectory level or at `ATOMIC_AGENTS_ROOT` (the parent
directory). See `extras/gcp/compute-vm/README.md §CRITICAL LAYOUT RULE`.

---

## LLM backend

**What works today:**

`VertexGeminiLLMBackend` (shipped) covers Gemini models via Application Default
Credentials (ADC). There is no LLM-backend env var: backend selection is by
model-ID prefix. Use a `vertex/gemini-*` model ID in `model.md` and the
framework routes to the Vertex Gemini backend automatically (the optional
`provider: vertex-gemini` line is only a tie-breaker when more than one
registered backend claims the same model ID). No API key needed - ADC uses the
Cloud Run service account.

The model ID MUST live under a `## Default model` heading - the parser reads
the default model from that section, not from a bare `model:` line:

```markdown
## Default model
`vertex/gemini-2.5-flash`

provider: vertex-gemini
```

Install the Vertex extra so the backend is importable - the Dockerfile's
`pip install` line must include `vertex`:
`pip install "atomic-agents-stack[serve,redis,vertex]"`.

**Planned (#345):**

A unified Vertex AI Model Garden backend (Anthropic Claude + Gemini + others
via a single IAM service account + single GCP billing line) is tracked in
#345. Until #345 ships, Anthropic models still require `ANTHROPIC_API_KEY`
injected as an env var.

---

## Secrets

**Two supported approaches.**

**(1) Inject secrets as Cloud Run environment variables** (simplest; no secret
backend selection needed). `FilesystemSecretBackend` (the default) probes env
vars first, so injected values work transparently with no
`ATOMIC_AGENTS_SECRET_BACKEND` env var set.

```bash
gcloud run services update <your-agent-name> \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest \
  --set-secrets ATOMIC_AGENTS_LOCK_BACKEND_URL=redis-url:latest
```

`secret-manager-bootstrap.sh` provisions the secrets into Secret Manager and
grants the service account accessor role. Re-running the script after a partial
failure is safe (idempotent).

**(2) Select the GCP Secret Manager backend** (shipped at #340 PR 2). The agent
resolves each credential from Secret Manager live at call time, so a rotated
secret is visible on the next run with no redeploy. Install the `[gcp]` extra
and set both env vars:

```bash
pip install "atomic-agents-stack[gcp]"

gcloud run services update <your-agent-name> \
  --set-env-vars ATOMIC_AGENTS_SECRET_BACKEND=gcp \
  --set-env-vars ATOMIC_AGENTS_SECRET_BACKEND_URL=projects/<your-project-id>/secrets
```

Secret names follow `KEY.lower().replace('_', '-')` (e.g. `ANTHROPIC_API_KEY` →
the `anthropic-api-key` secret, `latest` version). The selected backend incurs
one uncached Secret Manager access per credential resolution (one per LLM
iteration), which is billable and adds gRPC latency, so budget Secret Manager
access quota accordingly. See
[`docs/deployment/secret-backend.md`](../../docs/deployment/secret-backend.md)
for the full backend selection guide and the no-cache rotation contract.

---

## Scheduled triggers

Cloud Scheduler uses OIDC token authentication to call Cloud Run. The ingress
setting `internal-and-cloud-load-balancing` (set in `cloudrun-service.yaml`)
admits "resources allowed by the more restrictive `internal` setting" plus
external Application Load Balancer traffic. The Google internal-source list
(Cloud Scheduler, Pub/Sub, Eventarc, ...) is defined under `internal` and is
therefore inherited, so the doc-literal reading is that same-project Scheduler
hitting the default run.app URL is admitted as internal traffic. In that case
scheduled calls have **no `http_caller` field** in the JSONL audit record (no
IAP header - OIDC-only), and you can distinguish them from interactive runs by
that absence.
(Verified: ingress source classes at https://cloud.google.com/run/docs/securing/ingress.
Confirm at deploy time - the internal-source recognition is documented most
crisply under bare `internal`; if same-project Scheduler-to-run.app returns
403/404, use topology (b) or set ingress to bare `internal`.)

**Two topologies** (see `cloudscheduler-jobs.yaml.example §INGRESS RECONCILIATION`
for both options):

**(a) Same-project + default run.app URL:**
Doc-literal: Scheduler reaches Cloud Run directly as internal traffic, no load
balancer needed. Audit record: no `http_caller` field. If this returns 403/404
on your project, switch to (b) or bare `internal` ingress.

**(b) Custom domain, cross-project / VPC-SC perimeter, or the safe default:**
Route Scheduler through the load balancer / IAP instead. Works unconditionally
under `internal-and-cloud-load-balancing`. The request then carries the IAP
identity header, and the "no `http_caller`" distinction no longer applies -
scheduled calls WOULD carry the IAP-asserted identity.

---

## Cost controls and billing budget

**Cloud Armor rate limits** (cloud-armor-rules.yaml.example) cap request rates at
the edge but do NOT bound aggregate agent.call() LLM spend across all users:

- **Rule 1000 (per-IP)** works on any topology.
- **Rule 900 (per-identity)** works ONLY on the classic ALB. On the global
  external ALB this blueprint configures (`--load-balancing-scheme=EXTERNAL_MANAGED`,
  iap-setup.md), Cloud Armor evaluates BEFORE IAP, so the IAP-injected
  X-Goog-Authenticated-User-Email header is absent when Rule 900 evaluates and
  the rule no-ops (it is scoped with `has(...)` so it does not collaterally ban
  the shared empty-key bucket). Do NOT treat per-identity limiting as active on
  the default topology. (Verified: Cloud-Armor-before-IAP ordering on the global
  external ALB at https://cloud.google.com/armor/docs/integrating-cloud-armor.)
  Deploy on the classic ALB (`--load-balancing-scheme=EXTERNAL`) if you need
  per-identity limiting.

**A GCP billing budget alert is the only project-level ceiling** on total
agent.call() spend regardless of authentication topology or load-balancer type.
Set one up before deploying to production:

```bash
# Create a billing budget with email alerts at 80% and 100%
gcloud billing budgets create \
  --billing-account=<BILLING_ACCOUNT_ID> \
  --display-name="atomic-agents-<project-id>" \
  --budget-amount=<MONTHLY_LIMIT_USD>USD \
  --threshold-rule=percent=0.8,basis=current-spend \
  --threshold-rule=percent=1.0,basis=current-spend
```

Find your billing account ID: `gcloud billing accounts list`
(Verified: the basis enum must be lowercase-hyphen `current-spend` / `forecasted-spend` per the gcloud reference at https://cloud.google.com/sdk/gcloud/reference/billing/budgets/create)

The budget alert fires an email (and optionally a Cloud Monitoring notification
channel) when spend exceeds the threshold percentage. It does not automatically
stop the service - set up a Cloud Pub/Sub notification + Cloud Function if you
need automatic service suspension on budget breach.

---

## Disaster recovery

### Cloud Run (stateless v0)

On stateless Cloud Run, crash recovery for ephemeral state is not possible.
Ephemeral state in the container writable layer is gone with the container.

**What survives a crash:**
- Redis lock state (the lock is released by the lock backend on timeout)
- PostgresLogBackend run records (if PostgresLogBackend is activated)

**What does not survive:**
- Memory, goals, outcomes, journal, cascade queue, and all other ephemeral
  surfaces - lost on container replacement. There is no disk to remount.

For cascade stale-claim recovery, use the Compute Engine VM reference
(`extras/gcp/compute-vm/`) where the disk persists across restarts.

### SIGTERM / graceful shutdown (Cloud Run)

Cloud Run sends SIGTERM and then SIGKILLs the container after a FIXED 10 second
grace period. This shutdown grace is NOT configurable through the Cloud Run v2
API (the v2 RevisionTemplate schema exposes no terminationGracePeriodSeconds
field), and it is SEPARATE from `--timeout` / `--timeout-seconds`, which bounds a
single request's duration and does NOT extend the shutdown window. The framework
registers no signal handler directly, but `uvicorn`'s SIGTERM handling triggers
the Starlette lifespan shutdown, which calls `shutdown_executor()`
(`atomic_agents/serve/_runner.py`; the lifespan call site is `_app.py`). That
function internally calls the serve thread pool's `shutdown(wait=True)`
(`_runner.py`), so it waits for in-flight `agent.call()` work to drain up to the
10s grace window. So SIGTERM DOES drain
in-flight calls; only a hard SIGKILL (grace exceeded) cuts work mid-flight. On
Cloud Run, cascade claim sidecars live in ephemeral storage and are lost on
replacement regardless of drain - durability requires the managed backends.

- Do not expect to extend the shutdown drain: the 10s grace is fixed, and
  `--timeout-seconds` does not affect it. Design `agent.call()` work to be
  resumable rather than relying on a long shutdown window.
- For in-flight call durability, the only path is activating managed backends
  (Redis locks, PostgresLogBackend) so the durable surfaces survive container
  replacement. The shutdown grace cannot make ephemeral surfaces durable.

(Verified: fixed 10s SIGTERM grace, separate from request timeout, at
https://cloud.google.com/run/docs/container-contract ; no termination-grace
field in the v2 schema at
https://cloud.google.com/run/docs/reference/rest/v2/projects.locations.services)

### Upgrade runbook (Cloud Run)

Canary traffic splits (e.g., 50/50) are safe for stateless Cloud Run revisions
once no persistent disk is attached (which is the case for all Cloud Run v0
deployments after this fix). The surviving concurrency constraint is the default
FilesystemLockBackend (fcntl.flock, single-host advisory locking): because flock
does not coordinate across hosts, two concurrent revisions serving the same agent
do not see each other's lock and would race on ephemeral filesystem state. This
does not affect the canary split topology itself (traffic routing), only
concurrent same-agent requests across revisions. The Redis LockBackend
coordinates across revisions if you need it.

```bash
# Standard 100% traffic shift (safest):
gcloud run deploy <your-agent-name> \
  --image gcr.io/<project>/<agent>:<new-tag>

# If a prior revision was traffic-pinned, restore latest-only routing:
#   gcloud run services update-traffic <your-agent-name> --to-latest
```

---

## Scale-out path

Full elastic scale-out = scale-to-zero + many instances + any instance serves
any request + durable. The Cloud Run stateless shape is already the starting
point for the scale-out path: there is no disk to peel. The gates are the
managed backends shipping.

For the Compute Engine VM, the scale-out path is peeling every state surface
off the VM disk, one cloud-native backend at a time.

| Phase | What it does | Gate |
|---|---|---|
| **Phase 1** | Export contract - round-trip: write to cloud store, export, byte-equivalent vault files. Proves cloud store equals filesystem store. Gate for everything after. | Issue #379 |
| **Phase 2** | Memory backend on Postgres/pgvector (biggest disk shrink). Memory is the most-written, most-shared surface. | Issue #382 (T5 wiring seam) AND issue #258 (Postgres memory adapter) |
| **Phase 3** | Peel the rest: goals (#425, DRAFT spec/41) + outcomes (#426, DRAFT spec/42) + journal (#427, DRAFT spec/43) Protocols shipped — cloud adapters pending; cascade work-queue still needs a Protocol authored (#383; the work-queue's POSIX-rename claim is what T4 tracks). Mandate, policy, profile, tool-registry, corpus, and persona storage (`.personas/`) to Postgres adapters (#258). | Issue #383 + #258 + #425 + #426 + #427 + TENSIONS.md T4 + T13 |
| **Phase 4** | Flip topology: all state on multi-writer-safe cloud backends; remove min-instances=1 constraint; raise max-instances. Service is now stateless in the full sense: any instance serves any request, idle = $0. For Cloud Run v0, the disk was never present - Phase 4 is the managed-backend gate only. | All prior phases + issue #379 round-trip conformance |
| **Phase 5** | Concurrency ceiling (TENSIONS.md T2). Horizontal scale buys headroom before this is forced. | TENSIONS.md T2 triggers |

**Invariant across all phases:** the agent is the same agent. Config never
moves (it is in the image); the export contract (#379) guarantees the file shape
is always reconstructable. Scale-out is swapping registered backends behind
conformance tests - not rewriting the agent.

---

## Troubleshooting

**Container crash-loops immediately on deploy:**
Check that the CMD in the Dockerfile uses `${PORT:-8080}`. Cloud Run injects
`PORT` at runtime; the `--port ${PORT:-8080}` flag honors it. Here `8080` is the
Cloud Run shell-level fallback for `${PORT}`, not the framework default. The
framework default is `8000` (`atomic_agents/serve/_config.py`). The container
always passes `--port` explicitly, so nothing breaks; just don't expect an
`8080` default if you drop the flag. A hardcoded port that ignores `PORT` is what
breaks the health check. See `docs/deployment/serve.md §Cloud Run entrypoint
example`.

**min-instances=1 and cold-start latency:**
`min-instances=1` avoids cold-start 503s when Cloud Scheduler triggers arrive.
Set to 0 only if you accept cold-start latency and configure `retryCount=0` on
all Scheduler jobs. On stateless Cloud Run there is no disk state to lose on
scale-to-zero - the constraint is cost vs. availability, not durability.

**Doctor reports FAIL for ANTHROPIC_API_KEY:**
If the error says "not found": confirm the secret VERSION was added, not just
the secret shell. A secret with no active version returns `NOT_FOUND`, not
`PERMISSION_DENIED`:
```bash
gcloud secrets versions list anthropic-api-key --project=<project>
```

**HTTP 403 from Cloud Scheduler:**
Verify the scheduler service account has `roles/run.invoker` on the specific
Cloud Run service (not just project-level):
```bash
gcloud run services get-iam-policy <your-agent-name> --region=<region>
```

**HTTP 401 from Cloud Scheduler:**
The OIDC token `audience` in `cloudscheduler-jobs.yaml.example` must match
the Cloud Run service URL exactly (copy from Cloud Run console > Service URL,
no trailing slash).

**Agent state lost after redeployment:**
On stateless Cloud Run v0, every not-yet-cloud-backed state surface (memory,
goals, outcomes, etc.) is ephemeral and is lost on container replacement. This
is expected behavior. Activate the relevant managed backends to make surfaces
durable (PostgresLogBackend for logs, Redis for locks). For full filesystem
state persistence today, use the Compute Engine VM reference
(`extras/gcp/compute-vm/`).
