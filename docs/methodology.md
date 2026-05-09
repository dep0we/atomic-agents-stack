# Methodology — how this project gets built

A retrospective on the working methods used to build `atomic-agents-stack`,
captured at v0.10.0 (2026-05-09). This is not a contributor guide; it is a
description of practices that have shipped recognisable correctness and
velocity, written down so they survive the session that produced them.

The shape of the project so far: 3 published tags (v0.1.0 retroactive,
v0.9.0 retroactive, v0.10.0 cut today), 47 merged PRs, ~720 tests,
no production rollback events.

---

## The biggest method: review in rounds, not passes

Most teams treat code review as one pass. This project does 3-5 rounds per
non-trivial PR. Recent examples:

- **PR #75 (`atomic-agents doctor`)** — 3 rounds, 9 P2 findings closed.
- **PR #76 (SemVer policy + upgrade runbook)** — 5 rounds, 11 P2 findings closed.

The non-obvious property: **each round catches different things.** Not because
the reviewer "tries harder" the second time. Because each fix changes the
diff and exposes new edges. Round 5 of PR #76 was the only round that flagged
the `No migrations needed` claim — earlier rounds had cleared the diff that
contained it.

Sequential refinement is qualitatively different from one thorough pass.
Plan for it.

A side effect: most rounds run while you're doing something else (kicked off
as background tasks). Wall-clock cost stays low. Token cost is real but
amortises against the compounding correctness payoff.

---

## Codex as a real outside voice — not Claude-roleplaying-Claude

Reviews use the OpenAI Codex CLI, which pulls the actual diff via `git`,
reads files itself, and runs its own commands. **It is a different model
family with different blind spots.** When Codex and Claude agree on a
finding, that's high-confidence. When Codex catches something Claude
missed, that's the whole point.

Empirically: 3 rounds is sufficient for most diffs. We hit OpenAI's usage
cap on the 4th round of one PR; the prior 3 rounds had already covered the
same code thoroughly enough to ship.

The wrong version of this practice is "ask Claude to imagine being a
reviewer." That's prompting; this is verification.

---

## Verify before claim — empirically

When Codex says "your docs are wrong about this CLI flag," reproduce the
failure before accepting the finding. Recent examples in this project:

- `python -m atomic_agents.migrate --dry-run` (without `--to`) — Codex
  asserted exits 1; ran it, confirmed exit 1, fixed the runbook.
- `migrate --to vN` against an already-current vault — Codex asserted
  raises with `Target version vN is not above current vN`; ran it, got
  exactly that text, matched the docs to actual behavior.

The rule: **most code review is "your reviewer asserts a thing; you accept
or reject based on plausibility." This project mechanises "you accept or
reject by reproducing."** Slow per-finding. Eliminates rumor-driven
changes. The cost is not as high as it sounds because most claims are
trivially reproducible.

---

## Scope discipline by issue, not by PR

When something surfaces that isn't the current task — a missing `atomic-agents migrate`
top-level subcommand, personal references that need to come out for public
release — file it as a separate GitHub Issue and keep the current PR clean.

This project's convention, recorded in user memory: **all atomic-agents work
tracked in GitHub Issues at dep0we/atomic-agents-stack with title prefixes
(`[backend]`, `[deployment]`, `[polish]`, `[v0.X]`) and labels
(`enhancement`, `documentation`, `infrastructure`, `polish`, `backend`,
`deployment`, `spec`, `bug`).**

The discipline: **file these issues inline as part of completing the parent
task.** Don't ask the maintainer to do it. By the time they next look, the
scope-creep has a bug number. There is no "we should track that" debt —
there is "issue #N has it queued."

---

## Reversible vs irreversible — different gates

Local edits, branches, commits, running tests against tmp dirs — all
reversible, all auto-shipped without confirmation.

Pushing tags, merging PRs, creating GitHub Releases, force-pushes — all
require explicit approval.

The line is action-reversibility, not user-friction-minimization. Auto mode
does not override it. When tags were created locally for v0.1.0 and v0.9.0,
the distinction "created locally; not yet pushed" was load-bearing.

---

## Documentation matches reality, not aspirations

The upgrade runbook in `docs/deployment/upgrading.md` says "scripts must be
copied into `<vault>/_migrations/`" because that is the actual interface
today. The ideal interface is `atomic-agents migrate <agent>` as a packaged
command. The docs were not "fixed" to match the ideal — the docs were made
to match the implementation and a follow-up issue was filed for the future.

