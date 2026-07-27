#!/usr/bin/env bash
# decision-ledger.sh — standalone domain-neutral query tool for the arc decision ledger.
#
# Subcommands:
#   decision-ledger.sh query  --issue <N>           # retrieve all decisions for an issue (exact id match)
#   decision-ledger.sh query  --fork <forkId>        # retrieve a specific fork decision
#   decision-ledger.sh match-rate [--domain <d>] [--class <c>]  # rolling match-rate per decision-class per domain
#   decision-ledger.sh list   [--issue <N>] [--domain <d>]      # list all records (optional filters)
#   decision-ledger.sh append  <json-file>           # (trusted-skill use only) append one ledger record
#
# Naming rule: no code-specific words (review, build, PR, diff) in subcommand names.
# Domain-neutral subcommands: query, match-rate, list, append.
#
# Ledger location: .gstack/arc-rulings/decisions.jsonl
# Schema version: 1
#
# Schema (schemaVersion 1 — stays 1 per ADR-0020/AU1: new fields are added as
# optional + forward-defaulted on read, decisionType is widened IN PLACE, never
# bumped, since the validator hard-rejects any other schemaVersion/decisionType and a
# bump would break every already-installed validator for zero gain) — fields per
# JSONL record:
#   schemaVersion       : 1 (required)
#   issueId             : bare numeric id (normalized, e.g. "32")
#   issueTitle          : full human-readable issue description (optional)
#   forkId              : short kebab-case slug (required)
#   domain              : "code" | "content" (required; default "code")
#   decisionType        : "tier-a" | "tier-b" | "tier-b-audit" (required; widened by AU1/ADR-0020)
#   decisionClass       : spine class or domain subtype (required). "unclassified" is
#                         allowed ONLY together with a non-empty unclassifiedReason —
#                         see unclassifiedReason below. This companion requirement is
#                         the AU1 fix for "required class assignment": a bare
#                         'unclassified' with no reason is REJECTED at append time.
#   unclassifiedReason  : one-line string, REQUIRED whenever decisionClass=="unclassified",
#                         else optional/ignored (null on a real classification). AU1/ADR-0020.
#   options             : array of option strings
#   recommendation      : the recommendation offered to the operator
#   timestamp           : ISO-8601 (required)
#   project             : project name (optional)
#   # Prediction fields (PRODUCED by the shadow-compare workflow, not persisted by it):
#   predictedRuling     : option label the profile predicted (null if no prediction)
#   predictionConfidence: RAW float 0.0–1.0 or null — never overwritten, append-only truth.
#   effectiveConfidence : the CAPPED float downstream thresholds read, or null. AU1/ADR-0020:
#                         a separate, explicitly-stored field so raw predictionConfidence
#                         stays untouched. It cannot be set without predictionConfidence
#                         also present (it is a capped view of that raw source) and must
#                         never exceed it. The cap itself is CONFIDENCE_CAP (0.75, see the
#                         constant's own comment below) — a single named, provisional
#                         constant, not a config field.
#   challengeOutcome    : "agreed" | "pushed-back" | "unresolved" | null
#   challengeReason     : the fresh-agent challenger's stated reason (single-line; null if none)
#   challengeRan        : boolean
#   challengeRanCrossFamily : boolean
#   # Trusted ruling fields (written by /arc skill AFTER operator rules):
#   actualRuling        : the operator's chosen option (or agent's for Tier-B)
#   rationale           : one-line rationale (single-line, newlines stripped)
#   matchScore          : "exact" | "partial" | "miss" | null (null until actualRuling set;
#                         AU1/ADR-0020 — set ONLY by a fresh, independent blind-grader call,
#                         NEVER computed by the same skill/context that made the prediction;
#                         fail-closed to null, alongside scoredBy:null, when no grader ran)
#   scoredBy            : null, OR a structured object {family, mode, model} — WHO scored
#                         matchScore. AU1/ADR-0020. `mode` is a constrained enum:
#                         "cross-family" | "same-family-fresh" | "self" (independence is
#                         DERIVED from mode). `family`/`model` are stamped by the INVOKING
#                         SKILL based on which grading mechanism it actually dispatched —
#                         never self-reported by the grader agent (a grader that could
#                         label its own self-grade "cross-family" would defeat the whole
#                         mechanism). Legacy records written before AU1 have NO scoredBy
#                         key at all; on READ this defaults to the sentinel object
#                         {mode:"self-legacy", family:null, model:null} — "self-legacy" is
#                         a READ-ONLY default, NEVER a value the append validator accepts
#                         on write (only cross-family/same-family-fresh/self are writable).
#                         Independent-scoring metrics (calibration, the future graduation
#                         gate) EXCLUDE every record whose scoredBy.mode is "self" or
#                         "self-legacy" — both the explicit and the defaulted case.
#   auditOutcome        : null, OR "as-ruled" | "diverged" | "unclear" — the Tier-B
#                         post-ship audit's OWN verdict field (AU1/ADR-0020), distinct from
#                         matchScore (which stays Tier-A-only). Only meaningful on a
#                         decisionType=="tier-b-audit" record.
#   auditsForkId        : null, OR the forkId of the Tier-B decision this audit record
#                         audits (AU1/ADR-0020) — a soft linkage pointer, NOT referentially
#                         enforced at append time (would require reading the whole ledger).
#                         A tier-b-audit record's OWN forkId equals the audited decision's
#                         forkId (decisionType is what disambiguates them in the ledger and
#                         in the dedup key below); auditsForkId restates that same value
#                         explicitly so a reader/report never has to infer the link from
#                         forkId equality + decisionType alone.
#   correctionOf        : null, OR the forkId (scoped by the SAME issueId+decisionType) of
#                         a prior record this record retroactively corrects (AU1/ADR-0020,
#                         ruling retroactive-correction-format — a.k.a. "supersedes"). A
#                         soft pointer, same no-referential-integrity caveat as auditsForkId.
#   probeSource         : null, OR "S1" | "S2" — which one-time backfill probe produced a
#                         correction record ("S1" = blind re-score of matchScore/scoredBy;
#                         "S2" = retroactive decisionClass assignment). Only meaningful
#                         alongside a non-null correctionOf. Correction records are written
#                         by the trusted skill (or a script it alone invokes) — NEVER by
#                         arc-discovery.js/shadow-compare.js, the sandboxed workflow layer.
#
# Single-writer flow (NOT two-phase):
#   shadow-compare PRODUCES (does not persist) the prediction + challenge fields and
#   RETURNS them to the trusted /arc skill. shadow-compare never touches this ledger.
#   The /arc skill, AFTER the operator rules, writes ONE complete row per fork:
#   the prediction fields from shadow-compare's return value + the trusted ruling
#   fields (actualRuling, rationale, matchScore, scoredBy) it owns — matchScore/scoredBy
#   come from a FRESH, independently-invoked blind-grader call (AU1/ADR-0020), never a
#   same-context self-comparison. There is exactly one writer (the skill) and — for a
#   Tier-A/Tier-B decision record — one row per (issueId, forkId, decisionType).
#   Defensive de-dup: the match-rate report still de-duplicates on
#   (issueId, forkId, decisionType) last-row-wins WITHIN EACH RECORD KIND (query/list
#   intentionally surface every matching row, un-deduped) (AU1/ADR-0020
#   widened this from the original 2-field (issueId, forkId) key: a tier-b-audit record
#   deliberately SHARES issueId+forkId with the Tier-B decision it audits — via
#   auditsForkId — so the 2-field key would silently collapse a ruling row and its later
#   audit row into one. decisionType is what keeps them distinct. A retroactive
#   correction record uses the SAME (issueId, forkId, decisionType) tuple as the record
#   it corrects, so later-row-wins is exactly the intended "correction supersedes the
#   original" semantics), so an accidental double-append (e.g. a re-run) does not
#   double-count within a kind — but the design writes each fork+kind exactly once.
#
# Forward-compatible defaults: missing fields in older records are treated as
# their defined default (missing matchScore → null; missing challengeRan → false;
# missing scoredBy → {mode:"self-legacy", family:null, model:null}; missing
# effectiveConfidence/auditOutcome/auditsForkId/correctionOf/probeSource/unclassifiedReason
# → null). Unknown fields are silently skipped (lenient on read, strict on write).
#
# Concurrent writes: the trusted /arc skill still serializes ledger appends by
# design, but a concurrent/accidental extra writer is no longer assumed-safe on
# every host — R3 (issue #63) added a fail-open append lock (flock where
# available, else a portable mkdir-sentinel) around the write itself, plus a
# same-lock repair of a missing trailing newline (see cmd_append and the block
# comment above _cmd_append_cleanup). The query tool still tolerates a partial
# or malformed last line by skipping lines that do not parse as valid JSON
# rather than hard-aborting — that behavior is UNCHANGED and stays read-only
# (ruling read-print-side-scope; a pre-existing dirty-but-parseable row still
# prints raw on query/list).
#
# CONFIDENCE_CAP: hardcoded to 0.75 as the ONE authoritative constant effectiveConfidence
# is derived from (see the constant's own definition below, near validate_record_fields).
# Provisional: the principled successor is a cap derived from measured challenger
# precision, not a flat number — this is a placeholder until that data exists. There is
# deliberately NO arc.config field for this (AU1 ruling confidence-cap-configurable).

set -euo pipefail

SUBCOMMAND="${1:-}"
LEDGER="${LEDGER_PATH:-.gstack/arc-rulings/decisions.jsonl}"

# Pick a JSON runner once.
# Validation runner (for record writes): python3, else node. jq is intentionally
# NOT a validation runner: it cannot enforce the integer/float bounds and enum
# membership the append validator requires without diverging from python3/node, so
# the two validators (python3, node) are the only sanctioned write gates. If NEITHER
# python3 nor node is present, append fails closed (see VALIDATE_RUNNER guard below);
# we do NOT silently fall back to a weaker jq validator.
# Query runner (for read/filter operations): prefer jq (fastest streaming), then
# python3, then node.
VALIDATE_RUNNER=""
if command -v python3 >/dev/null 2>&1; then
  VALIDATE_RUNNER="python3"
elif command -v node >/dev/null 2>&1; then
  VALIDATE_RUNNER="node"
fi

JSON_RUNNER=""
if command -v jq >/dev/null 2>&1; then
  JSON_RUNNER="jq"
elif command -v python3 >/dev/null 2>&1; then
  JSON_RUNNER="python3"
elif command -v node >/dev/null 2>&1; then
  JSON_RUNNER="node"
fi

if [ -z "$JSON_RUNNER" ]; then
  echo "decision-ledger: requires jq, python3, or node to parse JSONL records" >&2
  exit 2
fi
# NOTE: a missing VALIDATE_RUNNER (no python3 and no node) is NOT a startup error.
# The read paths (query, match-rate, list) still work on a jq-only host. Only the
# append path requires a validation runner, and it fails closed there (see
# validate_record_fields), so a jq-only host can still query but cannot write.

# ---------------------------------------------------------------------------
# Shared JSONL helpers (always use jq/python3/node — never string-interpolate
# into filter expressions; all user inputs passed as --arg variables to avoid
# injection).
# ---------------------------------------------------------------------------

# ledger_records_python — emit all valid records as JSON lines, skipping
# partial/invalid lines with a stderr warning. Used by python3/node paths.
ledger_read_all_python() {
  python3 - "$LEDGER" <<'PY'
import json, sys

path = sys.argv[1]
CURRENT_SCHEMA = 1

try:
    lines = open(path, encoding="utf-8").readlines()
except FileNotFoundError:
    sys.exit(0)  # empty ledger is valid
except Exception as e:
    sys.stderr.write("decision-ledger: cannot read ledger: %s\n" % e)
    sys.exit(1)

for i, line in enumerate(lines, 1):
    line = line.rstrip("\n\r")
    if not line.strip():
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError as e:
        sys.stderr.write("decision-ledger: skipping malformed line %d: %s\n" % (i, e))
        continue
    # Forward-compatible: missing schemaVersion treated as 1.
    sv = rec.get("schemaVersion", 1)
    if not isinstance(sv, int):
        sys.stderr.write("decision-ledger: warning — line %d has unrecognized schemaVersion %r (processing anyway)\n" % (i, sv))
    # Forward defaults for optional fields.
    rec.setdefault("matchScore", None)
    rec.setdefault("challengeRan", False)
    rec.setdefault("predictionConfidence", None)
    rec.setdefault("predictedRuling", None)
    rec.setdefault("challengeOutcome", None)
    rec.setdefault("challengeReason", None)
    rec.setdefault("decisionClass", "unclassified")
    rec.setdefault("domain", "code")
    # AU1/ADR-0020 forward defaults — kept IDENTICAL across all three read paths
    # (this python3 block, the node block below, and the jq block in ledger_read_all)
    # and ALSO in cmd_match_rate's own separate read loops (that command re-implements
    # its own defaulting rather than calling this helper) — a field defaulted here but
    # not there reads as present-vs-undefined inconsistently between the two entry points.
    if "scoredBy" not in rec or rec.get("scoredBy") is None:
        rec["scoredBy"] = {"mode": "self-legacy", "family": None, "model": None}
    rec.setdefault("effectiveConfidence", None)
    rec.setdefault("auditOutcome", None)
    rec.setdefault("auditsForkId", None)
    rec.setdefault("correctionOf", None)
    rec.setdefault("probeSource", None)
    rec.setdefault("unclassifiedReason", None)
    print(json.dumps(rec))
PY
}

