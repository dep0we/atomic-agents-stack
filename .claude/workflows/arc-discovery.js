export const meta = {
  name: 'arc-discovery',
  description: 'Phase 1 of the quality loop. Reads an issue, surfaces every decision fork, classifies each by materiality tier (A escalate / B decide+justify / C just-do-it), runs a two-voice adversarial panel on every Tier A fork, and returns plain-language decision packets for the maintainer to rule on. Makes ZERO code changes.',
  phases: [
    { title: 'Discover' },
    { title: 'Re-classify' },
    { title: 'Panel' },
  ],
}

// ============================================================================
// PORTABLE arc-discovery — part of the dev-process-kit.
//
// The decision-first quality loop, generalized from a production quality
// loop. Project-specific rules (what counts as a "must-ask-the-maintainer"
// decision, which design docs to read, the project's name) are NOT hardcoded
// here — they arrive via `args.config`, which the /arc skill loads from the
// target repo's `arc.config.json` and injects. If no config is supplied, the
// baked-in DEFAULTS below are sound, project-agnostic versions so the kit runs
// usefully on day one and a project specializes by writing its own config.
// ============================================================================

// ---- SANDBOX RESTRICTIONS (issue #6 — worktree isolation) ------------------
// This workflow runs inside a sandboxed Workflow runtime. The following are
// FORBIDDEN here — they throw or return empty/undefined in the sandbox:
//   - new Date()          → resolve timestamps in the calling skill, inject via args
//   - Math.random()       → use a deterministic id or have the skill inject one
//   - os.homedir()        → resolve paths in the skill layer, inject via args
//   - os.tmpdir()         → same — the skill owns all path resolution
//   - require('fs'), require('os'), require('child_process')  → not available
//   - process.env.HOME    → empty in sandbox; resolve in skill layer
//
// Read-only discovery (issue #6) is ENFORCED by the /arc SKILL's net-state
// assertion (discovery-pre/discovery-verify against the MAIN repo), NOT by this
// file and NOT by the worktree. This workflow does ZERO file I/O: DESIGN_DOCS
// below is only interpolated into the agent() prompts, and the sub-agents read
// AND write files via THEIR own tool cwd (= the main repo). So this script does
// not (and cannot) re-root any path to the worktree — there is no
// `workingDir`-anchored path resolution here, and nothing the sub-agents do is
// routed through the worktree. The skill creates a throwaway worktree as a
// defense-in-depth scaffold, but a sub-agent write — like a read — lands in the
// MAIN repo and is DETECTED by the net-state assertion, not prevented by the
// worktree. Do not add file/path resolution here expecting it to be
// worktree-anchored; resolve in the skill and inject content via args instead.
// ---- inputs -------------------------------------------------------------
// args.issue          : issue number or scope description (required)
// args.codexAvailable : true if the main loop probed a cross-family model (Codex) and it answered
// args.config         : per-project rules (see arc.config.example.jsonc); optional
// Defensive: accept args as an object OR a JSON string (the loop must not silently mis-fire).
let A = args ?? {}
let ARGS_UNPARSEABLE = false
// A JSON string that parses to `null`/a scalar/`"…"` does NOT throw, so ARGS_UNPARSEABLE
// alone would stay false and the guard below would dereference `A.issue` on that null and
// throw an uncaught TypeError instead of returning bad-args (issue #61). Treat any
// non-object parse result as unparseable so null/primitives route uniformly to bad-args.
if (typeof A === 'string') { try { const P = JSON.parse(A); if (P && typeof P === 'object') { A = P } else { A = {}; ARGS_UNPARSEABLE = true } } catch { A = {}; ARGS_UNPARSEABLE = true } }
// F1 (issue #61): a Workflow `args` payload that is absent, unparseable, or carries no
// usable `issue` (missing, non-string, or empty) must halt immediately and loudly — never
// silently proceed on defaults. This is the known failure mode where an oversized args
// payload silently collapses to {} in the real Workflow runtime (memory: arc-execute-compact-args),
// which would otherwise run a full discovery against a phantom "the scope on the current
// branch" issue. Return before any agent() call so a malformed run burns zero agent spend.
// `issue` must be a non-empty STRING, not merely truthy: an object/array issue (e.g.
// `{"issue":{}}`) is truthy, would pass a `!A.issue` check, and then stringify to
// `[object Object]` / a comma-joined list inside every agent prompt. A type check halts
// that garbled-issue payload as bad-args instead of silently building on it.
// The two leading operands (`args == null`, `ARGS_UNPARSEABLE`) are redundant-by-design:
// on either, A collapses to {} so `typeof A.issue !== 'string'` already fires. They are
// kept as belt-and-suspenders (they harden a future refactor that stops collapsing to {}),
// so neither is independently negative-controllable — that redundancy is intentional, not
// a missing test.
if (args == null || ARGS_UNPARSEABLE || typeof A.issue !== 'string' || !A.issue.trim()) {
  return {
    status: 'bad-args',
    issue: null,
    receivedArgsType: typeof args,
    note: 'Workflow args were absent, unparseable, or carried no usable `issue` (missing, non-string, or empty). STOP: shrink the args payload, then re-send — do not retry with the identical payload (an oversized payload silently collapses to {} in the Workflow runtime, which is the known cause). NOTHING discovered; no agent calls were made.',
  }
}
const ISSUE = A.issue
const CFG = A.config ?? {}
// Cross-family CLI is configurable (arc.config.jsonc → crossFamily). Honor enabled +
// exec here too, not just in build/finish — otherwise discovery silently hardcodes codex.
const XF = CFG.crossFamily ?? {}
const XF_ENABLED = XF.enabled !== false
const XF_EXEC_RAW = XF.exec ?? 'codex exec'
// XF_EXEC is operator-controlled (arc.config.jsonc) and is interpolated into an agent
// instruction that asks the model to run a shell command. Validate it against a strict
// allowlist of safe tokens so a value like `codex exec; rm -rf ~/` cannot smuggle shell
// metacharacters through — a malformed/injected config value must never reach an LLM
// prompt that asks the model to shell out (config is maintainer-trusted, not a hard
// process-level security boundary; this guards against a typo or a compromised config
// file, not a hostile process). The pattern allows an executable path plus space-separated
// flags/words built from [a-zA-Z0-9_./-] only — no shell metacharacters (; | & $ ` ( ) < >
// newline, quotes, etc.). (Parity with shadow-compare.js's XF_EXEC_SAFE gate — issue #61 F6.)
const XF_EXEC_SAFE = /^[a-zA-Z0-9_./-]+( [a-zA-Z0-9_./-]+)*$/.test(XF_EXEC_RAW)
const XF_EXEC = XF_EXEC_SAFE ? XF_EXEC_RAW : 'codex exec'
if (!XF_EXEC_SAFE) {
  log(`arc-discovery: configured crossFamily.exec contains unexpected characters and was rejected; falling back to 'codex exec'`)
}
const CODEX = A.codexAvailable === true && XF_ENABLED && XF_EXEC_SAFE  // use the real cross-family skeptic only if reachable, enabled, AND the exec string is safe

