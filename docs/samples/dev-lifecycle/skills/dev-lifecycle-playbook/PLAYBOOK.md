---
name: dev-lifecycle-playbook
description: >
  Runs the full software-change lifecycle — from idea validation through shipping,
  deployment, and documentation — as a durable, resumable conductor run. Each
  human gate is explicit; each ruling is recorded with its rationale in the
  conductor decision ledger, so the scattered-decisions problem is closed for good.
kind: playbook
when_to_use: >
  Use when a change is non-trivial: it has moved past "should I build this?" and
  needs to go through the full lifecycle (spec, planning, build, security, ship,
  deploy, document). For trivial changes (a one-line fix, a pure-docs update) the
  overhead of a conductor run is not warranted — run the relevant stages directly.
---

This playbook encodes the **dev-process-kit** software-change lifecycle (stages 1–11)
as a conductor run. Each stage maps to one or two conductor stages: an automated
stage that does the work, followed by a gate stage where you rule before the run
continues. Every ruling lands in the conductor's decision ledger — one queryable
record of every gate decision with its rationale, ruler, and timestamp.

The playbook encodes 11 kit lifecycle stages as **18 conductor stages** (10 automated
+ 8 gates). Kit stages 0 (project bootstrap) and 12 (ongoing maintenance) are
out-of-scope for a single change run; they are included below as context only.

The automated-stage prompts below **approximate** the intent of each kit step in
plain language — they do not invoke the kit's slash-commands (`/spec`, `/arc`,
`/ship`, etc.). The kit-stage names in the stage comments are provenance, not a
coupling: this sample is portable content that names the lifecycle it mirrors, not
a binding to the kit's tooling.

---

> **Context only (not a conductor stage): Stage 0 — Project Bootstrap.**
> Runs once per project, not per change. Creates the repo, installs branch
> protection, scaffolds `docs/DECISIONS.md` and `docs/ARCHITECTURE.md`, and
> installs the arc loop. Not part of this playbook's conductor run.

---

<!-- DO NOT place another ```yaml block before this one — the loader picks the first match. -->

```yaml
# Pinned at run creation; editing this mid-suspension does NOT change the
# live run's cost ceiling. The pinned value is recorded in the
# conductor_run_started ledger event the moment run() mints the
# conductor_run_id. A fresh run reads this value; a resumed run uses the
# pinned ledger value, ignoring any edit made here.
#
# 50.00 is a conservative upper bound for an end-to-end change running all
# 18 conductor stages with real LLM calls. Most runs spend far less; the
# ceiling ensures no unbounded spend on a long multi-day run.
run_cap_usd: 50.00

