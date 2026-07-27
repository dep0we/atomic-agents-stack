export const meta = {
  name: 'grade-decision',
  description: 'AU1/ADR-0020 — independent blind grader. Compares a maintainer ruling against a shadow-compare prediction using a FRESH agent invocation that never sees the operator judgment profile or any prediction/challenge reasoning. Domain-neutral. Makes ZERO code changes. Returns ONLY a verdict (matchScore + reason) and a deterministic, workflow-observed gradedVia signal — never a self-reported family/mode/model.',
  phases: [
    { title: 'Grade' },
  ],
}

// ============================================================================
// PORTABLE grade-decision — part of the dev-process-kit.
//
// Fires from the trusted /arc skill's ledger-write step (SKILL.md step 9), inline,
// synchronously, immediately after the maintainer rules a Tier-A fork (ruling
// grading-timing). This is a SEPARATE workflow from shadow-compare.js by design: the
// skill instance that ran shadow-compare's predict/challenge phases already read the
// judgment profile and the predictor's own reasoning earlier in the SAME discovery run
// — grading the match in that same context is exactly the self-grading defect (M3)
// AU1 exists to close. This workflow is a genuinely separate Workflow invocation, so it
// starts with NO shared conversational context with the predictor: it is fed ONLY the
// three allow-listed fields below, never the profile, never _predictionReasoning, never
// _challengeReason, never the source ledger record.
//
// CONTRACT:
//   - Read-only. Writes nothing. The trusted /arc skill owns the ledger write and is
//     the ONLY thing that sets scoredBy.{family,mode,model} — this workflow returns a
//     deterministic, WORKFLOW-OBSERVED `gradedVia` signal (which code path actually ran,
//     decided by config/availability, never by the grader agent's own output) and the
//     grader's bare verdict; the skill maps `gradedVia` to the scoredBy object using its
//     own knowledge of which mechanism it invoked (never reading family/mode/model back
//     off the grader).
//   - The grader agent's OWN response schema requests ONLY {matchScore, reason} —
//     additionalProperties:false. It never sees and is never asked for family/mode/model,
//     so it structurally cannot mislabel its own grading as independent.
//   - Fail-closed (ruling grader-degradation-policy): on any failure — cross-family
//     unavailable AND same-family-fresh call fails/throws/malformed — return
//     matchScore:null, gradedVia:'unavailable'. NEVER fall back to a same-workflow
//     string/semantic comparison; that would reintroduce the exact self-grading
//     anti-pattern this workflow exists to replace.
//   - Sanitization: every field embedded in the grader prompt is clamped (length cap +
//     control/bidi-char strip) with the SAME clampRelayText treatment shadow-compare.js
//     already applies to its own untrusted relay fields — options/actualRuling/
//     predictedRuling are ultimately traceable to GitHub issue text (attacker-
//     controllable on a repo that accepts external issues). On TOP of that clamp, the
//     three untrusted fields are run through neutralizeShellMeta before interpolation:
//     the cross-family path (below) embeds the grader prompt inside an instruction that
//     asks a sub-agent to assemble a real `${XF_EXEC} "<prompt>"` shell command, and the
//     caller's backslash/double-quote escaping stops quote-breakout but NOT command
//     substitution INSIDE the quotes ($(...), ${...}, and backticks all execute within a
//     double-quoted shell string). Neutralizing the shell metacharacters that enable
//     substitution/chaining/redirection closes that sink.
//   - Allow-listed payload only: the prompt is built from an explicit
//     {options, actualRuling, predictedRuling} object constructed field-by-field —
//     NEVER a spread/serialize of the source ledger record (which would carry the OLD
//     matchScore and anchor a "blind" re-grade on the prior answer — the exact failure
//     the wargame doc's abort condition #15 calls out for the S1 retro-grade probe;
//     this workflow is used for both the live per-fork grade AND, when the trusted
//     skill invokes it for the S1 backfill probe, that retro-grade path).
// ============================================================================

