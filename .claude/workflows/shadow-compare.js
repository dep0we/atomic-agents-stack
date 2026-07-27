export const meta = {
  name: 'shadow-compare',
  description: 'Decision-delegation stage 1 foundation: predict + challenge Tier-A rulings using the operator judgment profile, silently before presenting packets. Domain-neutral (same step handles code or content forks). Makes ZERO code changes. Consumes arc-discovery output as input; returns augmented packets with prediction metadata appended as untrusted superset fields.',
  phases: [
    { title: 'Predict' },
    { title: 'Challenge' },
  ],
}

// ============================================================================
// PORTABLE shadow-compare — part of the dev-process-kit.
//
// Runs AFTER arc-discovery emits its packets and AFTER discovery-verify passes.
// Wired by the /arc skill between discovery-verify and presenting packets to the
// maintainer. Do NOT modify arc-discovery.js — this step is separate by design
// (lower blast radius, independently testable, domain-portable).
//
// CONTRACT:
//   - Read-only with respect to the per-repo judgment profile. Shadow-compare
//     reads the profile to form predictions but NEVER writes to it.
//   - The ledger write (decisions.jsonl) is owned by the TRUSTED /arc SKILL,
//     which calls this step and receives its structured return value. This step
//     returns predictions+challenge results; the skill appends ledger records.
//   - SILENT: no terminal output about predictions until after actualRuling is
//     written. The prediction fields are internal/untrusted, never displayed
//     before the operator rules.
//   - Degrades gracefully, two distinct paths:
//       Profile missing → proceeds with a generic heuristic and still produces
//       low-confidence predictions (packets are AUGMENTED, not unchanged). This
//       is the central design point: degrade to low-confidence predictions, not
//       to no predictions.
//       Hard failure (LLM unavailable, schema error) → caught per-fork, yields a
//       null prediction for that fork; the original packet fields are preserved
//       either way.
//     Shadow-compare must NEVER block the skill's presentation of Tier-A packets
//     or the auto-decision of Tier-B/Tier-C forks.
//   - Third-party transmission boundary: PROFILE and PREDICTION content are
//     enforced-by-construction never to reach any externally-relayed prompt —
//     the profile is read only into the Claude-local predict prompt, and the
//     external challenger prompt is built solely from the sanitized fork fields
//     (id, title, description, options). The behavioral harness proves the
//     profile sentinel never appears in a challenger prompt. NOTE: the fork
//     title/description/options ARE untrusted issue content relayed to the
//     cross-family CLI (same as arc-discovery). They are length/charset-clamped
//     before relay, but they are not a hard injection boundary — only the
//     profile/prediction boundary is.
// ============================================================================

// ---- inputs -----------------------------------------------------------------
// args.discoveryOutput  : the return value of arc-discovery (required)
//   { issue, codexUsed, tierA, tierB, tierC }
// args.codexAvailable   : true if the /arc skill's pre-discovery probe answered
// args.config           : per-project arc.config.jsonc (optional)
// Defensive: accept args as object OR JSON string.
let A = args ?? {}
let ARGS_UNPARSEABLE = false
// A JSON string that parses to `null`/a scalar/`"…"` does NOT throw, so ARGS_UNPARSEABLE
// alone would stay false and the guard below would dereference `A.discoveryOutput` / `A.issue`
// on that null and throw an uncaught TypeError instead of returning bad-args (issue #61).
// Treat any non-object parse result as unparseable so null/primitives route to bad-args.
if (typeof A === 'string') { try { const P = JSON.parse(A); if (P && typeof P === 'object') { A = P } else { A = {}; ARGS_UNPARSEABLE = true } } catch { A = {}; ARGS_UNPARSEABLE = true } }