// ---- generic defaults (overridden by arc.config.json) -------------------
const PROJECT = CFG.projectName ?? 'this project'
const DESIGN_DOCS = CFG.designDocs ?? 'CLAUDE.md, AGENTS.md (if present), and any architecture / decisions docs under docs/'

// The materiality tripwire checklist. Embedded as text (not a prompt the agent
// writes) so classification is a mechanical check against named invariants —
// not the agent's taste. Projects override `config.tripwires` to name THEIR
// load-bearing surfaces; these defaults cover the surfaces almost every
// codebase shares.
const DEFAULT_TRIPWIRES = `
A decision is TIER A (must escalate to the maintainer — the agent may NOT decide it) if it hits ANY of:
  1. Changes a public or user-facing surface: an API endpoint, CLI command/flag, config schema, public function name or signature, or wire/serialization format.
  2. Changes a security, authentication, authorization, secrets-handling, or permissions boundary.
  3. Adds, removes, or upgrades a dependency, or introduces a new external service / integration.
  4. Changes data persistence: a DB schema, a migration, a stored format — anything hard to reverse once real data exists.
  5. Introduces a NEW architectural concept, abstraction, or pattern not already established in the codebase.
  6. Touches money, billing, rate limits, quotas, or cost controls.
  7. Is a breaking change for existing users/callers, or changes behavior people already depend on.
  8. Would warrant a new entry in the project's architecture / decisions record, or is irreversible without significant effort.

TIER B (agent may decide, but ONLY by emitting a justification record and passing the shortcut-hunter): a genuine
fork between implementations that all stay INSIDE an existing public contract / architecture — private helper structure,
error-handling strategy, test layout, internal naming, refactors that change no public surface.

TIER C (agent just does it, no record): one obvious idiomatic answer, mechanical edits, following a pattern
already established in the codebase, doc wording that matches reality.

TIE-BREAK: if you are unsure whether a fork is A or B, classify it A. Ambiguity always resolves UP.`

