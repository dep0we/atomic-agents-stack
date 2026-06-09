#!/usr/bin/env bash
# secret-manager-bootstrap.sh — provision GCP Secret Manager secrets for
# atomic-agents-stack on Cloud Run.
#
# IMPORTANT — READ BEFORE RUNNING:
#
# GCPSecretManagerBackend does NOT yet exist. It ships at issue #340 PR 2.
# Until #340 PR 2 lands, the framework uses FilesystemSecretBackend, which
# probes environment variables first (FilesystemSecretBackend.resolve_with_spec
# in atomic_agents/secret_backend/filesystem.py). This script provisions
# secrets into Secret Manager for when #340 PR 2 lands; the values are
# immediately usable TODAY by injecting them as Cloud Run env vars via
# --set-secrets (see "Wire secrets to Cloud Run" section below).
#
# Do NOT set ATOMIC_AGENTS_SECRET_BACKEND=gcp in any Cloud Run env section.
# Setting that env var today raises SecretBackendNotRegistered when the agent
# resolves a provider credential (at the first LLM call). The cost gate is
# unaffected — it runs before key resolution and is always honored; the call
# simply fails closed at credential resolution with a backend-not-registered
# diagnostic naming the gcp extra and PR 2 of issue #340.
#
# When #340 PR 2 ships, replace the --set-secrets section with:
#   --set-env-vars ATOMIC_AGENTS_SECRET_BACKEND=gcp,\
#                  ATOMIC_AGENTS_SECRET_BACKEND_URL=projects/PROJECT_ID/secrets/
#
# SAFE TO RE-RUN: the script guards each create with a describe check so
# re-running after a partial failure leaves secrets in a consistent state.
#
# USAGE:
#   export PROJECT_ID=your-gcp-project
#   export ANTHROPIC_API_KEY=sk-ant-...
#   export REDIS_URL=redis://...
#   bash secret-manager-bootstrap.sh

set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID}"
: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY}"

# Optional — only required if using Redis lock backend (#60).
REDIS_URL="${REDIS_URL:-}"

# Optional — only required when PostgresLogBackend (#258) ships.
POSTGRES_URL="${POSTGRES_URL:-}"

# ── Helper: create a secret idempotently then add a version ──────────────────
# Ordering: (1) create shell, (2) add version, (3) grant IAM.
# This ordering matters: a secret with no version returns NOT_FOUND (not
# PERMISSION_DENIED) when accessed, which gives a confusing doctor error.
# Ensure the version exists before granting the binding.
upsert_secret() {
  local name="$1"
  local value="$2"

  if gcloud secrets describe "$name" --project="$PROJECT_ID" &>/dev/null; then
    echo "Secret $name already exists — adding new version."
  else
    echo "Creating secret $name."
    gcloud secrets create "$name" \
      --project="$PROJECT_ID" \
      --replication-policy="automatic"
  fi

  # printf '%s' (not echo) so the stored secret byte-matches the credential.
  # echo appends a trailing newline; the future GCPSecretManagerBackend
  # (#340 PR 2) reads the stored bytes directly, and a trailing \n is a classic
  # auth-failure footgun. (The env-var fallback path today is unaffected:
  # FilesystemSecretBackend .strip()s env values.)
  printf '%s' "$value" | gcloud secrets versions add "$name" \
    --project="$PROJECT_ID" \
    --data-file=-
  echo "Version added to $name."
}

# ── Provision secrets ─────────────────────────────────────────────────────────
upsert_secret "anthropic-api-key" "$ANTHROPIC_API_KEY"

if [[ -n "$REDIS_URL" ]]; then
  upsert_secret "redis-url" "$REDIS_URL"
fi

if [[ -n "$POSTGRES_URL" ]]; then
  upsert_secret "postgres-url" "$POSTGRES_URL"
fi

# ── Grant Cloud Run service account accessor role ────────────────────────────
# ORDERING: IAM binding comes AFTER version creation (see comment above).
# If you change SA_EMAIL, the binding must be re-applied.
: "${SA_EMAIL:?Set SA_EMAIL to the Cloud Run service account email}"

grant_accessor() {
  local name="$1"
  gcloud secrets add-iam-policy-binding "$name" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/secretmanager.secretAccessor"
  echo "Granted secretAccessor on $name to $SA_EMAIL."
}

grant_accessor "anthropic-api-key"
if [[ -n "$REDIS_URL" ]]; then grant_accessor "redis-url"; fi
if [[ -n "$POSTGRES_URL" ]]; then grant_accessor "postgres-url"; fi

# ── Wire secrets to Cloud Run (env-var injection, NOT ATOMIC_AGENTS_SECRET_BACKEND=gcp) ──
echo ""
echo "=== NEXT STEP: inject secrets as Cloud Run env vars ==="
echo ""
echo "Until #340 PR 2 ships, reference secrets as Cloud Run env vars:"
echo ""
echo "  gcloud run services update <your-agent-name> \\"
echo "    --region=<your-region> \\"
echo "    --project=$PROJECT_ID \\"
echo "    --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest \\"
if [[ -n "$REDIS_URL" ]]; then
  echo "    --set-secrets ATOMIC_AGENTS_LOCK_BACKEND_URL=redis-url:latest \\"
fi
if [[ -n "$POSTGRES_URL" ]]; then
  echo "    --set-secrets ATOMIC_AGENTS_LOG_BACKEND_URL=postgres-url:latest \\"
fi
echo ""
echo "The FilesystemSecretBackend reads env vars transparently."
echo "The cost gate and audit trail remain intact."
echo ""
echo "# NOTE: GCPSecretManagerBackend ships with issue #340 PR 2."
echo "# Until then, Secret Manager values are injected as Cloud Run env vars"
echo "# via --set-secrets; FilesystemSecretBackend reads them automatically."
echo ""
echo "Troubleshooting tip:"
echo "  If doctor reports FAIL for ANTHROPIC_API_KEY with 'not found':"
echo "  confirm the secret VERSION was added, not just the secret shell:"
echo "  gcloud secrets versions list anthropic-api-key --project=$PROJECT_ID"
echo "  A secret with no active version returns NOT_FOUND, not PERMISSION_DENIED."