ledger_read_all_node() {
  node - "$LEDGER" <<'NODE'
const fs = require("fs");
const path = process.argv[2];
let content;
try { content = fs.readFileSync(path, "utf8"); } catch (e) {
  if (e.code === "ENOENT") process.exit(0);
  process.stderr.write("decision-ledger: cannot read ledger: " + e.message + "\n");
  process.exit(1);
}
const lines = content.split("\n");
lines.forEach((line, i) => {
  line = line.trim();
  if (!line) return;
  let rec;
  try { rec = JSON.parse(line); } catch (e) {
    process.stderr.write("decision-ledger: skipping malformed line " + (i+1) + ": " + e.message + "\n");
    return;
  }
  if (!rec.schemaVersion) rec.schemaVersion = 1;
  if (rec.matchScore === undefined) rec.matchScore = null;
  if (rec.challengeRan === undefined) rec.challengeRan = false;
  if (rec.predictionConfidence === undefined) rec.predictionConfidence = null;
  if (rec.predictedRuling === undefined) rec.predictedRuling = null;
  if (rec.challengeOutcome === undefined) rec.challengeOutcome = null;
  if (rec.challengeReason === undefined) rec.challengeReason = null;
  if (rec.decisionClass === undefined) rec.decisionClass = "unclassified";
  if (rec.domain === undefined) rec.domain = "code";
  // AU1/ADR-0020 forward defaults — kept IDENTICAL across all three read paths (this
  // node block, the python3 block above, and the jq block in ledger_read_all) and ALSO
  // in cmd_match_rate's own separate read loops (that command re-implements its own
  // defaulting rather than calling this helper) — a field defaulted here but not there
  // reads as undefined inconsistently between the two entry points.
  if (rec.scoredBy === undefined || rec.scoredBy === null) rec.scoredBy = { mode: "self-legacy", family: null, model: null };
  if (rec.effectiveConfidence === undefined) rec.effectiveConfidence = null;
  if (rec.auditOutcome === undefined) rec.auditOutcome = null;
  if (rec.auditsForkId === undefined) rec.auditsForkId = null;
  if (rec.correctionOf === undefined) rec.correctionOf = null;
  if (rec.probeSource === undefined) rec.probeSource = null;
  if (rec.unclassifiedReason === undefined) rec.unclassifiedReason = null;
  process.stdout.write(JSON.stringify(rec) + "\n");
});
NODE
}

# Emit all valid records (one JSON line each) via the chosen runner.
ledger_read_all() {
  if [ ! -f "$LEDGER" ]; then return; fi
  case "$JSON_RUNNER" in
    jq)
      # A genuinely UNREADABLE ledger (permissions revoked, an I/O error, an
      # undecodable byte stream, etc.) must fail LOUDLY here — exactly like the
      # python3/node read paths, which print "cannot read ledger" and exit 1 on
      # the identical input. jq is picked FIRST when present, so without this a
      # broken/inaccessible ledger would read as a false-empty "0 record(s)"
      # success on the majority of installs — silently reporting existing data
      # as "nothing here", the exact silent-data-loss failure mode CLAUDE.md's
      # 'errors fail loud' principle and issue #63's retroactive-ledger-migration
      # ruling exist to prevent. A malformed-but-READABLE line is NOT a read
      # failure — the per-line `try ... catch null` below drops it and the
      # skip-notice reports it; jq only exits non-zero on an actual open/read
      # (or whole-file decode) error, which is precisely what we surface here.
      # This matches python3/node, whose whole-file readlines()/readFileSync()
      # likewise fail hard on an unreadable-or-undecodable file rather than
      # per-line-skipping it.
      if [ ! -r "$LEDGER" ]; then
        echo "decision-ledger: cannot read ledger at $LEDGER (not readable)" >&2
        return 1
      fi
      # jq reads JSONL as a stream of independent inputs by default (one per line).
      # Read each line defensively (skip malformed lines), then apply the same
      # forward-compatible defaults the python/node helpers use so every reader
      # path produces records with the same shape.
      #
      # The jq filter's own `try ... catch null | select(. != null)` silently drops
      # a malformed line with NO diagnostic at all — unlike the python3/node paths
      # above, which each print "skipping malformed line N" to stderr. Ruling
      # retroactive-ledger-migration (issue #63) requires the skip-on-read notice
      # to be genuinely visible, not silently absorbed — jq is picked FIRST when
      # present (a common default runner), so leaving it silent here would mean
      # the majority of real installs never see the warning python3/node already
      # give. Compare non-blank input lines to successfully-parsed output lines
      # and emit ONE summary warning (not per-line, since jq has no simple
      # per-line stderr hook without a version-fragile `stderr` filter) when
      # they differ, bringing this runner to parity rather than leaving it quiet.
      local _jq_out _jq_rc=0 _jq_in_nonblank _jq_out_count
      _jq_out="$(jq -Rc '
        . as $line
        | (try ($line | fromjson) catch null)
        | select(. != null)
        | .matchScore           //= null
        | .challengeRan         //= false
        | .predictionConfidence //= null
        | .predictedRuling      //= null
        | .challengeOutcome     //= null
        | .challengeReason      //= null
        | .decisionClass        //= "unclassified"
        | .domain               //= "code"
        | .scoredBy             //= {"mode":"self-legacy","family":null,"model":null}
        | .effectiveConfidence  //= null
        | .auditOutcome         //= null
        | .auditsForkId         //= null
        | .correctionOf         //= null
        | .probeSource          //= null
        | .unclassifiedReason   //= null
      ' "$LEDGER" 2>/dev/null)" || _jq_rc=$?
      # A non-zero jq exit is a genuine read/decode failure (the per-line
      # try/catch above already absorbs malformed-JSON lines without erroring),
      # so surface it loudly and stop, rather than reporting a false-empty read.
      # `-r` above catches the plain permission case; this catches the rest
      # (I/O error mid-read, an undecodable byte stream) and keeps parity with
      # the python3/node runners' fail-hard-on-read-error behavior.
      if [ "$_jq_rc" -ne 0 ]; then
        echo "decision-ledger: cannot read ledger at $LEDGER (jq exited $_jq_rc)" >&2
        return 1
      fi
      if [ -n "$_jq_out" ]; then printf '%s\n' "$_jq_out"; fi
      # Count non-blank INPUT lines with awk, NOT `grep -c`. A crash-truncated
      # ledger line can contain invalid-UTF-8 / non-text bytes (F14 truncation
      # mid-multibyte-char); on such a file BSD/macOS `grep -c` treats the file as
      # BINARY and prints no count (exit 1), and even `grep -ac` proved
      # LOCALE-DEPENDENT here — it undercounts the bad line in a UTF-8 locale, so
      # the `-gt` test below never fires and the skipped-malformed-line notice is
      # silently suppressed for exactly the corrupt input it must surface (ruling
      # retroactive-ledger-migration: the skip-on-read notice must be genuinely
      # visible). `awk 'NF{c++} END{print c+0}'` is byte-oriented and locale-stable:
      # it counts every line with a non-whitespace field (matching the old
      # `[^[:space:]]` intent), emits a bare integer (so no `|| true`/`|| echo 0`
      # two-line-"0" hazard, and no leading-space `wc -l` output to trip `-gt`), and
      # prints 0 on empty/all-whitespace files. `2>/dev/null || echo 0` keeps a
      # freak awk failure from wedging the read under `set -e`.
      _jq_in_nonblank="$(awk 'NF{c++} END{print c+0}' "$LEDGER" 2>/dev/null || echo 0)"
      _jq_out_count=0
      [ -n "$_jq_out" ] && _jq_out_count="$(printf '%s\n' "$_jq_out" | awk 'NF{c++} END{print c+0}')"
      if [ "${_jq_in_nonblank:-0}" -gt "${_jq_out_count:-0}" ]; then
        echo "decision-ledger: skipped $((_jq_in_nonblank - _jq_out_count)) malformed line(s) while reading the ledger (not valid JSON)" >&2
      fi
      ;;
    python3) ledger_read_all_python ;;
    node) ledger_read_all_node ;;
  esac
}

# ---------------------------------------------------------------------------
# Runner-agnostic per-line JSON helpers. Each reads ONE JSON record on stdin
# and dispatches on $JSON_RUNNER (jq → python3 → node), so query/list behave
# identically on every supported runner — no python3-only else branches that
# silently return wrong answers on a node-only host (AC #3).
# ---------------------------------------------------------------------------

# json_field_equals <field> <value>  — prints "yes" if record[field]==value (exact, string-compared), else "no".
json_field_equals() {
  local field="$1" value="$2"
  case "$JSON_RUNNER" in
    jq)
      jq -r --arg f "$field" --arg v "$value" 'if ((.[$f] // "") | tostring) == $v then "yes" else "no" end' 2>/dev/null || echo "no"
      ;;
    python3)
      FIELD="$field" VALUE="$value" python3 -c '
import json,os,sys
try:
  r=json.loads(sys.stdin.read())
  # Raw compare (no trim) to match the jq read path; append rejects whitespace on
  # identity fields, so stored values are already clean and all three runners agree.
  print("yes" if str(r.get(os.environ["FIELD"],""))==os.environ["VALUE"] else "no")
except Exception:
  print("no")
' 2>/dev/null || echo "no"
      ;;
    node)
      FIELD="$field" VALUE="$value" node -e '
let d="";process.stdin.on("data",c=>d+=c);process.stdin.on("end",()=>{
  try{const r=JSON.parse(d);const v=r[process.env.FIELD];
    // Raw compare (no trim) to match the jq read path; append rejects identity
    // whitespace, so stored values are already clean and all three runners agree.
    process.stdout.write(String(v==null?"":v)===process.env.VALUE?"yes":"no");
  }catch{process.stdout.write("no");}
});' 2>/dev/null || echo "no"
      ;;
  esac
}

# json_pretty — pretty-print one JSON record from stdin.
json_pretty() {
  case "$JSON_RUNNER" in
    jq)      jq . 2>/dev/null || cat ;;
    python3) python3 -c 'import json,sys; print(json.dumps(json.loads(sys.stdin.read()),indent=2))' 2>/dev/null || cat ;;
    node)    node -e 'let d="";process.stdin.on("data",c=>d+=c);process.stdin.on("end",()=>{try{console.log(JSON.stringify(JSON.parse(d),null,2));}catch{process.stdout.write(d);}});' 2>/dev/null || cat ;;
  esac
}

# json_project — emit a compact projection of the list-view fields from stdin.
json_project() {
  case "$JSON_RUNNER" in
    jq)
      jq -c '{issueId,forkId,domain,decisionType,decisionClass,matchScore,actualRuling,timestamp}' 2>/dev/null || cat
      ;;
    python3)
      python3 -c '
import json,sys
r=json.loads(sys.stdin.read())
out={k:r.get(k) for k in ["issueId","forkId","domain","decisionType","decisionClass","matchScore","actualRuling","timestamp"]}
print(json.dumps(out))
' 2>/dev/null || cat
      ;;
    node)
      node -e '
let d="";process.stdin.on("data",c=>d+=c);process.stdin.on("end",()=>{
  try{const r=JSON.parse(d);const keys=["issueId","forkId","domain","decisionType","decisionClass","matchScore","actualRuling","timestamp"];
    const o={};keys.forEach(k=>o[k]=r[k]===undefined?null:r[k]);console.log(JSON.stringify(o));
  }catch{process.stdout.write(d+"\n");}
});' 2>/dev/null || cat
      ;;
  esac
}

# CONFIDENCE_CAP — the ONE authoritative place effectiveConfidence's cap is defined
# (AU1/ADR-0020, ruling confidence-cap-configurable). 0.75 is PROVISIONAL: the
# principled successor is a cap derived from measured challenger precision, not a flat
# number — this is a placeholder until that data exists. There is deliberately NO
# arc.config field for this; every consumer reads/matches this single constant, never a
# second copy-pasted literal. The append validator CONSUMES it: both the python3 and node
# validator bodies receive it via the CONFIDENCE_CAP env var and REJECT any stored
# effectiveConfidence above it (AU1/ADR-0020, principle 7 — enforce, don't just ask).
CONFIDENCE_CAP="0.75"

# Validate mandatory fields for a record about to be appended.
# Exits non-zero with an error message if invalid.
#
# Validation runs on python3 OR node ONLY. The two validators below enforce
# IDENTICAL rules so the accept/reject verdict is the same regardless of which host
# tool is present (see the parity-fixture battery in shadow-compare.test.sh). jq is
# NOT a validation runner (it cannot enforce integer-vs-float schemaVersion, reject
# numeric-string predictionConfidence, or check the domain/challengeOutcome enums
# without diverging), so if neither python3 nor node is available, append fails
# closed here with a non-zero exit rather than falling back to a weaker gate.
# _read_rec_field <json-file> <field> — echo a top-level string field's value from a
# JSON record file (or empty string if absent/null/non-string). Runner-agnostic
# (jq/python3/node), used by cmd_append's AU1/ADR-0020 dangling-pointer warning.
# Defined as a standalone function (never a `case` inline inside `$(...)`) because
# bash 3.2 (macOS's shipped /bin/bash) mis-parses a `case … esac` containing a
# double-quoted string with embedded parens directly inside a command substitution.
_read_rec_field() {
  local file="$1" field="$2"
  case "$JSON_RUNNER" in
    jq)      jq -r --arg f "$field" 'if (.[$f] | type) == "string" then .[$f] else "" end' "$file" 2>/dev/null ;;
    python3) FIELD="$field" python3 -c "
import json, os, sys
try:
    rec = json.load(open(sys.argv[1], encoding='utf-8'))
    v = rec.get(os.environ['FIELD'])
    print(v if isinstance(v, str) else '')
except Exception:
    print('')
" "$file" 2>/dev/null ;;
    node)    FIELD="$field" node -e "
const fs = require('fs');
try {
  const rec = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
  const v = rec[process.env.FIELD];
  process.stdout.write(typeof v === 'string' ? v : '');
} catch { process.stdout.write(''); }
" "$file" 2>/dev/null ;;
    *) echo "" ;;
  esac
}

validate_record_fields() {
  local rec_file="$1"
  if [ -z "$VALIDATE_RUNNER" ]; then
    echo "decision-ledger append: record validation requires python3 or node, but neither is installed; refusing to append (fail closed). jq cannot validate the schema bounds/enums and is not used for validation." >&2
    exit 2
  fi
  if [ "$VALIDATE_RUNNER" = "python3" ]; then
    CONFIDENCE_CAP="$CONFIDENCE_CAP" python3 - "$rec_file" <<'PY'
import json, os, sys
# AU1/ADR-0020 — the ONE cap constant, injected from the shell CONFIDENCE_CAP so the
# validator actually CONSUMES it (no second copy-pasted literal). effectiveConfidence
# is rejected above this value (principle 7: enforce the cap, don't just ask the skill).
CONFIDENCE_CAP = float(os.environ.get("CONFIDENCE_CAP", "0.75"))

# R3 (issue #63, D8-opt1) — ONE shared control-character predicate, reused by
# the identity-field check, the dangling-pointer check, and the free-text
# scrub below so the three can never diverge into hand-written copies of the
# same byte range: C0 controls (0x00-0x1f), DEL (0x7f), C1 controls (0x80-0x9f).
def _has_control_char(s):
    return any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in s)

