# GCP deployment — Cloud Run + IAP + Cloud Scheduler

Reference deployment for atomic-agents-stack on Google Cloud. One Cloud Run
service per customer (billing and IAM isolation), IAP perimeter for auth,
Cloud Scheduler for scheduled triggers, persistent disk for v0 filesystem
state, Redis for locks, and Cloud Monitoring for observability.

**This is a reference deployment, not a one-command provisioner.** The
scaffolding ships now. `atomic-agents serve` (issue #342) is shipped — the
`[serve]` extra exists and the Dockerfile CMD works today. `PostgresLogBackend`
(issue #258 PR 1) is also shipped — activate it with `ATOMIC_AGENTS_LOG_BACKEND=postgres`
and the `[postgres]` extra to move run logs off the persistent disk. The
remaining gate on the 30-minute runnable acceptance bar is an operator's real
agent folder plus live credentials. Each gate is linked at the step it applies.

---

## What is in this directory vs. what is deferred

| Item | Status | Notes |
|---|---|---|
| `Dockerfile` | ✅ here | CMD runs `atomic-agents serve` (shipped, #342) |
| `cloudrun-service.yaml` | ✅ here | v0 topology, honest placeholders |
| `cloudscheduler-jobs.yaml.example` | ✅ here | OIDC auth, retry config |
| `secret-manager-bootstrap.sh` | ✅ here | Idempotent provisioning |
| `cloud-logging-config.md` | ✅ here | Signal streams + audit trail reality |
| `cloud-monitoring-policies.yaml` | ✅ here | HTTP 402/500/503 + instance alerts |
| `iap-setup.md` | ✅ here | Perimeter setup, Cloud Armor |
| `cloud-armor-rules.yaml.example` | ✅ here | Rate limit + WAF starter |
| Terraform module | 🔜 deferred | Revisit on real community demand; gcloud upsert semantics cover the reference case without a maintainer-owned HCL module that drifts with GCP API changes |
| `cloudsql-bootstrap.sql` | 🔜 deferred | PostgresLogBackend (#258 PR 1, shipped) self-provisions its schema on first connect — no manual DDL needed. Remaining #258 adapters will follow the same self-provision pattern. |
| Full elastic scale-out | 🔜 deferred | 4-phase path; see §Scale-out path |

---

## v0 deployment topology

```
   Cloud Scheduler ──OIDC──┐
                           ▼
   user → Cloud Armor → IAP → Cloud Run (1 instance/tenant, min-instances=1)
                               │  image = framework + baked-in config (immutable)
                               │  atomic-agents serve ✅ (#342)
                               ├→ Vertex AI (LLM)           see §LLM backend
                               ├→ Cloud SQL                 run logs (✅ #258 PR 1; activate with [postgres] extra)
                               ├→ Secret Manager            secrets (🔜 #340 PR 2)
                               ├→ Memorystore/Redis         locks ✅ (#60)
                               └→ Persistent disk           v0 state stopgap

                  Cloud Logging + Cloud Monitoring (HTTP access logs + alerts)
```

The persistent disk is the v0 honesty knob. It holds every state surface that
is filesystem-only today. Full elastic scale-out = emptying that disk layer by
layer. See §Scale-out path. This is per TENSIONS.md T15 (authority model).

---

## Per-layer state mapping

| Layer | v0 backend | Prerequisite to move off disk |
|---|---|---|
| Config (persona, model.md, tools.md, goal.md, skills) | Baked into container image | Never moves — config is immutable per deployment |
| Run logs | Filesystem JSONL on persistent disk (or ✅ PostgresLogBackend — activate with `ATOMIC_AGENTS_LOG_BACKEND=postgres` + `[postgres]` extra, #258 PR 1) | Backend shipped; activate to move off disk |
| Locks | Redis ✅ `ATOMIC_AGENTS_LOCK_BACKEND=redis` | Already shipped |
| Memory | Persistent disk (FilesystemMemoryBackend) | 🔜 #382 (T5 wiring seam) |
| Goals | Persistent disk (no Protocol yet) | 🔜 #383 (Protocol must be authored first) |
| Outcomes | Persistent disk (no Protocol yet) | 🔜 #383 |
| Journal | Persistent disk (no Protocol yet) | 🔜 #383 |
| Cascade queue | Persistent disk (POSIX rename claim) | 🔜 #383 + TENSIONS.md T4 |
| Mandate, Policy | Persistent disk (FilesystemBackend) | 🔜 #258 Postgres adapters |
| Profile, tool-registry, corpus | Persistent disk (SQLite files) | 🔜 #258 Postgres adapters |
| Persona-cascade leases | Persistent disk | 🔜 #383 |

**Two groups on the persistent disk:**

- **Group A — Protocols exist, cloud adapter can be written once the adapter
  ships:** logs (#258), memory (#382), mandate, policy, profile, tool-registry,
  corpus (#258).
- **Group B — No Protocol yet; Protocol must be authored before any adapter:**
  goals, outcomes, journal, cascade queue. Tracked in #383. The home user
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
container image at build time. It is never on the persistent disk and never in
a database.

**Changing agent config requires a new image build and re-deploy.** This is
intentional: config is immutable across a deployment, which eliminates
config-state races. To update config:

1. Edit the file in your agent folder.
2. Build a new image: `docker build -t gcr.io/<project>/<agent>:<tag> .`
3. Push: `docker push gcr.io/<project>/<agent>:<tag>`
4. Deploy the new revision: `gcloud run deploy ...`

The persistent disk is mounted at the **mutable-state subdirectories only**
(`memory/`, `log/`, `journal/`, and any others your agent writes) — never at
the agent folder root. Mounting a volume at `/app/agents/<name>` would shadow
the baked-in config files and leave the agent folder effectively empty on first
cold start, which fails healthz Check 3 (model.md present + parseable) and
crash-loops the container. See `cloudrun-service.yaml` §volumeMounts for the
per-subdirectory mount layout. `goal.md` is a config file (a file, not a
directory) and is NOT mounted — to change it, rebuild the image.

Do not edit config files at runtime — they are baked into the image, and the
disk is mounted under them at the state subdirs, not over them.

---

## LLM backend

**What works today:**

`VertexGeminiLLMBackend` (shipped) covers Gemini models via Application Default
Credentials (ADC). There is no LLM-backend env var: backend selection is by
model-ID prefix. Use a `vertex/gemini-*` model ID in `model.md` and the
framework routes to the Vertex Gemini backend automatically (the optional
`provider: vertex-gemini` line is only a tie-breaker when more than one
registered backend claims the same model ID). No API key needed — ADC uses the
Cloud Run service account.

The model ID MUST live under a `## Default model` heading — the parser reads
the default model from that section, not from a bare `model:` line:

```markdown
## Default model
`vertex/gemini-2.5-flash`

provider: vertex-gemini
```

Install the Vertex extra so the backend is importable — the Dockerfile's
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

Cloud Scheduler → Cloud Run uses OIDC token authentication, NOT the
`X-Goog-IAP-JWT-Assertion` header. **If** the scheduler calls the run.app
service URL directly (OIDC only), scheduler-triggered calls have **no
`http_caller` field** in the JSONL audit record, so you can distinguish them
from interactive runs by the absence of `http_caller`.

Note the ingress lock: `cloudrun-service.yaml` and `iap-setup.md` set ingress
to `internal-and-cloud-load-balancing`, which blocks direct run.app access —
including Scheduler's OIDC call. To keep that perimeter, route Scheduler
through the load balancer / IAP, in which case the call DOES carry the IAP
identity header and the "no `http_caller`" distinction no longer applies. See
`cloudscheduler-jobs.yaml.example` §INGRESS RECONCILIATION for the two
topologies, the service account binding, OIDC audience, and retry guidance.

---

## Persistent disk requirements

The persistent disk **MUST** be a Compute Engine persistent disk (ext4 or
xfs) mounted directly to the Cloud Run instance.

**GCS FUSE, Filestore, NFS-backed volumes, and RAM-disk mounts are
unsupported.** The framework's `atomic_write` (temp + fsync + rename — see
`atomic_agents/_io.py`) relies on POSIX `rename(2)` atomicity, which
FUSE-mounted GCS and network filesystems do not guarantee. A crash mid-write
on a non-POSIX mount leaves the target file absent rather than leaving a
recoverable `.tmp` file. See TENSIONS.md T4 and the `_io.py` docstring.

This is already set in `cloudrun-service.yaml` (`gce-persistent-disk` +
`fsType: ext4`). Do not substitute a different volume type.

---

## Disaster recovery

### Crash recovery

The cascade queue's stale-claim recovery (`recover_stale_claims` in
`atomic_agents/_cascade.py`) assumes access to the same filesystem state as the
crashed instance. On Cloud Run with a persistent disk:

1. When Cloud Run replaces a crashed container, the persistent disk is
   automatically remounted to the new instance (the volume mount name in
   `cloudrun-service.yaml` must remain stable across redeploys).
2. **`recover_stale_claims` is NOT invoked automatically.** It is a library
   function with no automatic call site — `AtomicAgent.__init__` runs mandate
   recovery (`_run_mandate_recovery_for_all_scopes`) but does not call cascade
   stale-claim recovery. Orphaned cascade work-claims persist on the disk until
   a cascade-processing run invokes `recover_stale_claims` (passing the project
   root and a `lease_seconds` window) or an operator clears them. Automatic
   boot-time cascade recovery is desired-but-unbuilt; tracked in #386.
3. The `lease_seconds` window (default 3600s, a function parameter — there is
   no env var for it today) determines how long orphaned claims are considered
   live before a recovery pass reclaims them. It is not boot-triggered.

**Non-atomic sidecar writes:** `_cascade.py:_write_sidecar` uses
`write_text` (not `atomic_write`). A crash between the `rename` (claim) and
the sidecar write leaves a claimed file with no sidecar. `recover_stale_claims`
falls back to mtime for exactly this case — the fallback is in the function,
but recovery still requires that function to be called (it is not automatic),
the disk to be attached, and the lease window to have elapsed.

### SIGTERM / graceful shutdown

Cloud Run sends SIGTERM and waits `--timeout-seconds` (default 300s) before
SIGKILL. The framework does not install a SIGTERM handler; a SIGKILL mid-call
leaves cascade claim sidecars without lease metadata.

- Set `--timeout-seconds` to at least 2× the expected max `agent.call()`
  runtime in your `gcloud run deploy` command.
- The stale-claim recovery window equals the `lease_seconds` argument passed by
  the cascade-processing run (default 3600s). It is a function parameter, not an
  env-configurable knob today — there is no `ATOMIC_AGENTS_CASCADE_LEASE_SECONDS`
  env var (filed as #387 if env tunability is wanted). Recovery is also not
  boot-triggered (see Crash recovery above).
- Long agents (>5 min) must either handle SIGTERM in the HTTP wrapper or
  accept the recovery delay. Reference: `atomic_agents/_cascade.py:_write_sidecar`.

### Upgrade runbook

When deploying a new image revision, **use 100% traffic shift** — do not use
canary traffic splits (e.g., 50/50) while a persistent disk is attached.

Canary splits create two concurrent Cloud Run revisions writing the same
persistent disk. `atomic_write` is safe for a single writer; concurrent
`atomic_write` calls from two processes targeting the SAME file on ext4 are
last-writer-wins (safe). Concurrent appends to the JSONL log file from two
processes are per-line atomic only while each line fits in PIPE_BUF (~4KB, and
only insofar as the buffered write lowers to a single append syscall; see
`atomic_agents/_io.py:atomic_append_jsonl` and #389) — log/eval lines are typically
< 1KB, so they do not interleave, but lines exceeding PIPE_BUF can. Canary
splits are safe only after Phase 4 (all state on multi-writer-safe cloud
backends, disk removed).

```bash
# Safe: 100% traffic shift on deploy. `gcloud run deploy` routes 100% of
# traffic to the new revision by default (unless --no-traffic is passed), so
# no traffic flag is needed.
gcloud run deploy <your-agent-name> \
  --image gcr.io/<project>/<agent>:<new-tag>

# If a prior revision was traffic-pinned, restore latest-only routing:
#   gcloud run services update-traffic <your-agent-name> --to-latest
```

---

## Scale-out path

Full elastic scale-out = scale-to-zero + many instances + any instance serves
any request + durable. The path is peeling every state surface off the
persistent disk, one cloud-native backend at a time, until the disk is empty.
Each phase is independently shippable. The home user is unaffected throughout
(filesystem backends; this work is purely additive cloud backends — the T15
portability payoff).

| Phase | What it does | Gate |
|---|---|---|
| **Phase 1** | Export contract — round-trip: write to cloud store → export → byte-equivalent vault files. Nothing leaves the disk yet; proves cloud store == filesystem store. Gate for everything after. | Issue #379 |
| **Phase 2** | Memory backend on Postgres/pgvector (biggest disk shrink). Memory is the most-written, most-shared surface. | Issue #382 (T5 wiring seam — memory backend selection is currently hardcoded to filesystem; there is no config field yet to swap it) AND issue #258 (Postgres memory adapter) |
| **Phase 3** | Peel the rest: goals, outcomes, journal, cascade-queue (no Protocol yet — #383 is the tracker). Mandate, policy, profile, tool-registry, corpus → Postgres adapters (#258). Persona-cascade work-claim → a real queue (TENSIONS.md T4). Migration runner becomes backend-shaped (TENSIONS.md T13). | Issue #383 (Protocol coverage gap) + #258 + TENSIONS.md T4 + T13 |
| **Phase 4** | Flip topology: remove volume mount, set `min-instances=0`, raise `max-instances`. Service is now stateless: any instance serves any request, idle = $0. | All prior phases + issue #379 round-trip conformance |
| **Phase 5** | Concurrency ceiling (TENSIONS.md T2). When per-instance throughput or MCP `asyncio.run()`-per-call latency hits named T2 triggers, do the async-first rebuild. Horizontal scale buys headroom before this is forced. | TENSIONS.md T2 triggers |

**Phase 2 plain-language note:** "Memory backend selection is currently
hardcoded to filesystem — there is no config field yet to swap it. Issue #382
adds that config seam." Until #382 ships, memory stays on the persistent disk
regardless of any env var.

**Phase 3 plain-language note:** Goals, outcomes, journal, and cascade queue
have no backend Protocol at all. Before any cloud adapter can be written for
them, a Protocol must be authored. Issue #383 is the tracker. This is the true
long tail of the scale-out path.

**Invariant across all phases:** the agent is the same agent. Config never
moves (it's in the image); the export contract (#379) guarantees the file shape
is always reconstructable. Scale-out is swapping registered backends behind
conformance tests — not rewriting the agent.

---

## Troubleshooting

**Container crash-loops immediately on deploy:**
Check that the CMD in the Dockerfile uses `${PORT:-8080}`. Cloud Run injects
`PORT` at runtime; the `--port ${PORT:-8080}` flag honors it. With the
recommended CMD, the `--port` CLI flag takes precedence over the
`ATOMIC_AGENTS_SERVE_PORT` env var (resolution order is CLI > env > serve.md >
default — see `atomic_agents/serve/_server.py`), so don't bother setting
`ATOMIC_AGENTS_SERVE_PORT`: while the CLI flag is present it does nothing but
add a confusing redundant override. A hardcoded port that ignores `PORT` is
what breaks the health check. See `docs/deployment/serve.md §Cloud Run
entrypoint example`.

**Disk unavailable on startup:**
Check that `min-instances=1` is set in `cloudrun-service.yaml`. If the service
scales to zero, the persistent disk detaches. The next cold start may fail or
attach a fresh empty disk, losing all filesystem-backed state.

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

**Stale cascade claims after container replacement:**
The persistent disk is remounted on instance replacement, but
`recover_stale_claims` does NOT fire automatically at agent boot (it has no
automatic call site — see §Disaster recovery). Orphaned claims persist until a
cascade-processing run calls `recover_stale_claims` or an operator clears them.
Confirm the volume mount name in `cloudrun-service.yaml` is stable (no rename
between deploys) and that the disk is attached; auto boot-time recovery is
tracked in #386.

---

## Acceptance criteria for this PR

This PR ships honest reference scaffolding. The original issue's acceptance
criteria included a 30-minute runnable path and a Terraform module; both are
deferred by the arc decisions:

- **30-minute runnable path:** `atomic-agents serve` (issue #342) is shipped and
  the Dockerfile CMD works today; the runnable acceptance bar still needs an
  operator's real agent folder + live credentials (and optionally
  PostgresLogBackend #258 for the Cloud SQL state path), which belongs to the
  arcs owning #342 serve and #258 Postgres, not this reference scaffolding.
- **Terraform module:** deferred to real community demand. A solo maintainer
  should not own an HCL module that drifts with GCP API changes absent
  demonstrated demand; `gcloud` upsert semantics suffice for the reference
  case.

The current acceptance bar:
- [x] `extras/gcp/` exists with honest scaffolding for every listed component
- [x] Per-layer state mapping is accurate: no surface claims cloud-native
      status it doesn't have today
- [x] Scale-out path is documented with explicit per-phase gates and issue links
- [x] Linked from root README §Deployment shapes and `extras/README.md`
- [ ] Runnable 30-minute path: serve (#342) shipped; remaining gate is an
      operator's real agent + creds (+ #258 for Cloud SQL state)
- [ ] Terraform module: deferred to community demand
