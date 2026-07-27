export const meta = {
  name: 'clarity-gate',
  description: 'Discovery hardening (issue #145): a rewrite-on-fail gate that runs after arc-discovery emits its packets and BEFORE shadow-compare. Deterministic jargon pre-lint plus a semantic comprehension check, both scoped to the maintainer-visible packet fields; a packet that fails either gets up to 2 bounded rewrite rounds before the best version is surfaced, honestly, with a could-not-fully-simplify note when the cap is hit. Makes ZERO code changes; consumes arc-discovery output and returns an augmented, shape-compatible superset that shadow-compare (and everything downstream) reads instead of the raw arc-discovery return.',
  phases: [
    { title: 'Lint' },
    { title: 'Rewrite' },
  ],
}

// ============================================================================
// PORTABLE clarity-gate — part of the dev-process-kit.
//
// Runs AFTER arc-discovery emits its packets and AFTER discovery-verify passes,
// BEFORE shadow-compare (issue #145, ruling sequence-vs-shadow-compare: shadow-compare
// must score the exact text the maintainer will see, so it reads THIS step's output,
// never arc-discovery's raw return, once this step has run). Wired by the /arc skill
// as its OWN standalone workflow (ruling gate-placement — Option B), mirroring the
// shadow-compare.js pattern. Do NOT modify arc-discovery.js or fold this logic into
// SKILL.md prose — this step is a separate, independently testable file by design.
//
// CONTRACT:
//   - ALWAYS runs (Armor A, ruling armor-classification): unlike shadow-compare it is
//     never skipped, never receives a `trust` argument, and is unconditional-by-
//     construction in the discovery flow — it protects OUTPUT QUALITY (what the
//     maintainer reads), not build machinery, so it is exempt from the trust switch
//     at every level, the same way shadow-compare is unconditional today.
//   - Return-shape parity with arc-discovery (`{issue, codexUsed, tierA, tierB,
//     tierC}`, superset only — no field renamed or removed) so shadow-compare's own
//     F1 bad-args guard (which checks `discoveryOutput.tierA` is an array) accepts
//     this step's output as a drop-in replacement for arc-discovery's raw return.
//   - STRICT deterministic pre-lint (ruling prelint-strictness): every flagged,
//     unglossed jargon term forces a rewrite round — it is an enforced floor, never
//     merely advisory. The semantic comprehension reader runs ON TOP of it, not
//     instead of it; either one failing forces a rewrite.
//   - Scope: only the maintainer-visible framing fields are scanned/rewritten —
//     plainTitle, plainFraming, recommendation, divergenceNote, options[].consequence.
//     `developerCaveat` and `options[].label` are the PRECISE-TERM-BY-DESIGN fields
//     (PACKET_SCHEMA's own description: "the precise technical term in parens, for
//     future searches") and are NEVER scanned or rewritten — scanning them would
//     force every packet through the round cap on a field designed to carry jargon.
//   - `agreement` and `developerCaveat` pass through completely untouched. The
//     rewrite step mutates ONLY the five scanned fields in place on a deep copy of
//     the packet — never a fresh schema-constrained regeneration of the whole
//     packet, which would risk silently dropping/altering `agreement`/`divergenceNote`
//     structural fields the presentation step depends on to tell converge from diverge.
//   - The "could-not-fully-simplify" note is an UNOFFICIAL sibling field (`_clarity`)
//     on the outer `{id, fork, grounded, skeptic, packet}` wrapper — mirroring
//     shadow-compare's own `_shadow` convention exactly (ruling
//     packet-schema-clarity-fields). It is a post-hoc JS spread applied AFTER the
//     schema-validated rewrite `agent()` call returns, never a property inside the
//     schema object passed to that call (PACKET_SCHEMA-equivalent schemas here are
//     `additionalProperties: false`, matching arc-discovery's own PACKET_SCHEMA).
//   - Hardcoded settings only (ruling config-surface — matches the #119 no-new-
//     config-surface precedent): the round cap, the concurrency cap, and the reader
//     model are bare top-of-file literals with NO `CFG.clarityGate?.xxx` fallback of
//     any kind, documented or not — do not "regularize" this into the CFG.* pattern
//     the adjacent MAX_SHADOW_FORKS-style constants use elsewhere in this codebase.
//   - The jargon wordlist is NOT hardcoded here and NOT read from disk here (the
//     sandboxed Workflow runtime has no `require('fs')`/`require('os')`/
//     `require('child_process')` — see SANDBOX RESTRICTIONS below). It arrives via
//     `args.jargonList`, loaded from the single shared data file
//     (`skill/arc/clarity-killlist.json`) by the TRUSTED /arc skill and injected in,
//     the exact same pattern already used for the judgment profile (`args.profile`).
//   - Third-party transmission boundary: only clamped, length-capped packet field
//     text (never profile content, never raw issue text beyond what arc-discovery
//     already surfaced in the packet) reaches the cross-family reader prompt.
// ============================================================================