// F1 (issue #61): shadow-compare is the FOURTH sandboxed workflow and carries the single
// largest args payload in the flow (`discoveryOutput` = every Tier A/B/C fork), so it is
// the MOST exposed to the oversized-args -> {} silent collapse in the real Workflow
// runtime (memory: arc-execute-compact-args). On collapse, `discoveryOutput` would default
// to {}, ALL_TIER_A to [], and this step would return an empty-tierA packet that the /arc
// skill (SKILL.md steps 6-7) renders to the maintainer as a false "zero Tier-A forks"
// result — the exact silent-false-empty harm the guard exists to close, one hop downstream
// of arc-discovery's own F1 guard. Halt loud instead: require args to be present/parseable
// AND to carry a well-shaped `discoveryOutput` (a NON-NULL object whose `tierA` is an
// ARRAY) AND a usable `issue` string, before any agent() call. A genuine zero-fork
// discovery stays valid — `discoveryOutput.tierA === []` is a PRESENT array and passes;
// only an ABSENT or mis-shaped payload is rejected.
const DISCOVERY = (A.discoveryOutput && typeof A.discoveryOutput === 'object') ? A.discoveryOutput : null
const RESOLVED_ISSUE = (typeof DISCOVERY?.issue === 'string' && DISCOVERY.issue.trim())
  ? DISCOVERY.issue
  : (typeof A.issue === 'string' && A.issue.trim() ? A.issue : null)
if (args == null || ARGS_UNPARSEABLE || DISCOVERY == null || !Array.isArray(DISCOVERY.tierA) || RESOLVED_ISSUE == null) {
  return {
    status: 'bad-args',
    issue: null,
    receivedArgsType: typeof args,
    note: 'Workflow args were absent, unparseable, or carried no well-shaped `discoveryOutput` (a non-null object whose `tierA` is an array) / usable `issue`. STOP: shrink the args payload, then re-send — do not retry with the identical payload (an oversized payload silently collapses to {} in the Workflow runtime, which is the known cause). NOTHING predicted; no agent calls were made. Do NOT render this as a "zero Tier-A forks" discovery.',
  }
}

const ALL_TIER_A = DISCOVERY.tierA
const TIER_B = Array.isArray(DISCOVERY.tierB) ? DISCOVERY.tierB : []
const ISSUE = RESOLVED_ISSUE
const CFG = A.config ?? {}
// Domain is a per-repo config field that selects per-domain taxonomy/profile
// behavior and is persisted into every ledger record (it groups match-rate stats).
// A typo ('CODE', 'contents') would silently fragment those stats permanently in the
// append-only ledger, so normalize to a known value and warn loudly on a fallback.
const KNOWN_DOMAINS = ['code', 'content']
const DOMAIN = KNOWN_DOMAINS.includes(CFG.domain) ? CFG.domain : 'code'
if (CFG.domain !== undefined && !KNOWN_DOMAINS.includes(CFG.domain)) {
  log(`shadow-compare: unrecognized config domain ${JSON.stringify(CFG.domain)} — falling back to 'code'. Set domain to 'code' or 'content' in arc.config.jsonc.`)
}
const PROJECT = CFG.projectName ?? 'this project'

// Hard cap on the number of Tier-A forks shadow-compare processes per run. The
// concurrency cap below bounds PARALLELISM (how many run at once), not TOTAL spend:
// each Tier-A fork fans out to predict + challenge + match agent() calls, so a
// pathological discovery (or an attacker-controlled issue) with dozens of Tier-A
// forks would fire dozens-to-hundreds of LLM calls before the operator sees a single
// packet. Truncate the list to MAX_SHADOW_FORKS (operator-tunable) and log when it
// trims, so cost is bounded by a lever the operator controls. The forks beyond the
// cap still reach the operator for ruling via the unchanged discovery packets — only
// their (optional, measurement-only) prediction is skipped, which AC#6 permits.
const MAX_SHADOW_FORKS = Number(CFG.shadowMaxForks) > 0
  ? Math.max(1, Math.floor(Number(CFG.shadowMaxForks)))
  : 10
