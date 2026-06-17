# GCP deployment - Cloud Run (stateless) + Compute Engine VM (stateful v0) + IAP + Cloud Scheduler

NOT YET DOGFOODED against the live GCP platform.
Claims verified against GCP provider docs as of 2026-06-17 (managed-ingestion
claims: Pub/Sub, Cloud Tasks, DLQ metric, monitoring policy 5); prior claims
as of 2026-06-09.
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
| Pub/Sub Subscription REST API field names: deadLetterPolicy.deadLetterTopic (full resource path projects/P/topics/T), deadLetterPolicy.maxDeliveryAttempts (integer 5-100), retryPolicy.minimumBackoff (duration string e.g. "10s"), retryPolicy.maximumBackoff (duration string e.g. "600s"), pushConfig.oidcToken.serviceAccountEmail, pushConfig.oidcToken.audience | [Pub/Sub Subscription REST API](https://cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions) | Verified against docs |
| Pub/Sub deadLetterPolicy.maxDeliveryAttempts valid range: 5–100; values below 5 rejected by API | [Pub/Sub Subscription REST API](https://cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions) | Verified against docs |
| retryPolicy supported on push subscriptions (not pull-only) | [Pub/Sub Subscription REST API](https://cloud.google.com/pubsub/docs/reference/rest/v1/projects.subscriptions) | Verified (doc-literal + inferred): the REST reference states retryPolicy "will be triggered on NACKs or acknowledgment deadline exceeded events for a given message" with no push/pull restriction; push applicability is INFERRED (a non-2xx push response IS a NACK), not push-explicitly-stated |
| Pub/Sub push OIDC auth requires TWO IAM grants. In this two-hop reference the push endpoint is the GEN2 transform function, so grant (1) is roles/cloudfunctions.invoker on that function for the push SA (`gcloud functions add-invoker-policy-binding`; equivalently roles/run.invoker on the function's backing Cloud Run service — see the Gen2-invoke row below). Grant (2) is roles/iam.serviceAccountTokenCreator on the project for the Pub/Sub service agent (service-PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com). | [Pub/Sub authenticate push subscriptions](https://cloud.google.com/pubsub/docs/authenticate-push-subscriptions) | Verified against docs |
| Pub/Sub service agent needs roles/pubsub.publisher on dead-letter topic + roles/pubsub.subscriber on source subscription for DLQ forwarding | [Pub/Sub dead-letter topics grant forwarding roles](https://cloud.google.com/pubsub/docs/handling-failures#grant_forwarding_roles) | Verified against docs |
| Pub/Sub push ingress: same-project Pub/Sub push to run.app URL admitted as internal traffic under internal-and-cloud-load-balancing. Same caveat as Cloud Scheduler: doc-literal, confirm at deploy time | [Cloud Run ingress settings](https://cloud.google.com/run/docs/securing/ingress) | Verified against docs (doc-literal); confirm at deploy time — same caveat as Cloud Scheduler |
| DLQ monitoring metric: pubsub.googleapis.com/subscription/num_undelivered_messages (GAUGE, INT64); subscription-level metric; resource.labels.subscription_id is the label key | [GCP Monitoring metrics — Pub/Sub](https://cloud.google.com/monitoring/api/metrics_gcp#gcp-pubsub) | Verified against docs |
| DLQ topic requires its own subscription for num_undelivered_messages metric to populate; a topic with no subscriptions emits no data points; dead-lettered messages are permanently lost without a subscription | [Pub/Sub dead-letter topics](https://cloud.google.com/pubsub/docs/handling-failures) | Verified against docs |
| DLQ forwarding "wraps the original message in a new one and adds attributes that identify the source subscription" (doc-literal); a messageId-derived key carried on a DLQ drain RE-RUNS rather than dedups | [Pub/Sub dead-letter topics — forwarding](https://cloud.google.com/pubsub/docs/handling-failures) | Verified (doc-literal): the doc states it "wraps the original message in a new one" and enumerates the ADDED `CloudPubSubDeadLetterSource*` attributes. INFERRED / UNVERIFIED (2026-06-17): the doc is SILENT on whether the wrapped message gets a distinct messageId AND on whether ORIGINAL publisher-set attributes (incl. an explicit `idempotency_key`) are retained across forwarding — both are inferred from "wraps … in a new one", not doc-stated. Do NOT rely on a Tier-1 `idempotency_key` surviving a DLQ drain without inspecting a dead-lettered message on your project |
| Cloud Tasks HTTP-target injected headers: X-CloudTasks-QueueName, X-CloudTasks-TaskName (short name — last path segment, NOT full resource path), X-CloudTasks-TaskRetryCount, X-CloudTasks-TaskExecutionCount, X-CloudTasks-TaskETA, X-CloudTasks-TaskPreviousResponse, X-CloudTasks-TaskRetryReason | [Cloud Tasks creating HTTP target tasks](https://cloud.google.com/tasks/docs/creating-http-target-tasks) | Verified against docs |
| Cloud Tasks Queue RetryConfig REST API field names: retryConfig.maxAttempts, retryConfig.maxRetryDuration (duration string), retryConfig.minBackoff (duration string), retryConfig.maxBackoff (duration string), retryConfig.maxDoublings | [Cloud Tasks Queue REST API](https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues) | Verified against docs |
| Cloud Tasks task-name dedup: best-effort window; doc-literal figure is "up to 24 hours" — "Cloud Tasks remembers task names for up to 24 hours after the task has been deleted from the queue" | [Cloud Tasks queue overview](https://cloud.google.com/tasks/docs/dual-overview) | Verified (doc-literal: overview states "up to 24 hours"; the REST name-field reference gives NO figure, only format constraints) |
| Cloud Tasks maxConcurrentDispatches is a queue-level field (NOT inside retryConfig) | [Cloud Tasks Queue REST API](https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues) | Verified against docs |
| X-CloudTasks-TaskName value is the SHORT task name (last path segment), not the full resource path | [Cloud Tasks creating HTTP target tasks](https://cloud.google.com/tasks/docs/creating-http-target-tasks) | Verified against docs |
| Cloud Tasks X-CloudTasks-TaskPreviousResponse and X-CloudTasks-TaskRetryReason are CONDITIONALLY present ("additional headers that might be present"), not always-injected; the other five are always injected | [Cloud Tasks creating HTTP target tasks](https://cloud.google.com/tasks/docs/creating-http-target-tasks) | Verified against docs |
| Cloud Tasks has NO native dead-letter queue; after max-attempts/max-retry-duration the task is dropped (capture failures in the worker or route via Pub/Sub) | [Cloud Tasks Queue REST API](https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues) | Inferred from the Queue REST resource exposing only `retryConfig` (maxAttempts/maxRetryDuration) and NO `deadLetterPolicy` field — the dual-overview narrative does not state DLQ absence in so many words; confirm at deploy time |
| `--log-sampling-ratio` controls the FRACTION (0.0-1.0) of queue operations written to Cloud Logging — it is NOT a dead-lettering mechanism | [gcloud tasks queues create](https://cloud.google.com/sdk/gcloud/reference/tasks/queues/create) | Verified against docs |
| `gcloud tasks create-http-task` takes TASK_ID as a POSITIONAL argument (no `--task-name` flag; positional ID enables de-duplication) and uses `--body-content`/`--body-file` (no `--body` flag) | [gcloud tasks create-http-task](https://cloud.google.com/sdk/gcloud/reference/tasks/create-http-task) | Verified against docs |
| Cloud Tasks name-dedup window: doc-literal "up to 24 hours" (uniform; the overview does NOT distinguish Cloud-Tasks-created vs queue.yaml/App Engine queues, and the REST name-field reference gives no figure) | [Cloud Tasks queue overview](https://cloud.google.com/tasks/docs/dual-overview) | Verified (doc-literal: overview states "up to 24 hours"). UNVERIFIED: the previously-asserted per-queue-type ~1-hour / ~9-day figures could NOT be located in the cited docs (2026-06-17) — do not rely on a queue-type-specific window |
| Pub/Sub push CANNOT target /call directly — the push endpoint must be an interposed transform (Cloud Function) that base64-decodes the envelope and sets the Idempotency-Key HTTP header; two OIDC hops result (push SA→function, function SA→Cloud Run) | [Pub/Sub push delivery](https://cloud.google.com/pubsub/docs/push) | Verified against docs (envelope/header behavior); transform topology is reference design |
| Invoking a Cloud Functions GEN2 function: the push-auth doc of record prescribes roles/cloudfunctions.invoker on the function for the push SA, granted via `gcloud functions add-invoker-policy-binding` (which for Gen2 adds the Cloud Run Invoker binding to the function's underlying Cloud Run service). Granting roles/run.invoker directly on that backing service is the equivalent lower-level form (Gen2 functions use the Cloud Run Invoker role); Gen1 uses roles/cloudfunctions.invoker on the function resource. The push subscription's OIDC audience must equal the function (transform) URL, not the Cloud Run /call URL. | [Pub/Sub authenticate push subscriptions](https://cloud.google.com/pubsub/docs/authenticate-push-subscriptions) + [Cloud Functions Gen2 authentication](https://cloud.google.com/functions/docs/securing/authenticating) + [gcloud functions add-invoker-policy-binding](https://cloud.google.com/sdk/gcloud/reference/functions/add-invoker-policy-binding) | Verified against docs |
| Pub/Sub push envelope format: {"message": {"data": "<base64>", "messageId": "...", "attributes": {...}}, "subscription": "..."} | [Pub/Sub push delivery](https://cloud.google.com/pubsub/docs/push) | Verified against docs |
| Pub/Sub message.data is standard base64 (RFC 4648 §4, with = padding) | [Pub/Sub push delivery](https://cloud.google.com/pubsub/docs/push) | Verified against docs |
| Pub/Sub messageId is stable across service-initiated redeliveries of the same message (same ID across redelivery attempts); a re-published message gets a NEW messageId. This is the entire correctness basis for the Tier-2 messageId fallback (distinguishes a safe fallback from the spec/45 W8 false-dedup hazard) | [Pub/Sub exactly-once delivery](https://cloud.google.com/pubsub/docs/exactly-once-delivery) | Verified against docs |

---

## What is in this directory vs. what is deferred

| Item | Status | Notes |
|---|---|---|
| `Dockerfile` | here | CMD runs `atomic-agents serve` (shipped, #342); non-root user (UID 10001) |
| `cloudrun-service.yaml` | here | Stateless Cloud Run service, ephemeral state annotations |
| `cloudscheduler-jobs.yaml.example` | here | OIDC auth, retry config, ingress topology options |
| `secret-manager-bootstrap.sh` | here | Idempotent provisioning |
| `cloud-logging-config.md` | here | Signal streams + audit trail reality |
| `cloud-monitoring-policies.yaml` | here | HTTP 402/500/503 + instance floor + DLQ depth alert (policy 5) |
| `iap-setup.md` | here | Perimeter setup, Cloud Armor pointer |
| `cloud-armor-rules.yaml.example` | here | Rate limit (IP + identity) + body-size + WAF starter |
| `compute-vm/` | here | Compute Engine VM with persistent disk, v0 stateful reference (one instance per tenant) |
| `pubsub-ingestion.yaml.example` | here | Pub/Sub push subscription + dead-letter topic + DLQ pull subscription provisioning (issue #390) |
| `pubsub-to-call-transform.py.example` | here | Cloud Function transform: Pub/Sub push envelope → /call body + Idempotency-Key header (issue #390) |
| `cloudtasks-queue.yaml.example` | here | Cloud Tasks queue provisioning + task creation pattern with Idempotency-Key header (issue #390) |
| Terraform module | deferred | Revisit on real community demand; gcloud upsert semantics cover the reference case without a maintainer-owned HCL module that drifts with GCP API changes |
| `cloudsql-bootstrap.sql` | deferred | PostgresLogBackend (#258 PR 1, shipped) self-provisions its schema on first connect - no manual DDL needed. |
| Full elastic scale-out | deferred | 4-phase path; see §Scale-out path |

---

## v0 deployment topology

**Cloud Run (stateless v0):**
```
   Cloud Scheduler ──OIDC──────────────────────────────────────┐
   Pub/Sub topic → Cloud Function (transform) ──OIDC──────────►│ (same-project: direct run.app URL)
   Cloud Tasks queue ──OIDC──────────────────────────────────► │
                                                               ▼
   user → Cloud Armor → IAP → Cloud Run (containerConcurrency=1, min-instances=1)
                               │  image = framework + baked config (immutable)
                               │  atomic-agents serve (non-root, UID 10001)
                               ├→ Vertex AI / Anthropic (LLM)   see §LLM backend
                               ├→ Cloud SQL                      run logs (activate PostgresLogBackend, #258)
                               ├→ Secret Manager                 secrets (env vars, or GCPSecretManagerBackend #340, shipped)
                               └→ Memorystore/Redis              locks (#60, shipped)

                  State surfaces: EPHEMERAL (container writable layer, lost on replacement)
                  - memory, goals, outcomes, journal, cascade, idempotency/: annotated in cloudrun-service.yaml
                  Cloud Logging + Cloud Monitoring (HTTP access logs + alerts + DLQ depth)
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
| Outcomes | Ephemeral (container writable layer) - OutcomeBackend Protocol shipped (LOCKED spec/42, FilesystemOutcomeBackend, #426 + #448 PR2 write-path adopted); durable once a cloud adapter ships | Issue #426 + #448 PR2 (Protocol shipped, write-path adopted); cloud adapter pending |
| Dreams | Ephemeral - operator-triggered dream artifacts at `<agent_root>/dreams/` (`atomic_agents/dream.py`); no Protocol yet; #383 (Phase 3) | Issue #383 |
| Journal | Ephemeral (container writable layer) - JournalBackend Protocol shipped (DRAFT spec/43, FilesystemJournalBackend, #427); only the filesystem impl is registered today, so durable once a cloud adapter ships | Issue #427 (Protocol shipped); cloud adapter pending |
| Cascade queue | Ephemeral - no Protocol yet; #383 + TENSIONS.md T4 (Phase 3) | Issue #383 |
| Mandate, Policy | Ephemeral - Postgres adapters tracked in #258 (Phase 3) | Issue #258 |
| Profile, tool-registry, corpus | Ephemeral - Postgres adapters tracked in #258 (Phase 3) | Issue #258 |
| Persona storage (`.personas/`, FilesystemPersonaBackend) | Ephemeral - gated on the PersonaBackend cloud adapter, #258 (Phase 3). This is persona-backend storage, NOT the cascade work-queue (that is the `Cascade queue` row above). | Issue #258 |
| Idempotency ledger (`idempotency/`, FilesystemDedupLedger) | Ephemeral (container writable layer) - spec/45, #520; LOST on container replacement. Redeliveries arriving after restart re-run. For cross-restart dedup: use Redis/Postgres IdempotencyBackend (follow-up issue). | Issue #520 (Protocol shipped); Redis/Postgres adapter pending |

**Compute Engine VM v0 (stateful) - see extras/gcp/compute-vm/:**

| Layer | v0 VM backend | Path to cloud backend |
|---|---|---|
| Config | Baked on disk (operator-deployed) | Never moves |
| Run logs | Persistent disk (FilesystemLogBackend) or PostgresLogBackend | Activate PostgresLogBackend for query |
| Locks | Redis (recommended) or FilesystemLockBackend | Redis already shipped |
| Memory | Persistent disk (FilesystemMemoryBackend) | Issue #382 (T5 wiring seam, shipped) AND issue #258 (Postgres memory adapter, Phase 2) |
| Goals, Outcomes, Journal | Persistent disk - GoalBackend (#425 + #448 PR1, DRAFT spec/41) + OutcomeBackend (#426 + #448 PR2, LOCKED spec/42) + JournalBackend (#427, DRAFT spec/43) Protocols shipped, filesystem reference impls | #425 + #426 + #427 + #448 (Protocols shipped, write-paths adopted); cloud adapters pending |
| Dreams | Persistent disk (no Protocol yet; dreams at `<agent_root>/dreams/`) | Issue #383 (Phase 3) |
| Cascade queue | Persistent disk (POSIX rename claim) | Issue #383 + TENSIONS.md T4 |
| Mandate, Policy, Profile, tool-registry, corpus | Persistent disk (Filesystem/SQLite backends) | Issue #258 (Phase 3) |
| Idempotency ledger (`idempotency/`, FilesystemDedupLedger) | Persistent disk (spec/45, #520); stale leases from crashed runs require manual removal (TTL sweep deferred; `supports_ttl=False`). See Troubleshooting for recovery command. | Issue #520 (Protocol shipped); Redis/Postgres adapter pending |

**Two groups (same for both topologies):**

- **Group A - Protocols exist, cloud adapter can be written once it ships:**
  logs (#258, shipped), memory (#382 override seam shipped; #258 Postgres
  adapter pending), goals (#425 + #448 PR1, DRAFT spec/41), outcomes (#426 + #448 PR2, LOCKED spec/42;
  write-path adopted), journal (#427, DRAFT spec/43), queue (#428, DRAFT spec/44), mandate,
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

## Managed ingestion — Pub/Sub + Cloud Tasks

This section covers the at-least-once managed ingestion patterns for
atomic-agents. See `pubsub-ingestion.yaml.example`, `pubsub-to-call-transform.py.example`,
and `cloudtasks-queue.yaml.example` for the provisioning commands.

### When to use Pub/Sub vs Cloud Tasks

| Capability | Pub/Sub push | Cloud Tasks |
|---|---|---|
| Requires transform | Yes (Cloud Function decodes base64 envelope) | No (task definition controls headers + body directly) |
| Idempotency-Key source | message.attributes.idempotency_key → messageId fallback | Bare task ID set in httpRequest.headers at task-creation time |
| Dead-letter queue | Built-in deadLetterPolicy | Not built-in; implement via error-tracking + re-queue |
| Retry backoff | retryPolicy.minimumBackoff / maximumBackoff | retryConfig.minBackoff / maxBackoff |
| Fan-out / broadcast | Yes (multiple subscriptions per topic) | No (point-to-point) |

### Architecture

```
[Publisher]──publish──► Pub/Sub topic
                              │
                              │ push
                              ▼
                       Cloud Function
                       (pubsub-to-call-transform.py.example)
                       - base64-decode message.data
                       - derive Idempotency-Key header
                       - POST /agents/<name>/call
                              │
                              ▼
                       Cloud Run (atomic-agents serve)
                              │
                              ▼
                        agent.call()
                       (dedup via FilesystemDedupLedger)

[Producer]──create-task──► Cloud Tasks queue
                                   │
                                   │ HTTP POST with Idempotency-Key header
                                   ▼
                            Cloud Run /agents/<name>/call
```

### At-least-once delivery and idempotency

Both Pub/Sub and Cloud Tasks deliver **at-least-once**: redelivery is expected,
not an error. The framework deduplicates per spec/45, but dedup is **opt-in**:

1. **Redelivery is expected.** Pub/Sub retries unacked messages; Cloud Tasks
   retries failed HTTP requests. Your agent MUST be idempotent in its external
   effects (database writes, emails sent), or you must wire Idempotency-Key.

2. **Dedup requires the Idempotency-Key header.** The framework reads the
   `Idempotency-Key` HTTP header (configured via `serve.md ## Idempotency Header`).
   It does NOT read idempotency_key from the JSON body, and it does NOT
   automatically extract the key from Pub/Sub envelope attributes.

3. **The operator-side transform is responsible for setting the header.**
   For Pub/Sub: the Cloud Function (`pubsub-to-call-transform.py.example`) must
   derive and set the header. For Cloud Tasks: set the header in
   `httpRequest.headers` at task-creation time. The transform forwards the
   decoded payload (`{work_item, ...}`) verbatim except that it **drops
   `critical`** — serve refuses `critical: true` with HTTP 422 (spec/37 MUST 5),
   so a publisher-supplied `critical` would poison every delivery. Publishers
   controlling the message body are subject to the same body-validation rules as
   any `/call` caller (see the Redelivery table's `critical` poison-field note).

4. **Absent header behavior depends on `dedup_body_hash_enabled`:**
   - **Default (`dedup_body_hash_enabled=False`):** absent header = no dedup =
     the agent runs unconditionally (intentional: dedup is opt-in per spec/37
     MUST 11 / the spec/45 opt-in posture — no header, no dedup. This is
     distinct from spec/45's fail-direction boundary for an UNREADABLE ledger
     marker: a tampered/unreadable per-key leaf fails **closed** — do-not-re-run,
     IN_FLIGHT for a lease leaf and COMPLETED for a terminal leaf, on both
     `begin()` and `lookup()` — and only a whole-ledger DIRECTORY escape on
     `lookup()` fails **soft** to FRESH. The absent-key case here is neither: it
     is simply "no dedup gate runs." See spec/45 §"Fail-closed vs fail-open
     boundary".)
   - **Enabled (`dedup_body_hash_enabled=True`) AND trigger in
     `{http, queue, cron}` (serve uses `http`):** when the header is ABSENT the
     framework AUTO-derives the key from
     `sha256(work_item + model + max_tokens + temperature)`
     (`agent.py` `_BODY_HASH_AUTO_DERIVE_TRIGGERS`). So a bit-identical
     redelivery — including a republished Pub/Sub message with a NEW `messageId`
     but identical body — IS deduped (returns a `text=''` replayed Response)
     even with no header. Header absence is the activation path here, not a
     blocker.

     **Caveat — the shipped Pub/Sub transform precludes this path.**
     `pubsub-to-call-transform.py.example` ALWAYS sets `Idempotency-Key` (from
     the publisher attribute, else `messageId`, which is effectively always
     present), so the no-header body-hash path never fires via the shipped
     transform — and because a republished message gets a NEW `messageId`, the
     explicit header wins and the message RE-RUNS rather than dedups. For
     cross-republish dedup use the explicit Tier-1 attribute (item 5 / the
     two-tier table below), not the body-hash path. The body-hash path applies
     only if you deliberately remove the `messageId` fallback or call `/call`
     from a source that sends no key.

5. **An explicit `Idempotency-Key` always wins (zero-override), on any
   trigger.** Body-hash auto-derivation is the no-header convenience path; it
   does NOT require the operator to also wire a header. If you want
   cross-republish dedup (stable across re-injection), set an explicit stable
   key — body-hash only dedups bit-identical bodies. (Source of truth:
   `agent.py` around the `dedup_body_hash_enabled` block.)

   **Precondition for the no-header body-hash path.** If you take item 4's
   advice and remove the transform's `messageId` fallback to force the
   no-header path, dedup ONLY fires if you ALSO set `dedup_body_hash_enabled=True`
   in `model.md`. The default is `False` = no dedup on an absent header (the call
   runs unconditionally). Verify both — the no-header path AND the `model.md`
   flag — before relying on body-hash dedup, or you get no dedup with no warning.

#### Idempotency-Key derivation — two tiers

**Pub/Sub:**

| Tier | Source | Stability | Use when |
|---|---|---|---|
| Stable (Tier 1) | `message.attributes["idempotency_key"]` | Stable across republish | Publisher controls the key; need exactly-once dedup even after re-injection |
| Delivery (Tier 2) | `message.messageId` | Stable only across SERVICE-initiated redeliveries of the same message; NEW id on republish AND on publisher-CLIENT publish retries | Default; at-least-once posture; republish re-runs the agent |

A republished message (re-injected from DLQ drain or operator action) gets a new
`messageId` — the agent re-runs if Tier 2 is used. Use Tier 1 (explicit
`idempotency_key` attribute) for cross-republish dedup.

**`messageId` stability is narrower than it looks.** Per the GCP exactly-once
doc, "multiple unique publishes by the Pub/Sub *service* ... lead to redeliveries
with the *same* message IDs" (the property Tier 2 relies on), BUT "multiple
unique publishes by the publishing *client*, across retries, lead to redeliveries
with *different* message IDs"
([exactly-once delivery](https://cloud.google.com/pubsub/docs/exactly-once-delivery)).
The Pub/Sub publisher client library has retries ON by default, so a transient
network blip during the ORIGINAL publish can create two messages with different
`messageId`s carrying the identical logical work item — and Tier 2 silently
re-runs the agent (no operator involved). Tier 2 dedups SERVICE-side
redeliveries; it does NOT dedup publisher-side publish duplicates. For any dedup
guarantee that must survive publish-time retries, use Tier 1 (an explicit
publisher-set `idempotency_key` attribute).

The `messageId` fallback is safe (no false-dedup hazard) because `messageId` is
GLOBALLY UNIQUE per publish (a republish — or a publisher retry — gets a new id)
— which is exactly the uniqueness property spec/45 W8 requires of any dedup key. W8 refuses the queue extractor's
`payload['idempotency_key']` → `payload['id']` fallback because work-item ids are
NOT globally unique across distinct items (→ false-dedup → a dropped real run);
`messageId` carries none of that hazard. (Tier 1's explicit attribute is still
the stronger guarantee — it also dedups across republish, which `messageId`
does not.)

**Cloud Tasks:**

Use the bare task ID (no slashes) as the Idempotency-Key. The task name is
unique within its queue for the Cloud Tasks best-effort dedup window (doc-literal
"up to 24 hours"; see the verification table). For multi-queue deployments,
prefix with the queue name
(`{queue-name}-{task-id}`) to avoid cross-queue key collisions.

**Critical: do NOT pass `X-CloudTasks-TaskName` directly as the
Idempotency-Key.** While X-CloudTasks-TaskName is already the SHORT name (no
slashes), the correct pattern is to set the key explicitly in the task definition
at creation time — not to derive it from injected headers at serve time. The
serve layer reads only the `Idempotency-Key` header, not `X-CloudTasks-TaskName`.

#### Redelivery behavior and audit trail

This table is keyed on the serve-layer HTTP response. The **Pub/Sub redelivery**
column describes what the transform (`pubsub-to-call-transform.py.example`) does
with that response; the **Cloud Tasks redelivery** column describes Cloud Tasks'
native retry behavior (no transform — the task hits `/call` directly).

| Run status (serve) | Idempotency outcome | Pub/Sub redelivery (via transform) | Cloud Tasks redelivery (direct) |
|---|---|---|---|
| ok (200) | Key committed (spec/45 W4) | Transform ACKs (204); done, no further delivery | 2xx = success; task deleted, no retry |
| deduped (200) | Not committed (was already committed) | Transform ACKs (204); same dedup outcome | 2xx = success; task deleted |
| in_flight (409) | Lease held by concurrent run | Transform NACKs (500); Pub/Sub redelivers after backoff until the run terminates, then dedups or re-runs | Non-2xx = retry; Cloud Tasks redelivers per `retryConfig` until the run terminates |
| skipped (402) | Lease not committed (pre-loop: never claimed; mid-loop: released) | Transform NACKs (500); redelivery re-runs and re-hits the cost gate. A MID-LOOP 402 already spent partial LLM cost on iterations 1..N-1 (non-zero `cost_usd` in the skipped JSONL record), so each redelivery re-spends that partial budget. 402 is deterministic only **within the redelivery window** (the cap is a rolling daily/monthly window per `_costs.py` — see note below), so within Pub/Sub's 5×≤600s budget it keeps recurring until `maxDeliveryAttempts`, then the message reaches the DLQ for inspection. To skip the budget burn, fast-route to the DLQ by ACKing (204) deterministic 402s with a structured error log in the transform — a deliberate tradeoff. | Non-2xx = retry; Cloud Tasks re-spends the same partial mid-loop budget on every attempt up to `maxAttempts`/`maxRetryDuration`. **There is no transform to ACK-route, and no DLQ:** once the budget is exhausted the task is silently DROPPED and the work is LOST (see Cloud Tasks note below). Route cost-sensitive work through Pub/Sub if you need a DLQ. |
| bad request (422) | Lease not committed (pre-loop refusal) | Transform NACKs (500); a malformed/poison body (incl. publisher-supplied `critical: true`, see note) re-hits the same 422 every delivery until DLQ | Non-2xx = retry; a poison body re-hits 422 until `maxAttempts`, then the task is silently DROPPED (no DLQ) |
| lock_busy (503) | Lease not claimed | Transform NACKs (500); **transient** — redelivery after backoff usually succeeds. A persistent 503 storm signals lock-pool saturation, not a poison message (reduce concurrency, see Cloud Tasks `maxConcurrentDispatches`) | Non-2xx = retry; same transient semantics — back off and retry |
| ESCALATE/deferred (200) | Lease released (spec/45 W4: deferred MUST NOT commit) | Transform ACKs (204); the Pub/Sub message is consumed and is **NOT automatically redelivered** (an ACK ends delivery — there is no auto-redelivery to re-process the work item). The escalation is NOT lost: it is durably enqueued in the agent's escalation/proposal queue (JSONL `escalation_queue_id` + PENDING files) and surfaced via the dashboard — that, NOT redelivery, is the surfacing mechanism. Re-processing the original work item after a human resolves the escalation requires a deliberate operator re-publish (use a Tier-1 explicit `idempotency_key` so the re-publish re-runs predictably; serve returns 200/`status:"ok"` for both ok AND deferred, so the transform cannot distinguish them — ACK is intentional). | 2xx = success to Cloud Tasks (task deleted); the task is NOT automatically re-enqueued. Same as Pub/Sub: the escalation is durably enqueued in the agent's escalation queue; re-processing the original work item requires a deliberate operator re-enqueue. |

**402 / cost-cap determinism note:** the cost cap is a *rolling* daily
(`daily_cap_usd`) / monthly (`monthly_cap_usd`) window — `sum_cost_for_period`
sums spend over `today` / `this_month` (`_costs.py`), not a fixed per-request
cap. A 402 is therefore deterministic only *within the active retry window*: a
delivery that lands after the daily window rolls (or the monthly cap frees) could
succeed. For Pub/Sub's 5×≤600s budget the retries stay inside the day, so the
402 keeps recurring until `maxDeliveryAttempts`.

**Cloud Tasks has no DLQ — exhausted tasks are dropped:** unlike the Pub/Sub
DLQ path, a Cloud Tasks task dropped after `maxAttempts`/`maxRetryDuration`
produces **no terminal GCP-side artifact** for operator inspection. For a task
that never produced a successful run (every attempt got 402/422), the only
record is the N per-attempt JSONL `skipped`/`error` lines — there is no
"this was the terminal drop" marker on the GCP side. Correlate the per-attempt
JSONL records by `Idempotency-Key` (each redelivery shares the bare task ID) and
treat the attempt at `X-CloudTasks-TaskRetryCount == maxAttempts - 1` as the
terminal drop. For inspectable terminal failures, prefer the Pub/Sub
`deadLetterPolicy` path.

**`critical` is a poison field (both paths):** the serve layer hard-refuses
`critical: true` in the body with HTTP 422 (spec/37 MUST 5 — the cost guardrail
cannot be bypassed from the network). A publisher/producer that puts
`critical: true` in the work payload produces a deterministic 422 on every
delivery and burns the full retry budget. The Pub/Sub transform drops `critical`
before forwarding (matching serve's silently-ignored-unless-true posture); for
Cloud Tasks, ensure your producer never sets `critical` in the body. Other
non-true `critical` values are silently ignored by serve.

For redelivered messages: the JSONL log shows multiple records sharing the same
`idempotency_key` value. The first has `status='ok'`; subsequent redeliveries
have `status='deduped'`. Filter on `idempotency_key` to correlate all runs.

Pub/Sub and Cloud Tasks deliver via OIDC SA identity (Cloud Tasks → `/call`
directly; Pub/Sub → the transform → `/call`), not via IAP, so
ingestion-triggered runs have **no `http_caller` field** in the JSONL audit
record (same behavior as Cloud Scheduler). Distinguish ingestion runs from
interactive runs by filtering on `trigger='http'` combined with absence of
`http_caller`, or correlate with Cloud Logging delivery records.

### Pub/Sub ingestion runbook

> **Ingress recommendation.** The "same-project push to the default run.app URL
> is admitted as internal traffic" claim is doc-literal but never dogfooded (it
> is the highest-residual-risk claim in this directory — same class as the #395
> false-positive). If you have not personally confirmed same-project internal
> admission on your project, make topology **(b)** [route push through the
> external ALB / IAP] your default rather than topology (a); (a) can silently
> return 403/404 on every push. See `pubsub-ingestion.yaml.example
> §INGRESS RECONCILIATION` for both options.

**Prerequisites:**
1. A deployed `atomic-agents serve` Cloud Run service
2. A Cloud Function running `pubsub-to-call-transform.py.example` with env vars:
   - `CLOUD_RUN_CALL_URL` = `https://YOUR_SERVICE_URL/agents/YOUR_AGENT/call`
   - `CLOUD_RUN_SERVICE_URL` = `https://YOUR_SERVICE_URL` (no trailing slash)
3. Resources provisioned per `pubsub-ingestion.yaml.example` in order:
   - Dead-letter topic → DLQ pull subscription → source topic → push subscription
   - IAM grants 1-4 (both push SA + Pub/Sub service agent grants)

**Checklist:**

- [ ] DLQ pull subscription exists on the dead-letter topic (required for alerts + message retention)
- [ ] HOP 1: Push SA has `roles/cloudfunctions.invoker` on the TRANSFORM Cloud Function (`gcloud functions add-invoker-policy-binding`; equivalently `roles/run.invoker` on the function's backing Cloud Run service)
- [ ] HOP 2: Transform runtime SA has `roles/run.invoker` on the agent Cloud Run service (lets the transform call `/call`)
- [ ] Pub/Sub service agent has `roles/iam.serviceAccountTokenCreator`
- [ ] Pub/Sub service agent has `roles/pubsub.publisher` on DLQ topic
- [ ] Pub/Sub service agent has `roles/pubsub.subscriber` on push subscription
- [ ] `ackDeadlineSeconds=600` (maximum) and Cloud Run `--timeout >= 600s`
- [ ] Cloud Function `--timeout >= 620s` (>= the transform's `requests` timeout >= `ackDeadlineSeconds`); default is 60s — see runbook prerequisites
- [ ] Push subscription OIDC audience = the TRANSFORM function URL (no trailing slash); the transform→`/call` hop audience = the Cloud Run service URL (set inside the transform)
- [ ] DLQ alert in `cloud-monitoring-policies.yaml` references the DLQ pull subscription name

### Cloud Tasks ingestion runbook

**Prerequisites:**
1. A deployed `atomic-agents serve` Cloud Run service
2. Cloud Tasks queue per `cloudtasks-queue.yaml.example`
3. Cloud Tasks SA has `roles/run.invoker` on Cloud Run service

**Checklist:**

- [ ] `maxConcurrentDispatches=1` (matches Cloud Run `containerConcurrency=1`)
- [ ] Task definition sets `Idempotency-Key: <bare-task-id>` in `httpRequest.headers`
- [ ] OIDC token audience = Cloud Run service URL (no trailing slash)
- [ ] Task ID is alphanumeric with no slashes (the serve-layer Idempotency-Key validation rejects `/` with HTTP 422)
- [ ] Task ID passed POSITIONALLY to `gcloud tasks create-http-task` (enables the best-effort name-dedup window — doc-literal "up to 24 hours"; see the verification table; there is no `--task-name` flag)

### DLQ depth alert

The DLQ depth alert (policy 5 in `cloud-monitoring-policies.yaml`) fires when
`num_undelivered_messages > 0` on the DLQ pull subscription. Before relying on
this alert, confirm the DLQ pull subscription exists:

```bash
gcloud pubsub subscriptions list --filter="topic:<dlq-topic-name>"
```

The alert targets the **pull subscription on the dead-letter topic**, NOT the
original push subscription or the dead-letter topic itself. Adjust the
`subscription_id` filter in the alert to match your DLQ pull subscription name.

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
| **Phase 3** | Peel the rest: goals (#425 + #448 PR1, DRAFT spec/41) + outcomes (#426 + #448 PR2, LOCKED spec/42) + journal (#427, DRAFT spec/43) + queue (#428, DRAFT spec/44) Protocols shipped — cloud adapters pending. Mandate, policy, profile, tool-registry, corpus, and persona storage (`.personas/`) to Postgres adapters (#258). | Issue #258 + #425 + #426 + #427 + #428 + TENSIONS.md T13 |
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

**Pub/Sub push messages going to DLQ immediately (403/401 errors):**
Check IAM grants. The push endpoint is the TRANSFORM Cloud Function (two-hop
topology), so the grants are (see `pubsub-ingestion.yaml.example` §OIDC PUSH AUTH):
1. HOP 1: Push SA → `roles/cloudfunctions.invoker` on the TRANSFORM function
   (`gcloud functions add-invoker-policy-binding`; equivalently `roles/run.invoker`
   on the function's backing Cloud Run service)
2. HOP 2: Transform runtime SA → `roles/run.invoker` on the agent Cloud Run service
3. Pub/Sub service agent → `roles/iam.serviceAccountTokenCreator`

A 401 (OIDC token rejected) means an audience mismatch — verify the push
subscription's OIDC audience matches the TRANSFORM function URL exactly (no
trailing slash). (The transform→`/call` hop audience is the Cloud Run service
URL, set inside the transform.)

**DLQ alert never fires despite confirmed dead-lettered messages:**
Verify the dead-letter topic has an active pull subscription:
```bash
gcloud pubsub subscriptions list --filter="topic:<dlq-topic-name>"
```
The `num_undelivered_messages` metric is a subscription-level metric. A topic
with no subscription emits no data points; the alert never fires. Also verify
the alert's `subscription_id` filter matches the pull subscription's name exactly.

**Pub/Sub redeliveries always return HTTP 409 on the VM topology:**
A stale IN_FLIGHT lease is wedging the idempotency key. This happens when the
Cloud Run container or VM process received SIGKILL while `agent.call()` held
an idempotency lease — the `try/finally` release never ran. Recovery:
```bash
# Find stale leases
ls <agent_root>/idempotency/*.lease.json

# Remove the wedged lease (safe — a stale lease is always from a run
# that never completed; commit() unlinks the lease file on success)
rm <agent_root>/idempotency/<key_hash>.lease.json
```
The next delivery will see FRESH and re-run. Note: FilesystemDedupLedger has
no TTL sweep (spec/45 `supports_ttl=False`); stale leases require manual
recovery on the VM topology. On stateless Cloud Run, container replacement
auto-clears the ephemeral ledger.

**Cloud Tasks deliveries return HTTP 422 (invalid Idempotency-Key):**
The Idempotency-Key header value contains `/` (path separator). The serve-layer
Idempotency-Key validation (in `serve/_app.py`, a strict superset of the
backend's `_validate_key` — it additionally rejects backslash) rejects
slash-bearing keys with HTTP 422. Ensure the task definition sets
`Idempotency-Key` to the BARE TASK ID (last path segment, no slashes), not the
full Cloud Tasks resource path
(`projects/P/locations/L/queues/Q/tasks/TASK_ID`).

**Cloud Function transform returns 500 / Pub/Sub keeps redelivering:**
Check that `CLOUD_RUN_CALL_URL` and `CLOUD_RUN_SERVICE_URL` env vars are set on
the Cloud Function. The transform must call `CLOUD_RUN_CALL_URL` (the /call
endpoint) and authenticate to `CLOUD_RUN_SERVICE_URL` (the audience for the
OIDC token). If these are swapped, the token audience will not match and every
request returns 401.