// ---- inputs -----------------------------------------------------------------
// args.options          : array of option label strings (required)
// args.actualRuling     : the maintainer's chosen option (required, non-empty string)
// args.predictedRuling  : the profile's predicted option, or null/absent (no prediction
//                         to grade — short-circuits to a "no-prediction" result, NOT a
//                         failure)
// args.codexAvailable   : true if the /arc skill's pre-run probe answered
// args.config           : per-project arc.config.jsonc (optional)
// Defensive: accept args as object OR JSON string (issue #61 F1 pattern).
let A = args ?? {}
let ARGS_UNPARSEABLE = false
if (typeof A === 'string') { try { const P = JSON.parse(A); if (P && typeof P === 'object') { A = P } else { A = {}; ARGS_UNPARSEABLE = true } } catch { A = {}; ARGS_UNPARSEABLE = true } }

const OPTIONS = Array.isArray(A.options) ? A.options : null
const ACTUAL_RULING = (typeof A.actualRuling === 'string' && A.actualRuling.trim()) ? A.actualRuling : null

if (args == null || ARGS_UNPARSEABLE || OPTIONS == null || ACTUAL_RULING == null) {
  return {
    status: 'bad-args',
    matchScore: null,
    gradedVia: null,
    reason: null,
    note: 'Workflow args were absent, unparseable, or missing a well-shaped `options` array / non-empty `actualRuling`. STOP: the skill must fail closed here (matchScore:null, scoredBy:null) — never compute a same-context fallback score.',
  }
}

// No prediction to grade — this is NOT a failure. The skill should still write the
// ledger record with matchScore:null/scoredBy:null; there is nothing to independently
// verify when shadow-compare produced no prediction for this fork.
const PREDICTED_RULING = (typeof A.predictedRuling === 'string' && A.predictedRuling.trim()) ? A.predictedRuling : null
if (PREDICTED_RULING == null) {
  return { status: 'ok', matchScore: null, gradedVia: null, reason: 'no prediction to grade' }
}

const CFG = A.config ?? {}

// Cross-family challenger config (same pattern as shadow-compare.js).
const XF = CFG.crossFamily ?? {}
const XF_ENABLED = XF.enabled !== false
const XF_EXEC_RAW = XF.exec ?? 'codex exec'
// XF_EXEC is operator-controlled (arc.config.jsonc) and is interpolated into an agent
// instruction that asks the model to run a shell command. Validate it against a strict
// allowlist of safe tokens so a value like `codex exec; rm -rf ~/` cannot smuggle shell
// metacharacters through — the SAME guard shadow-compare.js/arc-discovery.js already
// apply at every call site that reaches this sink.
const XF_EXEC_SAFE = /^[a-zA-Z0-9_./-]+( [a-zA-Z0-9_./-]+)*$/.test(XF_EXEC_RAW)
const XF_EXEC = XF_EXEC_SAFE ? XF_EXEC_RAW : 'codex exec'
if (!XF_EXEC_SAFE) {
  log(`grade-decision: configured crossFamily.exec contains unexpected characters and was rejected; falling back to 'codex exec'`)
}
const CODEX = A.codexAvailable === true && XF_ENABLED && XF_EXEC_SAFE

// Clamp untrusted text before it is relayed to any prompt (external CLI or same-family
// agent alike) — same treatment shadow-compare.js applies to its own relay fields.
function clampRelayText(s, max) {
  // Strip C0 controls + DEL (\x00-\x1f, \x7f), C1 controls (\x80-\x9f), and the common
  // Unicode bidi-override / zero-width / format characters used to obfuscate prompt
  // injection in attacker-controlled text (RLO/LRO U+202A-U+202E, LRM/RLM U+200E/200F,
  // zero-width U+200B-U+200D, word-joiner U+2060, BOM/ZWNBSP U+FEFF) — the SAME regex
  // shadow-compare.js already applies to its own untrusted relay fields.
  return String(s ?? '')
    .replace(/[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060\ufeff]+/g, ' ')
    .slice(0, max)
}