try:
    rec = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    sys.stderr.write("decision-ledger: record is not valid JSON: %s\n" % e)
    sys.exit(1)
required = ["schemaVersion", "issueId", "forkId", "domain", "decisionType", "decisionClass", "timestamp"]
for f in required:
    if f not in rec or rec[f] is None:
        sys.stderr.write("decision-ledger: record missing required field '%s'\n" % f)
        sys.exit(1)
# Required fields must be present AND non-empty (after trimming).
for f in required:
    if str(rec.get(f, "")).strip() == "":
        sys.stderr.write("decision-ledger: required field '%s' must be non-empty\n" % f)
        sys.exit(1)
# Identity/grouping fields must NOT carry leading/trailing whitespace: the stored
# value must equal its trimmed form (raw == trimmed). This keeps stored ids clean so
# the query path (which may or may not trim) cannot diverge. An empty issueId/forkId
# also defeats the (issueId, forkId, decisionType) dedup key.
for f in ("issueId", "forkId", "domain", "decisionClass"):
    raw = rec.get(f)
    if not isinstance(raw, str):
        sys.stderr.write("decision-ledger: identity field '%s' must be a string\n" % f)
        sys.exit(1)
    if raw != raw.strip():
        sys.stderr.write("decision-ledger: identity field '%s' has leading/trailing whitespace (store trimmed values)\n" % f)
        sys.exit(1)
    # issueId and forkId are echoed VERBATIM into cmd_append's dangling-pointer warning on
    # stderr (a maintainer-facing terminal line), exactly like the auditsForkId/correctionOf
    # pointers below — so they get the IDENTICAL control-char reject. raw==trimmed above only
    # catches leading/trailing whitespace; an embedded newline or ESC/CSI sequence survives it
    # (mid-string), so reject any C0/C1/DEL control byte here to close the same terminal/log
    # injection sink for every field that reaches that echo. domain/decisionClass are covered
    # too for symmetry (and domain is further pinned by the enum check below).
    if _has_control_char(raw):
        sys.stderr.write("decision-ledger: identity field '%s' contains a control character (newline/ESC/etc.), strip before writing\n" % f)
        sys.exit(1)
# schemaVersion value must be numerically 1: accept 1, 1.0, 1e0; reject 1.5, 2,
# strings ("1"), bools, non-numbers. Value-based (not type-based) so python3 (which
# splits int/float) and node (one number type) reach the SAME verdict. bool is an int
# subclass in Python, so exclude it explicitly.
sv = rec["schemaVersion"]
if isinstance(sv, bool) or not isinstance(sv, (int, float)) or sv != 1:
    sys.stderr.write("decision-ledger: schemaVersion value must be 1\n")
    sys.exit(1)
# domain enum.
if rec.get("domain") not in ("code", "content"):
    sys.stderr.write("decision-ledger: domain must be 'code' or 'content'\n")
    sys.exit(1)
# decisionType enum. Widened by AU1/ADR-0020 to add 'tier-b-audit' — update this
# literal AND the identical node-runner literal below IN THE SAME COMMIT (they must
# reach the same verdict); grep the repo for any other decisionType comparison before
# assuming this is the only spot.
if rec.get("decisionType") not in ("tier-a", "tier-b", "tier-b-audit"):
    sys.stderr.write("decision-ledger: decisionType must be 'tier-a', 'tier-b', or 'tier-b-audit'\n")
    sys.exit(1)
# AU1/ADR-0020 — required class assignment: decisionClass=="unclassified" is allowed
# ONLY together with a non-empty unclassifiedReason. This is a CONDITIONAL gate, not a
# new unconditionally-required field: a real classification still leaves
# unclassifiedReason optional/absent. This is the fix for the central AU1 defect (a bare
# 'unclassified' with no reason, previously accepted because "non-empty string" alone
# was satisfied by the literal word "unclassified").
if rec.get("decisionClass") == "unclassified":
    ur = rec.get("unclassifiedReason")
    if not isinstance(ur, str) or ur.strip() == "":
        sys.stderr.write("decision-ledger: decisionClass 'unclassified' requires a non-empty unclassifiedReason\n")
        sys.exit(1)
# unclassifiedReason, if present, must be a single-line string (or null/absent).
ur_val = rec.get("unclassifiedReason")
if ur_val is not None and not isinstance(ur_val, str):
    sys.stderr.write("decision-ledger: unclassifiedReason must be a string or null\n")
    sys.exit(1)
# matchScore enum (or null/absent).
ms = rec.get("matchScore")
if ms is not None and ms not in ("exact", "partial", "miss"):
    sys.stderr.write("decision-ledger: matchScore must be 'exact', 'partial', 'miss', or null\n")
    sys.exit(1)
# challengeOutcome enum (or null/absent).
co = rec.get("challengeOutcome")
if co is not None and co not in ("agreed", "pushed-back", "unresolved"):
    sys.stderr.write("decision-ledger: challengeOutcome must be 'agreed', 'pushed-back', 'unresolved', or null\n")
    sys.exit(1)
# AU1/ADR-0020 — scoredBy: null, OR an object {family, mode, model}. mode is a
# constrained enum; "self-legacy" is a READ-ONLY default sentinel and is NEVER accepted
# here (only cross-family/same-family-fresh/self are writable) — a grader/caller
# claiming "self-legacy" on write would just be lying about independence one field over.
sb = rec.get("scoredBy")
if sb is not None:
    if not isinstance(sb, dict) or isinstance(sb, bool):
        sys.stderr.write("decision-ledger: scoredBy must be an object {family, mode, model} or null\n")
        sys.exit(1)
    sb_family = sb.get("family")
    sb_mode = sb.get("mode")
    sb_model = sb.get("model")
    if not isinstance(sb_family, str) or sb_family.strip() == "":
        sys.stderr.write("decision-ledger: scoredBy.family must be a non-empty string\n")
        sys.exit(1)
    if sb_mode not in ("cross-family", "same-family-fresh", "self"):
        sys.stderr.write("decision-ledger: scoredBy.mode must be 'cross-family', 'same-family-fresh', or 'self'\n")
        sys.exit(1)
    if not isinstance(sb_model, str) or sb_model.strip() == "":
        sys.stderr.write("decision-ledger: scoredBy.model must be a non-empty string\n")
        sys.exit(1)
# AU1/ADR-0020 — matchScore/scoredBy pairing (grader-degradation-policy + principle 7):
# a non-null matchScore REQUIRES a non-null scoredBy. The fail-closed policy is "leave
# the record unscored: matchScore null AND scoredBy null" — a graded verdict with no
# record of WHO scored it is never a legal write. (Legacy rows already on disk with a
# matchScore and no scoredBy still READ as the self-legacy sentinel and are excluded
# from independent metrics; this gate only refuses to MINT new unprovenanced grades.)
if ms is not None and sb is None:
    sys.stderr.write("decision-ledger: matchScore is set but scoredBy is null/absent — a graded verdict requires a scoredBy record (fail-closed 'unscored' means BOTH null)\n")
    sys.exit(1)
# AU1/ADR-0020 — effectiveConfidence: a JSON number in [0.0,1.0], or null/absent. When
# set it REQUIRES predictionConfidence to also be present (it is a capped view of that
# raw source) and can never exceed it (the capped value can only ever be <= the raw
# value; a bug or later code path persisting an inflated or source-less effectiveConfidence
# would quietly reopen uncalibrated-confidence under the new field).
ec = rec.get("effectiveConfidence")
if ec is not None:
    if isinstance(ec, bool) or not isinstance(ec, (int, float)):
        sys.stderr.write("decision-ledger: effectiveConfidence must be a JSON number or null (no string coercion)\n")
        sys.exit(1)
    if not (0.0 <= float(ec) <= 1.0):
        sys.stderr.write("decision-ledger: effectiveConfidence out of range [0.0,1.0]: %r\n" % ec)
        sys.exit(1)
    # The capped value must never exceed the cap itself (else it was never actually
    # capped — the whole point of the field). Enforced here, not merely asked of the skill.
    if float(ec) > CONFIDENCE_CAP:
        sys.stderr.write("decision-ledger: effectiveConfidence (%r) exceeds CONFIDENCE_CAP (%s) — the capped value must not exceed the cap\n" % (ec, CONFIDENCE_CAP))
        sys.exit(1)
    # effectiveConfidence is a CAPPED VIEW of predictionConfidence — it cannot exist
    # without its raw source. Ruling capped-confidence-storage + SKILL.md step 9a
    # ("If predictionConfidence is null, effectiveConfidence is also null") require the
    # pairing to hold, so reject an effectiveConfidence set when predictionConfidence is
    # null/absent/non-numeric (principle 7: enforce, don't merely ask).
    pc_for_ec = rec.get("predictionConfidence")
    pcf = float(pc_for_ec) if (pc_for_ec is not None and not isinstance(pc_for_ec, bool) and isinstance(pc_for_ec, (int, float))) else None
    if pcf is None or pcf != pcf or pcf in (float("inf"), float("-inf")):
        sys.stderr.write("decision-ledger: effectiveConfidence is set but predictionConfidence is null/absent — a capped value requires its raw source\n")
        sys.exit(1)
    if float(ec) > pcf:
        sys.stderr.write("decision-ledger: effectiveConfidence (%r) must not exceed predictionConfidence (%r)\n" % (ec, pc_for_ec))
        sys.exit(1)
# AU1/ADR-0020 — auditOutcome: the tier-b-audit record's OWN verdict field (or
# null/absent on every non-audit record).
ao = rec.get("auditOutcome")
if ao is not None and ao not in ("as-ruled", "diverged", "unclear"):
    sys.stderr.write("decision-ledger: auditOutcome must be 'as-ruled', 'diverged', 'unclear', or null\n")
    sys.exit(1)
# AU1/ADR-0020 — a tier-b-audit record MUST carry its OWN verdict (auditOutcome) and its
# linkage (auditsForkId). An audit row with neither is an ambiguous/empty audit trail —
# "audited, found nothing" is indistinguishable from "audit never completed". "unclear"
# is the enum value for a genuinely inconclusive audit, so a real audit always has one.
if rec.get("decisionType") == "tier-b-audit":
    if ao not in ("as-ruled", "diverged", "unclear"):
        sys.stderr.write("decision-ledger: decisionType 'tier-b-audit' requires auditOutcome ('as-ruled', 'diverged', or 'unclear')\n")
        sys.exit(1)
    afid = rec.get("auditsForkId")
    if not isinstance(afid, str) or afid.strip() == "":
        sys.stderr.write("decision-ledger: decisionType 'tier-b-audit' requires a non-empty auditsForkId linkage\n")
        sys.exit(1)
# AU1/ADR-0020 — auditsForkId / correctionOf / probeSource: soft linkage pointers, type
# + shape checked only (no referential-integrity lookup against the rest of the ledger —
# that would require reading the whole file at append time; cmd_append does a
# best-effort non-fatal warning instead, see below).
for f in ("auditsForkId", "correctionOf"):
    v = rec.get(f)
    if v is not None:
        if not isinstance(v, str) or v.strip() == "":
            sys.stderr.write("decision-ledger: %s must be a non-empty string or null\n" % f)
            sys.exit(1)
        # Same raw==trimmed discipline as identity fields: a pointer stored with
        # leading/trailing whitespace could be silently normalized into a false match by
        # the cmd_append dangling-pointer warning's $(...) capture. Store trimmed values.
        if v != v.strip():
            sys.stderr.write("decision-ledger: %s has leading/trailing whitespace (store trimmed values)\n" % f)
            sys.exit(1)
        # These pointers are echoed VERBATIM into the cmd_append dangling-pointer warning
        # on stderr (a maintainer-facing terminal line). Reject embedded newlines (which
        # strip() leaves intact when mid-string, so raw==trimmed still passes) and C0/C1
        # control bytes so a value carrying \n/\r or an ESC-sequence cannot inject extra
        # log lines or terminal escapes into that output.
        if _has_control_char(v):
            sys.stderr.write("decision-ledger: %s contains a control character (newline/ESC/etc.), strip before writing\n" % f)
            sys.exit(1)
ps = rec.get("probeSource")
if ps is not None and ps not in ("S1", "S2"):
    sys.stderr.write("decision-ledger: probeSource must be 'S1', 'S2', or null\n")
    sys.exit(1)
# predictionConfidence: a JSON number in [0.0, 1.0], or null/absent. No string
# coercion on either runner (reject "0.5"); bool is rejected as a non-number.
pc = rec.get("predictionConfidence")
if pc is not None:
    if isinstance(pc, bool) or not isinstance(pc, (int, float)):
        sys.stderr.write("decision-ledger: predictionConfidence must be a JSON number or null (no string coercion)\n")
        sys.exit(1)
    if not (0.0 <= float(pc) <= 1.0):
        sys.stderr.write("decision-ledger: predictionConfidence out of range [0.0,1.0]: %r\n" % pc)
        sys.exit(1)
# R3 (issue #63, D8-opt1, ruling read-print-side-scope) — strict-on-write
# control-character/ESC scrub. Widens the OLD "reject embedded CR/LF only"
# check to the FULL control-byte range (the same _has_control_char predicate
# the identity/pointer fields above already use) across EVERY maintainer-
# facing free-text field, and ADDS issueTitle/project/each options[] element —
# the three fields F15 named as never checked at all. This is WRITE-ONLY: a
# pre-existing dirty row already on disk still round-trips through query/list
# unmodified (no read-side sanitization added here; issue #185 tracks the
# read-side terminal-escape-injection gap and the fate of already-saved dirty
# rows separately, not this build).
for field in ("rationale", "predictedRuling", "actualRuling", "recommendation",
              "challengeReason", "unclassifiedReason", "issueTitle", "project"):
    v = rec.get(field)
    if v is None:
        continue
    if not isinstance(v, str):
        # Non-string values on these optional fields are a separate,
        # pre-existing shape concern outside this scrub's scope; skip rather
        # than raise here so this check stays narrowly about control chars.
        continue
    if _has_control_char(v):
        sys.stderr.write("decision-ledger: field '%s' contains a control character (newline/ESC/etc.), strip before writing\n" % field)
        sys.exit(1)