const TIER_A = ALL_TIER_A.slice(0, MAX_SHADOW_FORKS)
if (ALL_TIER_A.length > TIER_A.length) {
  log(`shadow-compare: ${ALL_TIER_A.length} Tier-A forks exceed the shadowMaxForks cap (${MAX_SHADOW_FORKS}); predicting the first ${TIER_A.length}. The rest are still presented to the operator for ruling (no prediction). Raise shadowMaxForks in arc.config.jsonc to predict more.`)
}

// Cross-family challenger config (same as discovery/execute).
const XF = CFG.crossFamily ?? {}
const XF_ENABLED = XF.enabled !== false
const XF_EXEC_RAW = XF.exec ?? 'codex exec'
// XF_EXEC is operator-controlled (arc.config.jsonc) and is interpolated into an
// agent instruction that asks the model to run a shell command. Validate it
// against a strict allowlist of safe tokens so a value like `codex exec; rm -rf ~/`
// cannot smuggle shell metacharacters through. The pattern allows an executable
// path plus space-separated flags/words built from [a-zA-Z0-9_./-] only — no
// shell metacharacters (; | & $ ` ( ) < > newline, quotes, etc.).
const XF_EXEC_SAFE = /^[a-zA-Z0-9_./-]+( [a-zA-Z0-9_./-]+)*$/.test(XF_EXEC_RAW)
const XF_EXEC = XF_EXEC_SAFE ? XF_EXEC_RAW : 'codex exec'
if (!XF_EXEC_SAFE) {
  log(`shadow-compare: configured crossFamily.exec contains unexpected characters and was rejected; falling back to 'codex exec'`)
}
const CODEX = A.codexAvailable === true && XF_ENABLED && XF_EXEC_SAFE

// Concurrency cap for the predict + challenge phases. Each Tier-A fork fans out to
// predict + (cross-family OR fallback) + match agent() calls, so a discovery with
// many Tier-A forks can fire dozens of simultaneous LLM calls — which throttles the
// Anthropic API and runs up cost (see parallel-discoveries-throttle-opus.md). Cap
// the number of forks processed concurrently; the rest run in subsequent batches.
const MAX_TIER_A_CONCURRENCY = Number(CFG.shadowMaxConcurrency) > 0
  ? Math.max(1, Math.floor(Number(CFG.shadowMaxConcurrency)))
  : 8

// runBounded — run an array of async thunks with at most `limit` in flight at once,
// preserving input order in the returned results array. Falls back to the injected
// parallel() for each batch so the workflow runtime still schedules them.
async function runBounded(thunks, limit) {
  // Guard the batch size: a non-positive or non-finite limit would make slice()
  // produce an empty batch (slice(0, NaN) === []), silently dropping every thunk.
  const step = (Number.isFinite(limit) && limit > 0) ? Math.floor(limit) : thunks.length || 1
  const out = []
  for (let i = 0; i < thunks.length; i += step) {
    const batch = thunks.slice(i, i + step)
    const batchResults = await parallel(batch)
    for (const r of batchResults) out.push(r)
  }
  return out
}

// Global core profile path (outside any repo — never committed).
// Resolve the home dir via os.homedir() (reads passwd on POSIX, not just $HOME),
// since Node does NOT expand a literal '~'. If it can't be resolved, leave the
// path empty so the profile-load simply reports absent (degraded mode) rather
// than silently reading a relative file named '~' in the working directory.
const HOME_DIR = (() => { try { return require('os').homedir() || '' } catch { return '' } })()
const GLOBAL_PROFILE_PATH = HOME_DIR ? `${HOME_DIR}/.claude/decision-profile/core.json` : ''