stages:

  # ── Stage 1 · Office hours / go-no-go gate ──────────────────────────────
  # Kit stage 1: /office-hours — should I build this? Gate only (no
  # automated stage — the human brings the idea; the gate records the ruling).

  - stage_id: go-no-go-gate
    model: claude-opus-4-7-20260101
    label: "Stage 1 · Go / no-go gate"
    prompt: >
      Review the proposed change and rule on whether to proceed.
      Consider: Is the problem real? Is this the right shape? What is
      the smallest version worth building? Document your ruling and
      rationale — continue to proceed, halt to stop the run entirely.
    options:
      - "continue — the idea is solid, proceed to spec"
      - "halt — not worth building at this time"
    is_gate: true

  # ── Stage 2 · Spec ───────────────────────────────────────────────────────
  # Kit stage 2: /spec — turn fuzzy intent into a precise written target.

  - stage_id: spec-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 2a · Spec run"
    prompt: >
      Analyze the proposed change and produce a structured specification.
      Identify the scope (what is in, what is explicitly deferred), the
      constraints (dependencies, backward-compatibility requirements,
      performance or security surfaces), and a clear "done looks like"
      definition. Include explicit out-of-scope items so the spec is
      unambiguous about what will not be built.
    is_gate: false

  - stage_id: spec-scope-gate
    model: claude-opus-4-7-20260101
    label: "Stage 2b · Scope approval gate"
    prompt: >
      Review the specification produced in the previous stage and rule
      on the scope boundaries. Continue if the spec is crisp and complete;
      decline (halt) to stop the run if the spec misses critical scope
      or the change is not ready to proceed.
    options:
      - "continue — spec is crisp, scope boundaries approved"
      - "halt — spec needs rework before building"
    is_gate: true

  # ── Stage 3 · Autoplan ───────────────────────────────────────────────────
  # Kit stage 3: /autoplan — pressure-test the plan from four angles.

  - stage_id: autoplan-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 3a · Plan review run"
    prompt: >
      Review the plan from four angles before building. For each angle,
      produce a structured assessment and identify the key concern or
      approval signal:
      (1) Product / business: is this the right bet? Does it serve the
          user's actual need? Is the scope well-bounded?
      (2) Engineering: is the architecture sound? Are the dependencies
          appropriate? Are there simpler alternatives?
      (3) Design: does the experience hold up? Are the UX flows coherent?
      (4) Developer experience: will it be pleasant to build and maintain?
      Surface the most important concern from each angle.
    is_gate: false

  - stage_id: autoplan-concerns-gate
    model: claude-opus-4-7-20260101
    label: "Stage 3b · Concerns ruling gate"
    prompt: >
      Review the plan assessment from the previous stage and rule on
      which concerns to address before building and which to set aside.
      Your rationale becomes the record of why this plan was approved
      (or why the run was halted for rework).
    options:
      - "continue — concerns noted and addressed, proceed to build"
      - "halt — plan needs rework before building"
    is_gate: true

  # ── Stage 4 · Design ─────────────────────────────────────────────────────
  # Kit stage 4: /design-consultation → /design-shotgun → /design-html
  # Skip-with-approval is a recorded gate ruling, not a silent skip.

  - stage_id: design-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 4a · Design direction run"
    prompt: >
      Assess whether this change has a UI component. If it does, propose
      a design direction: identify the system's existing design language,
      propose two or three concrete directional options with trade-offs,
      and recommend one. If the change has no UI (backend, CLI, infra),
      state that explicitly and recommend skipping the design gate.
    is_gate: false

  - stage_id: design-direction-gate
    model: claude-opus-4-7-20260101
    label: "Stage 4b · Visual direction gate"
    prompt: >
      Review the design direction proposed in the previous stage and rule.
      If there is a UI: approve a direction to continue, or halt to request
      a revised direction. If there is no UI: skip this gate with your
      explicit approval (a skip is a recorded ruling, never a silent bypass).
    options:
      - "continue — visual direction approved"
      - "skip — no UI component, design gate not applicable"
      - "halt — design direction needs revision"
    is_gate: true

  # ── Stage 5 · Arc discovery ───────────────────────────────────────────────
  # Kit stage 5: /arc discovery — find every Tier-A decision fork.
  # NOTE (tracked #667): each Tier-A fork ideally gets its own GateDecision.
  # Until per-item granularity is wired, all forks are batch-ruled in one gate.

  - stage_id: discovery-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 5a · Decision discovery run"
    prompt: >
      Read the specification and identify every decision the build will
      face. Classify each as:
      Tier A — only the human may decide (architectural choices, design
      trade-offs, irreversible actions, anything with non-obvious consequences);
      Tier B — the agent decides with explicit justification;
      Tier C — the agent just does it.
      For every Tier-A fork: write a plain-language decision packet (what
      is the choice, what are the options, what is the consequence of each).
      Do not write any code. Verify no files were modified.
    is_gate: false

  - stage_id: tier-a-rulings-gate
    model: claude-opus-4-7-20260101
    label: "Stage 5b · Tier-A rulings gate"
    # NOTE: ideally one gate per fork; batched into one per Grok ruling.
    # Per-item granularity tracked in #667.
    prompt: >
      Review the Tier-A decision forks from the previous stage and rule
      on each one. For each fork: state which option you choose and why.
      Your rationale for each ruling is the load-bearing record in the
      decision ledger — future contributors will read it to understand
      why this design was chosen.
    options:
      - "continue — all Tier-A forks ruled, proceed to build"
      - "halt — needs more discovery or spec work before ruling"
    is_gate: true

  # ── Stage 6 · Arc build ───────────────────────────────────────────────────
  # Kit stage 6: /arc build — build to the rulings, adversarial review rounds.
  # This stage is never skipped. NOTE on unforeseen Tier-A forks mid-build:
  # because the stage list is static and completed stages are skipped on resume,
  # an unforeseen Tier-A fork mid-build cannot be ruled by re-running stages
  # 5a/5b within this run. The honest recovery is to ABANDON this run and start
  # a FRESH conductor run (which re-runs discovery + rulings from scratch). The
  # conductor does not branch backward or inject a gate mid-run.

  - stage_id: build-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 6 · Build run"
    prompt: >
      Build the change to the ruled decisions. Implement the feature or
      fix exactly as scoped, then conduct adversarial review: test-coverage
      analysis, a shortcut-hunter pass, a security lens, and a cross-family
      reviewer. Run the test suite and confirm it is green. Stop at
      PR-ready — do not merge. If you encounter an unforeseen decision that
      was not ruled in stage 5, HALT and report it. The static stage list does
      not branch backward or auto-inject a new gate mid-run, so the recovery is
      to abandon this run and start a fresh conductor run that re-runs discovery
      and rulings from scratch — not to resume this run after re-ruling.
    is_gate: false

  # ── Stage 7 · Verify / QA ─────────────────────────────────────────────────
  # Kit stage 7: /verify, /run, /qa, /browse
  # This stage is NOT skipped — "tests are green" and "feature works" differ.

  - stage_id: verify-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 7 · Verify and QA run"
    prompt: >
      Confirm the change actually works, not just that tests pass. Run the
      application and observe the changed behavior. For a web surface: open
      the relevant page and verify the feature behaves as specified. For a
      CLI or library: run the command or call the function and confirm the
      output. Document what was observed and any deviations from the spec.
      If nothing is runnable (a pure-docs change), state that explicitly.
    is_gate: false

  # ── Stage 8 · Security ────────────────────────────────────────────────────
  # Kit stage 8: /cso, security-review, cross-family
  # Mandatory when the change touches auth / secrets / money / untrusted input.
  # NOTE (tracked #667): each real finding ideally gets its own GateDecision.
  # Until per-item granularity is wired, all findings are batch-ruled in one gate.

  - stage_id: security-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 8a · Security review run"
    prompt: >
      Conduct a focused security review of the change. Assess whether it
      touches authentication, authorization, secrets handling, stored data,
      money or billing, or any untrusted user input. For each surface:
      identify specific findings (concrete risks with exploitation path),
      not vague suggestions. Run a cross-family perspective: what would a
      different model family catch that same-family review misses, especially
      for path/injection vulnerabilities? If the change touches none of
      those surfaces, state that explicitly with your assessment.
    is_gate: false

  - stage_id: security-findings-gate
    model: claude-opus-4-7-20260101
    label: "Stage 8b · Security findings gate"
    # NOTE: ideally one gate per finding; batched into one per Grok ruling.
    # Per-item granularity tracked in #667.
    prompt: >
      Review the security findings from the previous stage and rule on each
      one: accept (acknowledge the risk and proceed), avoid (fix the finding
      before shipping), or note as out-of-scope. If there are no findings,
      confirm the security assessment clears and continue. Your rationale
      for each finding becomes the security audit record for this change.
    options:
      - "continue — all findings ruled (accepted or fixed), security gate clears"
      - "halt — findings require fixes before proceeding"
    is_gate: true

  # ── Stage 9 · Ship / merge gate ───────────────────────────────────────────
  # Kit stage 9: /ship → merge approval.
  # conflict_keys: ["merge:main"] serializes the merge-APPROVAL gate: only one
  # run holds the merge decision at a time; a second run needing the key defers
  # behind this gate until it is answered. The key is RELEASED the moment the
  # gate is answered (held conflict keys clear in the answer transition), so it
  # serializes the human approval, not the raw git merge in stage 10a. Because
  # the key clears at answer time, two concurrent runs CAN still reach concurrent
  # real merges in stage 10a if both gates are answered close together. This key
  # serializes the approval decision, not the irreversible merge — do not rely on
  # it to prevent concurrent merges to `main`. Treat answering this gate as the
  # commit point — approve only when you are ready for the merge to proceed.

  - stage_id: ship-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 9a · Ship run"
    prompt: >
      Run the full ship pipeline: execute tests, write the CHANGELOG entry
      for this change (feature summary + issue reference), bump the version
      following the project's SemVer convention, create clean bisectable
      commits, push the branch, and open the pull request. Stop at the
      open PR — do not merge. The PR body must capture the why (rationale,
      decisions made, security posture) not just the what (diff summary).
    is_gate: false

  - stage_id: merge-gate
    model: claude-opus-4-7-20260101
    label: "Stage 9b · Merge gate (irreversible)"
    prompt: >
      Review the open pull request as if you were reading it cold for the
      first time as a reviewer. Check: does the diff match the spec? Are the
      commit messages bisectable? Is the CHANGELOG entry accurate? Are any
      tests missing for the new surface? Once you have reviewed it, rule:
      continue to approve the merge, or halt to request changes first.
      Answering this gate is the commit point: the merge:main key serializes
      the approval, and stage 10a performs the merge under your approval.
    options:
      - "continue — PR reviewed and approved, proceed to merge"
      - "halt — changes required before merging"
    # merge:main is the conflict key for the merge-APPROVAL gate (it is released
    # when the gate is answered, not held through the stage-10a merge — see the
    # serialization note above). A second run needing merge:main defers behind
    # this gate while THIS run is suspended at it.
    conflict_keys: ["merge:main"]
    is_gate: true

  # ── Stage 10 · Deploy / rollback gate ────────────────────────────────────
  # Kit stage 10: /land-and-deploy, /canary, /benchmark
  # Only active once the project has a live deployed surface.

  - stage_id: deploy-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 10a · Deploy and watch run"
    prompt: >
      Merge and deploy the change. After deploying: verify production is
      healthy (check error rates, latency, the core user flows). Run a
      canary check if available. If you detect a regression: document it,
      hold the rollback gate, and surface the finding so the human can
      rule. If no deployed surface exists yet, state that explicitly and
      recommend skipping the rollback gate.
    is_gate: false

  - stage_id: rollback-gate
    model: claude-opus-4-7-20260101
    label: "Stage 10b · Rollback decision gate"
    prompt: >
      Review the deploy outcome from the previous stage and rule on the
      production posture. Continue if production is healthy and the deploy
      succeeded. Skip if no deployed surface exists. Halt (with a rollback
      instruction in your rationale) if a regression was detected that
      requires rolling back before the run can be considered complete.
    options:
      - "continue — production healthy, deploy succeeded"
      - "skip — no deployed surface yet, gate not applicable"
      - "halt — regression detected, rollback required"
    is_gate: true

  # ── Stage 11 · Document ───────────────────────────────────────────────────
  # Kit stage 11: /document-release — sync docs to what actually shipped.
  # This stage runs after every ship. It is never skipped for a user/operator
  # facing change.

  - stage_id: document-run
    model: claude-sonnet-4-6-20260101
    label: "Stage 11 · Document run"
    prompt: >
      Sync the documentation to what actually merged and deployed. Update
      the README's "what's shipped" table, any operator-facing docs that
      describe the changed feature, and any architecture diagrams that no
      longer reflect reality. The test: a new operator reading the docs
      should understand the shipped state without finding any drift from
      what is actually in the code. If the change touched nothing
      user- or operator-facing, state that explicitly.
    is_gate: false
