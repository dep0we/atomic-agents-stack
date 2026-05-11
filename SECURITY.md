# Security policy

## Reporting a vulnerability

If you find an issue that lets a non-operator do something the framework documents as forbidden — read or write outside the agent's `tools.md` paths, bypass cost guardrails without the documented `critical=True` override, escape the autonomy ladder, recover deleted versions an operator chose to redact, etc. — please **do not file a public issue**.

Email the maintainer at **dep0we@gmail.com** with `[security]` in the subject. Include:

- A short description of what the issue is
- A minimal reproduction (code snippet, command sequence, or vault state)
- The version affected (`pip show atomic-agents-stack` or commit SHA)
- Your assessment of severity (what the issue lets an attacker do, what it does not let them do)
- Whether you have disclosed this to anyone else

The maintainer will acknowledge within 7 days, propose a fix or mitigation within 30 days, and ship a patched release within 90 days of acknowledgment. For severe issues (data loss, unauthorized writes outside `tools.md`, secrets exposure), the timeline compresses.

## Disclosure timeline

The project follows a **90-day disclosure window** by default. If a fix is not shipped within 90 days of your report being acknowledged, you are free to disclose publicly. The maintainer will work with you on disclosure timing if you ask.

Public release notes for security-relevant fixes go in the CHANGELOG under `### Security`. Reporters get credit unless they ask not to.

## Out of scope

The following are documented limitations, not vulnerabilities:

- **Path traversal protection is best-effort for MCP server args** (see `atomic_agents/mcp.py:validate_mcp_server_args` — best-effort heuristic check on path-shaped args; non-path-shaped args are not validated)
- **Cost guardrails are advisory in runtimes without the shared helper** — Claude Code skill without the helper installed cannot enforce `tools.md` write paths or cost caps. The framework says so explicitly in `docs/spec/01-anatomy.md` §"Policy vs. enforcement."
- **The vault is plain markdown** — no encryption at rest, no access control beyond filesystem permissions. The operator owns the directory; the framework does not add a security layer on top of `chmod`.
- **API keys** — the framework reads keys from environment variables, macOS Keychain, or `~/.config/atomic_agents/keys.json`. It does not protect keys you put in the wrong place (e.g., in a synced Obsidian vault).
- **Helper outputs are uncited prose by default** — the framework does not authenticate that a helper's output matches its source document. Cross-checking is the operator's responsibility (per `docs/spec/13-research-integrity.md`).

If you are unsure whether something is in scope, email anyway. Better one extra email than a silent issue.

## Supported versions

Only the latest released version receives security fixes. Older Minor versions do not get backports while the project is pre-1.0. Once v1.0 ships, the policy will be updated to specify a support window.

## Operator hygiene checklist

For operators running the framework in production-shaped contexts:

- Run `uv run atomic-agents doctor` after every upgrade and after any change to `tools.md` / `model.md` / `mcp.md`.
- Never put API keys in the synced vault. Use Keychain on macOS, environment variables in CI, or `~/.config/atomic_agents/keys.json` (chmod 600) elsewhere.
- Set `cost_guardrails.enabled: true` with reasonable values before scheduling any agent for unattended cron runs. See [`docs/deployment/cost-guardrail-sizing.md`](docs/deployment/cost-guardrail-sizing.md).
- Keep `.lock` and `.versions/` out of any sync that crosses hosts (per [`docs/deployment/obsidian.md`](docs/deployment/obsidian.md)).
- Read the release notes before upgrading (per [`docs/deployment/upgrading.md`](docs/deployment/upgrading.md)).