# options: an array of strings (schema header). Explicit array-type check
# BEFORE iterating — Python silently iterates a plain string's CHARACTERS if
# this isinstance(list) guard is skipped, so a non-array options (e.g. a bare
# string) must be rejected outright rather than mis-scanned character-by-character.
opts = rec.get("options")
if opts is not None:
    if not isinstance(opts, list):
        sys.stderr.write("decision-ledger: options must be an array of strings\n")
        sys.exit(1)
    for i, o in enumerate(opts):
        if isinstance(o, str) and _has_control_char(o):
            sys.stderr.write("decision-ledger: options[%d] contains a control character (newline/ESC/etc.), strip before writing\n" % i)
            sys.exit(1)
sys.exit(0)
PY
  else
    CONFIDENCE_CAP="$CONFIDENCE_CAP" node - "$rec_file" <<'NODE'
const fs = require("fs");
// AU1/ADR-0020 — the ONE cap constant, injected from the shell CONFIDENCE_CAP so the
// validator actually CONSUMES it (no second copy-pasted literal). effectiveConfidence
// is rejected above this value (principle 7: enforce the cap, don't just ask the skill).
const CONFIDENCE_CAP = parseFloat(process.env.CONFIDENCE_CAP || "0.75");
// R3 (issue #63, D8-opt1) — ONE shared control-character predicate, reused by
// the identity-field check, the dangling-pointer check, and the free-text
// scrub below so the three can never diverge into hand-written copies of the
// same byte range: C0 controls (0x00-0x1f), DEL (0x7f), C1 controls (0x80-0x9f).
const _hasControlChar = (s) => /[\x00-\x1f\x7f-\x9f]/.test(s);
let rec;
try { rec = JSON.parse(fs.readFileSync(process.argv[2], "utf8")); } catch (e) {
  process.stderr.write("decision-ledger: record is not valid JSON: " + e.message + "\n");
  process.exit(1);
}
const req = ["schemaVersion","issueId","forkId","domain","decisionType","decisionClass","timestamp"];
for (const f of req) {
  if (!(f in rec) || rec[f] === null || rec[f] === undefined) {
    process.stderr.write("decision-ledger: record missing required field '" + f + "'\n");
    process.exit(1);
  }
}
// Required fields must be present AND non-empty (after trimming).
for (const f of req) {
  if (String(rec[f]).trim() === "") {
    process.stderr.write("decision-ledger: required field '" + f + "' must be non-empty\n");
    process.exit(1);
  }
}
// Identity/grouping fields must NOT carry leading/trailing whitespace: raw === trimmed.
// Keeps stored ids clean so the query path cannot diverge; an empty issueId/forkId
// also defeats the (issueId, forkId, decisionType) dedup key.
for (const f of ["issueId","forkId","domain","decisionClass"]) {
  const raw = rec[f];
  if (typeof raw !== "string") {
    process.stderr.write("decision-ledger: identity field '" + f + "' must be a string\n"); process.exit(1);
  }
  if (raw !== raw.trim()) {
    process.stderr.write("decision-ledger: identity field '" + f + "' has leading/trailing whitespace (store trimmed values)\n"); process.exit(1);
  }
  // issueId and forkId are echoed VERBATIM into cmd_append's dangling-pointer warning on
  // stderr (a maintainer-facing terminal line), exactly like the auditsForkId/correctionOf
  // pointers below — so they get the IDENTICAL control-char reject. raw===trimmed above only
  // catches leading/trailing whitespace; an embedded newline or ESC/CSI sequence survives it
  // (mid-string), so reject any C0/C1/DEL control byte here to close the same terminal/log
  // injection sink for every field that reaches that echo. domain/decisionClass are covered
  // too for symmetry (and domain is further pinned by the enum check below).
  if (_hasControlChar(raw)) {
    process.stderr.write("decision-ledger: identity field '" + f + "' contains a control character (newline/ESC/etc.), strip before writing\n"); process.exit(1);
  }
}
// schemaVersion value must be numerically 1: accept 1, 1.0, 1e0; reject 1.5, 2,
// strings, non-numbers. Value-based (sv === 1), matching the python3 path, so the two
// runners agree despite different number models.
const sv = rec.schemaVersion;
if (typeof sv !== "number" || sv !== 1) {
  process.stderr.write("decision-ledger: schemaVersion value must be 1\n"); process.exit(1);
}
// domain enum.
if (rec.domain !== "code" && rec.domain !== "content") {
  process.stderr.write("decision-ledger: domain must be 'code' or 'content'\n"); process.exit(1);
}
// decisionType enum. Widened by AU1/ADR-0020 to add 'tier-b-audit' — update this
// literal AND the identical python3-runner literal above IN THE SAME COMMIT (they must
// reach the same verdict); grep the repo for any other decisionType comparison before
// assuming this is the only spot.
if (!["tier-a","tier-b","tier-b-audit"].includes(rec.decisionType)) {
  process.stderr.write("decision-ledger: decisionType must be 'tier-a', 'tier-b', or 'tier-b-audit'\n"); process.exit(1);
}
// AU1/ADR-0020 — required class assignment: decisionClass=="unclassified" is allowed
// ONLY together with a non-empty unclassifiedReason. This is a CONDITIONAL gate, not a
// new unconditionally-required field: a real classification still leaves
// unclassifiedReason optional/absent. This is the fix for the central AU1 defect (a bare
// 'unclassified' with no reason, previously accepted because "non-empty string" alone
// was satisfied by the literal word "unclassified").
if (rec.decisionClass === "unclassified") {
  const ur = rec.unclassifiedReason;
  if (typeof ur !== "string" || ur.trim() === "") {
    process.stderr.write("decision-ledger: decisionClass 'unclassified' requires a non-empty unclassifiedReason\n"); process.exit(1);
  }
}
// unclassifiedReason, if present, must be a string (or null/absent).
if (rec.unclassifiedReason !== null && rec.unclassifiedReason !== undefined && typeof rec.unclassifiedReason !== "string") {
  process.stderr.write("decision-ledger: unclassifiedReason must be a string or null\n"); process.exit(1);
}
// matchScore enum (or null/absent).
const ms = rec.matchScore;
if (ms !== null && ms !== undefined && !["exact","partial","miss"].includes(ms)) {
  process.stderr.write("decision-ledger: matchScore must be 'exact', 'partial', 'miss', or null\n");
  process.exit(1);
}
// challengeOutcome enum (or null/absent).
const co = rec.challengeOutcome;
if (co !== null && co !== undefined && !["agreed","pushed-back","unresolved"].includes(co)) {
  process.stderr.write("decision-ledger: challengeOutcome must be 'agreed', 'pushed-back', 'unresolved', or null\n");
  process.exit(1);
}
// AU1/ADR-0020 — scoredBy: null, OR an object {family, mode, model}. mode is a
// constrained enum; "self-legacy" is a READ-ONLY default sentinel and is NEVER accepted
// here (only cross-family/same-family-fresh/self are writable) — a grader/caller
// claiming "self-legacy" on write would just be lying about independence one field over.
const sb = rec.scoredBy;
if (sb !== null && sb !== undefined) {
  if (typeof sb !== "object" || Array.isArray(sb)) {
    process.stderr.write("decision-ledger: scoredBy must be an object {family, mode, model} or null\n"); process.exit(1);
  }
  if (typeof sb.family !== "string" || sb.family.trim() === "") {
    process.stderr.write("decision-ledger: scoredBy.family must be a non-empty string\n"); process.exit(1);
  }
  if (!["cross-family","same-family-fresh","self"].includes(sb.mode)) {
    process.stderr.write("decision-ledger: scoredBy.mode must be 'cross-family', 'same-family-fresh', or 'self'\n"); process.exit(1);
  }
  if (typeof sb.model !== "string" || sb.model.trim() === "") {
    process.stderr.write("decision-ledger: scoredBy.model must be a non-empty string\n"); process.exit(1);
  }
}
// AU1/ADR-0020 — matchScore/scoredBy pairing (grader-degradation-policy + principle 7):
// a non-null matchScore requires a non-null scoredBy; the fail-closed 'unscored' shape is
// BOTH null. A graded verdict with no record of WHO scored it is never a legal write.
// (Legacy on-disk rows with a matchScore and no scoredBy still READ as the self-legacy
// sentinel and are excluded from independent metrics; this gate only refuses new mints.)
if (ms !== null && ms !== undefined && (sb === null || sb === undefined)) {
  process.stderr.write("decision-ledger: matchScore is set but scoredBy is null/absent — a graded verdict requires a scoredBy record (fail-closed 'unscored' means BOTH null)\n"); process.exit(1);
}
// AU1/ADR-0020 — effectiveConfidence: a JSON number in [0.0,1.0], or null/absent. When
// set it REQUIRES predictionConfidence to also be present (it is a capped view of that
// raw source) and can never exceed it (the capped value can only ever be <= the raw
// value; a bug or later code path persisting an inflated or source-less effectiveConfidence
// would quietly reopen uncalibrated-confidence under the new field).
const ec = rec.effectiveConfidence;
if (ec !== null && ec !== undefined) {
  if (typeof ec !== "number" || !isFinite(ec)) {
    process.stderr.write("decision-ledger: effectiveConfidence must be a JSON number or null (no string coercion)\n"); process.exit(1);
  }
  if (ec < 0 || ec > 1) {
    process.stderr.write("decision-ledger: effectiveConfidence out of range [0.0,1.0]: " + ec + "\n"); process.exit(1);
  }
  // The capped value must never exceed the cap itself (else it was never actually capped).
  if (ec > CONFIDENCE_CAP) {
    process.stderr.write("decision-ledger: effectiveConfidence (" + ec + ") exceeds CONFIDENCE_CAP (" + CONFIDENCE_CAP + ") — the capped value must not exceed the cap\n"); process.exit(1);
  }
  // effectiveConfidence is a CAPPED VIEW of predictionConfidence — it cannot exist
  // without its raw source. Ruling capped-confidence-storage + SKILL.md step 9a
  // ("If predictionConfidence is null, effectiveConfidence is also null") require the
  // pairing to hold, so reject an effectiveConfidence set when predictionConfidence is
  // null/absent/non-numeric (principle 7: enforce, don't merely ask).
  const pcForEc = rec.predictionConfidence;
  if (pcForEc === null || pcForEc === undefined || typeof pcForEc !== "number" || !isFinite(pcForEc)) {
    process.stderr.write("decision-ledger: effectiveConfidence is set but predictionConfidence is null/absent — a capped value requires its raw source\n"); process.exit(1);
  }
  if (ec > pcForEc) {
    process.stderr.write("decision-ledger: effectiveConfidence (" + ec + ") must not exceed predictionConfidence (" + pcForEc + ")\n"); process.exit(1);
  }
}
// AU1/ADR-0020 — auditOutcome: the tier-b-audit record's OWN verdict field (or
// null/absent on every non-audit record).
const ao = rec.auditOutcome;
if (ao !== null && ao !== undefined && !["as-ruled","diverged","unclear"].includes(ao)) {
  process.stderr.write("decision-ledger: auditOutcome must be 'as-ruled', 'diverged', 'unclear', or null\n"); process.exit(1);
}
// AU1/ADR-0020 — a tier-b-audit record MUST carry its OWN verdict (auditOutcome) and its
// linkage (auditsForkId). An audit row with neither is an ambiguous/empty audit trail —
// "audited, found nothing" is indistinguishable from "audit never completed". "unclear"
// is the enum value for a genuinely inconclusive audit, so a real audit always has one.
if (rec.decisionType === "tier-b-audit") {
  if (!["as-ruled","diverged","unclear"].includes(ao)) {
    process.stderr.write("decision-ledger: decisionType 'tier-b-audit' requires auditOutcome ('as-ruled', 'diverged', or 'unclear')\n"); process.exit(1);
  }
  const afid = rec.auditsForkId;
  if (typeof afid !== "string" || afid.trim() === "") {
    process.stderr.write("decision-ledger: decisionType 'tier-b-audit' requires a non-empty auditsForkId linkage\n"); process.exit(1);
  }
}
// AU1/ADR-0020 — auditsForkId / correctionOf / probeSource: soft linkage pointers,
// type + shape checked only (no referential-integrity lookup against the rest of the
// ledger — that would require reading the whole file at append time; cmd_append does a
// best-effort non-fatal warning instead, see below).
for (const f of ["auditsForkId","correctionOf"]) {
  const v = rec[f];
  if (v !== null && v !== undefined) {
    if (typeof v !== "string" || v.trim() === "") {
      process.stderr.write("decision-ledger: " + f + " must be a non-empty string or null\n"); process.exit(1);
    }
    // Same raw==trimmed discipline as identity fields: a pointer stored with
    // leading/trailing whitespace could be silently normalized into a false match by the
    // cmd_append dangling-pointer warning's $(...) capture. Store trimmed values.
    if (v !== v.trim()) {
      process.stderr.write("decision-ledger: " + f + " has leading/trailing whitespace (store trimmed values)\n"); process.exit(1);
    }
    // These pointers are echoed VERBATIM into the cmd_append dangling-pointer warning on
    // stderr (a maintainer-facing terminal line). Reject embedded newlines (which trim()
    // leaves intact when mid-string, so raw==trimmed still passes) and C0/C1 control
    // bytes so a value carrying \n/\r or an ESC-sequence cannot inject extra log lines or
    // terminal escapes into that output.
    if (_hasControlChar(v)) {
      process.stderr.write("decision-ledger: " + f + " contains a control character (newline/ESC/etc.), strip before writing\n"); process.exit(1);
    }
  }
}
const ps = rec.probeSource;
if (ps !== null && ps !== undefined && !["S1","S2"].includes(ps)) {
  process.stderr.write("decision-ledger: probeSource must be 'S1', 'S2', or null\n"); process.exit(1);
}
// predictionConfidence: a JSON number in [0.0,1.0], or null/absent. No string
// coercion (reject "0.5"); reject booleans and non-finite.
const pc = rec.predictionConfidence;
if (pc !== null && pc !== undefined) {
  if (typeof pc !== "number" || !isFinite(pc)) {
    process.stderr.write("decision-ledger: predictionConfidence must be a JSON number or null (no string coercion)\n");
    process.exit(1);
  }
  if (pc < 0 || pc > 1) {
    process.stderr.write("decision-ledger: predictionConfidence out of range [0.0,1.0]: " + pc + "\n");
    process.exit(1);
  }
}
// R3 (issue #63, D8-opt1, ruling read-print-side-scope) — strict-on-write
// control-character/ESC scrub. Widens the OLD "reject embedded CR/LF only"
// check to the FULL control-byte range (the same _hasControlChar predicate
// the identity/pointer fields above already use) across EVERY maintainer-
// facing free-text field, and ADDS issueTitle/project/each options[] element —
// the three fields F15 named as never checked at all. This is WRITE-ONLY: a
// pre-existing dirty row already on disk still round-trips through query/list
// unmodified (no read-side sanitization added here; issue #185 tracks the
// read-side terminal-escape-injection gap and the fate of already-saved dirty
// rows separately, not this build).
for (const f of ["rationale","predictedRuling","actualRuling","recommendation","challengeReason","unclassifiedReason","issueTitle","project"]) {
  const v = rec[f];
  if (v === null || v === undefined) continue;
  if (typeof v !== "string") continue; // non-string shape: out of this scrub's scope
  if (_hasControlChar(v)) {
    process.stderr.write("decision-ledger: field '" + f + "' contains a control character (newline/ESC/etc.), strip before writing\n"); process.exit(1);
  }
}
// options: an array of strings (schema header). Explicit array-type check
// BEFORE iterating each element, so a non-array options value is rejected
// outright rather than silently mis-handled.
const optsN = rec.options;
if (optsN !== null && optsN !== undefined) {
  if (!Array.isArray(optsN)) {
    process.stderr.write("decision-ledger: options must be an array of strings\n"); process.exit(1);
  }
  for (let i = 0; i < optsN.length; i++) {
    const o = optsN[i];
    if (typeof o === "string" && _hasControlChar(o)) {
      process.stderr.write("decision-ledger: options[" + i + "] contains a control character (newline/ESC/etc.), strip before writing\n"); process.exit(1);
    }
  }
}
process.exit(0);
NODE
  fi
}

