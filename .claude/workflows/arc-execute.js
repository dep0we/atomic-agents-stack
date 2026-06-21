export const meta = {
  name: 'arc-execute',
  description: 'Phase 2 of the quality loop. Takes the maintainer\'s rulings on the Tier A forks and builds the change: parallel prep fan-out -> implement -> adversarial review IN ROUNDS (including a dedicated shortcut-hunter) -> doc-release stale-marker sweep. Stops at PR-ready; never merges, tags, or publishes. Halts and returns any NEW unforeseen Tier A fork instead of deciding it.',
  phases: [
    { title: 'Lessons' },
    { title: 'Prep' },
    { title: 'Implement' },
    { title: 'Review' },
    { title: 'DocSweep' },
  ],
}

// ---- inputs -------------------------------------------------------------
// args.issue     : issue number or scope description
// args.decisions : { "<forkId>": "<chosen option>" }  — the maintainer's rulings from arc-discovery
// args.maxRounds : adversarial rounds cap (default 3; minimum 2 per rule 11)
// Defensive: accept args as an object OR a JSON string (the loop must not silently mis-fire).
let A = args ?? {}
if (typeof A === 'string') { try { A = JSON.parse(A) } catch { A = {} } }
const ISSUE = A.issue ?? 'the scope on the current branch'
const DECISIONS = A.decisions ?? {}
const MAX_ROUNDS = Math.max(2, A.maxRounds ?? 3)
const RULINGS = Object.entries(DECISIONS).map(([k, v]) => `  - ${k}: ${v}`).join('\n') || '  (none — no Tier A forks)'

// ---- Phase 0: load the project's hard-won lessons (close the leverage loop) ----
// Workflow scripts are sandboxed (no fs access), but a subagent reads the project
// memory dir and distills the relevant feedback_*.md lessons into a compact brief,
// which is injected into every prep/implement/review prompt below — so the build
// APPLIES hard-won lessons instead of repeating what already failed. The retrieval
// path used to be fragile (it depended on the main loop remembering to hand-inject
// the right lesson into the right subagent); this makes it structural. Degrades to
// an empty string if the memory dir is absent (e.g. a fresh clone / other machine).
phase('Lessons')
const LESSONS_RAW = await agent(
  `Load this project's hard-won engineering lessons so this build can APPLY them instead of repeating what already failed.
Compute the memory dir: \`DIR="$HOME/.claude/projects/$(pwd | sed 's#/#-#g')/memory"\`. Read "$DIR/MEMORY.md" (the index — one line per lesson). Then read the feedback_*.md bodies MOST relevant to building AND adversarially reviewing issue ${ISSUE} — ALWAYS include the test-discipline lessons (false-green / per-invocation negative control, layered-except typed-branch false-green, mock side-effect shape-keying), the review-discipline lessons (round-2 catches round-1 fix regressions, /ship gates discover past arc convergence), and the git-safety lessons (never git-checkout uncommitted work); PLUS any whose index line matches this issue's domain.
Distill into a COMPACT, actionable brief: one bullet per lesson = the rule + the one-line "how to apply" + (if present) the concrete shape/file it bites. Max ~14 bullets, terse, imperative ("When X, do Y — verified by Z"), NOT prose. This brief is injected verbatim into every downstream prep / implement / review subagent.
If the dir or MEMORY.md does not exist, return EXACTLY: NO PROJECT LESSONS FOUND`,
  { label: 'load-lessons', phase: 'Lessons', model: 'sonnet' },
)
const LESSONS = (LESSONS_RAW && !/^NO PROJECT LESSONS FOUND/.test(String(LESSONS_RAW).trim()))
  ? `\nPROJECT LESSONS (hard-won — APPLY these; do NOT repeat what they warn against):\n${String(LESSONS_RAW).trim()}\n`
  : ''
log(LESSONS ? 'Loaded project lessons brief — injecting into prep/implement/review' : 'No project lessons found — proceeding without a lessons brief')