// ---- SANDBOX RESTRICTIONS (mirrors arc-discovery.js / shadow-compare.js) ---
// This workflow runs inside a sandboxed Workflow runtime. The following are
// FORBIDDEN here — they throw or return empty/undefined in the sandbox, and are
// auto-policed by test/arc-preflight.test.sh's SG1 dynamic source guard over
// EVERY workflows/*.js file:
//   - new Date(), Date.now()  → resolve timestamps in the calling skill
//   - Math.random()           → use a deterministic id, or have the skill inject one
//   - os.homedir(), os.tmpdir(), process.env.HOME → resolve in the skill layer
//   - require('fs'|'os'|'child_process') (any form: require, import, dynamic
//     import, re-export) → not available; the kill-list arrives via args.jargonList
//     instead of a file read (see CONTRACT above) — this is the load-bearing fix
//     for the "sandboxed workflow tries to require the shared kill-list file"
//     failure mode this file must never reproduce.
// ---- inputs -----------------------------------------------------------------
// args.discoveryOutput : arc-discovery's return value (required) — {issue, codexUsed, tierA, tierB, tierC}
// args.jargonList       : the parsed contents of skill/arc/clarity-killlist.json's
//                          `terms` array (required) — [{term, forms: [...]}]
// args.codexAvailable   : true if the /arc skill's pre-discovery probe answered
// args.config            : per-project arc.config.jsonc (optional; only crossFamily.* is read)
// Defensive: accept args as object OR JSON string, exactly like every other workflow.
let A = args ?? {}
let ARGS_UNPARSEABLE = false
if (typeof A === 'string') { try { const P = JSON.parse(A); if (P && typeof P === 'object') { A = P } else { A = {}; ARGS_UNPARSEABLE = true } } catch { A = {}; ARGS_UNPARSEABLE = true } }

// Bad-args guard (mirrors shadow-compare's F1 guard exactly, plus a required
// jargonList array — issue #145 P0/P1: a collapsed or malformed args payload MUST
// NOT silently resolve to "zero violations found", which would render raw,
// unglossed packets as if they had passed the gate. Fail loud and UNGATED instead.
const DISCOVERY = (A.discoveryOutput && typeof A.discoveryOutput === 'object') ? A.discoveryOutput : null
const RESOLVED_ISSUE = (typeof DISCOVERY?.issue === 'string' && DISCOVERY.issue.trim())
  ? DISCOVERY.issue
  : (typeof A.issue === 'string' && A.issue.trim() ? A.issue : null)
const JARGON_LIST = Array.isArray(A.jargonList) ? A.jargonList : null

if (args == null || ARGS_UNPARSEABLE || DISCOVERY == null || !Array.isArray(DISCOVERY.tierA) || RESOLVED_ISSUE == null || JARGON_LIST == null) {
  return {
    status: 'bad-args',
    issue: null,
    receivedArgsType: typeof args,
    note: 'Workflow args were absent, unparseable, or carried no well-shaped `discoveryOutput` (a non-null object whose `tierA` is an array) / usable `issue` / `jargonList` array. This is an UNGATED result, never a passed one: the caller MUST NOT present these packets as clarity-checked, and MUST fall back to the last known-good packets (clarity-gate\'s own prior output if any, else arc-discovery\'s raw return) rather than treat an absent tierA as "zero violations". Shrink the args payload, then re-send — do not retry with the identical payload (an oversized payload silently collapses to {} in the Workflow runtime, the known cause).',
  }
}