// Per-repo profile path (the project-specific layer, wins on conflict).
// Per the LOCKED committed-vs-gitignored ruling for issue #32, the per-repo
// judgment-profile layer is COMMITTED project knowledge (like CLAUDE.md); only
// the raw capture ledger and any *.local.json draft stay gitignored. Read-only
// here — shadow-compare never writes it.
//
// Anchor to the repo root if the skill passed it (CFG.repoRoot), so a workflow
// runtime whose CWD is a subdir or a different repo still reads THIS repo's
// profile rather than a relative file in whatever directory it happens to run in.
// Falls back to the bare relative path when no root is supplied (the skill runs
// from the repo root in normal operation).
const REPO_ROOT = (typeof CFG.repoRoot === 'string' && CFG.repoRoot.trim()) ? CFG.repoRoot.replace(/\/+$/, '') : ''
const REPO_PROFILE_PATH = REPO_ROOT
  ? `${REPO_ROOT}/.gstack/arc-judgment-profile.json`
  : `.gstack/arc-judgment-profile.json`

// Issue id normalization: strip scope/repo prefix, leave bare number.
function normalizeIssueId(raw) {
  let s = String(raw ?? '').trim()
  s = s.split(/\s+/)[0]               // drop " — scope" after first space
  if (s.includes('#')) s = s.slice(s.lastIndexOf('#') + 1)  // "owner/repo#32" → "32"
  return s
}

const ISSUE_ID = normalizeIssueId(ISSUE)

// ---- schema: prediction result (untrusted workflow fields only) --------------
// The trusted /arc skill owns matchScore, actualRuling, rationale.
// This step produces only the prediction-phase fields.
const PREDICTION_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['forkId', 'predictedRuling', 'predictionConfidence', 'predictionReasoning'],
  properties: {
    forkId: { type: 'string' },
    predictedRuling: { type: 'string', description: 'the option label the profile predicts the operator will choose' },
    predictionConfidence: { type: 'number', minimum: 0, maximum: 1, description: 'float 0.0–1.0' },
    predictionReasoning: { type: 'string', description: 'one-line internal rationale for the prediction (never shown to operator before ruling)' },
  },
}

const CHALLENGE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['forkId', 'challengeOutcome', 'challengeReason'],
  properties: {
    forkId: { type: 'string' },
    challengeOutcome: {
      type: 'string',
      enum: ['agreed', 'pushed-back', 'unresolved'],
      description: 'agreed = challenger agrees with prediction; pushed-back = challenger disputes; unresolved = challenger could not determine',
    },
    challengeReason: { type: 'string', description: 'the challenger\'s stated reason (one line, public-safe, no profile content)' },
  },
}

// ---- load profiles (graceful degradation on missing) -----------------------
phase('Predict')

// Read global core profile (outside repo, never committed).
// If absent, proceed with empty profile (degraded mode — predictions will be low-confidence).
let globalProfile = null
let repoProfile = null

// Preferred path: the trusted skill (which has real filesystem access) loads the
// profile and passes its content in args.profile = { global, repo }. The sandboxed
// Workflow runtime cannot reliably resolve the home dir (os.homedir() comes back empty)
// nor read files, so relying on in-workflow reads silently dropped the global profile
// (profilePresent:false even when the file existed). Use the passed content when present;
// the subagent reads below remain only as a fallback for legacy callers that did not
// pass args.profile.
const PASSED_PROFILE = (A.profile && typeof A.profile === 'object') ? A.profile : null
if (PASSED_PROFILE) {
  const g = PASSED_PROFILE.global
  const r = PASSED_PROFILE.repo
  if (g && typeof g === 'object' && !g.absent) globalProfile = g
  if (r && typeof r === 'object' && !r.absent) repoProfile = r
} else {
  if (GLOBAL_PROFILE_PATH) {
    try {
      const gRaw = await agent(
        `Read the file at "${GLOBAL_PROFILE_PATH}". If it exists and is valid JSON, return it. If the file is absent or unreadable, return exactly the JSON object {"absent":true}.`,
        { label: 'load-global-profile', phase: 'Predict', model: 'sonnet' },
      )
      if (gRaw && typeof gRaw === 'object' && !gRaw.absent) globalProfile = gRaw
      else if (typeof gRaw === 'string') { try { const p = JSON.parse(gRaw); if (!p.absent) globalProfile = p } catch {} }
    } catch { /* profile absent: degraded mode */ }
  } else {
    log('shadow-compare: no args.profile passed and home dir unresolved; global profile unavailable (degraded mode)')
  }
  try {
    const rRaw = await agent(
      `Read the file at "${REPO_PROFILE_PATH}". If it exists and is valid JSON, return it. If the file is absent or unreadable, return exactly the JSON object {"absent":true}.`,
      { label: 'load-repo-profile', phase: 'Predict', model: 'sonnet' },
    )
    if (rRaw && typeof rRaw === 'object' && !rRaw.absent) repoProfile = rRaw
    else if (typeof rRaw === 'string') { try { const p = JSON.parse(rRaw); if (!p.absent) repoProfile = p } catch {} }
  } catch { /* profile absent: degraded mode */ }
}

