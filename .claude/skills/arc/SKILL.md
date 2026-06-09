---
name: arc
description: Router for the atomic-agents arc-* quality workflows (decision-first build loop). Invoke when the maintainer types /arc with a subcommand — discovery <issue>, build <issue>, finish <issue|branch>, resume, or status. Wraps the Workflow scripts in .claude/workflows/ with the operational rules (scriptPath reliability, branch setup, Codex probe, the decision gate, the merge gate) so they run correctly even from a fresh session.
---

# /arc — atomic-agents arc workflow router

Routes `/arc <subcommand> [args]` to the arc-* Workflow scripts in `.claude/workflows/`. These run the decision-first quality loop. Background: memory `project-arc-workflow-system`, `feedback_arc_workflow_invocation_gotchas`, `feedback_arc_execute_converges_drafts_not_finals`. Read those if context is fresh.

Let `REPO = $(git rev-parse --show-toplevel)`. Scripts are `REPO/.claude/workflows/arc-{discovery,execute,finish}.js`.

## Hard rules — apply to EVERY subcommand

1. **Invoke via `scriptPath`, NEVER by workflow name.** The name registry freezes at session start; `scriptPath` reads the live file. Always: `Workflow({ scriptPath: "REPO/.claude/workflows/arc-<x>.js", args: {...} })`.
2. **`args` reaches the script as a JSON string** — the scripts parse it defensively. Just pass the JSON value.
3. **Builds serialize; one branch = one session.** Only ONE `arc-execute`/`arc-finish` at a time — they share one working tree. Never fire two concurrently; queue the second. AND never run two sessions against the same feature branch: each concurrent stream gets its own branch, or the shared working tree leaks one session's uncommitted edits into the other (the #345 incident — a parallel session's TENSIONS edits got quarantined out from under the build). `arc-discovery` is read-only but is NOT safe to run while a build's output is uncommitted (it can `git checkout` and drag the build onto another branch); run discoveries only between builds.
4. **Decisions are the maintainer's.** Never fire a build for an issue whose Tier A forks the maintainer hasn't ruled. Rulings live in `REPO/.gstack/arc-rulings/<issue>-pr1-args.json`.
5. **Merge is the maintainer's gate.** Builds stop at pr-ready; never merge, tag, or publish. `did-not-converge` or `blocked-on-decision` → park the branch (commit + push, no PR) and escalate to the maintainer; do not ship half-baked.
6. **Probe Codex before any discovery**: `timeout 90 codex exec "reply OK"`. Pass `codexAvailable: true` only if it answers; otherwise the panel falls back to an opposed-Opus skeptic (same-family) — note that in the result.
7. **Governing docs are never the build's to edit (hard tripwire).** A build must NEVER amend `CLAUDE.md` design principles / throughline / aesthetic rules or `docs/TENSIONS.md` — that is the single most Tier A change possible. `arc-execute` and `arc-finish` carry a deterministic tripwire that returns `blocked-on-decision` if the working tree touches those regions (status / backend-count tables are exempt — routine doc-accounting). When a build returns that status, surface the proposed change to the maintainer to rule and ship as its own docs PR; do not restore it into the build's PR. The decision content may be perfectly sound — that is separate from the autonomy boundary, which is the point. (Origin: the #345 build authored a Principle #1 / T15 amendment + filed an issue without halting — the judgment-based halt missed it, so the tripwire was made mechanical.)

## Subcommands

### `/arc discovery <issue>`
1. Probe Codex (rule 6).
2. Fire `arc-discovery` via scriptPath. `args = { issue: "<N> — <one-line scope to PR 1 of the arc>", codexAvailable: <probe> }`.
3. On completion: extract the decision packets (group converge / diverge). **Resolve factual-code diverges yourself** (questions answerable by reading the code, not taste) and present only genuine product/taste diverges to the maintainer, each with a recommendation.
4. After the maintainer rules, save the full decision set to `REPO/.gstack/arc-rulings/<N>-pr1-args.json` as `{issue, maxRounds:5, decisions:{<forkId>:<chosen>}}` (converged recs + the maintainer's diverge calls + your factual resolutions).

### `/arc build <issue>`
1. Require `REPO/.gstack/arc-rulings/<N>-pr1-args.json` to exist. Missing → tell the maintainer to `/arc discovery <N>` and rule first. Do NOT build unruled.
2. Check `build-queue.json` for an active build. If one is running → add <N> to the queue, tell the maintainer it'll fire when the active one lands. Else continue.
3. `git checkout main && git pull origin main`, then `git checkout -b feat/<slug>-<N>`.
4. Fire `arc-execute` via scriptPath with the saved args. In the `issue` scope string, include the explicit intended spec number (if any) and "build off current main" — prevents the spec-number collision class.
5. On completion: run the full suite independently (`uv run pytest -q`). `pr-ready` + green → **hand off to `/ship`** (see "Finalize via /ship" below) which does tests/CHANGELOG/version/bisectable-commits/push/PR end-to-end, then advance the queue. `did-not-converge`/`blocked-on-decision` → park (commit + push, no PR) with the open items in the commit message + queue note, escalate to the maintainer.

### `/arc finish <issue|branch>`
For an existing build that didn't converge (a parked branch).
1. Checkout the branch (or stay if already on the parked one). Confirm the build is committed (so `git diff main` shows it).
2. Fire the **hardened** `arc-finish` via scriptPath. `args = { issue:"<N> …", base:"main", maxRounds:5, seedFindings:[<the branch's known residuals from its parked commit message / prior result>] }`. (arc-finish uses an Opus holistic-planning + self-verify fix loop with sticky-finding escalation.)
3. Converged + suite green → **hand off to `/ship`** (see "Finalize via /ship" below). Still not converged → re-park + escalate the tighter residual set.

### `/arc resume` (alias `/arc status`)
Read `REPO/.gstack/arc-rulings/build-queue.json` and recall memory. Report what's merged / parked / queued / held, with branch names. Propose the next action. Do NOT auto-fire a build without the maintainer's explicit go.

## Finalize via /ship (the handoff — never hand-roll commit/push/PR)

When a build/finish reaches `pr-ready` and the suite is green, **invoke the real `/ship` skill** (gstack) via the Skill tool to finalize. Do NOT hand-roll `git commit`/`push`/`gh pr create` — that bypass is the exact drift `feedback_ship_end_to_end_no_shortcuts` warns against (it's how #342 shipped without /ship's checks).

Why this works now: session/runtime + dev-harness artifacts (`.gstack/`, `.context/`, `.claude/worktrees/`, `.claude/workflows/`, `.claude/skills/arc/`) are gitignored, so a build branch's working tree shows **only the issue's framework code**. `/ship`'s "include all uncommitted changes" is therefore safe — there's no tooling noise to sweep in.

Handoff rules:
- `/ship` runs end-to-end: merge base, tests, its review, bisectable commits, CHANGELOG, version, push, PR. It **stops at PR — never merges**. Merge stays the maintainer's gate.
- **Version gate:** `/ship` asks on MINOR/MAJOR. Default answer for arc backend/feature PRs: **no bump — accumulate in `[Unreleased]`** (project convention; tag at milestone release). Only bump if the maintainer says it's a release point.
- **PR body:** carry the arc run's graded-P2 findings (the non-blocking shortcuts/findings) into the PR body for the maintainer's merge review — they are the "for your judgment" surface.
- **Redundant review (accepted cost):** `/ship` runs its own review army even though arc already did adversarial rounds. That's the discipline, not waste. *Future optimization:* have the arc workflow write its adversarial result into the gstack review log so `/ship` Step 1's Review Readiness Dashboard sees it and skips the redundant army — not built yet.

## State files (durable across sessions/compaction)
- `REPO/.gstack/arc-rulings/build-queue.json` — queue state (active / queue / held / completed).
- `REPO/.gstack/arc-rulings/<issue>-pr1-args.json` — saved rulings per issue.
- Parked branches on origin preserve in-progress builds; their commit messages list open blockers.