const ALL_TIER_A = DISCOVERY.tierA
const ISSUE = RESOLVED_ISSUE
const CFG = A.config ?? {}

// ---- Hardcoded settings (ruling config-surface — NO arc.config.jsonc knob) --
// These three constants are deliberately bare literals. Do not add a
// `CFG.clarityGate?.xxx ?? <default>` read path here, even though the adjacent
// shadow-compare.js-style constants elsewhere in this codebase DO read from CFG —
// this file's settings are ruled out-of-config on purpose (matches #119).
const ROUND_CAP = 2               // ruling rewrite-round-cap: fixed 2 rewrite rounds, hardcoded
const MAX_CLARITY_CONCURRENCY = 8 // ruling semantic-gate-concurrency: modest per-packet parallel cap
const READER_MODEL = 'sonnet'     // hardcoded reader model for the semantic comprehension check
// MAX_CLARITY_PACKETS bounds TOTAL LLM-call volume, not just parallelism: each
// Tier-A packet fans out to up to (ROUND_CAP+1) comprehension checks + ROUND_CAP
// rewrites, so a pathological/attacker-influenced discovery with dozens of Tier-A
// forks would fire dozens-to-hundreds of LLM calls before the maintainer sees a
// single packet — the exact threat shadow-compare.js caps with MAX_SHADOW_FORKS,
// reproduced one pipeline stage earlier with MORE calls per fork. Hardcoded (no
// CFG knob) per the config-surface ruling, unlike shadow-compare's tunable cap.
// Forks beyond the cap are NEVER dropped: they pass straight through to the
// maintainer un-clarity-checked (still presented for ruling), mirroring how
// shadow-compare leaves over-cap forks unpredicted rather than discarding them.
const MAX_CLARITY_PACKETS = 10

// Cross-family challenger config — duplicated verbatim from arc-discovery.js/
// shadow-compare.js (both files independently re-implement this same guard;
// clarity-gate is a third, standalone file, so it re-implements it too).
const XF = CFG.crossFamily ?? {}
const XF_ENABLED = XF.enabled !== false
const XF_EXEC_RAW = XF.exec ?? 'codex exec'
// XF_EXEC is operator-controlled (arc.config.jsonc) and is interpolated into an agent
// instruction that asks the model to run a shell command. Validate it against a strict
// allowlist of safe tokens so a value like `codex exec; rm -rf ~/` cannot smuggle shell
// metacharacters through. The pattern allows an executable path plus space-separated
// flags/words built from [a-zA-Z0-9_./-] only — no shell metacharacters.
const XF_EXEC_SAFE = /^[a-zA-Z0-9_./-]+( [a-zA-Z0-9_./-]+)*$/.test(XF_EXEC_RAW)
const XF_EXEC = XF_EXEC_SAFE ? XF_EXEC_RAW : 'codex exec'
if (!XF_EXEC_SAFE) {
  log(`clarity-gate: configured crossFamily.exec contains unexpected characters and was rejected; falling back to 'codex exec'`)
}
const CODEX = A.codexAvailable === true && XF_ENABLED && XF_EXEC_SAFE

// Third-party transmission boundary — duplicated verbatim from shadow-compare.js.
function clampRelayText(s, max) {
  return String(s ?? '')
    .replace(/[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060\ufeff]+/g, ' ')
    .slice(0, max)
}