// The failure-mode dimensions the prep fan-out parametrizes on (the project's pre-impl prep pattern).
const PREP_DIMENSIONS = [
  { key: 'protocol-conformance', prompt: 'Conformance to the backend Protocol pattern (dataclasses, capability advertisement, WritePolicy, filesystem default) and the spec doc that governs this scope.' },
  { key: 'cost-and-guardrails', prompt: 'Cost guardrails and the audit/JSONL trail: does any new code path escape its cost gate, default critical=True, or fail to write its run_id / parent_run_id line?' },
  { key: 'atomicity-idempotency', prompt: 'Atomic writes, idempotent teardown, crash-recoverability, TOCTOU races, and dedupe correctness.' },
  { key: 'throughline', prompt: 'The home-user vs org throughline: does anything here optimize one shape while degrading the other?' },
  { key: 'docs-and-spec-drift', prompt: 'Spec/doc drift: will any doc, docstring, or error message claim something the implementation does not do?' },
]

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['severity', 'location', 'issue', 'fix'],
        properties: {
          severity: { type: 'string', enum: ['P0', 'P1', 'P2'] },
          location: { type: 'string' },
          issue: { type: 'string' },
          fix: { type: 'string' },
        },
      },
    },
  },
}

const IMPL_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['summary', 'filesTouched', 'newTierAForks'],
  properties: {
    summary: { type: 'string' },
    filesTouched: { type: 'array', items: { type: 'string' } },
    // The unforeseen-fork escape hatch: the implementer must NOT decide a new material fork.
    newTierAForks: {
      type: 'array',
      description: 'any material (Tier A) fork encountered that was NOT in the maintainer\'s rulings — STOP, do not decide, record it here',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'why', 'options'],
        properties: {
          title: { type: 'string' },
          why: { type: 'string', description: 'which Tier A tripwire it hits' },
          options: { type: 'array', items: { type: 'string' } },
        },
      },
    },
  },
}

const SHORTCUT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['shortcuts'],
  properties: {
    shortcuts: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['severity', 'location', 'easyPath', 'rightPath', 'principleViolated'],
        properties: {
          severity: { type: 'string', enum: ['P0', 'P1', 'P2'], description: 'P0/P1 = a real corner-cut compromising correctness, security, audit-honesty, or the throughline (BLOCKS). P2 = marginal/maintainability (a comment that will rot, an edge no code path hits) — reported, does not block.' },
          location: { type: 'string' },
          easyPath: { type: 'string', description: 'the corner that got cut' },
          rightPath: { type: 'string', description: 'the best-in-class alternative' },
          principleViolated: { type: 'string' },
        },
      },
    },
  },
}

// Tier A governing-doc tripwire. A bot must NEVER author a framework-principle
// amendment unattended (the #345 build wrote a CLAUDE.md Principle #1 / TENSIONS
// T15 amendment + filed an issue WITHOUT halting — the judgment-based halt missed
// it). This makes the halt deterministic: any edit to a governing doc forces a
// halt-and-return regardless of what the implementer self-reported. The decision
// content may be sound — that is not the point; a Tier A governing-doc change is
// the maintainer's to rule, shipped as its own docs PR.
const TRIPWIRE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['tripped', 'paths', 'detail'],
  properties: {
    tripped: { type: 'boolean' },
    paths: { type: 'array', items: { type: 'string' } },
    detail: { type: 'string' },
  },
}

async function governingDocTripwire(stage) {
  return await agent(
    `Tier A GUARDRAIL CHECK for issue ${ISSUE} (deterministic — do NOT judge whether any change is good; only whether it touches a governing doc). Read only; edit/revert nothing. Diff against the base branch \`main\` so the check catches changes whether the build committed them or left them uncommitted in the working tree. Run:
  git --no-pager diff main --stat
  git --no-pager diff main -- docs/TENSIONS.md
  git --no-pager diff main -- CLAUDE.md
Set \`tripped: true\` if EITHER holds:
  (1) docs/TENSIONS.md has ANY change (new/edited tension, ledger row, date, cross-ref), OR
  (2) CLAUDE.md changed inside its GOVERNING region — the throughline, the "## Design principles" section, any "### N." principle (esp. Principle #1 "the vault is the source of truth"), or the aesthetic rules.
A change ONLY to CLAUDE.md's status / backend-count table or the "Where things live" table is routine doc-accounting — that is NOT a trip; set tripped:false for those.
Report the offending paths + a one-line detail of what region changed.`,
    { schema: TRIPWIRE_SCHEMA, label: `tripwire:${stage}`, phase: 'Implement', model: 'sonnet' },
  )
}

