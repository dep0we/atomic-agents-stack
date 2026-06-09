# IAP perimeter setup for atomic-agents on Cloud Run

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

Set the Cloud Run ingress to `internal-and-cloud-load-balancing` so the
container is only reachable through the load balancer (and therefore IAP):

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
Scheduler — see `cloudscheduler-jobs.yaml.example`).

---

## What the framework does with the identity header

IAP injects `X-Goog-IAP-JWT-Assertion` on every authenticated request. The
framework reads the raw header value and passes it through into the JSONL audit
record as `http_caller`. **The framework never decodes or verifies the header —
IAP is the trust boundary.**

The full contract is in `docs/spec/37-serve.md` MUSTs 6 and 7.

---

## Cloud Armor (rate limiting + WAF)

Attach a Cloud Armor security policy to the backend service to add rate
limiting and basic WAF rules in front of IAP:

```bash
# Create a policy with a rate limit (example: 10 req/s per IP)
gcloud compute security-policies create <your-agent-name>-armor \
  --description="atomic-agents rate limit + WAF"

gcloud compute security-policies rules create 1000 \
  --security-policy=<your-agent-name>-armor \
  --expression="true" \
  --action=rate-based-ban \
  --rate-limit-threshold-count=10 \
  --rate-limit-threshold-interval-sec=1 \
  --ban-duration-sec=60

# Attach to the backend service
gcloud compute backend-services update <your-agent-name>-backend \
  --global \
  --security-policy=<your-agent-name>-armor
```

See `cloud-armor-rules.yaml.example` for a pre-built policy template.
