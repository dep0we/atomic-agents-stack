# Methodology — how this project gets built

A retrospective on the working methods used to build `atomic-agents-stack`,
captured at v0.10.0 (2026-05-09). This is not a contributor guide; it is a
description of practices that have shipped recognisable correctness and
velocity, written down so they survive the session that produced them.

The shape of the project so far (snapshot at the time of original capture,
2026-05-09): 4 published tags (v0.1.0 retroactive, v0.9.0 retroactive,
v0.10.0, v0.13.0), ~70 merged PRs, ~1327 tests, no production rollback
events. Three backend protocols shipped at that point (MemoryBackend,
LLMBackend, JudgeBackend); today seven are shipped (MemoryBackend,
LLMBackend, JudgeBackend, LockBackend, LogBackend, AgentProfileBackend,
ToolRegistryBackend) with parametrized conformance suites and 1953+ tests
— see the empirical record table below for arc-by-arc evidence of how the
methodology held across them.

---

## The biggest method: review in rounds, not passes

Most teams treat code review as one pass. This project does 2-5 rounds per
non-trivial PR — 2 is the minimum for non-trivial diffs (let the round-1 fix
commit become its own review surface in round 2), 3-5 when the diff is large
or the round-1 fix is substantial. The rounds-not-passes discipline holds
even on docs-only PRs. Recent examples:

- **PR #75 (`atomic-agents doctor`)** — 3 rounds, 9 P2 findings closed.
- **PR #76 (SemVer policy + upgrade runbook)** — 5 rounds, 11 P2 findings closed.
- **PR #206 (#64 PR 4 — docs-only spec lock + status refresh)** — **2 rounds, 11 findings + 1 new successor issue (#207).** Round 1 caught 5 (P1 numerical drift, P2 stale tense, P2 marketing voice, P3 skip-count, P3 MUST #4 conflation). **Round 2 caught 6 residuals round 1 missed PLUS a count-drift that the round-1 fix commit itself introduced** ("96 → 115 test sites"), plus 1 new P2 (unpinned Tier B conformance gap → filed inline as #207). Round 2's count-drift catch is the load-bearing empirical evidence: each round catches different things not because the reviewer tries harder, but because **each fix changes the diff and exposes new edges**.

The non-obvious property: **each round catches different things.** Round 5 of
PR #76 was the only round that flagged the `No migrations needed` claim —
earlier rounds had cleared the diff that contained it. Round 2 of PR #206 was
the only round that could have caught the count-drift, because round 1's fix
commit was what introduced it.

**Even on docs-only PRs.** The PR #206 evidence is the clean empirical case
for "rounds-not-passes even when no code changed." Don't downgrade the
review cadence because the diff is markdown — markdown drift is the same
shape as code drift, and a single-pass review on a fix commit cannot catch
what that fix commit introduces.

Sequential refinement is qualitatively different from one thorough pass.
Plan for it.

A side effect: most rounds run while you're doing something else (kicked off
as background tasks). Wall-clock cost stays low. Token cost is real but
amortises against the compounding correctness payoff.

---

## Reviewer roster — what the project actually does

The early v0.x convention was "Codex first, fall back to Claude / Kimi if
Codex hangs." The actual practice across the last five protocol arcs is the
opposite: **the Opus adversarial subagent is the default reviewer, Codex is
skipped per the standing project rule.** Documenting reality here so future
sessions don't try to re-prove the deferred convention.

The empirical record across recent arcs:

| Arc | PRs | Codex run? | Opus subagent run? | Findings caught pre-merge |
|---|---|---|---|---|
| #112 JudgeBackend | #162–#171, #174, #178 | Skipped (standing rule) | Every PR | P0 uncaught `JudgePolicyInvalid` at both `validate_amended_args` call sites; P1 `allow` semantic silently demoted in Step 9.1 patch; several P2/P3 |
| #60 LockBackend | #180–#184 | Skipped | Every PR | `RedisLockBackend` heartbeat lease-expiry race (`LockLost` detection); daemon-thread teardown shape |
| #61 LogBackend | #185–#188 | Skipped | Every PR | P0 cross-agent log isolation (`LogQuery.agent_name` filter) REPRODUCED; P0 cold-start schema race REPRODUCED via concurrent backend init |
| #63 AgentProfileBackend | #192–#195 | Skipped | Every PR | F-3 cross-agent path-traversal in `list_snapshots`/`restore` REPRODUCED; F-8 48-bit snapshot id entropy budget; 6 P0/P1 across the arc |
| #64 ToolRegistryBackend | #197–#199, #206 | Skipped | Every PR | **12 P0/P1 REPRODUCED across PRs 1-3** — REPRODUCED YAML alias-bomb DoS at 33 GB RSS, REPRODUCED control-char log-injection, REPRODUCED `chmod-000 tools/` blocks every agent, REPRODUCED TOCTOU install race 50/50, REPRODUCED multi-process WAL race 3/5, REPRODUCED URL credential leak. PR 4 docs-only added 11 findings + 1 new successor issue (#207) across 2 rounds (the docs-only P1 numerical drift is excluded from the 12-count). |