// Neutralize the shell metacharacters that enable command substitution ($ ` and the
// ( ) grouping), command chaining ( ; | & ), and redirection ( < > ) BEFORE this
// untrusted text is interpolated into the cross-family instruction (below), which asks
// a sub-agent to assemble a real `${XF_EXEC} "<prompt>"` shell command. clampRelayText's
// control/bidi strip and the caller's backslash/double-quote escaping stop quote-breakout
// but not $(...)/${...}/backtick substitution INSIDE the double quotes. The SAME actual
// and predicted text are compared for a semantic match, so replacing these rare
// characters uniformly (with a space) does not change the grade. Applied to every field
// that reaches graderPrompt (which feeds both the shell-embed path and the same-family
// path) so the two paths see identical, already-neutralized text.
function neutralizeShellMeta(s) {
  return String(s ?? '').replace(/[$`();|&<>]/g, ' ')
}

// Allow-listed payload — built field-by-field, NEVER a spread of the caller's args or
// any source ledger record (see the module comment's abort-condition-#15 note).
const SAFE_OPTIONS = OPTIONS.slice(0, 20).map(o => neutralizeShellMeta(clampRelayText(o, 200)))
const SAFE_ACTUAL = neutralizeShellMeta(clampRelayText(ACTUAL_RULING, 300))
const SAFE_PREDICTED = neutralizeShellMeta(clampRelayText(PREDICTED_RULING, 300))
const optionsList = SAFE_OPTIONS.join(' | ')

phase('Grade')

// The grader's OWN response schema requests ONLY the verdict — additionalProperties
// false, no family/mode/model field for it to (mis)report. The invoking skill stamps
// scoredBy entirely from `gradedVia` below, never from anything this schema returns.
const GRADE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['matchScore', 'reason'],
  properties: {
    matchScore: {
      type: 'string',
      enum: ['exact', 'partial', 'miss'],
      description: 'exact = same option chosen; partial = semantically aligned but different wording; miss = a different option',
    },
    reason: { type: 'string', description: 'one-line, public-safe justification for the verdict' },
  },
}

// The interpret pass has its OWN schema (NOT GRADE_SCHEMA) so it can signal that a raw
// cross-family reply is NOT a usable verdict — an error/unreachable/uninterpretable
// message. `ungradeable:true` leaves `result` unset, so grading falls through to the
// same-family-fresh path and, failing that, to matchScore:null (fail-closed, ruling
// grader-degradation-policy). Without this escape hatch GRADE_SCHEMA would force any
// non-empty text into a real exact/partial/miss and mislabel it gradedVia='cross-family'.
const INTERPRET_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['ungradeable'],
  properties: {
    ungradeable: {
      type: 'boolean',
      description: 'true if the reply is NOT a usable exact/partial/miss verdict (an error, an unreachable/CLI-failure message, or otherwise uninterpretable). When true, matchScore MUST be null.',
    },
    matchScore: {
      type: ['string', 'null'],
      enum: ['exact', 'partial', 'miss', null],
      description: 'the verdict IF the reply is a real one; null when ungradeable is true',
    },
    reason: { type: 'string', description: 'one-line, public-safe justification for the verdict (or why it was ungradeable)' },
  },
}

const graderPrompt = `You are an independent blind grader. You are given a maintainer's actual decision and a prior prediction for the SAME decision fork. You were NOT involved in making the prediction and have no other context about it.
OPTIONS: ${optionsList}
ACTUAL RULING (what the maintainer chose): "${SAFE_ACTUAL}"
PREDICTED RULING (what was predicted beforehand): "${SAFE_PREDICTED}"
Judge ONLY whether the predicted ruling matches the actual ruling:
- "exact": the predicted option is the same option the maintainer actually chose.
- "partial": the prediction is semantically aligned with the actual choice but phrased differently or only partially matches.
- "miss": the prediction named a different option than what was actually chosen.
Do not guess at any reasoning that was not given to you. Return your verdict.`

let gradedVia = 'unavailable'
let result = null

if (CODEX) {
  try {
    const xfResult = await agent(
      `Invoke the cross-family CLI to get an independent grading verdict.
Run: \`${XF_EXEC}\` with this prompt (keep it under 250 words): "${graderPrompt.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"
The CLI's reply should indicate one of exact/partial/miss and a one-line reason. If the CLI is unreachable or its reply cannot be interpreted, say so plainly.`,
      { label: 'grade:xf', phase: 'Grade', model: 'sonnet' },
    )
    if (xfResult && typeof xfResult === 'string' && xfResult.length > 10) {
      // Interpret the raw cross-family reply into the constrained schema via a
      // Claude-local structured pass (the cross-family CLI itself may not emit JSON).
      const interpreted = await agent(
        `A cross-family reviewer replied to a grading request with the following text. Interpret it into a structured verdict.
REPLY: "${clampRelayText(xfResult, 1500)}"
If the reply is a genuine grading verdict, return ungradeable:false with matchScore (exact|partial|miss) and a one-line reason.
If the reply is NOT a usable verdict — an error, an "unreachable"/CLI-failure message, or anything you cannot map to exact/partial/miss — return ungradeable:true with matchScore:null. Do NOT guess a verdict from a failure message.`,
        { schema: INTERPRET_SCHEMA, label: 'grade:xf:interpret', phase: 'Grade', model: 'sonnet' },
      )
      // Fail-closed: only accept a real verdict. An ungradeable reply (or a missing/null
      // matchScore) leaves `result` unset so we fall through to same-family-fresh, then
      // to unavailable (matchScore:null) — never mint an unprovenanced cross-family grade.
      if (interpreted && interpreted.ungradeable !== true &&
          (interpreted.matchScore === 'exact' || interpreted.matchScore === 'partial' || interpreted.matchScore === 'miss')) {
        result = { matchScore: interpreted.matchScore, reason: interpreted.reason }
        gradedVia = 'cross-family'
      }
    }
  } catch (err) {
    log(`grade-decision: cross-family grading failed: ${err?.message ?? err}`)
  }
}