// Compose layered profile: global core is the base; repo layer wins on conflict.
// Neither is ever sent to the external challenger.
const PROFILE_PRESENT = globalProfile !== null || repoProfile !== null

const PROFILE_SUMMARY = PROFILE_PRESENT
  ? JSON.stringify({ global: globalProfile, repo: repoProfile }, null, 2).slice(0, 3000)
  : 'No judgment profile found. Use generic heuristic: prefer the option with the best high-quality, long-term outcome; quality over speed/cost.'

// ---- Phase 1: predict each Tier-A ruling using the profile ------------------
// Predictions are generated from composed profile + fork context.
// They run silently and are NEVER shown to the operator before actualRuling is set.

const predictions = await runBounded(TIER_A.map(item => async () => {
  if (!item || !item.fork) return null
  const fork = item.fork
  const packet = item.packet

  // Build the prediction prompt.
  // IMPORTANT: profile content is Claude-local. Only the sanitized fork summary
  // (id, title, description, options, recommendation) is passed here.
  // No profile content appears in any prompt sent to an external model.
  const optionsList = (fork.options ?? []).join(' | ')
  const recommendation = packet?.recommendation ?? fork.leaning ?? 'unknown'

  let pred = null
  try {
    pred = await agent(
      `You are predicting how the operator will rule on a design fork, using their judgment profile.
JUDGMENT PROFILE (Claude-local, never shared externally):
${PROFILE_SUMMARY}

FORK TO PREDICT:
ID: ${fork.id}
TITLE: ${fork.title}
DESCRIPTION: ${fork.description}
OPTIONS: ${optionsList}
CURRENT RECOMMENDATION: ${recommendation}
DOMAIN: ${DOMAIN}
PROJECT: ${PROJECT}

Based on the operator's profile and the fork details, predict which option the operator will choose.
Set predictionConfidence to a float 0.0–1.0 reflecting your confidence.
Set predictionReasoning to a single-line internal reason (this is NEVER shown to the operator before they rule).
Be honest about uncertainty — a low-confidence prediction is better than a false high-confidence one.`,
      { schema: PREDICTION_SCHEMA, label: `predict:${fork.id}`, phase: 'Predict', model: 'sonnet' },
    )
    if (pred) pred.forkId = fork.id
  } catch (err) {
    log(`shadow-compare: prediction failed for fork ${fork.id}: ${err?.message ?? err}`)
  }

  // Clamp confidence to [0.0, 1.0] and coerce to float.
  if (pred && typeof pred.predictionConfidence !== 'undefined') {
    const raw = Number(pred.predictionConfidence)
    pred.predictionConfidence = isFinite(raw) ? Math.max(0.0, Math.min(1.0, raw)) : null
  }

  return pred
}), MAX_TIER_A_CONCURRENCY)

const predictionByFork = {}
for (const p of predictions) {
  if (p && p.forkId) predictionByFork[p.forkId] = p
}