// Escape a string for safe embedding inside a SHELL DOUBLE-QUOTED argument.
// The cross-family relay instruction (runComprehensionCheck) tells the invoking
// agent to run `${XF_EXEC}` "<prompt>". If that agent turns the instruction into
// a literal double-quoted shell command, a POSIX shell STILL interprets \, ", $
// and backtick inside double quotes — so untrusted packet text carrying $(...) or
// `...` would trigger command substitution and run BEFORE the CLI is ever
// invoked. Escape all four (backslash FIRST, so the escapes added for the other
// three are not themselves re-escaped). Defense-in-depth on the soft relay
// boundary, matching clampRelayText's control-char scrub — the untrusted packet
// fields (plainTitle/plainFraming/options/recommendation/divergenceNote,
// ultimately traceable to attacker-influenceable issue text) reach this prompt,
// so escaping them at the embed site closes the injection vector rather than
// trusting the invoking agent to shell out safely (issue #145 round 3, P1).
function escapeForShellDoubleQuote(s) {
  return String(s ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\$/g, '\\$')
    .replace(/`/g, '\\`')
}

// runBounded — duplicated verbatim from shadow-compare.js.
async function runBounded(thunks, limit) {
  const step = (Number.isFinite(limit) && limit > 0) ? Math.floor(limit) : thunks.length || 1
  const out = []
  for (let i = 0; i < thunks.length; i += step) {
    const batch = thunks.slice(i, i + step)
    const batchResults = await parallel(batch)
    for (const r of batchResults) out.push(r)
  }
  return out
}

// ============================================================================
// CLARITY-DETECTION (shared, byte-identical with skill/arc/clarity-lint.js —
// issue #145, ruling killlist-home / detection-algorithm). Do NOT edit this block
// in one file without applying the SAME edit to the other — an automated test
// (test/clarity-gate-behavior.test.js, DETECT-DUPLICATION) extracts and diffs both
// copies, so a drift here fails that test, not just a visual review.
//
// Word-boundary regex per jargon form, plus a "gloss present within ~8 words"
// check that requires a DEFINITIONAL marker in the following window — a bare
// parenthetical opener, or an explicit "meaning"/"i.e."/"aka" marker — rather than
// treating ANY trailing words as a gloss (a pure word-count check would credit
// unrelated trailing prose that never actually defines the term).
// ============================================================================
function detectJargon(text, jargonTerms) {
  const violations = []
  if (typeof text !== 'string' || !text.trim() || !Array.isArray(jargonTerms)) return violations
  const GLOSS_WINDOW_CHARS = 80 // ~8 words at ~10 chars/word including spaces
  const GLOSS_MARKERS = /\(|\bmeaning\b|\bi\.e\.|\baka\b|\ba\.k\.a\.\b/i
  for (const entry of jargonTerms) {
    if (!entry) continue
    const forms = Array.isArray(entry.forms) && entry.forms.length ? entry.forms : [entry.term].filter(Boolean)
    for (const form of forms) {
      if (typeof form !== 'string' || !form.trim()) continue
      const escaped = form.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      let re
      try { re = new RegExp('\\b' + escaped + '\\b', 'gi') } catch { continue }
      let m
      while ((m = re.exec(text)) !== null) {
        const after = text.slice(m.index + m[0].length, m.index + m[0].length + GLOSS_WINDOW_CHARS)
        const hasGloss = GLOSS_MARKERS.test(after)
        violations.push({ term: entry.term || form, form, index: m.index, hasGloss })
        if (m.index === re.lastIndex) re.lastIndex++ // guard a zero-width match infinite loop
      }
    }
  }
  return violations.filter(v => !v.hasGloss)
}
// ---- END CLARITY-DETECTION --------------------------------------------------

// Fields scanned/rewritten — the maintainer-visible framing surface ONLY.
// `developerCaveat` and `options[].label` are excluded by design (see CONTRACT).
const SCAN_TEXT_FIELDS = ['plainTitle', 'plainFraming', 'recommendation', 'divergenceNote']

function collectScannableText(packet) {
  const out = []
  for (const f of SCAN_TEXT_FIELDS) {
    if (typeof packet?.[f] === 'string') out.push({ field: f, optionIndex: null, text: packet[f] })
  }
  if (Array.isArray(packet?.options)) {
    packet.options.forEach((o, i) => {
      if (typeof o?.consequence === 'string') out.push({ field: 'options[].consequence', optionIndex: i, text: o.consequence })
    })
  }
  return out
}

function violationsForPacket(packet, jargonList) {
  const found = []
  for (const { field, optionIndex, text } of collectScannableText(packet)) {
    for (const v of detectJargon(text, jargonList)) {
      found.push({ ...v, field, optionIndex })
    }
  }
  return found
}

// ---- Phase 1: semantic comprehension reader ---------------------------------
const COMPREHENSION_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['pass', 'reason'],
  properties: {
    pass: { type: 'boolean', description: 'true only if a non-developer reading cold could answer both questions below' },
    reason: { type: 'string', description: 'one line: why it passes, or the specific place a non-developer would get stuck' },
  },
}

async function runComprehensionCheck(packet) {
  // Options render CONSEQUENCE ONLY — never the `label`. options[].label is the
  // PRECISE-TERM-BY-DESIGN field the CONTRACT says is "NEVER scanned or rewritten";
  // feeding a label into the semantic reader (which is told to fail on any unexplained
  // term) would be scanning it by another door, and since the rewrite step can never
  // change a label, a jargon-carrying label would loop straight to the round cap on a
  // field the exemption exists to keep OUT of the cap. Keep both detection layers
  // (deterministic pre-lint + semantic reader) scoped to the SAME field set.
  const optionsList = (Array.isArray(packet.options) ? packet.options : []).filter(o => o && typeof o === 'object').map(o => clampRelayText(o.consequence, 300)).join(' | ')
  const prompt = `You are checking whether a decision packet is legible to a non-developer reading it COLD, with no technical background.
TITLE: ${clampRelayText(packet.plainTitle, 300)}
FRAMING: ${clampRelayText(packet.plainFraming, 800)}
OPTION CONSEQUENCES: ${optionsList}
RECOMMENDATION: ${clampRelayText(packet.recommendation, 400)}
DIVERGENCE NOTE: ${clampRelayText(packet.divergenceNote, 400)}

From this text ALONE (no outside knowledge), could a non-developer answer BOTH:
  1. Which option is being recommended, in plain terms?
  2. What concretely breaks or goes wrong if the wrong option is picked?
Set pass to true ONLY if both are answerable without needing to know any technical term used in the text. If any
sentence relies on unexplained jargon, an acronym, or a term a non-developer would have to look up, pass must be false
and reason must name the specific sticking point.`

  try {
    if (CODEX) {
      const xfResult = await agent(
        `Invoke the cross-family CLI to get an independent comprehension read on this decision packet.
Run: \`${XF_EXEC}\` with this SHORT prompt (under 200 words): "${escapeForShellDoubleQuote(prompt)}"
Report whether it says a non-developer could follow the packet cold, as a JSON object {"pass": boolean, "reason": "<one line>"}.
If the CLI is unreachable or returns something you cannot parse into that shape, fall back to judging it yourself using the same criteria.`,
        { schema: COMPREHENSION_SCHEMA, label: 'clarity:comprehend:xf', phase: 'Lint', model: READER_MODEL },
      )
      if (xfResult && typeof xfResult.pass === 'boolean') return { ...xfResult, ranCrossFamily: true }
    }
  } catch (err) {
    log(`clarity-gate: cross-family comprehension check failed, falling back to same-family: ${err?.message ?? err}`)
  }
  try {
    const result = await agent(prompt, { schema: COMPREHENSION_SCHEMA, label: 'clarity:comprehend:fallback', phase: 'Lint', model: READER_MODEL })
    if (result && typeof result.pass === 'boolean') return { ...result, ranCrossFamily: false }
  } catch (err) {
    log(`clarity-gate: comprehension check failed: ${err?.message ?? err}`)
  }
  // Degrade fail-toward-rewrite (never fail-toward-pass) on a dead reader — an
  // unreadable check must not be read as "clean", mirroring the kit's degrade-loud
  // stance (ADR-0011) applied one layer down at the reader-call granularity.
  return { pass: false, reason: 'comprehension reader unavailable — treating as unresolved, not clean', ranCrossFamily: false }
}

// ---- Phase 2: bounded rewrite loop -------------------------------------------
const REWRITE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['plainTitle', 'plainFraming', 'recommendation', 'divergenceNote', 'options'],
  properties: {
    plainTitle: { type: 'string' },
    plainFraming: { type: 'string' },
    recommendation: { type: 'string' },
    divergenceNote: { type: 'string' },
    options: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['consequence'],
        properties: { consequence: { type: 'string' } },
      },
    },
  },
}

