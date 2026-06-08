export const meta = {
  name: 'arc-discovery',
  description: 'Phase 1 of the quality loop. Reads an issue, surfaces every decision fork, classifies each by materiality tier (A escalate / B decide+justify / C just-do-it), runs a two-voice adversarial panel on every Tier A fork, and returns plain-language decision packets for the maintainer to rule on. Makes ZERO code changes.',
  phases: [
    { title: 'Discover' },
    { title: 'Re-classify' },
    { title: 'Panel' },
  ],
}

// ---- inputs -------------------------------------------------------------
// args.issue          : issue number or scope description (required)
// args.codexAvailable : true if the main loop probed Codex and it answered
// Defensive: accept args as an object OR a JSON string (the loop must not silently mis-fire).
let A = args ?? {}
if (typeof A === 'string') { try { A = JSON.parse(A) } catch { A = {} } }
const ISSUE = A.issue ?? 'the scope on the current branch'
const CODEX = A.codexAvailable === true

// ---- the materiality tripwire checklist (Tier A = any hit) --------------
// Embedded here, not in a prompt the agent writes, so classification is a
// mechanical check against named invariants — not the agent's taste.
const TRIPWIRES = `
A decision is TIER A (must escalate to the maintainer — the agent may NOT decide it) if it hits ANY of:
  1. Defines, changes, or LOCKS a backend Protocol, its dataclasses, capability advertisement, or WritePolicy.
  2. Adds, changes, or locks a spec doc's normative content (any MUST).
  3. Touches a layer boundary (merge/split persona·memory·notes·wiki·tool·helper·delegate) or relaxes a one-level constraint.
  4. Changes a public or operator-facing surface: a CLI command/flag, a markdown-config schema, a public method name or signature.
  5. Touches agent.call() control flow, a cost guardrail, or the audit-trail / JSONL shape.
  6. Trades the home-user shape against the org shape (could helping one quietly degrade the other?).
  7. Introduces a new dependency, a new concept/abstraction, or a new config field.
  8. Would warrant a TENSIONS.md entry, or is irreversible without a major-version event.

TIER B (agent may decide, but ONLY by emitting a justification record and passing the shortcut-hunter): a genuine
fork between implementations that all stay INSIDE an existing protocol/contract — private helper structure,
error-handling strategy, test layout, internal naming, refactors that change no public/protocol surface.

TIER C (agent just does it, no record): one obvious idiomatic answer, mechanical edits, following a pattern
already established in the codebase, doc wording that matches reality.

TIE-BREAK: if you are unsure whether a fork is A or B, classify it A. Ambiguity always resolves UP.`

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
          principle: { type: 'string', description: 'the named CLAUDE.md principle that governs this fork' },
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
  `Read GitHub issue ${ISSUE} and the repo's design rules in CLAUDE.md, docs/architecture.md, and docs/TENSIONS.md.
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
  // Advisor 1 — project-grounded (fresh Opus, reasons in the framework's own principles).
  const grounded = agent(
    `You advise the maintainer on a material design decision for the atomic-agents framework. Read CLAUDE.md's design
principles and docs/TENSIONS.md first. The fork:
TITLE: ${fork.title}
WHAT IT IS: ${fork.description}
OPTIONS: ${fork.options.join(' | ')}
Argue for the option that is best-in-class for LONG-TERM product quality, grounded in a NAMED principle. Then name the
cheaper/easier path explicitly and say why taking it would be the wrong call. Never recommend an option because it is
faster or less work.`,
    { schema: ADVISOR_SCHEMA, label: `panel:grounded:${fork.id}`, phase: 'Panel', model: 'opus' },
  )
  // Advisor 2 — cross-family skeptic. Codex if reachable; else an opposed-framing Opus.
  const skepticPrompt = CODEX
    ? `Run the Codex CLI for a genuine cross-family second opinion: invoke \`codex exec\` with a prompt describing this
fork, and report Codex's recommendation faithfully. The fork:
TITLE: ${fork.title}
WHAT IT IS: ${fork.description}
OPTIONS: ${fork.options.join(' | ')}
Then add your own adversarial read: assume the implementing agent will be tempted by the easiest option — attack that
option and say what it costs long-term. If the Codex CLI is unreachable, say so and reason as an independent skeptic.`
    : `You are an independent skeptic from outside this codebase's assumptions. The fork:
TITLE: ${fork.title}
WHAT IT IS: ${fork.description}
OPTIONS: ${fork.options.join(' | ')}
Assume the implementing agent will be tempted by the easiest option. Attack that option. Argue from the OPPOSITE framing
to a project-insider: weight long-term capability, correctness, and best-in-class polish over local simplicity. Name the
shortcut and its long-term cost.`
  // Sonnet wrapper is a cheap relay; the actual cross-family reasoning is Codex's (shelled out to `codex exec`).
  const skeptic = agent(skepticPrompt, { schema: ADVISOR_SCHEMA, label: `panel:skeptic:${fork.id}`, phase: 'Panel', model: 'sonnet' })

  const [g, s] = await Promise.all([grounded, skeptic])
  // Translate the two technical takes into one plain-language packet for a non-developer.
  return agent(
    `Two advisors weighed in on a material design fork. Translate this into ONE decision packet a non-developer can rule
on cold, following the maintainer's plain-language-decisions rule (recommendation first, consequences legible, terms
defined inline, the precise technical term as a trailing parenthetical).
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