// ---- Phase 1: prep fan-out ---------------------------------------------
phase('Prep')
const prep = (await parallel(PREP_DIMENSIONS.map(d => () =>
  agent(
    `Pre-implementation risk scan for issue ${ISSUE}. Read the issue and CLAUDE.md first.
DIMENSION: ${d.prompt}
the maintainer has already ruled on these material forks — treat them as fixed constraints, do not re-litigate:
${RULINGS}
${LESSONS}List concrete risks the implementer must handle, each with a severity and a fix. Empty list if none.`,
    { schema: FINDINGS_SCHEMA, label: `prep:${d.key}`, phase: 'Prep', model: 'sonnet' },
  ),
))).filter(Boolean).flatMap(r => r.findings)
log(`Prep surfaced ${prep.length} findings to hand the implementer`)

// ---- Phase 2: implement -------------------------------------------------
phase('Implement')
const impl = await agent(
  `Implement issue ${ISSUE} on the current branch. Read CLAUDE.md and the governing spec doc first.
THE MAINTAINER'S RULINGS on the material forks (these are FIXED — build exactly to them, never substitute an easier option):
${RULINGS}
PREP FINDINGS to address as you build:
${JSON.stringify(prep, null, 2)}
${LESSONS}Follow every design principle in CLAUDE.md. Write tests that EXERCISE EVERY NEW SURFACE END-TO-END — if you add an HTTP
route, hit it with a test client; if you add a CLI command, invoke it; do NOT test only helper functions while the real
surface goes unrun. A green suite that never executes the new code path is NOT acceptance — it is the exact gap an
adversarial reviewer will expose. Run \`uv run pytest\` on your new tests AND confirm they actually invoke the new code.
CRITICAL: if you hit a NEW material decision (anything matching a Tier A tripwire) that the maintainer did NOT rule on, DO NOT
decide it. Stop work on that thread, leave it unimplemented, and record it in newTierAForks. A silent material decision
is a worse outcome than an incomplete branch.
HARD RULE — NO EXCEPTIONS: you may NEVER edit a governing doc. Do not touch docs/TENSIONS.md, and do not edit CLAUDE.md's
throughline, "## Design principles" section, any "### N." principle, or the aesthetic rules. (Updating CLAUDE.md's
status / backend-count table is fine — that is routine doc-accounting.) If the work seems to require amending a principle or
recording a tension, that is the single most Tier A decision possible: STOP, leave it unwritten, and record it in
newTierAForks for the maintainer to rule and ship as its own docs PR. Amending the framework's governing docs unattended is
never acceptable, even when the change looks correct.`,
  { schema: IMPL_SCHEMA, label: 'implement', phase: 'Implement', model: 'sonnet' },
)

// Honest halt: an unforeseen material fork goes back to the maintainer rather than getting decided unattended.
if (impl?.newTierAForks?.length) {
  log(`HALT: implementer hit ${impl.newTierAForks.length} unforeseen Tier A fork(s) — returning to the maintainer, no merge.`)
  return { issue: ISSUE, status: 'blocked-on-decision', newTierAForks: impl.newTierAForks, impl }
}