const TRIPWIRES = CFG.tripwires ?? DEFAULT_TRIPWIRES

const FORKS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['forks'],
  properties: {
    forks: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'title', 'tier', 'tripwires', 'description', 'options', 'leaning', 'principle'],
        properties: {
          id: { type: 'string', description: 'short kebab-case slug' },
          title: { type: 'string' },
          tier: { type: 'string', enum: ['A', 'B', 'C'] },
          tripwires: { type: 'array', items: { type: 'string' }, description: 'which numbered tripwires this hits, empty if none' },
          description: { type: 'string', description: 'what the fork actually is, in technical terms' },
          options: { type: 'array', items: { type: 'string' }, minItems: 2 },
          leaning: { type: 'string', description: 'which option looks best for long-term quality, and why' },
          principle: { type: 'string', description: 'the named design principle (from the project design docs) that governs this fork' },
        },
      },
    },
  },
}

const RETIER_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['rulings'],
  properties: {
    rulings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'tier', 'why'],
        properties: {
          id: { type: 'string' },
          tier: { type: 'string', enum: ['A', 'B', 'C'] },
          why: { type: 'string' },
        },
      },
    },
  },
}

const ADVISOR_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['recommendedOption', 'reasoning', 'shortcutWarning'],
  properties: {
    recommendedOption: { type: 'string' },
    reasoning: { type: 'string', description: 'why this option is best-in-class long-term, tied to a named principle' },
    shortcutWarning: { type: 'string', description: 'name the cheaper/easier path and why it would be the wrong call, or "none"' },
  },
}

const PACKET_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['plainTitle', 'plainFraming', 'options', 'recommendation', 'developerCaveat', 'agreement', 'divergenceNote'],
  properties: {
    plainTitle: { type: 'string' },
    plainFraming: { type: 'string', description: 'one or two plain sentences a non-developer reads cold; the product/values question underneath the technical fork' },
    options: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['label', 'consequence'],
        properties: { label: { type: 'string' }, consequence: { type: 'string' } },
      },
    },
    recommendation: { type: 'string', description: 'the recommended option label + one-line why' },
    developerCaveat: { type: 'string', description: 'the precise technical term in parens, for future searches' },
    agreement: { type: 'string', enum: ['converge', 'diverge'] },
    divergenceNote: { type: 'string', description: 'if diverge, what the two advisors disagreed on and what it hinges on; else "advisors agree"' },
  },
}

// ---- Phase 1: surface every fork ---------------------------------------
phase('Discover')
const discovered = await agent(
  `Read GitHub issue ${ISSUE} and the repo's design rules in ${DESIGN_DOCS}.
Enumerate EVERY decision fork the implementation will face — places where there is more than one defensible way to build it.
For each, classify its materiality tier using this checklist verbatim:
${TRIPWIRES}
Be exhaustive about Tier A. Missing a Tier A fork is the worst possible outcome: it means a material decision gets made silently. When in doubt, mark it A.`,
  { schema: FORKS_SCHEMA, label: 'discover-forks', phase: 'Discover', model: 'opus' },
)
const forks = discovered?.forks ?? []
log(`Discovered ${forks.length} forks (${forks.filter(f => f.tier === 'A').length} provisionally Tier A)`)

// ---- Phase 2: independent re-classification (mechanizes the tie-break) --
phase('Re-classify')
const retier = await agent(
  `Here are decision forks a first reviewer classified by materiality tier:
${JSON.stringify(forks.map(f => ({ id: f.id, title: f.title, description: f.description, firstTier: f.tier })), null, 2)}
Independently re-classify each one using this checklist verbatim — do NOT defer to the first reviewer:
${TRIPWIRES}
Your job is to catch UNDER-escalation: forks wrongly marked B or C that actually hit a Tier A tripwire.`,
  { schema: RETIER_SCHEMA, label: 'reclassify', phase: 'Re-classify', model: 'opus' },
)
// Escalate-on-disagreement: if EITHER pass says A, it's A. The asymmetric tie-break, in code.
const retierById = Object.fromEntries((retier?.rulings ?? []).map(r => [r.id, r.tier]))
const RANK = { A: 3, B: 2, C: 1 }
for (const f of forks) {
  const second = retierById[f.id]
  if (second && RANK[second] > RANK[f.tier]) {
    log(`Escalated ${f.id}: ${f.tier} -> ${second} (reviewers disagreed; resolving up)`)
    f.tier = second
  }
}
const tierA = forks.filter(f => f.tier === 'A')
const tierB = forks.filter(f => f.tier === 'B')
const tierC = forks.filter(f => f.tier === 'C')