async function rewritePacket(packet, violations, comprehension) {
  const flaggedTerms = [...new Set(violations.map(v => v.term))]
  // Clamp EVERY field interpolated into the rewrite prompt, exactly the way
  // runComprehensionCheck already clamps the fields it interpolates (issue #145
  // P1). PACKET_SCHEMA puts no maxLength on these string fields, so a pathological
  // discovery (e.g. a giant pasted-log issue body producing a huge plainFraming)
  // would otherwise re-embed unbounded text into up to ROUND_CAP rewrite calls per
  // packet, for up to MAX_CLARITY_PACKETS packets — multiplying per-call payload
  // SIZE well past what MAX_CLARITY_PACKETS (which bounds call COUNT only) can hold.
  // Caps match the comprehension reader's; options use the same 300-char label/
  // consequence bound so JSON.stringify can't smuggle an unbounded field back in.
  const optionsForPrompt = (Array.isArray(packet.options) ? packet.options : [])
    .filter(o => o && typeof o === 'object')
    .map(o => ({ label: clampRelayText(o.label, 300), consequence: clampRelayText(o.consequence, 300) }))
  const prompt = `Rewrite ONLY these five fields of a decision packet so a non-developer can read it cold, with no
technical background. Plain English first; if a technical term is genuinely useful, define it inline in a short
parenthetical (a few words) right where it's used — never leave a term unglossed. Lead with the consequence, not the
mechanism. Preserve the actual meaning and the recommended option — do not change WHICH option is recommended.

CURRENT plainTitle: ${clampRelayText(packet.plainTitle, 300)}
CURRENT plainFraming: ${clampRelayText(packet.plainFraming, 800)}
CURRENT recommendation: ${clampRelayText(packet.recommendation, 400)}
CURRENT divergenceNote: ${clampRelayText(packet.divergenceNote, 400)}
CURRENT options (label: consequence): ${JSON.stringify(optionsForPrompt)}

FLAGGED JARGON (unglossed, must be plain-language or glossed inline in the rewrite): ${clampRelayText(flaggedTerms.join(', ') || 'none', 400)}
COMPREHENSION FEEDBACK: ${clampRelayText(comprehension?.reason ?? 'n/a', 400)}

Return the SAME five fields, rewritten. Return exactly ${optionsForPrompt.length} options in the same order, each with
only a rewritten "consequence" (not the label).`

  // Guard the rewrite agent() call the same way runComprehensionCheck guards its
  // own (issue #145 P1): processPacket runs under runBounded's parallel(), which is
  // Promise.all(...) — one rejected rewrite promise would reject the WHOLE batch and
  // throw the entire clarity-gate workflow, degrading EVERY packet (including already
  // clean ones) to raw un-clarity-checked output over one flaky call. Instead, a
  // throw here degrades THIS packet only: return null, applyRewrite no-ops on null,
  // the round loop keeps `current`, and the packet honestly ends could-not-fully-
  // simplify — mirroring shadow-compare's per-fork resilience and the Armor-A
  // "the gate always runs" contract.
  try {
    const rewritten = await agent(prompt, { schema: REWRITE_SCHEMA, label: 'clarity:rewrite', phase: 'Rewrite', model: READER_MODEL })
    return rewritten
  } catch (err) {
    log(`clarity-gate: rewrite call failed for one packet, keeping the current best version: ${err?.message ?? err}`)
    return null
  }
}