// Deterministic governing-doc tripwire — does NOT rely on the implementer self-reporting.
// Catches the failure where the build amended a CLAUDE.md principle / TENSIONS.md entry
// without flagging it (the #345 gap). Halt-and-return for the maintainer regardless.
const implTrip = await governingDocTripwire('post-implement')
if (implTrip?.tripped) {
  log(`HALT: governing-doc tripwire fired (${(implTrip.paths || []).join(', ')}) — a framework-principle/TENSIONS edit must be the maintainer's, shipped as its own docs PR.`)
  return {
    issue: ISSUE,
    status: 'blocked-on-decision',
    newTierAForks: [{
      title: 'Build edited a governing doc (CLAUDE.md principle / TENSIONS.md) — must be maintainer-ruled',
      why: implTrip.detail || 'governing-doc tripwire',
      options: [
        'Maintainer rules the change; it ships as its own docs PR (decision content may be sound — that is separate from the autonomy boundary)',
        'Revert the governing-doc edit from this branch and proceed with the rest of the build',
      ],
    }],
    tripwire: implTrip,
    impl,
  }
}

// ---- Phase 3: adversarial review IN ROUNDS (rule 11) --------------------
phase('Review')
let round = 0
let lastFixDiff = 'initial implementation'
const roundLog = []
let converged = false
let openFindings = []
let openShortcuts = []
let reportedP2s = [] // non-blocking polish; surfaced to the maintainer, never gates convergence
while (round < MAX_ROUNDS) {
  round++
  // Five lenses run concurrently; the shortcut-hunter is a first-class reviewer, not an afterthought.
  const lenses = [
    { key: 'correctness', prompt: 'Find correctness bugs, edge cases, and regressions.' },
    { key: 'cost-audit', prompt: 'Find any LLM code path missing a cost gate, any default critical=True, any missing/broken audit JSONL line.' },
    { key: 'spec-conformance', prompt: 'Find divergence between the implementation and its governing spec doc / Protocol contract.' },
    { key: 'shortcut-hunt', prompt: 'Assume a corner was cut. Find where the implementation took the EASY path over the best-in-class one. Each finding: the easy path taken, the right path, the named CLAUDE.md principle it violates, AND a severity — P0/P1 if it compromises correctness/security/audit-honesty/the throughline (these block the PR), P2 if marginal or maintainability-only (a comment that will rot, an edge no code path hits; reported, does not block). Grade honestly: do not inflate a rot-someday nit to P1, nor deflate a real corner-cut to P2.' },
    { key: 'docs-reality', prompt: 'Find any doc/docstring/error-message claim that does not match what the code actually does.' },
  ]
  const reviews = (await parallel(lenses.map(l => () =>
    agent(
      `Adversarial review round ${round} of issue ${ISSUE}. This round, scrutinize the most recent change especially: ${lastFixDiff}.
LENS: ${l.prompt}
${LESSONS}Read the diff yourself (\`git diff\`) and run whatever commands you need. Report real findings only, each with severity and a concrete fix. Where a PROJECT LESSON above names a failure mode this lens could hit (e.g. a false-green test that would pass with the fix stripped, esp. a compound boolean where each operand needs its own negative control), actively check for it.`,
      { schema: l.key === 'shortcut-hunt' ? SHORTCUT_SCHEMA : FINDINGS_SCHEMA, label: `review:${l.key}:r${round}`, phase: 'Review', model: 'opus' },
    ),
  ))).filter(Boolean)

  const allFindings = reviews.flatMap(r => r.findings ?? [])
  const allShortcuts = reviews.flatMap(r => r.shortcuts ?? [])
  // Gate on zero P0/P1 (findings AND shortcuts). P2s of either kind are reported, never block — chasing them to zero
  // on hard backends is an asymptote (rot-someday comments, edges no code path hits). the maintainer's merge review adjudicates.
  const blocking = allFindings.filter(f => f.severity === 'P0' || f.severity === 'P1')
  const blockingShortcuts = allShortcuts.filter(s => s.severity === 'P0' || s.severity === 'P1')
  const p2s = [...allFindings.filter(f => f.severity === 'P2'), ...allShortcuts.filter(s => s.severity === 'P2')]
  const total = blocking.length + blockingShortcuts.length
  roundLog.push({ round, blocking: blocking.length, blockingShortcuts: blockingShortcuts.length, p2: p2s.length })
  openFindings = blocking
  openShortcuts = blockingShortcuts
  reportedP2s = p2s
  log(`Round ${round}: ${blocking.length} blocking findings + ${blockingShortcuts.length} blocking shortcuts + ${p2s.length} P2 (non-blocking)`)

  // Converged: zero blocking findings AND zero blocking shortcuts, after the mandatory round 2.
  if (total === 0 && round >= 2) { converged = true; log(`Converged at round ${round} (${p2s.length} non-blocking P2 noted).`); break }
  if (total === 0) continue

  // Opus holistic fix: plan the whole fix-set together (so a fix can't break an adjacent finding), apply, self-verify.
  const fix = await agent(
    `Round ${round} convergence pass for issue ${ISSUE}. Do NOT fix one-at-a-time. STEP 1 PLAN: read all findings + the diff
(\`git diff\`) together, write one coherent fix plan accounting for how fixes interact; fix shared root causes, not symptoms.
STEP 2 APPLY. STEP 3 SELF-VERIFY before returning: run the affected tests AND re-read your own diff hunting for the
failure CLASS each finding belongs to (precision/type, charset, comment-vs-code honesty, test vacuity, cross-backend
parity); if your fix introduced a new instance, fix it now, not next round.
${LESSONS}BLOCKING (P0/P1) findings — all must be fixed: ${JSON.stringify(blocking, null, 2)}
BLOCKING (P0/P1) shortcuts — non-negotiable, all must be fixed: ${JSON.stringify(blockingShortcuts, null, 2)}
NON-BLOCKING P2s (findings + shortcuts) — fix if cheap, fine to skip: ${JSON.stringify(p2s, null, 2)}
Return a one-line description of the diff you produced (this becomes the next round's focus).`,
    { label: `fix:r${round}`, phase: 'Review', model: 'opus' },
  )
  lastFixDiff = fix ?? `round ${round} fixes`
}

