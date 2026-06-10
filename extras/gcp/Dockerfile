# Dockerfile - atomic-agents-stack on Cloud Run (stateless v0)
#
# NOT YET DOGFOODED against the live GCP platform.
# Claims verified against GCP documentation as of 2026-06-09.
# See extras/gcp/README.md §External-claim verification status.
#
# v0 topology: one Cloud Run service per tenant, stateless (no persistent
# disk - Cloud Run v2 does not support GCE block persistent disk volumes;
# see extras/gcp/README.md §"Why no disk on v0 Cloud Run"). State directories
# in the baked image are ephemeral: they exist in the container writable layer
# and are LOST on container replacement. Activate managed backends (Redis for
# locks, PostgresLogBackend for run logs) to make those surfaces durable.
# For a stateful-today deployment with a real persistent disk, see
# extras/gcp/compute-vm/.
#
# SECURITY: this image runs as non-root user (UID 10001). Cloud Run
# fully-managed does not enforce runAsNonRoot via the Knative manifest spec;
# the Dockerfile USER directive is what is effective. Running as non-root
# limits the blast radius of any RCE in the serve layer, an agent tool, or
# an MCP subprocess.
#
# `atomic-agents serve` is shipped (issue #342, the [serve] extra). The CMD
# below works today and mirrors the canonical Cloud Run example in
# docs/deployment/serve.md (only the agent-name placeholder differs).
#
# Pin patch version for reproducible builds. Bump intentionally when upgrading.
# The framework's CI matrix covers 3.11 and 3.12; 3.11 is the conservative pin.
FROM python:3.11.9-slim

WORKDIR /app

# Create non-root user before installing or copying anything.
# UID 10001 avoids collision with common system UIDs.
# Create the group explicitly with a fixed GID so the COPY --chown=10001:10001
# group component is deterministic: without an explicit -g, useradd would create
# a primary group at whatever GID is free (usually 10001, but NOT guaranteed),
# and a mismatch would chown files to a GID appuser is not a member of.
# -r: system account (no home dir created by default without -m)
# -s /bin/false: no interactive shell
RUN groupadd -g 10001 appuser \
 && useradd -u 10001 -g 10001 -r -s /bin/false appuser

# Install framework with the serve extra (#342) and redis extra (#60). Add
# `vertex` if you use the Vertex Gemini LLM backend (see README §LLM backend);
# the VertexGeminiLLMBackend is only importable with the [vertex] extra.
# Remove or narrow extras based on what you actually use.
RUN pip install --no-cache-dir "atomic-agents-stack[serve,redis,vertex]"

# ATOMIC_AGENTS_ROOT points at the directory that holds agent folders.
# COPY the WHOLE agent folder - config AND the on-disk shape of mutable state.
# The baked-in config (persona/, model.md, tools.md, goal.md, skills/) is what
# the agent runs from. State directories (memory/, log/, journal/, ...) land
# in the container's ephemeral writable layer on stateless Cloud Run v0 -
# they are NOT overlaid by any persistent-disk volume mount (Cloud Run v2 does
# not support GCE persistent disk volumes). State written to these directories
# is LOST on container replacement.
# For the VM deployment (extras/gcp/compute-vm/), the persistent disk is
# mounted at ATOMIC_AGENTS_ROOT, and the agent folder is on the disk. The
# LAYOUT RULE (never mount at the agent folder root itself) still applies there.
# --chown ensures the non-root user can write to state directories at runtime.
COPY --chown=10001:10001 ./agents /app/agents
ENV ATOMIC_AGENTS_ROOT=/app/agents

# Drop to non-root for all subsequent layers and at runtime.
USER 10001

# Cloud Run injects PORT at container startup. The CMD honors it via
# `--port ${PORT:-8080}`. Here 8080 is the SHELL-level fallback for ${PORT}, NOT
# the framework default - the framework default is 8000 (atomic_agents/serve/
# _config.py). cloudrun-service.yaml pins containerPort: 8080 so PORT resolves to
# 8080 and matches the health-probe port. Don't set ATOMIC_AGENTS_SERVE_PORT
# here - the `--port` CLI flag takes precedence over it (CLI > env > serve.md >
# default 8000), so the env var would be an inert, confusing override.
# See docs/deployment/serve.md §"Cloud Run entrypoint example".
#
# --allow-no-auth is ONLY safe when IAP (or equivalent external auth perimeter)
# is enforced in front of this container. Without IAP, --allow-no-auth exposes
# the /call endpoint to unauthenticated requests - any caller can trigger
# agent.call() and incur LLM costs. See extras/gcp/iap-setup.md.
# For the Compute Engine VM reference (extras/gcp/compute-vm/), use IAP TCP
# tunneling or a VPC-internal-only interface instead.
#
# Replace <your-agent-name> with the folder name under ATOMIC_AGENTS_ROOT.
#
# Requires atomic-agents-stack[serve] (shipped, issue #342) - already installed
# by the pip line above.
CMD ["sh", "-c", "atomic-agents serve <your-agent-name> --host 0.0.0.0 --port ${PORT:-8080} --allow-no-auth"]