This is unusual. Most docs describe an aspirational world ("the framework
will discover scripts...") or a partial truth that drifts. By matching docs
to *current behavior + linking to the issue for future improvement*,
neither future-readers nor present-operators get misled.

The pre-merge expectation: if a doc claim does not match the implementation,
either fix the implementation or fix the doc — never let them diverge.

---

## Self-dogfood the work as it ships

Patterns observed:

- Wrote the SemVer release runbook, then immediately ran it on the
  retroactive v0.1.0 + v0.9.0 tags. The `awk` extractor was the first
  thing tested. **The runbook was operator-validated before any
  external operator existed.**
- Codex found bugs IN the SemVer docs as they were being written — the
  pre-1.0 caveat said "additive → Patch" while the policy table said
  "additive → Minor" — caught by reading our own docs cold, not by an
  operator stumbling on it months later.
- Doctor's check_provider_keys reuses the production lookup chain
  (`_llm._get_key()`) so doctor's verdict and runtime behavior cannot
  disagree. The "correctness ratchet" runs through the test suite.

---

## Bisectable commits, not save-points

Every merged PR splits into multiple logical commits when the work is
non-trivial:

- PR #75 — one commit for `doctor.py + tests`, one commit for
  `spec doc + getting-started + CHANGELOG`.
- PR #76 — one commit for `versioning.md + upgrading.md`, one commit for
  CHANGELOG conventions + README link.

Future operators running `git bisect` on a regression have clean atoms to
bisect against, not a 1873-line wall.

The shape works retroactively too. When historical v0.1.0 and v0.9.0 were
tagged today, they were tagged at the **commit where each release's
CHANGELOG entry landed** — `git log --oneline -- CHANGELOG.md` surfaced
them in seconds. Git history is operator-navigable when commits are sized
for it.

---

## CHANGELOG as the single source of truth

Established convention: GitHub Release notes come from the CHANGELOG entry
verbatim (via `awk` extraction with `--notes-file`), not from auto-generated
commit summaries.

Operators reading the GitHub Releases page see narrative notes — including
`### BREAKING` callouts — that match what they read in the file.

This sounds obvious in retrospect but most projects have the Releases page
diverge from CHANGELOG within a few releases, and it's hard to recover once
it has happened. The convention was baked in at v0.1.0 by writing the
release procedure into `docs/deployment/versioning.md` before any release
went out.

Corollary: every PR adds its own bullets to `[Unreleased]` as part of the
diff. There is no "release notes meeting" to remember.

---

## Retroactive tagging is real institutional work

The CHANGELOG had v0.1.0 and v0.9.0 entries dated weeks before any tag
existed. The release-cutting work today included tagging retroactively at
the right historical commits.

An operator looking at the v0.1.0 release today sees a real release that did
not exist as a published artifact yesterday. **That is load-bearing for
anyone who'll want to pin.**

If the historical tags had been deferred until v1.0.0, or v0.10.0 had been
shipped without backfilling, the gap between "what shipped" and "what's
tagged" would be permanent.

---

## The handoff is intentional

The vault file `~/ObsidianVault/Atomic Agents/RESUME-NEXT-SESSION.md`
exists because the previous session wrote it. It is an artifact of the
method.

The next session that opens this repo does not have to reconstruct context.
It has a self-contained brief that points at the four key files (CHANGELOG,
ROADMAP, the spec doc that establishes the protocol pattern, and the GitHub
issue list filters), explains the conventions established this session, and
recommends a starting point.

**The handoff cost is paid by the session that's leaving, not the session
that's arriving.**

---

## Things easy to miss

1. **`/ship` has a Step 18 that runs `/document-release` as a subagent.**
   Bypassing `/ship` for the v0.10.0 release cut today caused the README's
   "What's shipped" table to drift — caught only when the maintainer
   noticed. Workflows are correct when run end-to-end; manual shortcuts
   lose the consistency check.

2. **The substring search for personal references undersells the problem.**
   Direct mentions of "Dan" were ~5 hits across the repo. The bigger problem
   was `Sam` used as a real persona in spec docs (the Caldwell sample
   correctly frames Sam as fictional, but the spec docs use Sam as if
   defined elsewhere). The framing is more subtle than the literal string
   match. (See issue #77.)

3. **CHANGELOG-driven release notes is not a small win.** Every PR going
   forward writes its own release-notes content as part of the diff. There
   is no later moment when someone has to recall what a PR did and write
   notes for it. The PR body and the CHANGELOG entry and the git tag
   annotation can be the same prose, written once.

4. **Issue #77 (personal-references sweep) is a precondition for #10
   (public flip).** Nothing in the deployment-readiness backlog
   (#69–#73) helps if a public reader sees Sam's situation in the Caldwell
   sample and thinks they're meant to copy a real person's life. #77 is
   gating.

5. **The "agent-as-package" goal (strategic roadmap #3) means `atomic-agents
   doctor` will also be the install verifier for `pip install atomic-<agent>`.**
   That is why doctor needs to be the trust foundation — every future
   packaged-agent operator is going to run it post-install. Every other
   deployment doc references it for that reason.

---

## What this method does not optimise for

- **Maximum velocity.** A 5-round review cycle is slower than a 1-round
  review cycle. The compensation is shipped correctness, not raw throughput.
- **Cheap reviews.** Each Codex round is real spend. The compensation is
  9-11 P2 findings closed pre-merge per non-trivial PR, which would
  otherwise be field issues.
- **Brevity.** PR bodies are large. CHANGELOG entries are detailed. Spec
  docs are exhaustive. The compensation is durable institutional memory
  that survives the maintainer's session — and eventually, the maintainer.

If the project ever needs to optimise differently, this doc is the
honest description of the current trade-offs being accepted.

---

*Captured from a session retrospective on 2026-05-09, immediately after the
v0.10.0 cut. Update when the methods materially change, not when they wobble.*