// ---- Convergence gate: "pr-ready" REQUIRES a clean review round --------
// Hitting the round cap with findings still open is NOT done. Escalate to the maintainer with the open items instead of
// silently claiming pr-ready. The last round's fix was applied but never re-reviewed, so it is unverified.
if (!converged) {
  log(`DID NOT CONVERGE after ${round} rounds — ${openFindings.length} blocking + ${openShortcuts.length} shortcuts open. Escalating; NOT pr-ready.`)
  return {
    issue: ISSUE,
    status: 'did-not-converge',
    rulingsApplied: DECISIONS,
    filesTouched: impl?.filesTouched ?? [],
    rounds: roundLog,
    openFindings, // P0/P1 only
    openShortcuts,
    reportedP2s, // non-blocking polish for the maintainer's judgment
    note: `Review hit the ${MAX_ROUNDS}-round cap with BLOCKING (P0/P1) findings or shortcuts still open. Last fixes applied but not re-reviewed — treat as unverified. Resume with higher maxRounds. NOTHING merged.`,
  }
}

// ---- Phase 4: doc-release stale-marker sweep ---------------------------
phase('DocSweep')
const sweep = await agent(
  `Run a /document-release-style sweep for issue ${ISSUE}: scan README "What's shipped" tables, CHANGELOG [Unreleased],
spec docs, CLAUDE.md status block, and package metadata for stale markers, count drift, or claims that no longer match
the merged change. Return concrete FIX_NOW items with their fixes, and apply them.`,
  { schema: FINDINGS_SCHEMA, label: 'doc-sweep', phase: 'DocSweep', model: 'sonnet' },
)

return {
  issue: ISSUE,
  status: 'pr-ready',
  rulingsApplied: DECISIONS,
  filesTouched: impl?.filesTouched ?? [],
  rounds: roundLog,
  converged: true,
  reportedP2s, // non-blocking polish noted at convergence — the maintainer's call whether to address pre-merge
  docSweepFixes: sweep?.findings ?? [],
  note: `Converged at round ${round} (zero blocking P0/P1 + zero shortcuts; ${reportedP2s.length} non-blocking P2s noted in reportedP2s), then doc-swept. Verify the test suite independently before shipping. NOTHING irreversible has happened — merge, tag, and publish remain with the maintainer.`,
}
