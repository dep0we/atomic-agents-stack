export const meta = {
  name: 'arc-finish',
  description: 'Drive an EXISTING build (already on the current branch) to a clean, converged state: adversarial review IN ROUNDS (incl. shortcut-hunter) until a full round finds zero findings, then a doc-release sweep. Does NOT re-run prep or re-implement — use this to finish a branch arc-execute left short of convergence. Stops at PR-ready; never merges. Escalates open findings if it still cannot converge.',
  phases: [
    { title: 'Review' },
    { title: 'DocSweep' },
  ],
}

// ---- inputs -------------------------------------------------------------
let A = args ?? {}
if (typeof A === 'string') { try { A = JSON.parse(A) } catch { A = {} } }
const ISSUE = A.issue ?? 'the scope on the current branch'
const BASE = A.base ?? 'main'
const MAX_ROUNDS = Math.max(2, A.maxRounds ?? 5)
// Optional: known residual findings to seed round 1 so we don't burn a round rediscovering them.
const SEED = Array.isArray(A.seedFindings) ? A.seedFindings : []

const FINDINGS_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['findings'],
  properties: { findings: { type: 'array', items: {
    type: 'object', additionalProperties: false, required: ['severity', 'location', 'issue', 'fix'],
    properties: {
      severity: { type: 'string', enum: ['P0', 'P1', 'P2'] },
      location: { type: 'string' }, issue: { type: 'string' }, fix: { type: 'string' },
    },
  } } },
}
const SHORTCUT_SCHEMA = {
  type: 'object', additionalProperties: false, required: ['shortcuts'],
  properties: { shortcuts: { type: 'array', items: {
    type: 'object', additionalProperties: false, required: ['severity', 'location', 'easyPath', 'rightPath', 'principleViolated'],
    properties: {
      severity: { type: 'string', enum: ['P0', 'P1', 'P2'], description: 'P0/P1 = a real corner-cut compromising correctness, security, audit-honesty, or the throughline (BLOCKS). P2 = marginal/maintainability (a comment that will rot, an edge no code path hits) — reported, does not block.' },
      location: { type: 'string' }, easyPath: { type: 'string' },
      rightPath: { type: 'string' }, principleViolated: { type: 'string' },
    },
  } } },
}

phase('Review')
let round = 0
let lastFixDiff = SEED.length
  ? `the prior arc-execute run left these known-open items: ${JSON.stringify(SEED)}`
  : 'the full branch diff vs ' + BASE
const roundLog = []
let converged = false
let openFindings = []
let openShortcuts = []
let reportedP2s = []

// Seed: fix the known residuals first so round 1's review verifies real progress, not the same backlog.
if (SEED.length) {
  await agent(
    `Issue ${ISSUE}: a prior review left these KNOWN-OPEN items on the current branch. Fix every one; after fixing, run the
affected tests and confirm they pass AND actually exercise the changed code path (a missing-coverage finding must add a
test that invokes the real surface, not just imports it). For any item you judge already-resolved, verify it against the
current code (\`git diff ${BASE}\`) before dismissing it.
KNOWN-OPEN: ${JSON.stringify(SEED, null, 2)}
Return a one-line description of the diff you produced.`,
    { label: 'seed-fix', phase: 'Review', model: 'opus' },
  )
}

