#!/usr/bin/env node
// clarity-lint.js — the live-chat plain-language self-check (issue #145, ruling
// livechat-prelint-form: "a REAL runnable pre-lint tool ... NOT merely a documented
// rule plus a list").
//
// This is a real, standalone, unsandboxed Node CLI — invoke it (via Bash) on a draft
// before it reaches the maintainer as an AskUserQuestion prompt or decision-prose,
// the same self-check discipline CLAUDE.md's "Plain-language decisions" section
// already asks for, now backed by code instead of only a documented reminder.
//
// This is a BEST-EFFORT self-check, not a hard gate (ruling new-principle-and-adr —
// distinct verbs per surface): unlike the arc-loop's clarity-gate.js, which BLOCKS
// and rewrites, AskUserQuestion is a native tool with no interception point an
// installed workflow can hook, so there is no code path that can refuse to send a
// message. Running this script before drafting a decision question is a discipline,
// not an enforced floor — treat a non-zero exit as "rewrite before sending", not as
// a crash.
//
// USAGE:
//   node skill/arc/clarity-lint.js "some draft text to check"
//   printf '%s' "$DRAFT" | node skill/arc/clarity-lint.js
//
// Reads the SAME shared kill-list (clarity-killlist.json, next to this file) that
// workflows/clarity-gate.js consumes via args.jargonList — one shared data file,
// two readers, so the two surfaces cannot drift on which terms count as jargon
// (ruling killlist-home). The DETECTION ALGORITHM below is a separate artifact from
// the wordlist and is duplicated byte-for-byte from workflows/clarity-gate.js (the
// sandboxed Workflow runtime has no `require`, so a shared module is not possible —
// see that file's own header) — an automated test
// (test/clarity-gate-behavior.test.js, DETECT-DUPLICATION) extracts and diffs both
// copies, so the two detectors cannot silently drift apart either.
//
// Exit codes: 0 = clean (no unglossed jargon found). 1 = violations found (rewrite
// before sending). 2 = usage/read error (no draft text supplied).

'use strict'
const fs = require('fs')
const path = require('path')

// ============================================================================
// CLARITY-DETECTION (shared, byte-identical with workflows/clarity-gate.js —
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

function loadJargonList() {
  const killlistPath = path.join(__dirname, 'clarity-killlist.json')
  const raw = fs.readFileSync(killlistPath, 'utf8')
  const parsed = JSON.parse(raw)
  if (!Array.isArray(parsed.terms)) throw new Error(`clarity-killlist.json is missing a "terms" array`)
  return parsed.terms
}

function readDraftText() {
  const argText = process.argv.slice(2).join(' ').trim()
  if (argText) return argText
  if (!process.stdin.isTTY) {
    try {
      return fs.readFileSync(0, 'utf8')
    } catch {
      return ''
    }
  }
  return ''
}

function main() {
  const draft = readDraftText()
  if (!draft.trim()) {
    process.stderr.write('clarity-lint: no draft text supplied (pass it as an argument or pipe it via stdin)\n')
    process.exit(2)
  }
  const jargonList = loadJargonList()
  const violations = detectJargon(draft, jargonList)
  const result = {
    clean: violations.length === 0,
    violationCount: violations.length,
    terms: [...new Set(violations.map(v => v.term))],
    violations,
  }
  process.stdout.write(JSON.stringify(result, null, 2) + '\n')
  process.exit(result.clean ? 0 : 1)
}

if (require.main === module) {
  main()
}

module.exports = { detectJargon, loadJargonList }