if (!result) {
  // Same-family-fresh fallback: a NEW agent() call (this Workflow invocation itself has
  // no prior turns from the predict/challenge phases — those ran in an entirely
  // separate Workflow run — so this is genuinely a fresh context, not merely a fresh
  // prompt in a shared session).
  try {
    const fallback = await agent(graderPrompt, { schema: GRADE_SCHEMA, label: 'grade:fallback', phase: 'Grade', model: 'sonnet' })
    if (fallback && fallback.matchScore) {
      result = fallback
      gradedVia = 'same-family-fresh'
    }
  } catch (err) {
    log(`grade-decision: same-family-fresh grading failed: ${err?.message ?? err}`)
  }
}

if (!result) {
  // Fail-closed (ruling grader-degradation-policy): no same-workflow fallback score.
  log('grade-decision: no grader was available or reachable; returning unscored (matchScore:null) — the skill must NOT substitute a same-context comparison.')
  return { status: 'ok', matchScore: null, gradedVia: 'unavailable', reason: null }
}

return {
  status: 'ok',
  matchScore: result.matchScore,
  reason: (result.reason ?? '').replace(/[\r\n]+/g, ' ').slice(0, 500),
  // gradedVia is a DETERMINISTIC signal this workflow's own code computed (which
  // branch actually ran), never something the grader agent self-reported. The
  // invoking skill maps this to scoredBy.mode + fills family/model from its own
  // knowledge of the dispatched mechanism (e.g. the configured crossFamily provider,
  // or the same-family model string this workflow itself passed to agent()).
  gradedVia,
}