```

---

> **Context only (not a conductor stage): Stage 12 — Ongoing maintenance.**
> Not part of a single change run. The rhythm that keeps a project healthy:
> code-quality dashboard (periodic), engineering retrospective (weekly),
> systematic root-cause debugging when something breaks, and capturing
> hard-won lessons. These run on their own schedule, not per-change.

---

## Per-stage model dials

Each stage in the YAML block above carries a `model:` dial. The conductor
**parses** these fields but does **not yet apply** them at dispatch time — every
stage currently runs on the agent's configured `model.md` model, and `run()`
emits a warning on each run()/resume() process that a per-stage `model:` was set
but not honored.
Per-stage actor-model wiring is tracked in [#668](https://github.com/dep0we/atomic-agents-stack/issues/668); the dials
are authored now so the intended pattern is recorded and applies the moment the
wiring lands.

The dial pattern for this playbook:
- Gate stages (`is_gate: true`): a higher-reasoning model (Opus) to surface
  context clearly for the human reviewer.
- Automated stages (`is_gate: false`): a capable but cheaper model (Sonnet)
  for the bulk of the legwork.

## Querying the decision ledger

Every gate ruling in this playbook — from the go/no-go through the merge gate —
is recorded in the conductor decision ledger with the ruling's author, timestamp,
and rationale. To browse the decisions for a completed run, query the run's
`goals/<conductor_run_id>/goal_history.jsonl` directly for events named
`conductor_gate_answered`.

An optional `conductor decisions <run_id>` CLI verb to surface the ledger in
human-readable form is tracked in [#669](https://github.com/dep0we/atomic-agents-stack/issues/669).

## Stage 0 and stage 12 placement

Stages 0 (project bootstrap) and 12 (ongoing maintenance) are documented as
context sections above and below the stage block. They are **not** conductor
stages — they do not appear in the YAML block, and they are never dispatched
as sub-goals. Adding them to the YAML block would cause the conductor to attempt
to dispatch them on every change run, producing nonsensical outcomes.