// ---- Phase 2: challenge each prediction (availability-gated) ----------------
// The challenger receives ONLY the public fork summary — no profile, no prediction,
// no rationale. This is the third-party transmission boundary.
phase('Challenge')

// Clamp untrusted fork text before it is relayed to the external CLI. These come
// from the GitHub issue (attacker-controllable on a repo that accepts external
// issues). They are NOT a hard injection boundary (the model constructs the relay),
// but clamping length + stripping control chars limits the blast radius.
function clampRelayText(s, max) {
  // Strip C0 controls + DEL (\x00-\x1f, \x7f), C1 controls (\x80-\x9f), and the common
  // Unicode bidi-override / zero-width / format characters used to obfuscate prompt
  // injection in attacker-controlled issue text (RLO/LRO U+202A-U+202E, LRM/RLM
  // U+200E/200F, zero-width U+200B-U+200D, word-joiner U+2060, BOM/ZWNBSP U+FEFF).
  // Defense-in-depth on the soft relay boundary (not a hard injection boundary).
  return String(s ?? '')
    .replace(/[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060\ufeff]+/g, ' ')
    .slice(0, max)
}

// Escape a string for safe embedding inside a SHELL DOUBLE-QUOTED argument.
// The cross-family relay instruction below tells the invoking agent to run
// `${XF_EXEC}` "<prompt>". If that agent turns the instruction into a literal
// double-quoted shell command, a POSIX shell STILL interprets \, ", $ and
// backtick inside double quotes, so untrusted fork text carrying $(...) or
// `...` would trigger command substitution and run BEFORE the CLI is ever
// invoked. Escape all four (backslash FIRST, so the escapes added for the
// other three are not themselves re-escaped). Mirrors clarity-gate.js's
// escapeForShellDoubleQuote, which closed the same class of hole there
// (issue #146, mirrors issue #145 round 3, P1).
function escapeForShellDoubleQuote(s) {
  return String(s ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\$/g, '\\$')
    .replace(/`/g, '\\`')
}

const challenges = await runBounded(TIER_A.map(item => async () => {
  if (!item || !item.fork) return null
  const fork = item.fork
  const pred = predictionByFork[fork.id]
  if (!pred) {
    // No prediction to challenge — record as not ran.
    return {
      forkId: fork.id,
      challengeOutcome: null,
      challengeReason: 'no prediction to challenge',
      challengeRan: false,
      challengeRanCrossFamily: false,
    }
  }

  // SANITIZED fork summary for external challenger: ONLY public fields.
  // No prediction, no confidence, no profile excerpts, no prior rationale.
  // The text is untrusted issue content, so clamp length + strip control chars
  // before it enters any relayed prompt (defense-in-depth, not a hard boundary).
  const sanitizedFork = {
    id: clampRelayText(fork.id, 200),
    title: clampRelayText(fork.title, 300),
    description: clampRelayText(fork.description, 1000),
    options: (fork.options ?? []).map(o => clampRelayText(o, 200)),
  }
  const optionsList = sanitizedFork.options.join(' | ')

  // The challenger is asked: given this fork, which option is best?
  // The cross-family model (Codex) provides an independent opinion.
  // We compare its recommendation to our prediction to produce challengeOutcome.
  // challengeOutcome reflects whether the challenger AGREES with our prediction,
  // not whether our prediction is correct (the operator hasn't ruled yet).

  const challengerPromptForExternal = `Design fork analysis for ${PROJECT}: which option is best for long-term quality?
FORK: ${sanitizedFork.title}
DESCRIPTION: ${sanitizedFork.description}
OPTIONS: ${optionsList}
Reply in one short paragraph recommending one option and why. No preamble.`

  const challengerPromptForInternal = `You are an independent skeptic reviewing a design fork for ${PROJECT}.
FORK: ${sanitizedFork.title}
DESCRIPTION: ${sanitizedFork.description}
OPTIONS: ${optionsList}
Which option is best for long-term quality? Reply in one short paragraph recommending one option. No preamble.`

  let challengeRan = false
  let challengeRanCrossFamily = false
  let challengeOutcome = null
  let challengeReason = 'challenger unavailable'

  try {
    let challengerRecommendation = null

    if (CODEX) {
      // Fresh per-prediction cross-family invocation.
      // CRITICAL: only sanitizedFork fields in the prompt — no profile content.
      try {
        const xfResult = await agent(
          `Invoke the cross-family CLI to get an independent opinion on this design fork.
Run: \`${XF_EXEC}\` with this SHORT prompt (keep it under 200 words): "${escapeForShellDoubleQuote(challengerPromptForExternal)}"
If the CLI answers, report its exact recommendation. If unreachable or empty, say so.`,
          { label: `challenge:xf:${fork.id}`, phase: 'Challenge', model: 'sonnet' },
        )
        if (xfResult && typeof xfResult === 'string' && xfResult.length > 20) {
          challengerRecommendation = xfResult
          challengeRanCrossFamily = true
        }
      } catch { /* cross-family unavailable */ }
    }

    if (!challengerRecommendation) {
      // Same-family fresh skeptic fallback.
      const fallbackResult = await agent(
        challengerPromptForInternal,
        { label: `challenge:fallback:${fork.id}`, phase: 'Challenge', model: 'sonnet' },
      )
      if (fallbackResult && typeof fallbackResult === 'string') {
        challengerRecommendation = fallbackResult
      }
    }

    if (challengerRecommendation) {
      challengeRan = true
      // The challenger recommendation is the verbatim output of the EXTERNAL
      // cross-family CLI (untrusted, derived from attacker-influenceable issue text).
      // Clamp + strip it before embedding in this Claude-local match prompt, the same
      // treatment the fork fields already get, so a compromised challenger cannot
      // inject instructions to steer the match outcome (enum-constrained anyway, and
      // it only affects shadow measurement data, never the operator's ruling).
      const safeChallenger = clampRelayText(challengerRecommendation, 1500)
      // Determine if challenger's recommendation matches the prediction.
      const matchResult = await agent(
        `Compare two option recommendations for the same design fork.
FORK OPTIONS: ${optionsList}
PREDICTION (to evaluate): "${pred.predictedRuling}"
CHALLENGER RECOMMENDATION: "${safeChallenger}"
Do these two recommend the same option? Return a JSON with:
- "outcome": "agreed" | "pushed-back" | "unresolved"
- "reason": one-line explanation (public-safe, no internal profile content)`,
        {
          schema: {
            type: 'object',
            required: ['outcome', 'reason'],
            properties: {
              outcome: { type: 'string', enum: ['agreed', 'pushed-back', 'unresolved'] },
              reason: { type: 'string' },
            },
          },
          label: `challenge:match:${fork.id}`,
          phase: 'Challenge',
          model: 'sonnet',
        },
      )
      if (matchResult) {
        challengeOutcome = matchResult.outcome ?? 'unresolved'
        // R3 (issue #63): route through the shared clampRelayText scrub instead of
        // an ad hoc \r\n-only replace. clampRelayText already strips the FULL
        // C0/C1/DEL control-byte range (matchResult.reason is itself untrusted,
        // model-emitted text) plus the Unicode bidi/zero-width obfuscation
        // characters — a strict superset of the old CR/LF-only clamp, and the
        // SAME scrub the decision-ledger append validator now enforces on write
        // (D8-opt1), so a legitimate reason string can never be accepted here
        // and then rejected at ledger append for a control byte this clamp
        // missed.
        challengeReason = clampRelayText(matchResult.reason, 500)
      }
    }
  } catch (err) {
    log(`shadow-compare: challenge failed for fork ${fork.id}: ${err?.message ?? err}`)
  }

  return {
    forkId: fork.id,
    challengeOutcome,
    challengeReason,
    challengeRan,
    challengeRanCrossFamily,
  }
}), MAX_TIER_A_CONCURRENCY)

const challengeByFork = {}
for (const c of challenges) {
  if (c && c.forkId) challengeByFork[c.forkId] = c
}

// ---- Compose augmented packets -----------------------------------------------
// Return the original discovery output augmented with prediction metadata.
// The original packet shape is UNCHANGED (no field renamed or removed).
// New prediction fields are added as a _shadow superset — never replace existing.
// The trusted /arc skill reads these and writes the ledger record.

// Map over ALL Tier-A forks (not the capped TIER_A) so every fork is still returned
// to the operator for ruling. Forks beyond MAX_SHADOW_FORKS simply have no prediction
// (the lookups below miss → null _shadow), which is exactly the bounded-cost behavior:
// they are presented and ruled, just not predicted/measured this run.
const augmentedTierA = ALL_TIER_A.map(item => {
  if (!item) return item
  const pred = predictionByFork[item.fork?.id]
  const challenge = challengeByFork[item.fork?.id]
  return {
    ...item,
    // Untrusted prediction fields (workflow-proposed, never shown before operator rules).
    _shadow: {
      predicted: pred ? {
        predictedRuling: pred.predictedRuling ?? null,
        predictionConfidence: pred.predictionConfidence ?? null,
        // predictionReasoning is internal — not exposed in any user-facing output.
      } : null,
      challenged: challenge ? {
        challengeOutcome: challenge.challengeOutcome ?? null,
        challengeRan: challenge.challengeRan ?? false,
        challengeRanCrossFamily: challenge.challengeRanCrossFamily ?? false,
        // challengeReason is kept internal — not shown before ruling.
      } : null,
      // Internal fields (never user-facing) — prefixed _ in the return value
      // so callers that log the full packet don't accidentally surface them.
      _predictionReasoning: pred?.predictionReasoning ?? null,
      _challengeReason: challenge?.challengeReason ?? null,
    },
  }
})

// Tier-B: no prediction pass (agent IS the decider for Tier-B). The /arc skill
// writes Tier-B ledger records directly from the discovery output. Shadow-compare
// returns tierB unchanged so the skill can append ledger rows for Tier-B forks.
const augmentedTierB = TIER_B

log(`shadow-compare: processed ${augmentedTierA.length} Tier-A forks, ${augmentedTierB.length} Tier-B forks. Profile present: ${PROFILE_PRESENT}. Cross-family challenger available: ${CODEX}.`)

return {
  // Carry through the original discovery output fields unchanged.
  issue: ISSUE, // resolved from discoveryOutput.issue (or the args.issue fallback), never a silent 'unknown'
  codexUsed: DISCOVERY.codexUsed,
  tierC: DISCOVERY.tierC,
  // Augmented tiers with prediction metadata.
  tierA: augmentedTierA,
  tierB: augmentedTierB,
  // Shadow-compare metadata for the skill. Deliberately NO absolute profile paths
  // here: GLOBAL_PROFILE_PATH contains the OS username ($HOME), and the skill stores
  // this whole return value, so emitting it would leak an identifying path into the
  // agent's context/logs. The booleans below carry the only signal the skill needs.
  shadowMeta: {
    profilePresent: PROFILE_PRESENT,
    globalProfileUsed: globalProfile !== null,
    repoProfileUsed: repoProfile !== null,
    codexUsedForChallenge: CODEX,
    domain: DOMAIN,
    issueId: ISSUE_ID,
    // No timestamp here: the Workflow runtime forbids Date.now()/new Date() (it breaks
    // resume), and the trusted skill stamps the ledger record's timestamp at write time
    // anyway (SKILL.md ledger-write step). Emitting one here both crashed the real run
    // and duplicated the skill's stamp.
  },
}
