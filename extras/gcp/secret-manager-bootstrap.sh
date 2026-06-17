#!/usr/bin/env bash
# secret-manager-bootstrap.sh - provision GCP Secret Manager secrets for
# atomic-agents-stack on Cloud Run.
#
# NOT YET DOGFOODED against the live GCP platform.
# Claims verified against GCP documentation as of 2026-06-09.
# See extras/gcp/README.md §External-claim verification status.
#
# IMPORTANT - READ BEFORE RUNNING:
#
# GCPSecretManagerBackend is shipped (spec/38 LOCKED at #340 PR 2).
# This script provisions secrets into Secret Manager. Once provisioned,
# you have two supported delivery paths - choose one:
#
# PATH 1 (simplest) - inject secrets as Cloud Run env vars.
# FilesystemSecretBackend (the default) probes env vars first, so no
# ATOMIC_AGENTS_SECRET_BACKEND selection is needed. See "Wire secrets to
# Cloud Run" below for the full --set-secrets command this script prints,
# built from the env vars you provided.
#
# PATH 2 - select GCPSecretManagerBackend for live resolution at call time.
# Each credential is fetched from Secret Manager uncached on every LLM
# iteration, so secret rotation is visible on the next run with no redeploy.
# Requires the [gcp] extra and both env vars:
#
#   pip install "atomic-agents-stack[gcp]"
#
#   gcloud run services update <your-agent-name> \
#     --set-env-vars ATOMIC_AGENTS_SECRET_BACKEND=gcp \
#     --set-env-vars ATOMIC_AGENTS_SECRET_BACKEND_URL=projects/<your-project-id>/secrets
#
# Key-to-secret-name mapping: KEY.lower().replace('_', '-')
# e.g. ANTHROPIC_API_KEY -> anthropic-api-key (always resolved at 'latest').
# See docs/deployment/secret-backend.md for the full backend selection guide.
#
# SAFE TO RE-RUN: the script guards each create with a describe check so
# re-running after a partial failure leaves secrets in a consistent state.
# The grant_accessor function calls add-iam-policy-binding, which is idempotent:
# adding the same member+role binding twice is a no-op on the policy. Re-runs
# after partial failure (e.g., create succeeded but version-add failed) are
# always safe - the re-run re-adds the version and grants the binding, which is
# the intended recovery path.
#
# USAGE:
#   export PROJECT_ID=your-gcp-project
#   export ANTHROPIC_API_KEY=sk-ant-...
#   export REDIS_URL=redis://...
#   bash secret-manager-bootstrap.sh

set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID}"
: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY}"

# Optional - only required if using Redis lock backend (#60).
REDIS_URL="${REDIS_URL:-}"

# Optional - only required if using PostgresLogBackend (#258, shipped).
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
    echo "Secret $name already exists - adding new version."
  else
    echo "Creating secret $name."
    gcloud secrets create "$name" \
      --project="$PROJECT_ID" \
      --replication-policy="automatic"
  fi

  # printf '%s' (not echo) so the stored secret byte-matches the credential.
  # echo appends a trailing newline; both GCPSecretManagerBackend and
  # FilesystemSecretBackend .strip() the resolved value, but a trailing \n
  # stored in Secret Manager is a classic auth-failure footgun for any caller
  # that reads the raw bytes directly (e.g. third-party tooling). Use printf.
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
# add-iam-policy-binding is idempotent: adding the same member+role twice is a
# no-op on the policy. Re-runs after partial failure are always safe.
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

# ── Wire secrets to Cloud Run (PATH 1: env-var injection via --set-secrets) ──
echo ""
echo "=== NEXT STEP: inject secrets as Cloud Run env vars ==="
echo ""
echo "PATH 1: inject secrets as Cloud Run env vars (FilesystemSecretBackend, no ATOMIC_AGENTS_SECRET_BACKEND needed)."
echo ""
echo "# IMPORTANT: --set-secrets is a SINGLE [KEY=VALUE,...] flag, NOT repeatable."
echo "# Passing --set-secrets more than once does NOT accumulate: gcloud removes"
echo "# all existing secrets first, then applies ONLY the last occurrence. So all"
echo "# secret mappings MUST go in one comma-separated --set-secrets flag, below."
echo ""
# Build the comma-separated secret assignment list conditionally so unset
# backends (no REDIS_URL / no POSTGRES_URL) are dropped, never emitted empty.
SECRET_ASSIGNMENTS="ANTHROPIC_API_KEY=anthropic-api-key:latest"
if [[ -n "$REDIS_URL" ]]; then
  SECRET_ASSIGNMENTS="$SECRET_ASSIGNMENTS,ATOMIC_AGENTS_LOCK_BACKEND_URL=redis-url:latest"
fi
if [[ -n "$POSTGRES_URL" ]]; then
  SECRET_ASSIGNMENTS="$SECRET_ASSIGNMENTS,ATOMIC_AGENTS_LOG_BACKEND_URL=postgres-url:latest"
fi
echo "  gcloud run services update <your-agent-name> \\"
echo "    --region=<your-region> \\"
echo "    --project=$PROJECT_ID \\"
echo "    --set-secrets $SECRET_ASSIGNMENTS"
echo ""
echo "The FilesystemSecretBackend reads env vars transparently."
echo "The cost gate and audit trail remain intact."
echo ""
echo "# PATH 2 (optional): resolve secrets live from Secret Manager on every call."
echo "# Install the [gcp] extra and set both env vars:"
echo "#   ATOMIC_AGENTS_SECRET_BACKEND=gcp"
echo "#   ATOMIC_AGENTS_SECRET_BACKEND_URL=projects/<your-project-id>/secrets"
echo "# See docs/deployment/secret-backend.md for the full guide."
echo ""
echo "Troubleshooting tip:"
echo "  If doctor reports FAIL for ANTHROPIC_API_KEY with 'not found':"
echo "  confirm the secret VERSION was added, not just the secret shell:"
echo "  gcloud secrets versions list anthropic-api-key --project=$PROJECT_ID"
echo "  A secret with no active version returns NOT_FOUND, not PERMISSION_DENIED."
