# Secret Backend: Deployment Guide

## The filesystem backend is the permanent default

`FilesystemSecretBackend` is the default and permanent storage option. It reads
credentials from three machine-scoped sources in fixed priority order:

1. Environment variables (e.g. `ANTHROPIC_API_KEY`)
2. macOS Keychain (`security find-generic-password`)
3. `~/.config/atomic_agents/keys.json`

**No configuration is required for this shape.** If you set provider API keys via
environment variables (the most common setup), nothing changes. The filesystem
backend is fully supported for local development, home deployments, and any
server where env vars or the machine Keychain hold credentials.

To verify your current backend:

```bash
atomic-agents secrets validate
```

To check whether a specific key resolves:

```bash
atomic-agents secrets which ANTHROPIC_API_KEY
atomic-agents secrets check ANTHROPIC_API_KEY
```

---

## Optional: GCP Secret Manager for cloud deployments

GCP Secret Manager is the first opt-in remote backend. Use it when your agent
runs on Google Cloud (Cloud Run, GKE, Compute Engine) and you want credentials
managed centrally in Secret Manager rather than injected as env vars.

This is a configuration choice, not an upgrade. The filesystem backend remains
fully supported. There is no requirement to migrate.

### Prerequisites

1. A GCP project with the Secret Manager API enabled.
2. Secrets created under `projects/<project_id>/secrets/` with names matching
   the lowercased, hyphenated form of each key:
   - `ANTHROPIC_API_KEY` → secret name `anthropic-api-key`
   - `OPENAI_API_KEY` → secret name `openai-api-key`
   - `REDIS_URL` → secret name `redis-url`
3. Authentication via Application Default Credentials (ADC): either
   `gcloud auth application-default login` (local dev) or a service account
   with the `secretmanager.secretAccessor` role (Cloud Run / GKE).

### Install the `[gcp]` extra

```bash
uv add 'atomic-agents-stack[gcp]'
```

This installs `google-cloud-secret-manager>=2.16`. The SDK is lazy-imported;
it does not load at base package startup.

### Configure via env vars

```bash
export ATOMIC_AGENTS_SECRET_BACKEND=gcp
export ATOMIC_AGENTS_SECRET_BACKEND_URL=projects/<your-project-id>/secrets
```

On Cloud Run, set these as environment variables in your service configuration.
On GKE, set them in your Pod spec or via a ConfigMap.

### Creating secrets

The key-to-secret-name mapping is fixed: `key.lower().replace('_', '-')`.

```bash
# Create a secret (first time only)
gcloud secrets create anthropic-api-key --replication-policy=automatic

# Add a secret version (use printf to avoid trailing newline)
printf '%s' 'sk-ant-...' | gcloud secrets versions add anthropic-api-key --data-file=-
```

Using `printf '%s'` instead of `echo` is important: `echo` appends a trailing
newline which causes silent authentication failures in provider SDKs. The
backend strips trailing whitespace, but using `printf` is the correct practice.

### Verify the GCP backend

```bash
atomic-agents doctor --agent <name>   # runs check_gcp_secret_backend(): ADC liveness probe
atomic-agents secrets validate
atomic-agents secrets which ANTHROPIC_API_KEY
```

Note: the `secret-backend` check (and its GCP ADC probe) runs only when
`--agent` is supplied; bare `atomic-agents doctor` reports `secret-backend` as
SKIPPED. Without an agent, use `atomic-agents secrets validate` to confirm the
backend instantiates.

The `check_gcp_secret_backend()` doctor check performs a non-billable ADC
liveness probe (`credentials.refresh(Request())`) and warns when
`GOOGLE_CLOUD_PROJECT` is not set (safe to ignore on Cloud Run / GKE where the
project is resolved from the metadata server automatically).

### Rotation

Each `get()` call resolves the `latest` secret version live. To rotate a
credential:

1. Add a new version to the secret in Secret Manager:
   ```bash
   printf '%s' 'sk-ant-new-key' | gcloud secrets versions add anthropic-api-key --data-file=-
   ```
2. The next `get()` call picks it up automatically. No process restart required.

### Cost and latency of the no-cache contract

Live rotation has a cost: each `get()` performs an uncached
`access_secret_version` RPC against Secret Manager. Provider credentials are
resolved once per LLM iteration, so an `agent.call()` with N tool iterations
issues on the order of N+1 billable Secret Manager accesses, each adding gRPC
round-trip latency to the iteration. This is intentional: `supports_rotation=True`
(spec/38 MUST 9) means the backend never caches, so a rotated secret is always
visible on the very next call. The LLM-spend cost gate is unaffected (it runs
before key resolution). Budget Secret Manager
[access quota](https://cloud.google.com/secret-manager/quotas) accordingly. If a
deployment cannot tolerate per-iteration access, prefer approach (1): injecting
secrets as Cloud Run env vars resolved through the default filesystem backend,
which reads them once at process start.

### URL format reference

`ATOMIC_AGENTS_SECRET_BACKEND_URL` must be the resource path prefix:

```
projects/<project_id>/secrets
```

Example: `projects/my-gcp-project-123/secrets`

The backend appends `/<secret_name>/versions/latest` for each `get()` call.

---

## Switching back to filesystem

To revert to the filesystem backend, unset the env vars:

```bash
unset ATOMIC_AGENTS_SECRET_BACKEND
unset ATOMIC_AGENTS_SECRET_BACKEND_URL
```

No data migration is needed; the filesystem backend reads from env vars,
Keychain, and `keys.json` exactly as before.
