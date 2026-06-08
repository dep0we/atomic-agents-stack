export const meta = {
  name: 'arc-execute',
  description: 'Phase 2 of the quality loop. Takes the maintainer\'s rulings on the Tier A forks and builds the change: parallel prep fan-out -> implement -> adversarial review IN ROUNDS (including a dedicated shortcut-hunter) -> doc-release stale-marker sweep. Stops at PR-ready; never merges, tags, or publishes. Halts and returns any NEW unforeseen Tier A fork instead of deciding it.',
  phases: [
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

// ---- Phase 1: prep fan-out ---------------------------------------------
phase('Prep')
const prep = (await parallel(PREP_DIMENSIONS.map(d => () =>
  agent(
    `Pre-implementation risk scan for issue ${ISSUE}. Read the issue and CLAUDE.md first.
DIMENSION: ${d.prompt}
the maintainer has already ruled on these material forks — treat them as fixed constraints, do not re-litigate:
${RULINGS}
List concrete risks the implementer must handle, each with a severity and a fix. Empty list if none.`,
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
Follow every design principle in CLAUDE.md. Write tests that EXERCISE EVERY NEW SURFACE END-TO-END — if you add an HTTP
route, hit it with a test client; if you add a CLI command, invoke it; do NOT test only helper functions while the real
surface goes unrun. A green suite that never executes the new code path is NOT acceptance — it is the exact gap an
adversarial reviewer will expose. Run \`uv run pytest\` on your new tests AND confirm they actually invoke the new code.
CRITICAL: if you hit a NEW material decision (anything matching a Tier A tripwire) that the maintainer did NOT rule on, DO NOT
decide it. Stop work on that thread, leave it unimplemented, and record it in newTierAForks. A silent material decision
is a worse outcome than an incomplete branch.`,
  { schema: IMPL_SCHEMA, label: 'implement', phase: 'Implement', model: 'sonnet' },
)

// Honest halt: an unforeseen material fork goes back to the maintainer rather than getting decided unattended.
if (impl?.newTierAForks?.length) {
  log(`HALT: implementer hit ${impl.newTierAForks.length} unforeseen Tier A fork(s) — returning to the maintainer, no merge.`)
  return { issue: ISSUE, status: 'blocked-on-decision', newTierAForks: impl.newTierAForks, impl }
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
Read the diff yourself (\`git diff\`) and run whatever commands you need. Report real findings only, each with severity and a concrete fix.`,
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
BLOCKING (P0/P1) findings — all must be fixed: ${JSON.stringify(blocking, null, 2)}
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
