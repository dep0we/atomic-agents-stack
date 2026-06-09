# Dockerfile — atomic-agents-stack on Cloud Run
#
# v0 topology: one Cloud Run service per tenant, persistent disk for
# filesystem-backed state, IAP perimeter for auth.
#
# `atomic-agents serve` is shipped (issue #342, the [serve] extra). The CMD
# below works today and mirrors the canonical Cloud Run example in
# docs/deployment/serve.md (only the agent-name placeholder differs).
#
# Pin patch version for reproducible builds. Bump intentionally when upgrading.
# The framework's CI matrix covers 3.11 and 3.12; 3.11 is the conservative pin.
FROM python:3.11.9-slim

WORKDIR /app

# Install framework with the serve extra (#342) and redis extra (#60). Add
# `vertex` if you use the Vertex Gemini LLM backend (see README §LLM backend);
# the VertexGeminiLLMBackend is only importable with the [vertex] extra.
# Remove or narrow extras based on what you actually use.
RUN pip install --no-cache-dir "atomic-agents-stack[serve,redis,vertex]"

# ATOMIC_AGENTS_ROOT points at the directory that holds agent folders.
# COPY the WHOLE agent folder — config AND the on-disk shape of mutable state.
# The baked-in config (persona/, model.md, tools.md, goal.md, skills/) is what
# the agent runs from; the mutable subdirectories (memory/, log/, journal/, …)
# are then OVERLAID at runtime by per-subdirectory persistent-disk mounts (see
# cloudrun-service.yaml volumeMounts — mounted at the subdirs, NOT at the agent
# folder root, so the baked config stays visible). Do NOT mount the disk over
# /app/agents/<name> itself: that would shadow the baked config and the agent
# folder would look empty on first cold start, failing healthz Check 3.
COPY ./agents /app/agents
ENV ATOMIC_AGENTS_ROOT=/app/agents

# Cloud Run injects PORT at container startup. The CMD honors it via
# `--port ${PORT:-8080}`. cloudrun-service.yaml pins containerPort: 8080 so PORT
# resolves to 8080 and matches the health-probe port. Don't set
# ATOMIC_AGENTS_SERVE_PORT here — the `--port` CLI flag takes precedence over it
# (CLI > env > serve.md > default), so the env var would be an inert, confusing
# override. See docs/deployment/serve.md §"Cloud Run entrypoint example".
#
# --allow-no-auth is safe here because IAP is the perimeter — requests are
# authenticated by GCP before they reach this container. Never use
# --allow-no-auth without a perimeter. See extras/gcp/iap-setup.md.
#
# Replace <your-agent-name> with the folder name under ATOMIC_AGENTS_ROOT.
#
# Requires atomic-agents-stack[serve] (shipped, issue #342) — already installed
# by the pip line above.
CMD ["sh", "-c", "atomic-agents serve <your-agent-name> --host 0.0.0.0 --port ${PORT:-8080} --allow-no-auth"]