const seen = new Map() // finding signature -> rounds-seen count, drives sticky-finding deep-fix
const sig = (x) => (x.location || '').split(/[:(]/)[0].trim() + '|' + ((x.issue || x.easyPath || '').slice(0, 40))
while (round < MAX_ROUNDS) {
  round++
  const lenses = [
    { key: 'correctness', prompt: 'Find correctness bugs, edge cases, and regressions.' },
    { key: 'cost-audit', prompt: 'Find any LLM code path missing a cost gate, any default critical=True, any missing/broken audit JSONL line.' },
    { key: 'spec-conformance', prompt: 'Find divergence between the implementation and its governing spec doc / Protocol contract, and any acceptance-criterion in the issue not yet met.' },
    { key: 'shortcut-hunt', prompt: 'Assume a corner was cut. Build/run the real surface yourself (e.g. a test client against the HTTP routes) — do not trust the existing tests. Find where the implementation took the EASY path over the best-in-class one. Each finding: the easy path taken, the right path, the named CLAUDE.md principle it violates, AND a severity — P0/P1 if it compromises correctness/security/audit-honesty/the throughline (these block the PR), P2 if it is marginal or maintainability-only (a comment that will rot, an edge no code path hits; reported, does not block). Grade honestly: do not inflate a rot-someday nit to P1, and do not deflate a real corner-cut to P2.' },
    { key: 'docs-reality', prompt: 'Find any doc/docstring/error-message claim that does not match what the code actually does, and any user-facing reference (error strings, spec links) pointing at a file or behavior that does not exist.' },
  ]
  const reviews = (await parallel(lenses.map(l => () =>
    agent(
      `Adversarial review round ${round} of issue ${ISSUE} on the current branch (diff vs ${BASE}). This round, scrutinize especially: ${lastFixDiff}.
LENS: ${l.prompt}
Read the diff yourself (\`git diff ${BASE}\`) and RUN whatever commands you need (build the app, hit the routes, run the tests). Report real findings only, each with severity and a concrete fix.`,
      { schema: l.key === 'shortcut-hunt' ? SHORTCUT_SCHEMA : FINDINGS_SCHEMA, label: `review:${l.key}:r${round}`, phase: 'Review', model: 'opus' },
    ),
  ))).filter(Boolean)

  const allFindings = reviews.flatMap(r => r.findings ?? [])
  const allShortcuts = reviews.flatMap(r => r.shortcuts ?? [])
  // Gate on zero P0/P1 (findings AND shortcuts). P2s of either kind are reported, never block — chasing them to
  // zero on hard backends is an asymptote (rot-someday comments, edges no code path hits). the maintainer's merge review adjudicates.
  const blocking = allFindings.filter(f => f.severity === 'P0' || f.severity === 'P1')
  const blockingShortcuts = allShortcuts.filter(s => s.severity === 'P0' || s.severity === 'P1')
  const p2s = [...allFindings.filter(f => f.severity === 'P2'), ...allShortcuts.filter(s => s.severity === 'P2')]
  const total = blocking.length + blockingShortcuts.length
  roundLog.push({ round, blocking: blocking.length, blockingShortcuts: blockingShortcuts.length, p2: p2s.length })
  openFindings = blocking
  openShortcuts = blockingShortcuts
  reportedP2s = p2s
  log(`Round ${round}: ${blocking.length} blocking findings + ${blockingShortcuts.length} blocking shortcuts + ${p2s.length} P2 (non-blocking)`)

  if (total === 0 && round >= 2) { converged = true; log(`Converged at round ${round} (${p2s.length} non-blocking P2 noted).`); break }
  if (total === 0) continue

  // Sticky-finding tracking: anything whose signature recurs across rounds got a prior fix that DIDN'T hold.
  const mustFix = [...blocking, ...blockingShortcuts]
  for (const f of mustFix) { const k = sig(f); seen.set(k, (seen.get(k) || 0) + 1) }
  const recurring = mustFix.filter(f => (seen.get(sig(f)) || 0) >= 2)
  if (recurring.length) log(`Round ${round}: ${recurring.length} RECURRING finding(s) — prior fixes did not hold, escalating to root-cause fix.`)

  // Convergence pass is Opus-grade and HOLISTIC: plan the whole fix-set first (so a fix can't break an adjacent
  // finding), apply coherently, then self-verify before returning. This is the lever against round-by-round oscillation.
  const fix = await agent(
    `Round ${round} convergence pass for issue ${ISSUE}. Do NOT fix findings one-at-a-time. Work in three explicit steps:

STEP 1 — PLAN: read ALL the findings below together and the current diff (\`git diff ${BASE}\`). Write a single coherent
fix plan that accounts for how the fixes interact — a change for one finding must not reintroduce or worsen another.
Identify any shared root cause behind multiple findings and fix the root, not each symptom.

STEP 2 — APPLY: implement the whole plan.

STEP 3 — SELF-VERIFY before returning: run the affected tests AND re-read your own diff specifically hunting for the
failure CLASS each finding belongs to (e.g. precision/type mismatches, charset rules, comment-vs-code honesty,
test vacuity, cross-backend parity). If your own fix introduced a new instance of any class, fix it now — do not leave
it for the next round. A fix that passes tests but says one thing while doing another is not done.

RECURRING findings (a prior round already 'fixed' these and the fix did NOT hold — find and fix the ROOT cause, and say
in your summary why the earlier attempt failed): ${JSON.stringify(recurring, null, 2)}
BLOCKING (P0/P1) findings — all must be fixed: ${JSON.stringify(blocking, null, 2)}
BLOCKING (P0/P1) shortcuts — non-negotiable, all must be fixed: ${JSON.stringify(blockingShortcuts, null, 2)}
NON-BLOCKING P2s (findings + shortcuts) — fix if cheap, fine to skip: ${JSON.stringify(p2s, null, 2)}

Return a one-line description of the diff you produced (becomes the next round's review focus).`,
    { label: `fix:r${round}`, phase: 'Review', model: 'opus' },
  )
  lastFixDiff = fix ?? `round ${round} fixes`
}

// Convergence gate: pr-ready REQUIRES a clean round.
if (!converged) {
  log(`DID NOT CONVERGE after ${round} rounds — ${openFindings.length} findings + ${openShortcuts.length} shortcuts open. Escalating; NOT pr-ready.`)
  return {
    issue: ISSUE, status: 'did-not-converge', rounds: roundLog,
    openFindings, openShortcuts, reportedP2s,
    note: `Hit the ${MAX_ROUNDS}-round cap with BLOCKING (P0/P1) findings or shortcuts still open; last fix unverified. Re-run with a higher maxRounds, or address by hand. NOTHING merged.`,
  }
}

phase('DocSweep')
const sweep = await agent(
  `Run a /document-release-style sweep for issue ${ISSUE}: scan README "What's shipped" tables, CHANGELOG [Unreleased],
spec docs, CLAUDE.md status block, and package metadata for stale markers, count drift, or claims that no longer match the
branch. Return concrete FIX_NOW items with their fixes, and apply them.`,
  { schema: FINDINGS_SCHEMA, label: 'doc-sweep', phase: 'DocSweep', model: 'sonnet' },
)

return {
  issue: ISSUE, status: 'pr-ready', converged: true, rounds: roundLog,
  reportedP2s,
  docSweepFixes: sweep?.findings ?? [],
  note: `Converged at round ${round} (zero blocking P0/P1 findings + zero blocking shortcuts; ${reportedP2s.length} non-blocking P2s noted in reportedP2s for the maintainer's merge review), then doc-swept. Verify the test suite independently before shipping. NOTHING irreversible has happened — merge, tag, publish remain with the maintainer.`,
}
