# MAINTAINER.md

How pull requests are reviewed and merged in this project. Sharing this so contributors know what to expect.

## TL;DR

PRs are reviewed in tiers based on risk. A README typo and a change to `_locks.py` get different scrutiny. Smaller, more scoped PRs move faster.

## The tier system

| Tier | What it looks like | What happens |
|---|---|---|
| **1 — Trivial** | Docs only (README, comments, spec docs), tests added, diff <20 lines, no source code touched | Skim diff, check CI green, merge. Usually within hours. |
| **2 — Normal code** | Source file changes, scoped to one feature, no security-sensitive areas | AI-assisted review. If clean, merge. Usually within a day. |
| **3 — Security-sensitive** | Touches `_locks.py`, `_costs.py`, `agent.py` main loop, `_capture.py`, MCP handling, dependency manifests, CI workflows, file-path resolution, or any Protocol surface | Multiple independent AI reviews. May ask for changes. Usually a day or two. |
| **4 — Stranger + larger change** | New contributor + non-trivial logic change | Tier 3 stack plus a check on contributor's GitHub history. Slower; expect dialogue before merge. |

## Auto-elevating red flags

These elevate any PR's tier, regardless of how small the diff looks:

- **Adds dependencies** (changes to `pyproject.toml` or `uv.lock`)
- **Modifies `.github/workflows/`** — workflow changes can affect secrets and CI behavior
- **Adds binary files** — please contribute source-only; binaries can't be meaningfully audited
- **PR description doesn't match diff scope** — please split into separate PRs by topic
- **Disables or removes existing tests** — fix the test, don't delete it
- **Brand-new GitHub account + non-trivial change** — expect a conversation before merge

## What you can do to make PRs easy to merge

1. **Pick from `good first issue` or `help wanted`** — these are pre-scoped, low-risk, and likely to merge quickly
2. **Keep PRs focused on one thing** — don't bundle a typo fix with a refactor
3. **Write a PR description that matches the diff** — name what changed and why
4. **If touching design-rule territory** (CLAUDE.md's 14 design rules, `agent.call()`, `_capture.py`, `_costs.py`, `_locks.py`, or any Protocol surface) — open an issue first
5. **Don't add dependencies without discussion** — open an issue first; new deps need justification

## Review tooling

Reviews use a combination of AI-assisted review tools and human judgment:

- Pre-landing diff analysis for code quality and design alignment
- Independent cross-model review for security-sensitive changes
- OWASP-style scans for secrets, vulnerabilities, and dependency issues

AI reviewers don't auto-merge anything. The maintainer reads the AI verdicts and makes the merge call.

## Response time

This is an alpha project with a single maintainer. Reasonable expectations:

- **Tier 1 PRs** — usually merged within a day
- **Tier 2 PRs** — usually within a few days
- **Tier 3–4 PRs** — may take longer; expect dialogue

If a week passes with no response, please ping the PR thread — it may have slipped past.

## If your PR isn't merged

It's almost never personal. The most common reasons:

- **Scope mismatch** — the change is good but doesn't fit the current direction
- **Design alignment** — see CLAUDE.md for the 14 design rules; some changes need to be reshaped to fit
- **Timing** — the area is in active flux and the change conflicts with in-flight work

In every case, you'll get an explanation. Reopened or reworked PRs after a discussion are welcome.

## See also

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to set up, run tests, and structure a PR
- [`CLAUDE.md`](CLAUDE.md) — the 14 design rules every PR is checked against
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — community expectations
- [`SECURITY.md`](SECURITY.md) — how to report security vulnerabilities