// ---- Phase 3: two-voice adversarial panel on every Tier A fork ----------
phase('Panel')
const packets = await parallel(tierA.map(fork => async () => {
  // Advisor 1 — project-grounded (fresh Opus, reasons in the project's own principles).
  const grounded = agent(
    `You advise the maintainer on a material design decision for ${PROJECT}. Read ${DESIGN_DOCS} first. The fork:
TITLE: ${fork.title}
WHAT IT IS: ${fork.description}
OPTIONS: ${fork.options.join(' | ')}
Argue for the option that is best-in-class for LONG-TERM product quality, grounded in a NAMED principle. Then name the
cheaper/easier path explicitly and say why taking it would be the wrong call. Never recommend an option because it is
faster or less work.`,
    { schema: ADVISOR_SCHEMA, label: `panel:grounded:${fork.id}`, phase: 'Panel', model: 'opus' },
  )
  // Advisor 2 — cross-family skeptic. A cross-family model (Codex) if reachable; else an opposed-framing Opus.
  const skepticPrompt = CODEX
    ? `Run a cross-family second opinion via the configured cross-family CLI: invoke \`${XF_EXEC}\` with a SHORT prompt
describing this fork (keep it to a few sentences with the facts inline — long prompts can silently return empty), and
report its recommendation faithfully. The fork:
TITLE: ${fork.title}
WHAT IT IS: ${fork.description}
OPTIONS: ${fork.options.join(' | ')}
Then add your own adversarial read: assume the implementing agent will be tempted by the easiest option — attack that
option and say what it costs long-term. If the CLI is unreachable or returns empty, say so and reason as an independent skeptic.`
    : `You are an independent skeptic from outside this codebase's assumptions. The fork:
TITLE: ${fork.title}
WHAT IT IS: ${fork.description}
OPTIONS: ${fork.options.join(' | ')}
Assume the implementing agent will be tempted by the easiest option. Attack that option. Argue from the OPPOSITE framing
to a project-insider: weight long-term capability, correctness, and best-in-class polish over local simplicity. Name the
shortcut and its long-term cost.`
  // Sonnet wrapper is a cheap relay; the actual cross-family reasoning is the external model's (shelled out to `codex exec`).
  const skeptic = agent(skepticPrompt, { schema: ADVISOR_SCHEMA, label: `panel:skeptic:${fork.id}`, phase: 'Panel', model: 'sonnet' })

  const [g, s] = await Promise.all([grounded, skeptic])
  // Translate the two technical takes into one plain-language packet for a non-developer.
  return agent(
    `Two advisors weighed in on a material design fork. Translate this into ONE decision packet a non-developer can rule
on cold: recommendation first, consequences legible, technical terms defined inline, the precise technical term as a
trailing parenthetical for future searches.
FORK: ${fork.title} — ${fork.description}
OPTIONS: ${fork.options.join(' | ')}
GOVERNING PRINCIPLE: ${fork.principle}
PROJECT-GROUNDED ADVISOR: recommends "${g?.recommendedOption}" — ${g?.reasoning} | shortcut warning: ${g?.shortcutWarning}
SKEPTIC ADVISOR: recommends "${s?.recommendedOption}" — ${s?.reasoning} | shortcut warning: ${s?.shortcutWarning}
Set agreement to "converge" if both advisors point to the same option, else "diverge". If they diverge, the divergenceNote
must say plainly what the choice hinges on — this is where the maintainer's product judgment is the tiebreaker.`,
    { schema: PACKET_SCHEMA, label: `panel:translate:${fork.id}`, phase: 'Panel', model: 'sonnet' },
  ).then(packet => ({ id: fork.id, fork, grounded: g, skeptic: s, packet }))
}))

return {
  issue: ISSUE,
  codexUsed: CODEX,
  tierA: packets.filter(Boolean),
  tierB: tierB.map(f => ({ id: f.id, title: f.title, options: f.options, leaning: f.leaning, principle: f.principle })),
  tierC: tierC.map(f => ({ id: f.id, title: f.title })),
}