# ---------------------------------------------------------------------------
# Subcommand: query — retrieve decisions by issueId or forkId (exact match)
# Usage: decision-ledger.sh query --issue <N>
#        decision-ledger.sh query --fork <forkId>
# ---------------------------------------------------------------------------
cmd_query() {
  local filter_issue="" filter_fork=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --issue) filter_issue="$2"; shift 2 ;;
      --fork)  filter_fork="$2";  shift 2 ;;
      *) echo "decision-ledger query: unknown option '$1'" >&2; exit 2 ;;
    esac
  done

  if [ -z "$filter_issue" ] && [ -z "$filter_fork" ]; then
    echo "decision-ledger query: requires --issue <N> or --fork <forkId>" >&2
    exit 2
  fi

  if [ ! -f "$LEDGER" ]; then
    echo "decision-ledger: no ledger found at $LEDGER" >&2
    exit 1
  fi

  # Read the whole ledger FIRST and check its exit status (see cmd_list for the
  # full rationale): the old `done < <(ledger_read_all)` process substitution
  # discarded ledger_read_all's `return 1` on an unreadable/undecodable ledger,
  # so an unreadable-but-populated ledger read as "no records found" — a silent
  # false-empty. Capture then propagate; the capture is its own statement (a
  # `local x=$(...)` would mask the exit code). A distinct 'cannot read' message
  # (not the "no records found for the given filter" text) keeps "unreadable"
  # from masquerading as "genuinely absent".
  local _all_records _read_rc=0
  _all_records="$(ledger_read_all)" || _read_rc=$?
  if [ "$_read_rc" -ne 0 ]; then
    echo "decision-ledger: cannot read ledger at $LEDGER — records may exist but could not be read (see stderr)" >&2
    exit 1
  fi

  local found=0
  # Iterate normalized records (ledger_read_all works on jq/python3/node alike).
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # Exact field match — NOT a grep on raw text. Runner-agnostic.
    local matches=true

    if [ -n "$filter_issue" ]; then
      local got_issue; got_issue="$(printf '%s' "$line" | json_field_equals issueId "$filter_issue")"
      [ "$got_issue" != "yes" ] && matches=false
    fi

    if [ -n "$filter_fork" ]; then
      local got_fork; got_fork="$(printf '%s' "$line" | json_field_equals forkId "$filter_fork")"
      [ "$got_fork" != "yes" ] && matches=false
    fi

    if [ "$matches" = "true" ]; then
      printf '%s' "$line" | json_pretty
      found=$((found + 1))
    fi
  done <<< "$_all_records"

  if [ "$found" -eq 0 ]; then
    echo "decision-ledger: no records found for the given filter" >&2
    exit 1
  fi
  echo "---"
  echo "decision-ledger: $found record(s) found"
}

# ---------------------------------------------------------------------------
# Subcommand: match-rate — rolling match-rate grouped by domain + decisionClass
# Usage: decision-ledger.sh match-rate [--domain code|content] [--class <spine>]
#
# Only records with actualRuling AND predictedRuling AND matchScore are scored.
# Reports: count of ran/not-ran challenger states, exact/partial/miss per group.
# ---------------------------------------------------------------------------
cmd_match_rate() {
  local filter_domain="" filter_class=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --domain) filter_domain="$2"; shift 2 ;;
      --class)  filter_class="$2";  shift 2 ;;
      *) echo "decision-ledger match-rate: unknown option '$1'" >&2; exit 2 ;;
    esac
  done

  if [ ! -f "$LEDGER" ]; then
    echo "decision-ledger: no ledger found at $LEDGER" >&2
    echo "match-rate: 0 scorable records"
    exit 0
  fi

  # Prefer python3, then node, then jq for match-rate aggregation. All three paths
  # produce the SAME report (Total-records, per-group exact/partial/miss, Challenger
  # availability, Overall summary) and all skip a malformed middle line individually
  # rather than aborting the whole stream (the ledger is append-only and a crash can
  # leave a partial last line — one bad line must not zero out the report).
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$LEDGER" "$filter_domain" "$filter_class" <<'PY'
import json, math, sys
from collections import defaultdict

path, fdom, fcls = sys.argv[1], sys.argv[2], sys.argv[3]

records = []
try:
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line: continue
        try:
            r = json.loads(line)
        except:
            sys.stderr.write("decision-ledger: skipping malformed line %d\n" % i)
            continue
        r.setdefault("matchScore", None)
        r.setdefault("domain", "code")
        r.setdefault("decisionClass", "unclassified")
        r.setdefault("challengeRan", False)
        # AU1/ADR-0020 forward defaults — this loop re-implements its own defaulting
        # rather than calling ledger_read_all, so these MUST be kept in lockstep with
        # that helper's python3/node blocks and the jq block in ledger_read_all (a field
        # defaulted there but not here reads inconsistently between the two entry
        # points for the same field).
        if "scoredBy" not in r or r.get("scoredBy") is None:
            r["scoredBy"] = {"mode": "self-legacy", "family": None, "model": None}
        r.setdefault("effectiveConfidence", None)
        r.setdefault("auditOutcome", None)
        r.setdefault("auditsForkId", None)
        r.setdefault("predictionConfidence", None)
        r.setdefault("correctionOf", None)
        r.setdefault("probeSource", None)
        r.setdefault("unclassifiedReason", None)
        records.append(r)
except FileNotFoundError:
    pass

# Dedup on (issueId, forkId, decisionType) — last-row-wins WITHIN EACH RECORD KIND
# (AU1/ADR-0020 widened this from the original 2-field key: a tier-b-audit record
# deliberately shares issueId+forkId with the Tier-B decision it audits, via
# auditsForkId, so the 2-field key would silently collapse a ruling row and its later
# audit row into one — decisionType is what keeps them distinct. A retroactive
# correction record uses the SAME 3-field tuple as the record it corrects, so
# later-row-wins is exactly the intended "correction supersedes original" semantics.)
# Key is a 3-tuple (collision-proof: three fields never flatten into one ambiguous
# string), matching the NUL-joined keys the jq/node paths use.
seen = {}
for r in records:
    key = (str(r.get("issueId","")), str(r.get("forkId","")), str(r.get("decisionType","")))
    seen[key] = r
records = list(seen.values())

# Apply filters.
if fdom:
    records = [r for r in records if r.get("domain") == fdom]
if fcls:
    records = [r for r in records if r.get("decisionClass") == fcls]

# Only Tier-A records with a matchScore are scorable.
scorable = [r for r in records if r.get("decisionType") == "tier-a" and r.get("matchScore") in ("exact","partial","miss")]

if not records:
    print("match-rate: no records (empty or filtered ledger)")
    sys.exit(0)

print("match-rate report")
print("=================")
print("Total records: %d (after dedup)" % len(records))

# Check for required field presence — flag missing rather than silently skip.
for r in records:
    if not r.get("decisionClass"):
        sys.stderr.write("decision-ledger: record forkId=%r is missing decisionClass — counted as 'unclassified'\n" % r.get("forkId"))
    if not r.get("domain"):
        sys.stderr.write("decision-ledger: record forkId=%r is missing domain — counted as 'code' (default)\n" % r.get("forkId"))

# Group scorable by (domain, decisionClass). This section is UNCHANGED by AU1 — it
# stays computed over ALL scored rows (self-graded/self-legacy included), backward
# compatible. The independent-only calibration section below is a separate block.
groups = defaultdict(lambda: {"exact": 0, "partial": 0, "miss": 0, "total": 0})
for r in scorable:
    key = (r.get("domain","code"), r.get("decisionClass","unclassified"))
    groups[key][r["matchScore"]] += 1
    groups[key]["total"] += 1

print("\nMatch-rate by domain + decision-class (Tier-A, scored only):")
if not groups:
    print("  No scored Tier-A records yet.")
else:
    for (dom, cls), counts in sorted(groups.items()):
        total = counts["total"]
        exact = counts["exact"]
        pct = int(100 * exact / total) if total else 0
        print("  [%s / %s] exact=%d partial=%d miss=%d total=%d (exact-rate=%d%%)" % (
            dom, cls, exact, counts["partial"], counts["miss"], total, pct))

# Challenger availability report.
tier_a_all = [r for r in records if r.get("decisionType") == "tier-a"]
ch_ran = sum(1 for r in tier_a_all if r.get("challengeRan") is True)
ch_notran = sum(1 for r in tier_a_all if r.get("challengeRan") is False)
ch_unclear = len(tier_a_all) - ch_ran - ch_notran
print("\nChallenger availability (Tier-A records):")
print("  ran: %d   not-ran: %d   unclear: %d" % (ch_ran, ch_notran, ch_unclear))

# Overall summary.
if scorable:
    total_sc = len(scorable)
    total_exact = sum(1 for r in scorable if r.get("matchScore") == "exact")
    total_partial = sum(1 for r in scorable if r.get("matchScore") == "partial")
    total_miss = sum(1 for r in scorable if r.get("matchScore") == "miss")
    pct = int(100 * total_exact / total_sc) if total_sc else 0
    print("\nOverall: exact=%d partial=%d miss=%d / %d scored (exact-rate=%d%%)" % (
        total_exact, total_partial, total_miss, total_sc, pct))
else:
    print("\nOverall: no scored records yet.")

# ---------------------------------------------------------------------------
# AU1/ADR-0020 — calibration report (independent-only, raw confidence).
# Maintainer ruling calibration-methodology: fixed 4-bucket scheme + simple
# mean-absolute-deviation (MAD), computed over INDEPENDENT-only records (scoredBy.mode
# is "cross-family" or "same-family-fresh" — excludes "self" AND the read-default
# sentinel "self-legacy") at RAW (uncapped) predictionConfidence. This is a SEPARATE
# block from the match-rate section above (which stays computed over ALL scored rows,
# unchanged) — the two intentionally report different populations, each clearly
# labeled, so a reader is never confused about which rows fed which number.
#
# ONE explicit mapping (picked per the maintainer's "simple" ruling): correctness is
# exact=1.0, everything else (partial/miss)=0.0 — no partial credit.
# ONE explicit bucket rule: bucket = floor(confidence*4) clamped to [0,3], i.e. the 4
# buckets are [0,.25) [.25,.5) [.5,.75) [.75,1.0]. Per-bucket deviation is
# abs(mean(confidence) - mean(correctness)) among that bucket's records; the reported
# calibration error is the UNWEIGHTED mean of the per-bucket deviations across buckets
# that have at least one record (an empty bucket contributes nothing — "simple", not a
# weighted ECE). This exact recipe must be reproduced identically in jq and node.
independent_scorable = [
    r for r in scorable
    if isinstance(r.get("scoredBy"), dict)
    and r["scoredBy"].get("mode") in ("cross-family", "same-family-fresh")
    and isinstance(r.get("predictionConfidence"), (int, float))
    and not isinstance(r.get("predictionConfidence"), bool)
]
if not independent_scorable:
    print("\nCalibration error (MAD, raw confidence, independent-only): n/a (no independent-scored records with predictionConfidence)")