That's five consecutive arcs with Codex skipped and the Opus subagent doing
the load-bearing review — every arc produced multiple P0 / P1 findings from
the subagent pass, and the #61 / #63 / #64 arcs in particular produced
**REPRODUCED** findings (race conditions / data-corruption shapes where the
adversarial subagent ran the reproducer and confirmed the failure mode
pre-fix). Codex hasn't been re-validated since the #112-arc hang. The
reviewer comparison table below reflects that.

### Reviewer comparison table

| Reviewer | Family | When to use | Caveats |
|---|---|---|---|
| Opus adversarial subagent + verify-against-code prompt | Anthropic (Claude) | **Default** for every non-trivial PR — including docs-only PRs. Use the Step 11 adversarial brief: think like an attacker, find ways production fails, classify P0/P1/P2/P3, end with a `Recommendation:` line. Run in rounds (2-5), not one pass. | Same model family as the author — in theory catches less than a true cross-family reviewer would. In practice across 5 arcs has caught REPRODUCED P0/P1 findings every arc. The "same-family blind spot" risk is real but bounded; cross-family coverage is the deferred-not-deleted backup. |
| `codex exec` / `codex review` | OpenAI (GPT) | **Deprecated default.** Skipped per the standing project rule since the #112-arc hang. Worth re-evaluating when a session has time to verify Codex is responsive AND the diff is large enough (~500+ LOC) to justify the cross-family overhead. | Has hung on multiple occasions during round 2-3 of large spec docs (PR #117 + #118); has session-level rate limits. The five-arc Opus-subagent track record is the data; before re-instating Codex as default, run a verified Codex round alongside an Opus round on the same PR and compare findings. |
| `atomic-agents review --backend kimi` | Moonshot | Codex unavailable AND the reviewer wants a genuine cross-family second opinion. Calls Moonshot via the project's `_llm.py` client with the same verify-against-code system prompt. | Today's default model is `moonshot/moonshot-v1-128k` (non-thinking). In one empirical test (PR #145 cost-source filter) it caught 0 of 3 findings Opus caught and produced several hallucinated ones; use it as a *third* opinion alongside Opus, not as a primary reviewer. The Kimi K2.x thinking models are stronger reviewers but their output lives in a separate `reasoning_content` field not extracted by `_llm._call_moonshot` yet — tracked as a follow-up to ship with the LLMBackend protocol (#87). |

**Decision rule.** Run the Opus subagent on every non-trivial PR — including
docs-only PRs — in rounds (2 minimum for non-trivial diffs, 3+ when the
round-1 fix is large enough to be its own review surface). Codex is the
deferred cross-family backup; re-instate when a session can verify
responsiveness on a small probe before committing to a full review pass.
Kimi is third-opinion-only.

**Setup notes for Kimi.** Reads `MOONSHOT_API_KEY` (or
`ATOMIC_AGENTS_MOONSHOT_KEY`) from env, or `atomic-agents-moonshot` from
macOS Keychain, or `moonshot` from `~/.config/atomic_agents/keys.json`.
International (api.moonshot.ai) operators must set
`MOONSHOT_BASE_URL=https://api.moonshot.ai/v1` until the LLMBackend
protocol (#87) lands proper per-region routing.

**Security caveat for `MOONSHOT_BASE_URL`.** This env var determines where
the operator's Moonshot API key AND the full review prompt (including any
`--read-files` contents) are sent. Don't set it to a host you don't trust.
Anthropic and OpenAI clients in `_llm.py` don't expose the same per-call
endpoint override, so this is a new affordance the wrapper introduces.

---

## Plan-subagent — pre-implementation design review

A second-layer review surface added in the #63/#64 arcs: **a plan subagent
that runs BEFORE implementation**, takes the implementation plan as input,
and surfaces architectural risks the Step 11 adversarial would catch only
after the code is written. Five SEVERE risks caught pre-implementation
across two arcs:

- **#63 PR 3** — 2 SEVERE risks. **Snapshot non-atomicity**: original
  design called for `shutil.copytree` to snapshot an agent's directory
  tree — but a crash mid-copy leaves the agent partially snapshotted, and
  `copytree(snapshot_dir, agent_root, dirs_exist_ok=True)` on restore has
  no good atomicity story either. Switched to JSON-based snapshot trio
  (serialize `AgentProfile.to_dict()` once; restore via
  `AgentProfile.from_dict()` + the backend's existing atomic
  `save_profile()`). **Conformance helper trap**: the previous
  `make_agent_dir`-only helper writes filesystem state which SQLite's
  `load_profile` can't see — would have broken ~20 of the parametrized
  conformance tests when SQLite landed. Added `make_agent_in_backend`
  (uses the Protocol surface `save_profile`) pre-implementation.
- **#64 PR 3** — 3 SEVERE risks. **Risk A**: base64-encoded handler source +
  `exec()` would have silently broken closures, module-level `import`
  statements, top-level resource setup (`session = requests.Session()`).
  Switched to hybrid metadata-in-SQL + handler-bodies-on-disk under
  `<handlers_root>/<agent_scope>/<name>.py` using `importlib.util.
  spec_from_file_location`. **Risk D**: schema `PRIMARY KEY (name)` would
  have collided across `agent_scope` values → switched to composite
  `PRIMARY KEY (agent_scope, name)`. **Risk J**: parametrized conformance
  suite needed a `make_tool_in_backend` helper to dispatch per-backend-shape
  BEFORE SQLite landed → added pre-implementation (same shape as #63 PR 3's
  helper-trap catch).

All five would likely have surfaced in Step 11 adversarial post-
implementation, but at the cost of a re-architecture cycle when they did.
**The plan-subagent is genuinely additive to Step 11 — runs at the
cheapest possible time, before code is written.** Empirically ratified
across two arcs.

When to invoke: any PR introducing a new Protocol, a new storage shape, a
new lifecycle hook, or any change that touches the agent-construction or
agent-dispatch path. Skip for one-line atomic-primitive fixes (e.g., the
#208 SQLiteLogBackend cold-start race fix didn't need it).

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
   Direct mentions of the maintainer's name were ~5 hits across the repo. The bigger problem
   was the sample's persona name used as a real persona in spec docs (the
   Caldwell sample correctly framed its user as fictional, but the spec docs
   referenced that name as if defined elsewhere). The framing is more subtle
   than the literal string match. (See issue #77.)

3. **CHANGELOG-driven release notes is not a small win.** Every PR going
   forward writes its own release-notes content as part of the diff. There
   is no later moment when someone has to recall what a PR did and write
   notes for it. The PR body and the CHANGELOG entry and the git tag
   annotation can be the same prose, written once.

4. **Issue #77 (personal-references sweep) is a precondition for #10
   (public flip).** Nothing in the deployment-readiness backlog
   (#69–#73) helps if a public reader sees the sample persona's situation in
   the Caldwell sample and thinks they're meant to copy a real person's
   life. #77 is gating.

5. **The "agent-as-package" goal (strategic roadmap #3) means `atomic-agents
   doctor` will also be the install verifier for `pip install atomic-<agent>`.**
   That is why doctor needs to be the trust foundation — every future
   packaged-agent operator is going to run it post-install. Every other
   deployment doc references it for that reason.

---

## What this method does not optimise for

- **Maximum velocity.** A 5-round review cycle is slower than a 1-round
  review cycle. The compensation is shipped correctness, not raw throughput.
- **Cheap reviews.** Each adversarial round is real spend (Opus subagent
  tokens for the default reviewer; Codex tokens when the cross-family
  backup is re-instated). The compensation is 9-11 P2 findings closed
  pre-merge per non-trivial PR, which would otherwise be field issues.
- **Brevity.** PR bodies are large. CHANGELOG entries are detailed. Spec
  docs are exhaustive. The compensation is durable institutional memory
  that survives the maintainer's session — and eventually, the maintainer.

If the project ever needs to optimise differently, this doc is the
honest description of the current trade-offs being accepted.

---

*Captured from a session retrospective on 2026-05-09, immediately after the
v0.10.0 cut. Update when the methods materially change, not when they wobble.*