function applyRewrite(packet, rewritten) {
  if (!rewritten) return packet
  const next = { ...packet }
  if (typeof rewritten.plainTitle === 'string') next.plainTitle = rewritten.plainTitle
  if (typeof rewritten.plainFraming === 'string') next.plainFraming = rewritten.plainFraming
  if (typeof rewritten.recommendation === 'string') next.recommendation = rewritten.recommendation
  if (typeof rewritten.divergenceNote === 'string') next.divergenceNote = rewritten.divergenceNote
  if (Array.isArray(rewritten.options) && Array.isArray(packet.options) && rewritten.options.length === packet.options.length) {
    next.options = packet.options.map((o, i) => ({ ...o, consequence: (typeof rewritten.options[i]?.consequence === 'string') ? rewritten.options[i].consequence : o.consequence }))
  }
  // agreement + developerCaveat are never touched — carried through on `next` via
  // the initial spread above, exactly as arc-discovery originally produced them.
  return next
}

async function processPacket(item) {
  if (!item || !item.packet) return item
  // Deep copy BEFORE any mutation (P2 concurrency fix): packets processed under
  // runBounded's parallelism must never share nested references (a shared
  // `options` array template, or any object two forks happen to reuse by
  // reference from the upstream generator) — a shallow `{...item}` would let one
  // worker's in-place rewrite bleed into another packet still being read.
  const base = JSON.parse(JSON.stringify(item))
  const candidates = []

  let current = base.packet
  for (let round = 0; round <= ROUND_CAP; round++) {
    const violations = violationsForPacket(current, JARGON_LIST)
    const comprehension = await runComprehensionCheck(current)
    const clean = violations.length === 0 && comprehension.pass === true
    candidates.push({ round, packet: current, violationCount: violations.length, comprehensionPass: comprehension.pass, comprehensionReason: comprehension.reason })
    if (clean || round === ROUND_CAP) break
    const rewritten = await rewritePacket(current, violations, comprehension)
    current = applyRewrite(current, rewritten)
  }

  // Pick the best candidate: fewest pre-lint violations wins; ties prefer a
  // comprehension pass, then the EARLIEST round (P2 fix — a later round is not
  // guaranteed to improve on an earlier one; never blindly take the last round).
  let best = candidates[0]
  for (const c of candidates.slice(1)) {
    const better =
      c.violationCount < best.violationCount ||
      (c.violationCount === best.violationCount && c.comprehensionPass && !best.comprehensionPass)
    if (better) best = c
  }
  const finalClean = best.violationCount === 0 && best.comprehensionPass === true

  return {
    ...base,
    packet: best.packet,
    _clarity: {
      couldNotFullySimplify: !finalClean,
      note: finalClean
        ? null
        : `Could not fully simplify within ${ROUND_CAP} rewrite round(s). Best version shown (round ${best.round}): ${best.violationCount} unglossed term(s) remaining${best.comprehensionPass ? '' : `; comprehension check: ${best.comprehensionReason}`}.`,
      roundsRun: candidates.length,
      finalViolationCount: best.violationCount,
      finalComprehensionPass: best.comprehensionPass,
    },
  }
}