else:
    buckets = defaultdict(list)
    for r in independent_scorable:
        conf = float(r["predictionConfidence"])
        correct = 1.0 if r.get("matchScore") == "exact" else 0.0
        b = min(3, max(0, int(conf * 4)))
        buckets[b].append((conf, correct))
    deviations = []
    for b, items in sorted(buckets.items()):
        avg_conf = sum(c for c, _ in items) / len(items)
        avg_correct = sum(k for _, k in items) / len(items)
        deviations.append(abs(avg_conf - avg_correct))
    mad = sum(deviations) / len(deviations) if deviations else 0.0
    # AU1/ADR-0020 (issue #78) — format MAD via integer cents, round-half-up, IDENTICAL
    # to the jq ($mad*100|round) and node (Math.round(mad*100)) paths so all three runners
    # emit the SAME string for the same ledger. The old "%.2f" used round-half-to-even
    # (banker's) and disagreed by a cent with jq/node on a half-boundary (e.g. MAD 0.125
    # printed "0.12" here but "0.13" there). mad is a mean of absolute deviations, always
    # >= 0. Compare the fractional part to 0.5 via floor (NOT int(y + 0.5)): adding 0.5 to
    # a double just below a half-integer, e.g. y=0.49999999999999994, rounds UP to 1.0
    # under IEEE-754 before truncation, so int(y + 0.5) would yield 1 where Node's
    # Math.round(y) and jq's (y|round) both yield 0 — a cross-runner divergence. floor never
    # performs that lossy addition, so it matches Math.round/round for every non-negative y.
    y = mad * 100
    floor_y = math.floor(y)
    cents = floor_y + (1 if (y - floor_y) >= 0.5 else 0)
    print("\nCalibration error (MAD, raw confidence, independent-only): %d.%02d" % (cents // 100, cents % 100))
    print("  n=%d across %d bucket(s)" % (len(independent_scorable), len(buckets)))
PY
  elif [ "$JSON_RUNNER" = "jq" ]; then
    # jq path, brought to full parity with python3/node. Source records from
    # ledger_read_all (which reads each line defensively and SKIPS a malformed line
    # individually, so one bad middle line no longer zeroes out the whole report -
    # the previous [inputs] slurp aborted the stream on the first parse error).
    # Then slurp the normalized stream with -s and emit the same report shape:
    # Total-records, per-group lines, Challenger availability, and Overall summary.
    ledger_read_all | jq -s -r --arg fd "$filter_domain" --arg fc "$filter_class" '
      # AU1/ADR-0020 — dedup key widened to (issueId, forkId, decisionType), NUL-joined
      # so no combination of the three fields can flatten into one ambiguous string. A
      # tier-b-audit record deliberately shares issueId+forkId with the Tier-B decision
      # it audits (via auditsForkId); decisionType is what keeps the two rows distinct
      # under dedup — a 2-field key would silently collapse a ruling row and its later
      # audit row into one. A retroactive correction record uses the SAME 3-field tuple
      # as the record it corrects, so last-row-wins is exactly "correction supersedes".
      (reduce .[] as $r ({}; .[(($r.issueId|tostring) + "\u0000" + ($r.forkId|tostring) + "\u0000" + ($r.decisionType|tostring))] = $r) | [.[]]) as $allDeduped
      # Apply the domain/class filter to the FULL deduped set BEFORE deriving Total,
      # Challenger-availability, and scorable — so every reported line covers the same
      # filtered population (matches the python3 path; jq/node previously filtered only
      # $scorable, so Total + Challenger-availability ignored the filter).
      | ($allDeduped
         | map(select($fd == "" or .domain == $fd)
               | select($fc == "" or .decisionClass == $fc))) as $deduped
      | ($deduped
         | map(select(.decisionType == "tier-a")
               | select((.matchScore // null) != null))) as $scorable
      | ($deduped | map(select(.decisionType == "tier-a"))) as $tierA
      # AU1/ADR-0020 — independent-only subset of $scorable for the calibration report
      # below: scoredBy.mode is "cross-family" or "same-family-fresh" (excludes "self"
      # and the read-default sentinel "self-legacy"), AND predictionConfidence is a
      # present JSON number (raw, uncapped).
      | ($scorable
         | map(select(((.scoredBy.mode // "self-legacy") == "cross-family") or ((.scoredBy.mode // "self-legacy") == "same-family-fresh"))
               | select((.predictionConfidence // null) != null and (.predictionConfidence | type) == "number"))) as $indep
      # Fixed 4-bucket scheme: bucket = floor(confidence*4) clamped to [0,3]. Simple MAD:
      # correctness is exact=1.0/else=0.0; per-bucket deviation is
      # abs(mean(confidence) - mean(correctness)); the reported error is the UNWEIGHTED
      # mean of per-bucket deviations across buckets with >=1 record.
      | ($indep
         | map(. + {_bucket: ([3, (.predictionConfidence * 4 | floor)] | min | [0, .] | max),
                    _correct: (if .matchScore == "exact" then 1.0 else 0.0 end)})
         | group_by(._bucket)
         | map({avgConf: (map(.predictionConfidence) | add / length),
                 avgCorrect: (map(._correct) | add / length)})
         | map((.avgConf - .avgCorrect) | if . < 0 then -. else . end)) as $deviations
      | "match-rate report",
        "=================",
        "Total records: \($deduped | length) (after dedup)",
        "",
        "Match-rate by domain + decision-class (Tier-A, scored only):",
        ( if ($scorable | length) == 0 then "  No scored Tier-A records yet."
          else
            ( $scorable
              | group_by((.domain // "code") + " / " + (.decisionClass // "unclassified"))
              | .[]
              | {
                  key: ((.[0].domain // "code") + " / " + (.[0].decisionClass // "unclassified")),
                  exact: (map(select(.matchScore=="exact")) | length),
                  partial: (map(select(.matchScore=="partial")) | length),
                  miss: (map(select(.matchScore=="miss")) | length),
                  total: length
                }
              | "  [\(.key)] exact=\(.exact) partial=\(.partial) miss=\(.miss) total=\(.total) (exact-rate=\(if .total>0 then (100*.exact/.total|floor) else 0 end)%)" )
          end ),
        "",
        "Challenger availability (Tier-A records):",
        "  ran: \($tierA | map(select(.challengeRan == true)) | length)   not-ran: \($tierA | map(select(.challengeRan == false)) | length)   unclear: \($tierA | map(select(.challengeRan != true and .challengeRan != false)) | length)",
        "",
        ( if ($scorable | length) == 0 then "Overall: no scored records yet."
          else
            "Overall: exact=\($scorable | map(select(.matchScore=="exact")) | length) partial=\($scorable | map(select(.matchScore=="partial")) | length) miss=\($scorable | map(select(.matchScore=="miss")) | length) / \($scorable | length) scored (exact-rate=\(if ($scorable|length)>0 then (100 * ($scorable | map(select(.matchScore=="exact")) | length) / ($scorable | length) | floor) else 0 end)%)"
          end ),
        "",
        ( if ($indep | length) == 0 then "Calibration error (MAD, raw confidence, independent-only): n/a (no independent-scored records with predictionConfidence)"
          else
            ( ($deviations | add / length) as $mad
              # AU1/ADR-0020 (issue #78) — integer-cents, round-half-up formatting,
              # IDENTICAL across all three runners: python3 uses math.floor(y)+(1 if
              # frac>=0.5 else 0), node uses Math.round(mad*100), and this jq path uses
              # |round. All three are round-half-up for a MAD that is always >= 0, so the
              # three emit the SAME string for the same ledger, and a future automated
              # consumer (the N1 graduation gate) greps one stable format regardless of
              # host tooling. jq has no printf-style float formatting, so build it from
              # integer cents; python3 and node now match this exact rule (the old python3
              # %.2f used round-half-to-even and disagreed by a cent on a half-boundary).
              # NOTE: keep this comment free of apostrophes — the whole jq program is a
              # single-quoted bash string, and a stray apostrophe would terminate it.
              | ($mad * 100 | round) as $cents
              | "Calibration error (MAD, raw confidence, independent-only): \($cents / 100 | floor).\(if ($cents % 100) < 10 then "0\($cents % 100)" else ($cents % 100 | tostring) end)\n  n=\($indep | length) across \($deviations | length) bucket(s)" )
          end )
    ' 2>/dev/null || echo "(jq aggregation unavailable — ledger may be empty)"
  else
    # node path: dedup + group identically to python3/jq.
    node - "$LEDGER" "$filter_domain" "$filter_class" <<'NODE'
const fs = require("fs");
const [,, ledger, fd, fc] = process.argv;
let lines;
try { lines = fs.readFileSync(ledger, "utf8").split("\n"); } catch { console.log("match-rate: no ledger"); process.exit(0); }
const records = [];
lines.forEach((line, i) => {
  line = line.trim(); if (!line) return;
  try {
    const r = JSON.parse(line);
    // AU1/ADR-0020 forward defaults — this loop re-implements its own defaulting
    // rather than calling ledger_read_all, so these MUST be kept in lockstep with that
    // helper's python3/node blocks and the jq block in ledger_read_all (a field
    // defaulted there but not here reads inconsistently between the two entry points
    // for the same field).
    if (r.scoredBy === undefined || r.scoredBy === null) r.scoredBy = { mode: "self-legacy", family: null, model: null };
    if (r.challengeRan === undefined) r.challengeRan = false;
    if (r.effectiveConfidence === undefined) r.effectiveConfidence = null;
    if (r.auditOutcome === undefined) r.auditOutcome = null;
    if (r.auditsForkId === undefined) r.auditsForkId = null;
    if (r.predictionConfidence === undefined) r.predictionConfidence = null;
    if (r.correctionOf === undefined) r.correctionOf = null;
    if (r.probeSource === undefined) r.probeSource = null;
    if (r.unclassifiedReason === undefined) r.unclassifiedReason = null;
    records.push(r);
  } catch { process.stderr.write("skip line " + (i+1) + "\n"); }
});
// AU1/ADR-0020 — dedup key widened to (issueId, forkId, decisionType) last-row-wins
// WITHIN EACH RECORD KIND — match the python3/jq paths. A tier-b-audit record
// deliberately shares issueId+forkId with the Tier-B decision it audits (via
// auditsForkId); decisionType is what keeps the two rows distinct under dedup — a
// 2-field key would silently collapse a ruling row and its later audit row into one.
// Join the three id fields with NUL bytes that cannot appear in a normalized id, so no
// combination collapses to the same key (a bare "" separator collides: issueId
// "1"+forkId "23" and issueId "12"+forkId "3" both keyed to "123").
const seen = {};
records.forEach(r => { seen[String(r.issueId) + "\u0000" + String(r.forkId) + "\u0000" + String(r.decisionType)] = r; });
// Apply the domain/class filter to the FULL deduped set BEFORE deriving Total,
// Challenger-availability, and scorable — so every reported line covers the same
// filtered population (matches python3; previously only $scorable was filtered, so
// Total + Challenger-availability ignored the filter).
const deduped = Object.values(seen)
  .filter(r => !fd || r.domain === fd)
  .filter(r => !fc || r.decisionClass === fc);
console.log("match-rate report");
console.log("=================");
console.log("Total records: " + deduped.length + " (after dedup)");
const scorable = deduped
  .filter(r => r.decisionType === "tier-a" && ["exact","partial","miss"].includes(r.matchScore));
const groups = {};
scorable.forEach(r => {
  const k = (r.domain||"code") + " / " + (r.decisionClass||"unclassified");
  if (!groups[k]) groups[k] = {exact:0,partial:0,miss:0,total:0};
  groups[k][r.matchScore]++; groups[k].total++;
});
console.log("");
console.log("Match-rate by domain + decision-class (Tier-A, scored only):");
const gkeys = Object.keys(groups).sort();
if (gkeys.length === 0) {
  console.log("  No scored Tier-A records yet.");
} else {
  gkeys.forEach(k => {
    const c = groups[k];
    const pct = c.total ? Math.floor(100*c.exact/c.total) : 0;
    console.log("  [" + k + "] exact=" + c.exact + " partial=" + c.partial + " miss=" + c.miss + " total=" + c.total + " (exact-rate=" + pct + "%)");
  });
}
// Challenger availability — match the python3/jq paths.
const tierA = deduped.filter(r => r.decisionType === "tier-a");
const chRan = tierA.filter(r => r.challengeRan === true).length;
const chNot = tierA.filter(r => r.challengeRan === false).length;
const chUnclear = tierA.length - chRan - chNot;
console.log("");
console.log("Challenger availability (Tier-A records):");
console.log("  ran: " + chRan + "   not-ran: " + chNot + "   unclear: " + chUnclear);
// Overall summary.
console.log("");
if (scorable.length === 0) {
  console.log("Overall: no scored records yet.");
} else {
  const ex = scorable.filter(r => r.matchScore === "exact").length;
  const pa = scorable.filter(r => r.matchScore === "partial").length;
  const mi = scorable.filter(r => r.matchScore === "miss").length;
  const pct = Math.floor(100*ex/scorable.length);
  console.log("Overall: exact=" + ex + " partial=" + pa + " miss=" + mi + " / " + scorable.length + " scored (exact-rate=" + pct + "%)");
}

// AU1/ADR-0020 - calibration report (independent-only, raw confidence). See the
// python3 block's full comment for the exact recipe (fixed 4-bucket scheme, simple
// MAD, exact=1.0/else=0.0 correctness mapping) - reproduced identically here.
const independentScorable = scorable.filter(r =>
  r.scoredBy && (r.scoredBy.mode === "cross-family" || r.scoredBy.mode === "same-family-fresh") &&
  typeof r.predictionConfidence === "number" && isFinite(r.predictionConfidence)
);
console.log("");
if (independentScorable.length === 0) {
  console.log("Calibration error (MAD, raw confidence, independent-only): n/a (no independent-scored records with predictionConfidence)");
} else {
  const bucketsMap = {};
  independentScorable.forEach(r => {
    const conf = r.predictionConfidence;
    const correct = r.matchScore === "exact" ? 1.0 : 0.0;
    const b = Math.min(3, Math.max(0, Math.floor(conf * 4)));
    if (!bucketsMap[b]) bucketsMap[b] = [];
    bucketsMap[b].push([conf, correct]);
  });
  const deviations = Object.keys(bucketsMap).map(b => {
    const items = bucketsMap[b];
    const avgConf = items.reduce((s, it) => s + it[0], 0) / items.length;
    const avgCorrect = items.reduce((s, it) => s + it[1], 0) / items.length;
    return Math.abs(avgConf - avgCorrect);
  });
  const mad = deviations.reduce((s, d) => s + d, 0) / deviations.length;
  // AU1/ADR-0020 (issue #78) — integer-cents round-half-up, IDENTICAL to python3
  // (math.floor(y)+(1 if frac>=0.5 else 0)) and jq ($mad*100|round). Replaces mad.toFixed(2) so the three
  // runners never disagree on the string (python's "%.2f" used banker's rounding and
  // differed by a cent on a half-boundary, e.g. MAD 0.125). mad >= 0, so Math.round is
  // round-half-up. Build "d.dd" from cents so an exact-two-decimal format is guaranteed.
  const cents = Math.round(mad * 100);
  console.log("Calibration error (MAD, raw confidence, independent-only): " + Math.floor(cents / 100) + "." + String(cents % 100).padStart(2, "0"));
  console.log("  n=" + independentScorable.length + " across " + deviations.length + " bucket(s)");
}
NODE
  fi
}

# ---------------------------------------------------------------------------
# Subcommand: list — enumerate records with optional filters
# Usage: decision-ledger.sh list [--issue <N>] [--domain code|content]
# ---------------------------------------------------------------------------
cmd_list() {
  local filter_issue="" filter_domain=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --issue) filter_issue="$2"; shift 2 ;;
      --domain) filter_domain="$2"; shift 2 ;;
      *) echo "decision-ledger list: unknown option '$1'" >&2; exit 2 ;;
    esac
  done

  if [ ! -f "$LEDGER" ]; then
    echo "decision-ledger: no ledger found at $LEDGER"
    exit 0
  fi

  # Read the whole ledger FIRST and check its exit status. ledger_read_all
  # `return 1`s on a genuinely unreadable/undecodable ledger (F[0]/F[2]); the old
  # `done < <(ledger_read_all)` process substitution DISCARDED that exit code, so
  # an unreadable-but-data-bearing ledger printed "0 record(s)" and exited 0 —
  # byte-identical to a genuinely empty ledger, the exact silent false-empty
  # (a "just a buried stderr line") that ruling retroactive-ledger-migration and
  # CLAUDE.md's 'errors fail loud' reject. Capture then propagate. NOTE: the
  # capture must be its OWN statement, NOT `local _all=$(...)` — `local` always
  # returns 0 and would swallow the very exit code we need.
  local _all_records _read_rc=0
  _all_records="$(ledger_read_all)" || _read_rc=$?
  if [ "$_read_rc" -ne 0 ]; then
    # Loud on STDOUT (not only ledger_read_all's stderr) + non-zero exit, so a
    # consumer that parses stdout or checks $? can never read this as "0 records".
    echo "decision-ledger: cannot read ledger at $LEDGER — records may exist but could not be enumerated (see stderr)"
    exit 1
  fi

  local count=0
  # Iterate normalized records (runner-agnostic, like cmd_query).
  while IFS= read -r line; do
    [ -z "${line// /}" ] && continue
    local include=true

    if [ -n "$filter_issue" ]; then
      local got; got="$(printf '%s' "$line" | json_field_equals issueId "$filter_issue")"
      [ "$got" != "yes" ] && include=false
    fi

    if [ -n "$filter_domain" ]; then
      local got; got="$(printf '%s' "$line" | json_field_equals domain "$filter_domain")"
      [ "$got" != "yes" ] && include=false
    fi

    if [ "$include" = "true" ]; then
      printf '%s' "$line" | json_project
      count=$((count + 1))
    fi
  done <<< "$_all_records"

  echo "---"
  echo "decision-ledger: $count record(s)"
}

# ---------------------------------------------------------------------------
# R3 (issue #63) — portable append lock + same-lock newline repair.
#
# F13 (concurrent appends can interleave/corrupt a line on a host without
# flock) and F14 (a crash mid-append can leave a trailing half-line that
# swallows the NEXT append too) are closed together here: every append path
# funnels through ONE critical section that (a) repairs a missing trailing
# newline if needed, THEN (b) appends — under whichever lock mechanism this
# host has, or, if the lock genuinely cannot be acquired, unlocked (fail-open).
#
# Ruling lock-failure-and-stale-policy is FAIL-OPEN, deliberately unlike the
# fail-closed, manual-rmdir arc-discovery.lock.d / arc-build.lock.d pattern
# elsewhere in this repo (arc-preflight.sh's append_guarded_line): those guard
# real build work; this ledger is measurement-only and must never wedge a
# write. A lock that cannot be acquired (ordinary contention, or a stale
# sentinel left by a crash — NEVER auto-broken by age, per the ruling) always
# warns loudly on stderr and then still completes the append, unlocked.
#
# LEDGER_LOCK_MAX_WAIT_SECONDS / LEDGER_LOCK_MAX_ATTEMPTS are hardcoded
# internal constants (ruling lock-tuning-config-surface — the CONFIDENCE_CAP
# precedent: deliberately no arc.config.jsonc knob). The only work this lock
# ever guards is a single JSONL line append, a sub-millisecond operation, so
# 10s is already generous — it comfortably covers the worst legitimate hold
# this file should ever see. Read together with lock-failure-and-stale-
# policy's repeated, explicit "a stuck lock NEVER wedges the measurement
# write": lock-tuning-config-surface's "fail loud rather than proceed unlocked
# when the bound is exceeded" is implemented here as the LOUDNESS of the
# stderr warning below, not as an abort — the append always completes, locked
# or unlocked. A genuine environment fault unrelated to lock contention (e.g.
# the ledger directory becoming unwritable) is not masked by this policy: it
# still surfaces on its own, later in this same function, the moment the
# actual `cat >> $LEDGER` write itself fails.
LEDGER_LOCK_MAX_WAIT_SECONDS=10
LEDGER_LOCK_POLL_INTERVAL="0.1"
LEDGER_LOCK_MAX_ATTEMPTS=100   # 100 * 0.1s ~= LEDGER_LOCK_MAX_WAIT_SECONDS

# _cmd_append_cleanup — the SINGLE trap-dispatched cleanup for cmd_append,
# covering BOTH the temp-file removal and the mkdir-sentinel lock release. A
# bash `trap ... EXIT` REPLACES any prior EXIT trap rather than stacking one on
# top of another — a separate `trap release_lock EXIT` installed after the
# temp-file trap would silently drop the temp-file cleanup on every error path
# after lock acquisition. One combined handler avoids that. Defined as a named
# function (never an inline `trap '...$var...' EXIT` string) so nothing here
# is re-tokenized when the trap fires — the same bash-3.2 (macOS's shipped
# /bin/bash) hazard arc-preflight.sh's release_lock comment documents, where an
# inline `$lockdir` in the trap string was observed to re-execute a `$(...)`
# payload embedded in a path at fire time. `tmp`/`_ledger_lockdir` are `local`
# to cmd_append and visible here via bash's dynamic scoping, since this
# handler only ever fires while cmd_append is still on the call stack. Release
# is a plain `rmdir` only (never `rm -rf`) — this only ever removes a
# directory THIS process itself created.
_cmd_append_cleanup() {
  [ -n "${tmp:-}" ] && rm -f "$tmp"
  [ -n "${_ledger_lockdir:-}" ] && rmdir "$_ledger_lockdir" 2>/dev/null
  return 0
}

# _ledger_repair_trailing_newline <ledger-path> — if the ledger file EXISTS and
# has NONZERO size and its last byte is not '\n', append a single repair
# newline (ruling newline-repair-strategy). Only the structural line-break is
# ever written here — NEVER any authored record content — so a crash-truncated
# record (e.g. `{"half":`) still cannot become valid JSON; it is merely
# isolated onto its own line, where it fails parse LOUDLY on read (nothing is
# masked). Empty or absent files are a no-op.
#
# `tail -c1 file` via a bare command substitution `$(...)` strips ANY trailing
# newline from ITS OWN output — so a naive `[ -n "$(tail -c1 "$f")" ]` cannot
# tell "file ends in \n" (tail's one captured byte is itself stripped, leaving
# "") apart from "file is empty" (tail captured nothing, also ""): both
# collapse to the same empty test result. Checking size with `-s` first
# disambiguates the two before ever inspecting the last byte. The `;printf x`
# / `${var%x}` trick below then recovers the true last byte even when it IS a
# newline, by giving the substitution a non-newline tail so bash's stripping
# has nothing trailing to remove.
_ledger_repair_trailing_newline() {
  local ledger_file="$1"
  [ -s "$ledger_file" ] || return 0   # absent or empty: nothing to repair
  local last_byte
  last_byte="$(tail -c1 "$ledger_file" 2>/dev/null; printf x)"
  last_byte="${last_byte%x}"
  # $'\n' (ANSI-C quoting, bash 3.2-safe) is a literal newline character here —
  # NOT a command substitution, so it is never itself trailing-newline-stripped
  # the way "$(printf '\n')" would be (which would wrongly compare against "").
  if [ "$last_byte" != $'\n' ]; then
    printf '\n' >> "$ledger_file"
  fi
}

# _ledger_append_critical_section <ledger-path> <tmp-file> — the ONE append
# critical section every code path below calls identically: locked-flock,
# locked-mkdir-sentinel, AND the unlocked fail-open fallback of either. Repair
# runs INSIDE this same section, immediately before the append, so no writer
# can observe a missing trailing newline and race to repair-and-append at the
# same time (which would reproduce F13's corruption inside the very code this
# issue exists to fix) — never a separate pre-check outside whatever lock (or
# lack of one) is in effect.
_ledger_append_critical_section() {
  local ledger_file="$1" tmp_file="$2"
  # TOCTOU shrink (F[3]): cmd_append's `[ -L "$LEDGER" ]` guard runs many steps
  # earlier — after it, mktemp + round-trip validation + lock acquisition all
  # elapse before we get here. Both operations below FOLLOW a symlink (the repair
  # newline and the `cat >>` append), so re-verify the ledger is still a real file
  # immediately before following it, closing the wide window in which a planted
  # symlink could be swapped in after the early check. This is NOT fully race-free
  # — portable POSIX shell has no O_NOFOLLOW, so an attacker winning the last
  # sub-millisecond between this `[ -L ]` and the `cat` is a documented residual
  # risk (issue #185 tracks the read-side and the broader hardening) — but it
  # shrinks the exposure from "the whole validate+lock window" to that irreducible
  # instant. fail-CLOSED (exit 2), matching cmd_append's early guard: a symlinked
  # ledger path is attack/misconfiguration, never lock contention, so the
  # fail-OPEN lock policy deliberately does not apply. Runs on EVERY append branch
  # (locked-flock, locked-mkdir, both fail-open fallbacks) since all funnel here.
  if [ -L "$ledger_file" ]; then
    echo "decision-ledger append: refusing to append — $ledger_file became a symlink (TOCTOU guard)" >&2
    exit 2
  fi
  _ledger_repair_trailing_newline "$ledger_file"
  cat "$tmp_file" >> "$ledger_file"
}

# ---------------------------------------------------------------------------
# Subcommand: append — write one complete, validated ledger record (atomic).
# For TRUSTED /arc SKILL use only. The skill passes a JSON file containing the
# full record; this tool validates it and appends atomically.
# Usage: decision-ledger.sh append <json-file>
# ---------------------------------------------------------------------------
cmd_append() {
  local rec_file="${1:-}"
  if [ -z "$rec_file" ]; then
    echo "decision-ledger append: requires <json-file> argument" >&2
    exit 2
  fi
  if [ ! -f "$rec_file" ]; then
    echo "decision-ledger append: file not found: $rec_file" >&2
    exit 2
  fi

  # Validate before writing.
  validate_record_fields "$rec_file"

  # AU1/ADR-0020 — best-effort, non-fatal referential warning (never a blocker: full
  # referential integrity would require reading the whole ledger at append time, and
  # this is fail-LOUD not fail-CLOSED, per CLAUDE.md's "errors fail loud" principle). A
  # typo'd auditsForkId/correctionOf silently orphans the audit/correction record with
  # no error otherwise — grep the existing ledger for a matching row and warn if none is
  # found, so the typo is visible immediately rather than discovered only when a report
  # comes up short later. The match is scoped by the pointer's DOCUMENTED target kind, not
  # issueId+forkId alone (schema header): auditsForkId points at the tier-b decision the
  # audit audits, so it must resolve to a decisionType=="tier-b" row; correctionOf points
  # at a prior record with the SAME issueId+decisionType, so it must resolve to a row whose
  # decisionType equals THIS record's own. A pointer that lands on some other kind (e.g. an
  # audit whose auditsForkId only matches a tier-a ruling) is still dangling and still warns.
  # validate_record_fields already rejects leading/trailing whitespace on the pointer fields
  # (raw==trimmed), so the _ptr_val captured through $(...) below cannot be silently
  # normalized into a false match by a trailing newline the JSON string didn't actually
  # carry.
  _new_type="$(_read_rec_field "$rec_file" "decisionType")"
  for _ptr_field in auditsForkId correctionOf; do
    _ptr_val="$(_read_rec_field "$rec_file" "$_ptr_field")"
    if [ -n "$_ptr_val" ]; then
      _ptr_issue="$(_read_rec_field "$rec_file" "issueId")"
      if [ "$_ptr_field" = "auditsForkId" ]; then
        _want_type="tier-b"
      else
        _want_type="$_new_type"
      fi
      # This dangling-pointer scan is ADVISORY (best-effort, never blocking). The
      # old `done < <(ledger_read_all)` process substitution discarded
      # ledger_read_all's `return 1` on an unreadable ledger — so an unreadable
      # existing ledger yielded an empty loop, left _found="no", and emitted a
      # FALSE "dangling pointer" warning. Capture and check the read's exit code
      # instead: on a read failure, SKIP the check with an honest "could not
      # verify" note rather than either (a) crying dangling-pointer falsely or
      # (b) exiting — exiting here would WEDGE the append, which ruling
      # lock-failure-and-stale-policy (and the fail-open measurement-ledger
      # design) forbid. The append itself never needs to read the ledger; if the
      # file is genuinely unwritable too, the later `cat >>` surfaces that on its
      # own. Capture is its own statement (a `local x=$(...)` would mask the code).
      _found="no"
      _dang_records=""; _dang_rc=0
      _dang_records="$(ledger_read_all 2>/dev/null)" || _dang_rc=$?
      if [ "$_dang_rc" -ne 0 ]; then
        echo "decision-ledger append: warning — could not read the existing ledger to verify $_ptr_field=$_ptr_val; skipping the dangling-pointer check (append NOT blocked, fail-open)" >&2
      else
        while IFS= read -r _line; do
          [ -z "$_line" ] && continue
          _match_issue="$(printf '%s' "$_line" | json_field_equals issueId "$_ptr_issue")"
          _match_fork="$(printf '%s' "$_line" | json_field_equals forkId "$_ptr_val")"
          _match_type="$(printf '%s' "$_line" | json_field_equals decisionType "$_want_type")"
          if [ "$_match_issue" = "yes" ] && [ "$_match_fork" = "yes" ] && [ "$_match_type" = "yes" ]; then
            _found="yes"
            break
          fi
        done <<< "$_dang_records"
        if [ "$_found" != "yes" ]; then
          echo "decision-ledger append: warning — $_ptr_field=$_ptr_val does not match any existing decisionType=$_want_type record for issueId=$_ptr_issue in the ledger (dangling pointer; not blocking the append)" >&2
        fi
      fi
    fi
  done

  # Serialize to a single-line JSON string (no embedded newlines).
  local json_line
  if [ "$JSON_RUNNER" = "jq" ]; then
    json_line="$(jq -c . "$rec_file")"
  elif [ "$JSON_RUNNER" = "python3" ]; then
    json_line="$(python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1],'r',encoding='utf-8'))))" "$rec_file")"
  else
    json_line="$(node -e "process.stdout.write(JSON.stringify(JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'))))" - "$rec_file")"
  fi

  if [ -z "$json_line" ]; then
    echo "decision-ledger append: failed to serialize record" >&2
    exit 1
  fi

  # Atomic append: write to temp file on same filesystem, then cat-append.
  # This keeps each record on a single complete line (POSIX O_APPEND is atomic
  # for pipe-buf-sized writes on local filesystems, but JSON.stringify output
  # can exceed PIPE_BUF for large rationale fields, so we use the safer pattern).
  local ledger_dir; ledger_dir="$(dirname "$LEDGER")"
  mkdir -p "$ledger_dir"
  # Refuse a symlinked ledger dir before writing through it. LEDGER_PATH is
  # env-overridable, so (unlike arc-preflight.sh's append_guarded_line, which
  # only ever guards its own fixed two-level .gstack/arc-rulings path) this
  # can't assume a fixed set of ancestor directories to walk; guarding the
  # final, directly-written-through directory closes the same class of hole
  # for the one path component every write in this function actually touches.
  if [ -L "$ledger_dir" ]; then
    echo "decision-ledger append: refusing to append — $ledger_dir is a symlink" >&2
    exit 2
  fi
  # Also refuse a symlinked ledger FILE inside an otherwise-real directory. Both
  # the newline repair (_ledger_repair_trailing_newline) and the append itself
  # (`cat "$tmp" >> "$LEDGER"`) FOLLOW a symlink, so a planted decisions.jsonl
  # symlink would redirect the repair newline and every appended record into the
  # link target. Same class of hole as the directory guard above (and the
  # lockfile guard below); fail-CLOSED (exit 2) to match the directory guard — a
  # symlinked ledger path is a misconfiguration/attack, not lock contention, so
  # the fail-OPEN lock policy deliberately does not apply here. `[ -L ]` is false
  # for a not-yet-created ledger, so a first append is unaffected.
  if [ -L "$LEDGER" ]; then
    echo "decision-ledger append: refusing to append — $LEDGER is a symlink" >&2
    exit 2
  fi

  local tmp; tmp="$(mktemp "$ledger_dir/.decision-ledger-append.XXXXXX")"
  # Guarantee the temp file AND (if acquired) the lock sentinel are cleaned up
  # on ANY exit path (early error, signal) — see _cmd_append_cleanup above for
  # why this is one combined trap rather than two separate ones.
  local _ledger_lockdir=""
  trap _cmd_append_cleanup EXIT
  trap '_cmd_append_cleanup; exit 130' INT TERM
  printf '%s\n' "$json_line" > "$tmp"

  # Validate the written line round-trips correctly (every runner gets the check).
  case "$JSON_RUNNER" in
    jq)      jq -e . "$tmp" >/dev/null 2>&1 || { echo "decision-ledger append: written line failed round-trip validation" >&2; exit 1; } ;;
    python3) python3 -c "import json,sys; json.loads(open(sys.argv[1]).read())" "$tmp" >/dev/null 2>&1 || { echo "decision-ledger append: written line failed round-trip validation" >&2; exit 1; } ;;
    node)    node -e "JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'))" - "$tmp" >/dev/null 2>&1 || { echo "decision-ledger append: written line failed round-trip validation" >&2; exit 1; } ;;
  esac

  # Atomic append, serialized against concurrent writers via a portable lock
  # (R3, issue #63 — see the block comment above _cmd_append_cleanup for the
  # full fail-open policy). flock is Linux/util-linux; on hosts without it
  # (e.g. stock macOS) a portable mkdir-sentinel lock takes over. EITHER way,
  # if the lock cannot be acquired within the bound, this warns loudly and
  # still performs the append, unlocked — never a hard failure for lock
  # contention alone. The newline-repair-then-append critical section
  # (_ledger_append_critical_section) runs IDENTICALLY on every branch below —
  # locked-flock, locked-mkdir, and both fail-open fallbacks — so F14's fix
  # can never be accidentally scoped to only the "happy path" lock branch.
  if command -v flock >/dev/null 2>&1; then
    local lockfile="$ledger_dir/.decision-ledger.lock"
    # Guard the lockfile path ITSELF before opening fd 9. `9>"$lockfile"` is a
    # plain bash redirection: it FOLLOWS a symlink and TRUNCATES the target the
    # instant the fd is opened — unconditionally, before flock is ever attempted,
    # with no race required. So a planted `.decision-ledger.lock` symlink would
    # zero out whatever file it points at on every single append. The ledger-dir
    # and ledger-file guards above do not cover this sibling path. Refuse to open
    # a symlinked lockfile and fall back to the UNLOCKED append (fail-OPEN, per
    # ruling lock-failure-and-stale-policy — the append still targets $LEDGER,
    # which is itself symlink-guarded above, so nothing is written through the
    # planted link and a stuck/hostile lock never wedges the measurement write).
    # Residual TOCTOU note (conscious call, not an oversight): the `[ -L ]` check
    # and the `9>"$lockfile"` open below cannot be made a single atomic operation
    # in portable shell (no O_NOFOLLOW), so a symlink planted in the sub-instant
    # between them would still be followed. The exposure is bounded to that
    # irreducible instant (the check sits immediately adjacent to the open), and
    # the truncation sink it protects is only the sibling LOCK file — the ledger
    # itself is separately re-guarded inside _ledger_append_critical_section right
    # before every write. Fully race-free lockfile handling is out of reach within
    # the portable-shell fence; issue #185 tracks broader symlink hardening.
    if [ -L "$lockfile" ]; then
      echo "decision-ledger append: WARNING — $lockfile is a symlink; refusing to open it as a write lock (opening it would follow and truncate the link target). Falling back to an UNLOCKED append (fail-open by design)." >&2
      _ledger_append_critical_section "$LEDGER" "$tmp"
    else
      (
        if flock -w "$LEDGER_LOCK_MAX_WAIT_SECONDS" 9; then
          _ledger_append_critical_section "$LEDGER" "$tmp"
        else
          echo "decision-ledger append: WARNING — could not acquire the flock write lock within ${LEDGER_LOCK_MAX_WAIT_SECONDS}s (contention); falling back to an UNLOCKED append so the write is never wedged (measurement-only ledger, fail-open by design)" >&2
          _ledger_append_critical_section "$LEDGER" "$tmp"
        fi
      ) 9>"$lockfile" || exit $?
      # Propagate the subshell's exit code (issue #63 security-gate P0): the
      # critical section's fail-CLOSED `exit 2` (TOCTOU symlink guard) only exits
      # THIS `( … )` subshell, not cmd_append. Without this, a tripped guard on a
      # flock host would fall through and return success — silently dropping the
      # measurement row instead of failing loud, breaking the guard's own "runs on
      # EVERY append branch" contract on exactly the Linux/CI path flock covers.
      # The fail-open WARNING paths return 0, so this only fires on a real refusal.
    fi
  else
    # Portable mkdir-sentinel lock. Named distinctly from the *.lock.d
    # convention arc-preflight.sh uses for arc-discovery/arc-build (those are
    # fail-closed, manual-rmdir build guards; this one is fail-open and
    # self-recovering — a maintainer who greps for *.lock.d out of habit
    # should not find this and apply the wrong recovery runbook to it).
    # The mkdir TARGET path is held in a SEPARATE local from the trap-visible
    # cleanup handle `_ledger_lockdir`. Critical distinction: `_ledger_lockdir`
    # (which _cmd_append_cleanup rmdir's on EXIT/INT/TERM) must name ONLY a
    # sentinel THIS process actually created — it stays "" through the entire
    # contended-wait window and is bound to the path ONLY inside the mkdir
    # success branch. If it were assigned before mkdir succeeds, a SIGINT/SIGTERM
    # arriving while this process is still WAITING for another writer's lock would
    # fire the trap and rmdir the OTHER writer's held sentinel — freeing it under
    # a live appender and re-opening the exact F13 concurrent-corruption this lock
    # exists to close. So bind the handle at, and only at, the moment of ownership.
    local _ledger_lock_path="$ledger_dir/.decision-ledger-append-lock"
    local _ledger_lock_waited=0
    local _ledger_lock_acquired="no"
    while :; do
      if mkdir "$_ledger_lock_path" 2>/dev/null; then
        _ledger_lockdir="$_ledger_lock_path"   # own it now — cleanup may release it
        _ledger_lock_acquired="yes"
        break
      fi
      _ledger_lock_waited=$((_ledger_lock_waited + 1))
      if [ "$_ledger_lock_waited" -ge "$LEDGER_LOCK_MAX_ATTEMPTS" ]; then
        break
      fi
      sleep "$LEDGER_LOCK_POLL_INTERVAL"
    done
    if [ "$_ledger_lock_acquired" = "yes" ]; then
      _ledger_append_critical_section "$LEDGER" "$tmp"
      rmdir "$_ledger_lockdir" 2>/dev/null || true
      _ledger_lockdir=""
    else
      # acquisition failed: `_ledger_lockdir` was never bound (still ""), so the
      # trap has nothing of ours to release. Report the path via _ledger_lock_path.
      echo "decision-ledger append: WARNING — could not acquire the append lock after ~${LEDGER_LOCK_MAX_WAIT_SECONDS}s (contention, or a stale lock dir left by a crash at $_ledger_lock_path — NOT auto-removed by age, per ruling; a human may 'rmdir' it by hand if genuinely stuck and no other append is in flight); falling back to an UNLOCKED append so the write is never wedged (measurement-only ledger, fail-open by design)" >&2
      _ledger_append_critical_section "$LEDGER" "$tmp"
    fi
  fi
  rm -f "$tmp"
  trap - EXIT
  trap - INT TERM
  # Extract forkId for the confirmation line via $JSON_RUNNER (covers node-only
  # hosts too — the previous jq||python3 form left forkId= empty when only node
  # was present, which was the tell that the node append path had no test).
  local _forkid
  case "$JSON_RUNNER" in
    jq)      _forkid="$(jq -r '.forkId // "?"' "$rec_file" 2>/dev/null || echo '?')" ;;
    python3) _forkid="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('forkId','?'))" "$rec_file" 2>/dev/null || echo '?')" ;;
    node)    _forkid="$(node -e "process.stdout.write(String(JSON.parse(require('fs').readFileSync(process.argv[2],'utf8')).forkId||'?'))" - "$rec_file" 2>/dev/null || echo '?')" ;;
    *)       _forkid="?" ;;
  esac
  echo "decision-ledger: appended record for forkId=$_forkid"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "$SUBCOMMAND" in
  query)      shift; cmd_query "$@" ;;
  match-rate) shift; cmd_match_rate "$@" ;;
  list)       shift; cmd_list "$@" ;;
  append)     shift; cmd_append "$@" ;;
  ""|--help|-h)
    cat <<USAGE
decision-ledger.sh — arc decision ledger query tool (domain-neutral)

Subcommands:
  query      --issue <N>           Retrieve all decisions for issue N (exact id match)
  query      --fork <forkId>       Retrieve a specific fork decision
  match-rate [--domain code|content] [--class <spine>]
                                   Rolling match-rate per decision-class per domain,
                                   plus a calibration-error report (independent-only,
                                   raw confidence — AU1/ADR-0020)
  list       [--issue <N>] [--domain code|content]
                                   List all records (optional filters)
  append     <json-file>           (trusted /arc skill only) Append one ledger record.
                                   AU1/ADR-0020: decisionType now also accepts
                                   "tier-b-audit"; decisionClass=="unclassified"
                                   requires a companion non-empty unclassifiedReason.
                                   R3 (issue #63): the append is lock-serialized
                                   (flock, else a portable mkdir-sentinel lock;
                                   fail-open with a loud warning if a lock can't
                                   be acquired — never wedged), auto-repairs a
                                   missing trailing newline before appending, and
                                   REJECTS any free-text field (including
                                   issueTitle/project/options[]) containing a
                                   control character or ANSI/CSI escape.

Ledger: .gstack/arc-rulings/decisions.jsonl (gitignored runtime state)

Examples (per AC #3 and AC #4):
  bash .claude/workflows/decision-ledger.sh query --issue 32
  bash .claude/workflows/decision-ledger.sh match-rate
  bash .claude/workflows/decision-ledger.sh match-rate --domain code --class tech-choice
  bash .claude/workflows/decision-ledger.sh list --issue 32

Schema: schemaVersion 1. decisionType: tier-a | tier-b | tier-b-audit. See file header
for the full field list, including the AU1/ADR-0020 additions (scoredBy,
effectiveConfidence, auditOutcome, auditsForkId, correctionOf, probeSource,
unclassifiedReason) and the widened (issueId, forkId, decisionType) dedup key.
USAGE
    exit 0
    ;;
  *)
    echo "decision-ledger: unknown subcommand '$SUBCOMMAND'. Run with --help for usage." >&2
    exit 2
    ;;
esac
