# iap-setup.md - IAP perimeter setup for atomic-agents on Cloud Run
#
# NOT YET DOGFOODED against the live GCP platform.
# Claims verified against GCP documentation as of 2026-06-09.
# See extras/gcp/README.md §External-claim verification status.

Identity-Aware Proxy (IAP) is the trust boundary for this deployment. IAP
authenticates callers before requests reach the Cloud Run container; the
framework never re-verifies the identity header. See `docs/spec/37-serve.md`
MUSTs 6 and 7 for the full identity contract, and
`docs/deployment/serve.md §IAP / ALB / Cloudflare Access / Tailscale Serve
pass-through` for how the serve layer handles the header.

---

## What the operator configures in GCP

### 1. Enable IAP on the Cloud Run backend service

IAP for Cloud Run requires a load balancer in front of the service:

```bash
# Create a serverless NEG pointing at your Cloud Run service
gcloud compute network-endpoint-groups create <your-agent-name>-neg \
  --region=<region> \
  --network-endpoint-type=serverless \
  --cloud-run-service=<your-agent-name>

# Create a backend service
gcloud compute backend-services create <your-agent-name>-backend \
  --global \
  --load-balancing-scheme=EXTERNAL_MANAGED

# Add the NEG to the backend service
gcloud compute backend-services add-backend <your-agent-name>-backend \
  --global \
  --network-endpoint-group=<your-agent-name>-neg \
  --network-endpoint-group-region=<region>

# Enable IAP on the backend service
gcloud compute backend-services update <your-agent-name>-backend \
  --global \
  --iap=enabled
```

### 2. Restrict direct Cloud Run access

Set the Cloud Run ingress to `internal-and-cloud-load-balancing`. Per the
ingress docs, this setting allows two source classes: (1) everything the more
restrictive `internal` setting allows, PLUS (2) traffic through an external
Application Load Balancer. The list of Google Cloud internal-traffic sources
(Cloud Scheduler, Cloud Tasks, Pub/Sub, Eventarc, Workflows, BigQuery, and
others, when same-project or same VPC-SC perimeter) is defined under the
`internal` setting and is therefore inherited here as the "(1)" class.
(Verified: the doc enumerates these sources for `internal` and states
`internal-and-cloud-load-balancing` = "resources allowed by the more restrictive
internal setting" + External Application Load Balancer, at
https://cloud.google.com/run/docs/securing/ingress)

The doc-literal reading is therefore that a same-project Cloud Scheduler job
hitting the default run.app URL is admitted as internal traffic under
`internal-and-cloud-load-balancing`. There is one operational caveat the docs do
not spell out crisply: the Google-internal-source recognition is most clearly
documented under the bare `internal` value, so on some projects operators report
needing the bare `internal` ingress (or the LB/IAP path) for direct
Scheduler-to-run.app to work. This is not a guarantee either way - confirm on
your project.

The two reliable topologies, in priority order (named, not lettered, so the
labels cannot invert against `cloudscheduler-jobs.yaml.example`):
- **via-LB-IAP: keep `internal-and-cloud-load-balancing` and route Scheduler
  through the external load balancer / IAP** - works unconditionally, carries the
  IAP identity header, and is the recommended path under this ingress value.
- **direct-internal: set ingress to `internal` (not
  `internal-and-cloud-load-balancing`)** if you specifically want same-project
  Scheduler to reach run.app directly as internal traffic with no LB - this is
  the value under which the internal-source list is unambiguously documented.

If you use a custom domain, or your Scheduler jobs are in a different GCP project
(cross-perimeter), you MUST use the via-LB-IAP topology - route them through the
load balancer / IAP.

```bash
gcloud run services update <your-agent-name> \
  --region=<region> \
  --ingress=internal-and-cloud-load-balancing
```

This matches the `run.googleapis.com/ingress` annotation in
`cloudrun-service.yaml`.

### 3. Grant IAP-secured Web App User role

Only principals with `roles/iap.httpsResourceAccessor` on the backend service
can reach the Cloud Run service through IAP:

```bash
gcloud iap web add-iam-policy-binding \
  --resource-type=backend-services \
  --service=<your-agent-name>-backend \
  --member=user:someone@example.com \
  --role=roles/iap.httpsResourceAccessor
```

Grant to a group or service account for non-interactive callers (e.g., Cloud
Scheduler calling via the LB/IAP path - this is the via-LB-IAP topology here,
which corresponds to option (b) "route Scheduler through the external load
balancer / IAP" in `cloudscheduler-jobs.yaml.example §INGRESS RECONCILIATION`).

---

## What the framework does with the identity header

IAP injects `X-Goog-IAP-JWT-Assertion` on every authenticated request. The
framework reads the raw header value and passes it through into the JSONL audit
record as `http_caller`. **The framework never decodes or verifies the header -
IAP is the trust boundary.**

The full contract is in `docs/spec/37-serve.md` MUSTs 6 and 7.

---

## Cloud Armor (rate limiting + WAF)

Attach a Cloud Armor security policy to the backend service to add rate
limiting and basic WAF rules in front of IAP.

For a full policy template including:
- Per-IP rate limiting (works on any topology)
- Per-identity rate limiting, keyed on X-Goog-Authenticated-User-Email - but see
  the ORDERING CAVEAT below: it works ONLY on the classic ALB, NOT on the global
  external ALB this guide configures
- Body-size limit (OOM guard, issue #401)
- XSS WAF rules

...use `cloud-armor-rules.yaml.example`. It is more complete than the inline
example below. See that file for tier requirements and the aggregate spend
control note.

**ORDERING CAVEAT (load-balancer type decides whether per-identity limiting
works).** Step 1 above creates a GLOBAL EXTERNAL ALB
(`--load-balancing-scheme=EXTERNAL_MANAGED --global`). On that load-balancer
type, Cloud Armor evaluates BEFORE IAP, so IAP has not yet injected
`X-Goog-Authenticated-User-Email` when Cloud Armor runs - the per-identity Rule
900 sees no identity key and is a no-op (it is scoped with `has(...)` so it does
not collaterally ban header-less traffic). Per-identity limiting works only on a
CLASSIC ALB (`--load-balancing-scheme=EXTERNAL`), where IAP evaluates first and
the header is present. On the global external ALB, rely on the per-IP rule plus
the billing budget (README §"Cost controls and billing budget") for spend
control. (Verified: "For a backend service of a global external Application Load
Balancer, Cloud Armor evaluation happens first ... If Cloud Armor allows a
request, IAP then evaluates the request"; the classic ALB evaluates IAP first, at
https://cloud.google.com/armor/docs/integrating-cloud-armor)

Quick-start (create and attach the policy):
```bash
gcloud compute security-policies create <your-agent-name>-armor \
  --description="atomic-agents rate limit + WAF"

# Attach to the backend service
gcloud compute backend-services update <your-agent-name>-backend \
  --global \
  --security-policy=<your-agent-name>-armor
```

Then apply the rules from `cloud-armor-rules.yaml.example`. The file is a
reference template, not a turnkey import artifact: it interleaves explanatory
NOTE/comment blocks and shows rule intent using illustrative action forms
(`deny(403)`, `rate_based_ban`), so it is not guaranteed to round-trip cleanly
through `gcloud compute security-policies import` (which expects the full
exported security-policy resource shape). The verified-safe path is to translate
each rule into a `gcloud compute security-policies rules create` invocation (or
enter them in the GCP console) using the per-rule fields documented in that file.
If you do assemble a single import file, validate it against the live
`gcloud ... import` schema first - that single-file shape was not dogfooded.