// Bound TOTAL LLM-call volume (not just parallelism): clarity-check the first
// MAX_CLARITY_PACKETS Tier-A packets, pass the rest straight through un-checked.
// Over-cap packets are NEVER dropped — they still reach the maintainer for ruling,
// just without a _clarity annotation, mirroring shadow-compare's over-cap handling.
const TO_PROCESS = ALL_TIER_A.slice(0, MAX_CLARITY_PACKETS)
const PASSTHROUGH = ALL_TIER_A.slice(MAX_CLARITY_PACKETS)
if (PASSTHROUGH.length > 0) {
  log(`clarity-gate: ${ALL_TIER_A.length} Tier-A packets exceed the hardcoded clarity cap (${MAX_CLARITY_PACKETS}); clarity-checking the first ${TO_PROCESS.length}. The remaining ${PASSTHROUGH.length} pass through to the maintainer un-clarity-checked (still presented for ruling), never dropped.`)
}

phase('Lint')
const processed = await runBounded(TO_PROCESS.map(item => () => processPacket(item)), MAX_CLARITY_CONCURRENCY)
phase('Rewrite')
const augmentedTierA = processed.concat(PASSTHROUGH)

const couldNotFullySimplifyCount = augmentedTierA.filter(i => i?._clarity?.couldNotFullySimplify).length
log(`clarity-gate: clarity-checked ${processed.length} of ${augmentedTierA.length} Tier-A packet(s); ${couldNotFullySimplifyCount} could not be fully simplified within ${ROUND_CAP} round(s). Cross-family reader available: ${CODEX}.`)

return {
  // Return-shape parity with arc-discovery (superset only) — see CONTRACT above.
  issue: ISSUE,
  codexUsed: DISCOVERY.codexUsed,
  tierA: augmentedTierA,
  tierB: DISCOVERY.tierB,
  tierC: DISCOVERY.tierC,
  clarityMeta: {
    packetsProcessed: processed.length,
    packetsPassedThrough: PASSTHROUGH.length,
    couldNotFullySimplifyCount,
    codexUsedForClarity: CODEX,
    roundCap: ROUND_CAP,
    packetCap: MAX_CLARITY_PACKETS,
  },
}
