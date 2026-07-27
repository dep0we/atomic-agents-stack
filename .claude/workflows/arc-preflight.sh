#!/usr/bin/env bash
# arc-preflight.sh — enforced safety gates for the /arc quality loop.
#
# Turns the arc loop's advisory safety gates into HARD, in-runner gates with a
# real exit code. The /arc skill calls this BEFORE firing any workflow and
# AFTER discovery; a non-zero exit blocks the next step.
#
# Usage:
#   arc-preflight.sh build <issue>      # dirty-worktree + rulings-required + stale-check + spec checks
#   arc-preflight.sh finish <issue>     # dirty-worktree check
#   arc-preflight.sh discovery-pre [trust]      # (run against MAIN repo, cwd=REAL_REPO) dirty-worktree check, then store a fingerprint of the main tree state (before discovery fires in the worktree). trust=hardened (default if omitted) needs ARC_DISCOVERY_SNAPSHOT_KEY and stores a KEYED MAC; trust=solo needs no key and stores a PLAIN fingerprint — see TRUST PROFILE below.
#   arc-preflight.sh discovery-verify [trust]   # (run against MAIN repo, cwd=REAL_REPO) recompute main tree state and compare against the stored snapshot; any delta fires discovery-mutated (after discovery in worktree). The comparison method is read off the STORED snapshot's own tag, not the trust arg passed here — see TRUST PROFILE below; the trust arg is used only to detect a regime MISMATCH (e.g. a plain snapshot re-verified at trust=hardened) and gate closed on one.
#   arc-preflight.sh commit-residual <issue>    # (issue #62, D1) commit-before-gates catch-all: stages + commits whatever ADR-0012's per-round commits didn't already capture (excluding CHANGELOG.md/VERSION, left for /ship), as `arc(<id>): build output`. Scans the staged diff for secret-shaped content first and refuses to commit (dirty-residual-sensitive) rather than swallow a hit.
#   arc-preflight.sh dirtycheck                 # (issue #62, D1) the enforced clean-tree ASSERTION that follows commit-residual — Armor A, never trust-gated, never receives a trust arg.
#   arc-preflight.sh govcheck <base> <path>...  # block if any guarded governing-doc path changed vs <base>
#   arc-preflight.sh fencecheck <base> <issue>  # (issue #70) block pr-ready if the diff crosses the run's declared scope fence with no matching fenceException. NOT trust-gated — stays a hard stop, and its receipt-read containment stays fully hardened, at every trust level.
#   arc-preflight.sh tests <testCommand> [coverageCommand] [testTimeoutSeconds]  # block pr-ready unless the test command ran + exited 0. testTimeoutSeconds (issue #62, D3) is OPTIONAL — unset preserves today's no-limit behavior; when set, it bounds BOTH testCommand and coverageCommand and a timeout counts as tests-failed.
#   arc-preflight.sh seccheck <base> <issue>  # block pr-ready if the diff touches a sensitive surface with no security-review receipt
#   arc-preflight.sh speccheck <issue>  # block discovery/build if no spec receipt exists for the issue (planning gate)
#   arc-preflight.sh write-guarded <relname> [trust]    # (issue #69) write STDIN atomically to .gstack/arc-rulings/<relname>, symlink+containment guarded. trust is an OPTIONAL trailing arg (default hardened when omitted, so an unmigrated caller stays safe) — see TRUST PROFILE below.
#   arc-preflight.sh append-guarded <relname> [trust]   # (issue #69) append one STDIN line to .gstack/arc-rulings/<relname>, same guard + a per-file lock; used for <issue>-rounds.jsonl. trust threads through to the write_guarded call it makes.
#   arc-preflight.sh riskcheck pre-build <issue> [trust]              # (#119, L3) ADVISORY-ONLY, DIFFERENT KIND from every mode above — see the full header comment on the riskcheck functions. NEVER blocks; always exits 0; prints exactly one `risk-depth:<light|normal|full>` token to stdout (never a `preflight-failed:` token). Partial pre-build floor (Tier-A fork count only).
#   arc-preflight.sh riskcheck post-implement <base> <issue> [trust]  # (#119, L3) ADVISORY-ONLY — same non-blocking contract as pre-build. Full composed read (sensitive-surface + Tier-A forks + diff-size), RATCHETED against any pre-build floor already stored for this issue — can only raise the depth, never lower it.
#   arc-preflight.sh reconcile-died <base>  # (#132, R2; buildQueueShape added #66, R6) ADVISORY-ONLY, DIFFERENT KIND, NOT trust-gated (no trust arg — see the full header comment on reconcile_died). NEVER blocks; always exits 0 on a completed check; prints exactly one JSON object to stdout carrying a `verdict`/`overall` field (never a `preflight-failed:` token). Read-only detection of a died arc build/finish run: committed arc(<id>): work with no covering telemetry line and no live signal (build lock dir + build-queue.json active entry, per the liveness-proxy ruling). Also carries a `buildQueueShape` verdict — a lenient corruption tripwire for build-queue.json itself (present/parses/has the four required keys) — read by both /arc resume and an advisory-only check at /arc build step 2; see docs/arc-state-lifecycle.md.
#
# TRUST PROFILE (L2, issue #118, ADR-0017 + docs/ARMOR-TAXONOMY.md):
#   A single `trust` value — "solo" | "hardened" — thins a NAMED set of Armor-B
#   (anti-attacker, not output-quality) checks. It is resolved ONCE per /arc
#   invocation by the skill (never re-read mid-run) and passed EXPLICITLY as a
#   positional argument to each check below that it applies to — never an
#   ambient env var, never exported, so a downgrade can never silently spread to
#   a check nobody meant to relax. Any value other than the exact string "solo"
#   (absent, empty, mis-cased, wrong type) resolves to "hardened" — the
#   fail-safe default. Exactly four things vary by trust, and nothing else:
#     1. discovery-pre/verify: hardened stores/verifies a per-run-KEYED MAC of
#        the net-state fingerprint (requires ARC_DISCOVERY_SNAPSHOT_KEY); solo
#        stores/verifies a PLAIN (unkeyed) fingerprint of the same net state.
#        The net-state assertion itself — "did anything change?" — is NEVER
#        skipped at either trust level; only whether the comparison is
#        forgery-resistant varies.
#     2. write_guarded/append_guarded_line's directory-ancestor containment
#        check: hardened resolves both the target dir and the repo root via
#        `pwd -P` and requires an EXACT match (catches a symlinked ancestor,
#        e.g. macOS /tmp -> /private/tmp); solo skips that resolution and
#        relies on the plain `-L` (is-this-path-itself-a-symlink) checks that
#        run at every trust level regardless.
#     3. The discovery worktree scaffold (SKILL.md discovery step 3): solo
#        skips creating it entirely (by design — it isolates nothing today, see
#        ARMOR-TAXONOMY.md) and logs one honest line; hardened/unset creates it
#        as today. The lock sentinel and the net-state assertion above are
#        UNCHANGED at every trust level either way.
#     4. install.sh's `guard_not_symlink` — cut UNCONDITIONALLY (every trust
#        level, not thinned behind the profile): the installer only ever runs
#        on the maintainer's own machine (ADR-0017 point 6), so this one is not
#        part of the trust switch at all; it just no longer exists.
#   Everything else in this file — govcheck, fencecheck's hard stop and its
#   receipt-read containment, the test/coverage gates, seccheck, speccheck, the
#   rulings gate, the crash-safe mktemp+mv write pattern — is Armor A and does
#   NOT vary by trust. See docs/ARMOR-TAXONOMY.md for the full split.
#
# Design (per the #4 PR1 rulings):
#   * ONE shared gate() helper (defer-boundary-explicit = Option C): run a named
#     check; on failure print a structured token to stdout + a plain remedy to
#     stderr, then exit non-zero. No registry, no plug-in framework.
#   * Structured failure tokens (gate-failure-output-contract = Option 2):
#       preflight-failed:dirty-worktree
#       preflight-failed:dirty-worktree-postbuild
#       preflight-failed:dirty-residual-sensitive
#       preflight-failed:dirty-residual-unexpected-path
#       preflight-failed:dirty-residual-head-moved
#       preflight-failed:dirty-residual-undo-failed
#       preflight-failed:missing-rulings
#       preflight-failed:incomplete-rulings
#       preflight-failed:discovery-mutated
#       preflight-failed:governing-doc-edit
#       preflight-failed:fence-crossing
#       preflight-failed:fence-receipt-invalid
#       preflight-failed:tests-unconfigured
#       preflight-failed:tests-failed
#       preflight-failed:tests-timeout-misconfigured
#       preflight-failed:coverage-floor
#       preflight-failed:needs-security-gate
#       preflight-failed:missing-spec
#       preflight-failed:missing-latest-discovery
#       preflight-failed:stale-rulings
#       preflight-failed:isolation-unavailable
#         (emitted by the /arc skill flow when the throwaway worktree for discovery
#          cannot be created; NOT a preflight script mode — the worktree lifecycle
#          lives in the skill layer, not here. Listed so the token set is exhaustive.
#          The skill fails CLOSED on it: discovery does not proceed without the
#          scaffold unless the human sets the one-shot ARC_ALLOW_NO_ISOLATION=1
#          override for that single run — see the worktree note below.)
#     exactly one token line to stdout; a human remedy to stderr; non-zero exit.
#   * ALL gates fail-closed, NO override (fail-closed-vs-warn-per-gate = Option A):
#     no --allow-dirty, no env-var/sentinel bypass. This script only DETECTS and
#     REPORTS. It never auto-stashes and never auto-reverts — the skill offers the
#     stash; the script performs no destructive action.
#
# EXIT CODE CONTRACT:
#   exit 0 — all gates passed.
#   exit 1 — a GATED failure: exactly one `preflight-failed:<gate>` token is
#            printed to stdout (plain remedy to stderr). The caller maps the token.
#   exit 2 — an ENVIRONMENT/USAGE error (no python3/node, no shasum/sha256sum,
#            not a git repo, unknown mode, internal abort). A message is printed
#            to stderr; NO `preflight-failed:` token is emitted. The caller cannot
#            map a gate — it must relay the stderr message and not proceed.
#
# RISKCHECK IS NOT PART OF THIS CONTRACT (#119, L3). `riskcheck pre-build` /
# `riskcheck post-implement` NEVER participate in the exit-0/1/2 scheme above —
# they are advisory-only depth-selectors, not pass/fail gates (what-risk-
# controls ruling: risk only turns review intensity up/down, it never skips a
# safety check). They ALWAYS exit 0, on every path including an internal
# failure, and print exactly one `risk-depth:<light|normal|full>` token to
# stdout — never a `preflight-failed:*` token, never a bare non-zero exit. A
# caller that sees `riskcheck` produce a non-zero exit (e.g. an un-upgraded
# installed copy of this file that predates #119 and hits the generic
# unknown-mode `exit 2` path) must treat that identically to a missing/
# unconfigured/inconclusive risk read: resolve to "full", never STOP the
# build over it. See the full design note on the riskcheck functions below.
#
# RECONCILE-DIED IS NOT PART OF THIS CONTRACT EITHER (issue #132, R2).
# `reconcile-died <base>` is READ-ONLY DETECTION, not a pass/fail gate — it
# NEVER blocks `/arc resume`/`/arc status`. It ALWAYS exits 0 on a completed
# check (even when the check finds a died run) and prints exactly ONE JSON
# object to stdout carrying an `overall` field (`"matches"` | `"died"` |
# `"cant-tell"`, plus a per-issue `issues[]` breakdown) — the three-way
# outcome lives ENTIRELY inside that JSON payload, never encoded into the
# exit code (a `died` verdict is bad news about a DIFFERENT, already-ended
# run, not a failure of the status check itself — deliberately NOT the
# govcheck/fencecheck exit-1-means-blocked convention above). A non-zero
# exit from this mode means the check genuinely could not run at all (no
# git repo, no JSON_RUNNER, an unresolvable <base>) — the same class as any
# other mode's exit 2, no `preflight-failed:` token either way (this mode
# never gates). It is also NOT trust-gated: it takes no `trust` argument at
# all (read-path-safety ruling — a trust level would control nothing on
# this read-only path; see the reconcile_died functions' own header comment
# for the full design).
#
# RULINGS GATE — what it actually proves (honest scope):
#   exists + valid JSON + `issue` leading id matches the build's <issue> arg +
#   non-empty `decisions` + `ruledAt` + non-empty `discoveryRunId` + non-empty
#   `forkIds` all covered by `decisions`. It ALSO proves the rulings were made
#   against the latest discovery run (run-id currency via the latest-discovery
#   marker, with a legacy-affirmed escape hatch for pre-marker repos).
#
# READ-ONLY-DISCOVERY INVARIANT (issue #6):
#   ENFORCED GUARANTEE — net-state assertion (this script's discovery-pre /
#   discovery-verify modes): run against the MAIN repo (cwd=REAL_REPO), AFTER
#   discovery fired, to DETECT any net tracked-file write the discovery run leaked
#   back to the main tree. discovery-pre snapshots the main tree before discovery
#   fires; discovery-verify recomputes it after. Captures HEAD, tracked file contents,
#   non-ignored untracked files (porcelain), and arc tooling files (committed +
#   porcelain-visible under the self-contained model; the leg also uniquely catches a
#   freshly-CREATED untracked tooling file). discovery-verify fails on ANY net delta. SCOPE/LIMIT: it asserts
#   the NET state, not "nothing was written" — a transient write-then-restore within
#   the run window is the disclosed gap. SNAPSHOT INTEGRITY: the snapshot file lives in
#   the git dir, which a discovery sub-agent (cwd=REAL_REPO) can WRITE — so under
#   trust=hardened (default/unset) it stores only a KEYED MAC of the fingerprint, never
#   the plaintext. The key is a per-run secret the TRUSTED skill generates and passes via
#   ARC_DISCOVERY_SNAPSHOT_KEY to BOTH discovery-pre and discovery-verify; the sandboxed
#   discovery fire never receives it. A sub-agent can overwrite the snapshot file but
#   cannot forge a MAC matching its tampered state, so it cannot mask a PERMANENT
#   mutation by re-fingerprinting. Both hmac/envelope modes fail CLOSED (exit 2) if the
#   key is absent — the integrity property is not silently downgradable AT THAT TRUST
#   LEVEL. TRUST=SOLO (an explicit maintainer opt-in, never the default — see the TRUST
#   PROFILE block at the top of this file): no key is required at all; discovery-pre/
#   verify instead store/compare a PLAIN (unkeyed) fingerprint of the same net state.
#   This trades away only the anti-FORGERY property (a hostile sub-agent could otherwise
#   forge a matching plain fingerprint for a tampered tree) — the underlying "did
#   anything change?" net-state assertion itself is NEVER skipped or weakened at either
#   trust level, and still fails closed on ANY delta. discovery-verify decides which
#   comparison to run off the algo TAG stored in the snapshot file at discovery-pre time
#   (not off its own trust argument), and gates closed on a regime mismatch between the
#   two (e.g. a plain-tagged snapshot handed to a hardened verify call). SNAPSHOT PATH: the snapshot lives at the
#   per---git-dir location — the common .git when REAL_REPO is the main repo, the
#   per-worktree .git/worktrees/<name>/ metadata dir when REAL_REPO is itself a linked
#   worktree (`git rev-parse --git-dir` does NOT collapse to the common dir from a linked
#   worktree). That is fine because discovery-pre and discovery-verify both run from the
#   SAME cwd=REAL_REPO, so they resolve the same --git-dir and agree on the path; the
#   only hard requirement is that the two run from the same cwd (mixing a main-repo
#   discovery-pre with a worktree-cwd discovery-verify, or vice versa, would split the
#   path and emit a false discovery-mutated). This is the ONLY enforced read-only
#   protection, and it runs unconditionally on BOTH the worktree and override paths.
#
#   DEFENSE-IN-DEPTH SCAFFOLD — worktree (NOT the enforced guarantee; HARDENED/UNSET
#   TRUST ONLY — see the TRUST PROFILE block at the top of this file): under
#   trust=hardened (the default) or trust unset, the /arc skill creates a throwaway git
#   worktree under a private mktemp parent (under TMPDIR, outside the repo tree) before
#   firing arc-discovery, and tears it down unconditionally on every exit path. Be
#   precise about what it buys: its tracked-file mutation-isolation applies ONLY to
#   operations explicitly routed through the worktree checkout, and the discovery fire
#   has NONE — the fire and its sub-agents run with cwd=REAL_REPO (no verified runtime
#   arg re-roots them into the worktree), so a sub-agent write, like a read, lands in the
#   MAIN repo and is caught by the net-state assertion above, NOT prevented by the
#   worktree. The worktree is a scaffold for any future op the skill explicitly routes
#   through it (e.g. HEAD-content injection); it does not isolate what the discovery run
#   actually writes or reads as wired today. UNDER TRUST=SOLO the skill does not attempt
#   to create this scaffold AT ALL (by design — per the analysis above it isolates
#   nothing today) and logs one honest line saying so; the per-repo lock sentinel and
#   the net-state assertion (discovery-pre/discovery-verify against REAL_REPO) run
#   exactly as they do under hardened — only the worktree create/teardown is absent.
#
#   FAIL-CLOSED + MINIMAL HUMAN OVERRIDE (HARDENED/UNSET TRUST ONLY): if the skill
#   cannot create the worktree scaffold, it emits preflight-failed:isolation-unavailable
#   and halts — it does NOT silently skip the scaffold and proceed. There is no
#   forge-able receipt file and no prose claim/consume chain. The ONLY way past a failed
#   scaffold is a one-shot operator action: the human re-runs that single discovery with
#   ARC_ALLOW_NO_ISOLATION=1 in the environment. A build never sets environment variables
#   (arc-execute fires workflows with args, not env), so this override is reachable only
#   by a human at the terminal, never auto-reachable by a build. It is low-stakes because
#   the net-state assertion above always runs and is the enforced floor — skipping the
#   best-effort scaffold does not remove the enforced read-only protection. UNDER
#   TRUST=SOLO this entire hard-stop is moot: the scaffold is never attempted, so it can
#   never fail to create, so `isolation-unavailable` never fires and this override is
#   never reached — see the DEFENSE-IN-DEPTH SCAFFOLD note above.

set -euo pipefail

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

MODE="${1:-}"

# Operate on the current repo. The skill cd's into the repo before calling us.
# Resolve the toplevel so snapshots + git invocations are path-stable.
if ! REPO="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "arc-preflight: not inside a git repository (cwd=$(pwd))" >&2
  exit 2
fi

# Store the snapshot inside the git dir (works for worktrees too via --git-dir),
# where git never reports it in `status --porcelain`. Writing it under the work
# tree (e.g. .gstack/) would show as an untracked change and self-trip the
# dirty-worktree gate in a real target repo.
if ! SNAP_DIR="$(git -C "$REPO" rev-parse --git-dir 2>/dev/null)"; then
  echo "arc-preflight: could not resolve the git dir for snapshot storage (cwd=$(pwd))" >&2
  exit 2
fi
# rev-parse --git-dir can be relative to REPO; make it absolute.
case "$SNAP_DIR" in
  /*) : ;;
  *) SNAP_DIR="$REPO/$SNAP_DIR" ;;
esac
SNAPSHOT_FILE="$SNAP_DIR/arc-discovery-snapshot"

# Pick a JSON parser once. Honor a pre-set JSON_RUNNER (the test suite forces
# `JSON_RUNNER=node` to exercise the node validators on boxes that also have
# python3 — without this, python3 always wins and the ~190-line node branch of
# every JSON gate ships untested). When unset, auto-detect: prefer python3, fall
# back to node. An explicitly-set runner that isn't actually installed exits 2.
#
# The override is allowlisted to exactly `python3` or `node` — the only two
# interpreters that speak this gate's stdin-script protocol. Any other value (a
# typo, a stray export, `cat`) would emit garbage instead of the controlled
# match/stale/absent/ok/invalid vocabulary and route the gate to an undefined
# outcome, so a bad value fails closed with an exit-2 usage error, same class as
# "not on PATH".
JSON_RUNNER="${JSON_RUNNER:-}"
if [ -n "$JSON_RUNNER" ]; then
  case "$JSON_RUNNER" in
    python3|node) ;;
    *)
      echo "arc-preflight: JSON_RUNNER must be 'python3' or 'node' (got '${JSON_RUNNER}')" >&2
      exit 2
      ;;
  esac
  if ! command -v "$JSON_RUNNER" >/dev/null 2>&1; then
    echo "arc-preflight: JSON_RUNNER='${JSON_RUNNER}' was set but is not on PATH" >&2
    exit 2
  fi
elif command -v python3 >/dev/null 2>&1; then
  JSON_RUNNER="python3"
elif command -v node >/dev/null 2>&1; then
  JSON_RUNNER="node"
fi

# ---------------------------------------------------------------------------
# Shared pass/fail helper (the ONE gate function — ruling defer-boundary-explicit)
# ---------------------------------------------------------------------------
#
# gate <token> <remedy>
#   Called on FAILURE only. Prints the structured token to stdout, the
#   plain-language remedy to stderr, and exits non-zero. There is no override.
gate() {
  local token="$1"
  local remedy="$2"
  printf 'preflight-failed:%s\n' "$token"      # structured token → stdout
  printf 'arc-preflight: %s\n' "$remedy" >&2    # human remedy     → stderr
  exit 1
}

ok() {
  # Success path: a single advisory line to STDERR (not stdout). The skill parses
  # ONLY the failure token from stdout, so the per-gate "ok" line must NOT land on
  # stdout — otherwise a chained build (check_rulings -> check_spec -> check_stale
  # -> ok "build") that fails a LATER gate would leave an earlier gate's
  # `preflight-ok:speccheck` line on stdout ALONGSIDE the `preflight-failed:*`
  # token, breaking the "exactly one token line to stdout" contract the skill's
  # token parser depends on. Routing ok() to stderr keeps stdout = at most one
  # token line (the failure token, or empty on a clean pass).
  printf 'preflight-ok:%s\n' "$1" >&2
}

# is_solo <trust> — the ONE shared trust check, used at every call site that varies
# behavior by trust (discovery-pre/verify, write_guarded, append_guarded_line). A
# STRICT ALLOWLIST: true (rc 0) iff the value is the EXACT string "solo". Anything
# else — absent, empty, whitespace, wrong case ("Solo"), a typo ("solo2"), or a
# non-string value smuggled through — resolves FALSE (hardened). This is
# deliberately an allowlist, never a blocklist (`[ "$1" != "hardened" ]`): a
# blocklist fails OPEN on any unexpected input (wrong case, null, a stray value),
# landing on the WEAKER path — the exact inversion of the ADR's fail-safe-toward-
# hardened rule. An allowlist can only ever fail toward the stronger path.
is_solo() {
  [ "${1:-}" = "solo" ]
}

# dir_ancestor_containment_guard <dir> <expected-suffix-from-repo-root> <trust>
#
# The ELABORATE half of the symlink-containment-thin ruling: resolves BOTH <dir>
# and $REPO via `pwd -P` and requires an EXACT match against
# "<resolved-repo>/<expected-suffix>" — this is what catches a symlinked ANCESTOR
# of REPO (e.g. macOS /tmp -> /private/tmp), which the plain per-component `-L`
# checks the callers already run (at EVERY trust level, unconditionally — the
# "quick test ! -L" half of the same ruling) cannot see on their own.
#
# trust=solo SKIPS the resolution entirely and ECHOES <dir> UNRESOLVED — the quick
# per-component `-L` checks the caller already ran are the full solo-mode protection;
# this is the ONLY thing that varies. trust=hardened (or anything other than exactly
# "solo") always runs the full resolution, hard-exits on any mismatch, and ECHOES THE
# RESOLVED PATH on success — the caller must use that returned value (not the raw
# <dir>) for the actual write, so the write operates on the SAME already-canonicalized
# path this check just verified rather than re-traversing the ancestor chain a second
# time (which would reopen a narrow TOCTOU window between the check and the write).
# Shared by write_guarded and, transitively, by append_guarded_line (which delegates
# its actual write to write_guarded and passes trust through) — one primitive, not a
# pasted copy per call site.
dir_ancestor_containment_guard() {
  local dir="$1" suffix="$2" trust="$3"
  if is_solo "$trust"; then
    printf '%s' "$dir"
    return 0
  fi
  local resolved_dir resolved_repo
  resolved_dir="$(cd "$dir" 2>/dev/null && pwd -P)" || { echo "arc-preflight: cannot resolve ${dir}" >&2; exit 2; }
  resolved_repo="$(cd "$REPO" 2>/dev/null && pwd -P)" || { echo "arc-preflight: cannot resolve repo root" >&2; exit 2; }
  if [ "$resolved_dir" != "$resolved_repo/$suffix" ]; then
    echo "arc-preflight: ${dir} resolved to '${resolved_dir}', expected '${resolved_repo}/${suffix}' — refusing to write/read (symlinked intermediate?)" >&2
    exit 2
  fi
  printf '%s' "$resolved_dir"
}

# normalize_issue_id <issue>: the ONE shared issue-arg normalizer + traversal guard.
#
# Every gate funnels the raw issue arg through here UNCONDITIONALLY, at the TOP of
# the function, before any early return or path construction. All four gates
# (check_rulings, check_spec, check_stale, and check_security/seccheck) normalize
# up front: seccheck no longer waits for its sensitive branch, so a traversal arg
# fails closed even on a non-sensitive diff (see the inline note at the top of
# check_security). It strips the scope suffix and the "owner/repo#" prefix to
# the bare id, then HARD-REQUIRES that id be purely numeric. A non-numeric id
# (which could carry "../" path-traversal sequences) is a USAGE error: it exits 2
# with a stderr diagnostic and NO preflight-failed token (the same class as
# "no git repo" or "no python3"). This is the single security boundary for
# issue-id to path construction; do not re-implement the %%/## stripping inline
# in a gate and skip the numeric check (that is the exact defense-inconsistency
# this helper exists to prevent). Echoes the validated bare id to stdout.
#
# CALL-SITE CONTRACT — always invoke as TWO separate lines:
#     local issue_id; issue_id="$(normalize_issue_id "$issue")"
# NEVER the combined form `local issue_id="$(normalize_issue_id "$issue")"`: under
# set -e the combined `local` assignment masks the subshell's nonzero exit (bash
# reports the `local` builtin's return code, always 0), so the exit 2 from the
# traversal guard is silently swallowed and the guard becomes a no-op. All four
# call sites use the two-line form; keep it that way on any refactor.
normalize_issue_id() {
  local raw="${1:-}"
  local id="$raw"
  id="${id%%[[:space:]]*}"   # drop " — scope" etc. after the first space
  id="${id##*#}"             # "owner/repo#4" → "4"
  if ! printf '%s' "$id" | grep -qE '^[0-9]+$'; then
    echo "arc-preflight: issue id '${id}' is not a bare numeric id (possible path traversal)" >&2
    exit 2
  fi
  printf '%s' "$id"
}

# ARC_SENSITIVE_CONTENT_RE — the added-line content-token half of
# classify_sensitive_surface's rulebook, hoisted to a single shared
# constant (issue #62, D1) so commit_residual()'s pre-commit secret-shaped-
# content scan can reuse the EXACT same pattern seccheck's classifier already
# applies, rather than hand-maintaining a second copy that could silently drift
# (the same "one shared list, never a second one" discipline the risky-surface-
# classifier-source ruling already established for the path/diff-size legs).
ARC_SENSITIVE_CONTENT_RE='(password|passwd|secret|credential|api[_-]?key|apikey|access[_-]?token|bearer |authorization|authenticate|authoriz|private[_-]?key|begin rsa|begin [a-z ]*private key|set-?cookie|eval\(|exec\(|os\.system|subprocess|child_process|pickle|deserializ|innerhtml|dangerouslysetinnerhtml|document\.write|select .*from |drop table|chmod |chown |sudo |setuid)'

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

# check_dirty_worktree — fail-closed if the working tree has ANY change
# (tracked OR untracked, staged OR unstaged). porcelain output is empty iff clean.
check_dirty_worktree() {
  local status
  status="$(git -C "$REPO" status --porcelain)"
  if [ -n "$status" ]; then
    gate "dirty-worktree" \
      "Uncommitted changes in the working tree. Commit or stash them first (the skill can run 'git stash' for you), then re-run."
  fi
}

# commit_residual <issue> — the commit-before-gates catch-all (issue #62, D1,
# ruling d1-close-seccheck-asymmetry / d1-commit-reconcile-adr0012).
#
# ADR-0012's per-round commits stay the PRIMARY commit mechanism (issue #69):
# every fix agent commits its own round's work as it lands. This function is
# only the EXCEPTION-RESIDUAL catch-all for whatever ADR-0012 didn't already
# capture, called by the skill ONCE, right before govcheck, on the pr-ready
# path — so seccheck/fencecheck/govcheck's `base..HEAD` diffs become correct
# by construction (they only ever saw committed bytes; this closes the gap
# where a build could reach the gates with real, unreviewed content still
# sitting uncommitted).
#
# NEVER `git add -A` (P0 prep finding): the dirty set here is unpredictable,
# so a blanket add could sweep in something that never went through a review
# round. Instead: enumerate the dirty set explicitly via
# `git status --porcelain -z --no-renames` (NUL-safe) and stage EXACTLY the
# paths whose WORKTREE side is still unstaged (porcelain Y column != space,
# which also covers untracked `??`) with `git add -A -- <literal pathspecs>`.
# A path that is ALREADY fully staged (Y == space — e.g. a `D ` staged
# deletion or an `A ` staged add) is deliberately EXCLUDED from that add: its
# index state is already correct for both the `--cached` scan and the
# pathspec-scoped commit, and `git add -A -- :(literal)<deleted-and-staged>`
# would FATAL with `pathspec did not match any files` (rc 128, R2 blocking
# shortcut) — one staged deletion would poison the whole batch, so any OTHER
# committable residual in the same run would be lost too. The commit below is
# still scoped to the FULL enumerated set, so those already-staged paths are
# committed regardless (verified: a scoped `git commit -- <staged-deletion>`
# commits the deletion without a preceding re-add).
#
# Routine release-accounting files (CHANGELOG.md, VERSION, exact repo-root
# path — the SAME allowlist convention check_fence already uses) are excluded
# from the enumerated set on purpose: the doc-release sweep (arc-execute.js
# Phase 4) runs BEFORE this step and deliberately leaves those two dirty for
# /ship to make the final commit (ADR-0012 rule 2 — "the doc-sweep stays
# uncommitted... pre-committing it would put two owners on the release
# notes"). Committing them here would silently reopen exactly that.
#
# Before committing, the staged diff is scanned for secret-shaped content
# using the SAME pattern seccheck's classifier applies to added lines
# (ARC_SENSITIVE_CONTENT_RE, shared — never a second hand-maintained list). A
# hit does NOT auto-commit: unstage and fail CLOSED with a distinct token
# (principle 6 — gates surface failures, never swallow into a plausible
# default) so a maintainer looks at it by hand.
#
# Commit message: `arc(<id>): build output` (d1-commit-message-contract) — the
# SAME `<id>` normalization (normalize_issue_id) every other `arc(<id>): ...`
# label in this loop uses, so the existing salvage-sweep grep
# (`^arc(<id>): `) and reconcile_died's commit-recognizer already catch it.

# _arc_scan_added_for_secrets <unified-diff-text> — echoes the secret-shaped
# added lines (empty output = none) from a unified diff, using the SHARED
# ARC_SENSITIVE_CONTENT_RE so commit_residual's pre-commit and post-commit
# scans apply the IDENTICAL pattern the same way (never a second, drifting
# copy — principle 1). Added content lines only: `^+`, minus the `^+++` file
# header. The trailing `|| true` swallows grep's no-match exit under this
# script's `set -euo pipefail` so an empty result is not treated as an error.
_arc_scan_added_for_secrets() {
  printf '%s\n' "$1" \
    | grep -E '^\+' \
    | grep -vE '^\+\+\+' \
    | grep -iE "$ARC_SENSITIVE_CONTENT_RE" \
    || true
}

# _arc_numstat_has_binary <numstat-text> — echoes the numstat record(s) for any
# binary ADD/MODIFY in a diff (empty output = none) (R3 blocking finding). git's
# `--numstat` prints a "-<TAB>-<TAB><path>" record for a file it classifies as
# binary (a NUL byte in the first ~8000 bytes), in place of the usual
# "<added><TAB><deleted>" line counts — so a binary residual is recognizable by
# its "-\t-" counts ALONE, structurally, independent of the path text and of
# locale (numstat's dashes and tabs are never gettext-translated, unlike git's
# "Binary files ... differ" summary line an earlier version of this gate matched).
# _arc_scan_added_for_secrets is structurally blind to binary content (git emits
# NO ^+ content lines for a binary file), so a credential embedded in an
# accidentally-binary .env, a keystore, or a downloaded artifact would sail past
# the textual scan. commit_residual therefore treats a binary residual as
# UNSCANNABLE and fails closed (principle 6 — gates surface failures, never
# swallow into a plausible default) rather than auto-committing unreviewed bytes.
#
# DELETIONS are excluded by git STATUS, not by path text (R3 blocking finding).
# Both call sites feed numstat produced with `--diff-filter=d`, which drops a pure
# DELETION by its change status (D). The earlier version instead excluded a
# deletion by suffix-matching ` and /dev/null differ` on the summary line — but
# that suffix is path-ambiguous: a binary ADD whose path ends in ` and /dev/null`
# renders as `Binary files /dev/null and b/<path> differ`, i.e. ALSO ends in
# ` and /dev/null differ`, so it was wrongly excluded and its unreviewed bytes
# auto-committed — the exact case the gate exists to block. `--diff-filter=d` has
# no such ambiguity: a binary DELETION (a stale image/fixture/compiled artifact —
# zero new bytes to scan or hide a secret in) is legitimately committable and is
# dropped; a binary ADD/MODIFY (status A/M) is kept and blocks, at ANY path.
# `--no-renames` splits a rename so its add-half still blocks. `--no-textconv`
# stops a .gitattributes textconv driver from converting binary bytes into
# scannable line counts that would mask the "-\t-" binary signal. The trailing
# `|| true` swallows grep's no-match exit under this script's `set -euo pipefail`.
_arc_numstat_has_binary() {
  printf '%s\n' "$1" | grep -E "$(printf '^-\t-\t')" || true
}

# _arc_assert_head_at <expected_sha> — fail-closed guard for the undo-on-hit
# resets in commit_residual (issue #62, R5 blocking finding). The resets rewind
# HEAD to old_head, which is only safe while HEAD is still the residual commit we
# just created. Because this runs against the maintainer's real checkout under an
# advisory lock, a concurrent commit could land in the window between the commit
# and a reset; an unscoped reset would then silently drop it. Call this
# immediately before each reset: if HEAD no longer matches the commit we made, do
# NOT reset (that would discard whatever landed on top) — gate closed with a
# distinct token and tell the maintainer to reconcile by hand.
_arc_assert_head_at() {
  local expected="$1" current
  if ! current="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || [ "$current" != "$expected" ]; then
    gate "dirty-residual-head-moved" \
      "The residual commit's committed content tripped the secret/binary scan and needs to be undone, but HEAD moved unexpectedly after the commit (expected ${expected:-<unknown>}, found ${current:-<unresolved>}) — a concurrent commit likely landed on the branch. Refusing to reset, which would silently drop whatever landed on top. Reconcile by hand: inspect 'git log'/'git status', remove or redact the flagged content from the residual commit yourself (e.g. 'git rebase' or 'git revert'), then re-run."
  fi
}

# _arc_path_covered <committed-path> <enumerated-path...> — is the committed
# path covered by one of the enumerated residual paths (issue #62, R4 P0)? This
# mirrors git PATHSPEC semantics, NOT exact-string membership: the enumerated set
# comes from `git status --porcelain`, which collapses an untracked DIRECTORY to a
# single `dir/` entry, while the post-commit name-only diff reports the individual
# FILES under it (e.g. enumerated `lib/`, committed `lib/VERSION`). The residual was
# staged + committed with `:(literal)<path>` pathspecs, which match a path OR
# anything beneath it as a directory prefix — so a committed file is "expected" iff
# it EQUALS an enumerated path or lives UNDER one. Bash 3.2-safe (no associative
# arrays — the kit runs on stock macOS bash). The `"$base"/*` case pattern quotes
# base so its own glob metacharacters are matched literally; only the trailing `/*`
# is a wildcard, so a sibling like `impl.js.bak` is never mis-covered by `impl.js`.
_arc_path_covered() {
  local needle="$1"; shift
  local p base
  for p in "$@"; do
    base="${p%/}"                 # strip the trailing slash porcelain puts on a dir
    [ "$needle" = "$base" ] && return 0
    case "$needle" in
      "$base"/*) return 0 ;;
    esac
  done
  return 1
}

commit_residual() {
  local issue="${1:-}"
  if [ -z "$issue" ]; then
    echo "arc-preflight: commit-residual requires an <issue> argument" >&2
    exit 2
  fi
  local issue_id
  issue_id="$(normalize_issue_id "$issue")"

  local status
  status="$(git -C "$REPO" status --porcelain)"
  if [ -z "$status" ]; then
    ok "commit-residual (clean tree, nothing to commit)"
    return 0
  fi

  local statusfile
  statusfile="$(mktemp "${TMPDIR:-/tmp}/arc-residual-status.XXXXXX")" || {
    echo "arc-preflight: commit-residual: cannot create temp file for status scan" >&2
    exit 2
  }
  if ! git -C "$REPO" status --porcelain=v1 -z --no-renames > "$statusfile"; then
    rm -f "$statusfile"
    echo "arc-preflight: commit-residual: 'git status --porcelain -z' failed" >&2
    exit 2
  fi

  # --no-renames turns a rename into a plain delete+add pair in the porcelain
  # listing, so every record here is a SINGLE NUL-terminated "XY<sp>path" token
  # (no rename continuation field to parse) — correct-enough for staging
  # purposes; we don't need rename identity, only the set of touched paths.
  local paths=() add_paths=() path entry ycol
  while IFS= read -r -d '' entry; do
    [ -z "$entry" ] && continue
    # porcelain -z record is "XY<sp>path"; Y (index 1) is the WORKTREE column.
    ycol="${entry:1:1}"
    path="${entry:3}"
    case "$path" in
      CHANGELOG.md|VERSION) continue ;;
    esac
    paths+=( "$path" )
    # Only a path with an unstaged worktree side (Y != space, which also covers
    # untracked `??`) needs (re)staging. An already-fully-staged path (Y ==
    # space) is correct in the index as-is, and re-adding a staged-and-deleted
    # path would FATAL the whole batch (R2 blocking shortcut). The commit below
    # still covers the FULL enumerated set, so it is committed regardless.
    if [ "$ycol" != " " ]; then
      add_paths+=( "$path" )
    fi
  done < "$statusfile"
  rm -f "$statusfile"

  if [ "${#paths[@]}" -eq 0 ]; then
    ok "commit-residual (only CHANGELOG.md/VERSION dirty — left for /ship, nothing else to commit)"
    return 0
  fi

  local pathspecs=()
  for path in "${paths[@]}"; do
    pathspecs+=( ":(literal)${path}" )
  done
  # Stage ONLY the worktree-dirty subset. If every committable path is already
  # fully staged (add_paths empty — e.g. a lone staged deletion), skip the add
  # entirely: expanding an empty array under `set -u` would abort, and there is
  # nothing to (re)stage. A genuine `git add` failure still fails loud (principle 6).
  if [ "${#add_paths[@]}" -gt 0 ]; then
    local add_pathspecs=()
    for path in "${add_paths[@]}"; do
      add_pathspecs+=( ":(literal)${path}" )
    done
    if ! git -C "$REPO" add -A -- "${add_pathspecs[@]}"; then
      echo "arc-preflight: commit-residual: 'git add' failed for the enumerated residual paths" >&2
      exit 2
    fi
  fi

  # Pre-commit early scan, scoped to EXACTLY the pathspecs about to be committed
  # (issue #62, R2 blocking finding). An UNSCOPED `git diff --cached` diffs the
  # whole index against HEAD, so it would also see any pre-staged CHANGELOG.md/
  # VERSION — files this commit deliberately EXCLUDES (left for /ship). An
  # ordinary release note ("Add password reset flow", "Fix authorization bug")
  # in a staged CHANGELOG would then spuriously fire dirty-residual-sensitive,
  # AND the remedy below (which names ${paths[@]}, the committable set) would
  # point the maintainer at a clean file while the real match sits in a file
  # that is not even being committed. Scoping the scan to ${pathspecs[@]} makes
  # the scan set identical to the commit set, so the two can never disagree.
  # The secret scan reads the UNIFIED diff (its ^+ added-content lines). The
  # binary check reads a separate --numstat of the SAME staged pathspecs (R3
  # blocking finding): binary detection is now structural ("-\t-" counts +
  # --diff-filter=d), not a match on git's translatable "Binary files ... differ"
  # summary, so it no longer needs a locale pin. LC_ALL=C LANGUAGE=C is retained on
  # the unified diff for output determinism (the same discipline this file applies
  # to `sort`); `--no-textconv` on BOTH commands keeps a .gitattributes textconv
  # driver from masking binary bytes (as scannable text in the unified diff, or as
  # fabricated line counts hiding the "-\t-" signal in numstat).
  local staged_diff diffrc=0
  staged_diff="$(LC_ALL=C LANGUAGE=C git -C "$REPO" diff --no-ext-diff --no-textconv --cached -- "${pathspecs[@]}")" || diffrc=$?
  if [ "$diffrc" -ne 0 ]; then
    echo "arc-preflight: commit-residual: 'git diff --cached' failed (exit ${diffrc})" >&2
    exit 2
  fi
  local staged_numstat nsrc=0
  staged_numstat="$(git -C "$REPO" diff --no-textconv --numstat --diff-filter=d --no-renames --cached -- "${pathspecs[@]}")" || nsrc=$?
  if [ "$nsrc" -ne 0 ]; then
    echo "arc-preflight: commit-residual: 'git diff --cached --numstat' failed (exit ${nsrc})" >&2
    exit 2
  fi
  # Binary residual → unscannable → fail closed (R3 blocking finding). A binary
  # file produces no ^+ lines for the secret scan below to inspect, so scan it
  # FIRST and refuse rather than silently committing bytes no gate could read.
  if [ -n "$(_arc_numstat_has_binary "$staged_numstat")" ]; then
    git -C "$REPO" reset -- "${pathspecs[@]}" >/dev/null 2>&1 || true
    gate "dirty-residual-sensitive" \
      "The uncommitted residual left after the build's review rounds contains a BINARY file (in one of: $(printf '%s ' "${paths[@]}")). Binary content cannot be scanned for secret-shaped strings (an accidental .env or keystore with a NUL byte, or a downloaded artifact, could hide a credential), so the auto-commit refuses it rather than committing unreviewed bytes. Inspect the file by hand and remove it from the tree if it does not belong (or commit it deliberately yourself if it is a legitimate binary asset), then re-run."
  fi
  if [ -n "$(_arc_scan_added_for_secrets "$staged_diff")" ]; then
    git -C "$REPO" reset -- "${pathspecs[@]}" >/dev/null 2>&1 || true
    gate "dirty-residual-sensitive" \
      "The uncommitted residual left after the build's review rounds contains secret-shaped content (a password/token/key-looking string) in one of: $(printf '%s ' "${paths[@]}"). Refusing to auto-commit it. Inspect 'git status'/'git diff' by hand, remove or redact the sensitive content (or commit it deliberately yourself if this is a false positive), then re-run."
  fi

  # Capture the pre-commit HEAD so the post-commit verification below can scan
  # exactly what LANDED, not the staged snapshot scanned above (issue #62, R2
  # P0). A pathspec-scoped `git commit` re-reads the current working-tree bytes
  # of those paths at commit time AND runs the repo's own pre-commit hook —
  # either can inject or rewrite content AFTER the pre-commit scan (lint-staged/
  # husky/prettier-style auto-fixer hooks are ordinary in a drop-into-any-repo
  # kit), so the staged snapshot is not proof of what the commit object holds.
  local old_head
  if ! old_head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"; then
    echo "arc-preflight: commit-residual: cannot resolve HEAD before committing" >&2
    exit 2
  fi

  # Scope the commit to ONLY the enumerated residual pathspecs (issue #62, R2
  # blocking finding). A bare `git commit` with no pathspec commits the WHOLE
  # index, so if anything staged CHANGELOG.md/VERSION into the index before this
  # ran, the earlier CHANGELOG.md|VERSION exclusion would be bypassed and the
  # residual commit would silently take ownership of the release-notes files
  # ADR-0012 rule 2 reserves for /ship. A pathspec-scoped commit builds a temp
  # index from HEAD plus only these paths, so any pre-staged CHANGELOG.md/VERSION
  # can never ride along — the exclusion holds structurally, not by luck.
  if ! git -C "$REPO" commit -q -m "arc(${issue_id}): build output" -- "${pathspecs[@]}"; then
    echo "arc-preflight: commit-residual: 'git commit' failed" >&2
    exit 2
  fi

  # Pin the exact commit we just created (issue #62, R5 blocking finding). The
  # undo-on-secret-hit resets below rewind HEAD to old_head, which is safe ONLY
  # if HEAD is still the residual commit and nothing landed on top of it. This
  # flow runs against the maintainer's REAL checkout (not an isolated worktree)
  # guarded only by an advisory lock, so a human or another tool could land a
  # concurrent commit in the window between here and a reset. An UNSCOPED reset
  # to old_head would then silently drop that concurrent commit from the branch
  # tip too. Capture new_head now; re-assert it before every reset so we never
  # rewind past a commit we did not create.
  local new_head
  if ! new_head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)"; then
    echo "arc-preflight: commit-residual: cannot resolve HEAD after committing" >&2
    exit 2
  fi

  # Verify the ACTUAL committed bytes, not the pre-commit staged snapshot (issue
  # #62, R2 P0), AND that the commit touched ONLY the enumerated residual paths
  # (issue #62, R4 P0). These post-commit scans are deliberately UNSCOPED — no
  # `-- "${pathspecs[@]}"` restriction. A pathspec-scoped `git commit` still runs
  # the repo's own pre-commit hook, and a hook's own `git add <path>` stages into
  # the real index; git folds ANY such path into the resulting commit tree
  # REGARDLESS of the pathspec on the commit command (verified empirically: a hook
  # that `git add`s a brand-new file, or one of the CHANGELOG.md/VERSION paths this
  # function deliberately EXCLUDES, lands it in the commit). A re-scan SCOPED to the
  # pre-hook pathspec list is therefore structurally blind to exactly the paths a
  # hook can inject — the one class of content this re-scan claims to defend against.
  # So the verification is now: (1) diff old_head..HEAD UNSCOPED; (2) assert every
  # committed path is COVERED by the enumerated ${paths[@]} pathspecs (equal-or-under,
  # via _arc_path_covered) — any uncovered path is a hook injection (a new file, or an
  # excluded release-notes file silently reopening the "no second commit owner"
  # guarantee) and fails closed with a distinct token BEFORE the content scans, since
  # even a NON-sensitive injection breaks the guarantee; (3) run the secret/binary
  # scans over that full unscoped
  # diff. On any hit, undo the commit with the SAME SCOPED soft-reset + pathspec
  # unstage the sensitive/binary branches use (HEAD back to old_head, the reviewed
  # residual returned to the working tree unstaged; a hook-injected extra path stays
  # staged for the maintainer to inspect) — never `--hard`, which would destroy the
  # build output. The secret scan reads the unified old_head..HEAD diff; the binary
  # re-check reads a matching --numstat of the same range (R3 blocking finding —
  # structural "-\t-" + --diff-filter=d, no locale dependency). LC_ALL=C on the
  # unified diff is output determinism only; `--no-textconv` on BOTH keeps a
  # textconv driver from masking committed binary bytes.
  local committed_diff cdiffrc=0
  committed_diff="$(LC_ALL=C LANGUAGE=C git -C "$REPO" diff --no-ext-diff --no-textconv "$old_head" HEAD)" || cdiffrc=$?
  if [ "$cdiffrc" -ne 0 ]; then
    echo "arc-preflight: commit-residual: post-commit 'git diff' failed (exit ${cdiffrc})" >&2
    exit 2
  fi
  local committed_numstat cnsrc=0
  committed_numstat="$(git -C "$REPO" diff --no-textconv --numstat --diff-filter=d --no-renames "$old_head" HEAD)" || cnsrc=$?
  if [ "$cnsrc" -ne 0 ]; then
    echo "arc-preflight: commit-residual: post-commit 'git diff --numstat' failed (exit ${cnsrc})" >&2
    exit 2
  fi

  # Path-set assertion (issue #62, R4 P0): the committed tree must touch ONLY paths
  # covered by the enumerated residual pathspecs (_arc_path_covered — equal-or-under,
  # matching the `:(literal)` pathspec the commit itself used, since porcelain
  # enumerates an untracked dir as `dir/` while the diff lists its files). Any
  # committed path NOT so covered was staged by a pre-commit hook's own `git add`
  # after the residual was enumerated/scanned and rode into the commit despite the
  # pathspec-scoped `git commit`. Catch it here, BEFORE the content scans, so even a
  # NON-sensitive hook injection into an excluded file (which the secret/binary scans
  # would pass) is refused. NUL-delimited name-only diff for path safety (mirrors the
  # porcelain -z parse above).
  local committed_paths_file cpfrc=0
  committed_paths_file="$(mktemp "${TMPDIR:-/tmp}/arc-residual-committed.XXXXXX")" || {
    echo "arc-preflight: commit-residual: cannot create temp file for committed-paths scan" >&2
    exit 2
  }
  if ! git -C "$REPO" diff -z --name-only --no-renames "$old_head" HEAD > "$committed_paths_file"; then
    rm -f "$committed_paths_file"
    echo "arc-preflight: commit-residual: post-commit 'git diff --name-only' failed" >&2
    exit 2
  fi
  local unexpected_paths=() cpath
  while IFS= read -r -d '' cpath; do
    [ -z "$cpath" ] && continue
    if ! _arc_path_covered "$cpath" "${paths[@]}"; then
      unexpected_paths+=( "$cpath" )
    fi
  done < "$committed_paths_file"
  rm -f "$committed_paths_file"
  if [ "${#unexpected_paths[@]}" -gt 0 ]; then
    _arc_assert_head_at "$new_head"
    # Same SCOPED two-step undo + swallowed-reset hazard as the content branches
    # below (R2 blocking finding): `reset --soft` removes the commit without
    # touching the index, then a pathspec-scoped `reset` unstages only the reviewed
    # residual paths — the hook-injected extra paths stay staged for inspection, and
    # no unrelated concurrently-staged file is ever swept. Branch the maintainer
    # message on the soft reset's real exit code, never asserting an undo that failed.
    local up_resetrc=0
    git -C "$REPO" reset --soft "$old_head" >/dev/null 2>&1 || up_resetrc=$?
    if [ "$up_resetrc" -ne 0 ]; then
      gate "dirty-residual-undo-failed" \
        "The residual commit touched paths OUTSIDE the reviewed residual set ($(printf '%s ' "${unexpected_paths[@]}")) — a pre-commit hook staged them with its own 'git add' — and the attempt to UNDO that commit FAILED ('git reset --soft ${old_head}' exited ${up_resetrc} — a concurrent process may hold .git/index.lock). The flagged commit is STILL on HEAD (${new_head}); it was NOT removed. Reconcile by hand: reset HEAD back to ${old_head} yourself ('git reset ${old_head}'), inspect the unexpected paths, then re-run."
    fi
    git -C "$REPO" reset -- "${pathspecs[@]}" >/dev/null 2>&1 || true
    gate "dirty-residual-unexpected-path" \
      "The residual commit's ACTUAL committed content touched paths OUTSIDE the reviewed residual set ($(printf '%s ' "${unexpected_paths[@]}")) — a pre-commit hook staged them with its own 'git add', and git folded them into the commit regardless of the pathspec-scoped 'git commit'. Those paths never went through a review round, and an excluded release-notes file (CHANGELOG.md/VERSION) landing here would silently break the 'no second commit owner' guarantee /ship relies on. The commit has been undone and the reviewed residual returned to your working tree, unstaged; the hook-injected paths remain staged for you to inspect. Remove or reconcile them (and consider whether the pre-commit hook should be staging files at all), then re-run."
  fi
  # Post-commit binary check mirrors the pre-commit one (R3 blocking finding): a
  # pre-commit hook or last-moment edit could introduce a binary file AFTER the
  # pre-commit scan, so re-check the committed bytes for an unscannable binary
  # residual too and undo the commit (soft reset off HEAD + scoped unstage) on a hit.
  if [ -n "$(_arc_numstat_has_binary "$committed_numstat")" ]; then
    _arc_assert_head_at "$new_head"
    # Undo the flagged commit with a SCOPED two-step reset (R2 blocking finding):
    # `reset --soft old_head` removes the commit from HEAD without touching the
    # index, then `reset -- pathspecs` unstages ONLY the residual paths (leaving
    # their content in the working tree, unstaged — the documented remedy). A bare
    # `git reset old_head` (a MIXED reset of the WHOLE index) would instead unstage
    # every file staged in the window since old_head — e.g. a pre-commit hook's own
    # side effect, or a concurrent `git add` on the maintainer's real checkout (the
    # same concurrency threat _arc_assert_head_at guards HEAD against, but the index
    # is a distinct surface). Capture the SOFT reset's exit status (R2 blocking
    # finding): a swallowed `|| true` there would let the gate below assert "the
    # commit has been undone" even when the commit-removal FAILED (e.g. a concurrent
    # process holds .git/index.lock), leaving the flagged content in real git
    # history while telling the maintainer it is gone. On a failed soft reset, fail
    # closed with a DISTINCT token stating the sensitive commit is STILL on HEAD
    # (principle 6). The scoped unstage that follows is best-effort (`|| true`,
    # matching the pre-commit undo): if it fails the commit is already off HEAD (the
    # safety-critical step succeeded) and only the residual paths' staged state is
    # left as-is — no unrelated file is ever touched either way.
    local bin_resetrc=0
    git -C "$REPO" reset --soft "$old_head" >/dev/null 2>&1 || bin_resetrc=$?
    if [ "$bin_resetrc" -ne 0 ]; then
      gate "dirty-residual-undo-failed" \
        "The residual commit's ACTUAL committed content contains a BINARY file (in one of: $(printf '%s ' "${paths[@]}")), and the attempt to UNDO that commit FAILED ('git reset --soft ${old_head}' exited ${bin_resetrc} — a concurrent process may hold .git/index.lock). The flagged commit is STILL on HEAD (${new_head}); it was NOT removed. Reconcile by hand: reset HEAD back to ${old_head} yourself ('git reset ${old_head}'), remove the flagged binary file, then re-run."
    fi
    git -C "$REPO" reset -- "${pathspecs[@]}" >/dev/null 2>&1 || true
    gate "dirty-residual-sensitive" \
      "The residual commit's ACTUAL committed content contains a BINARY file (in one of: $(printf '%s ' "${paths[@]}")) — introduced after the pre-commit scan (a pre-commit hook or a last-moment edit). Binary content cannot be scanned for secret-shaped strings, so the commit has been undone and the changes returned to your working tree, unstaged. Inspect the file by hand and remove it if it does not belong (or commit it deliberately yourself if it is a legitimate binary asset), then re-run."
  fi
  if [ -n "$(_arc_scan_added_for_secrets "$committed_diff")" ]; then
    _arc_assert_head_at "$new_head"
    # Same SCOPED two-step undo + swallowed-reset hazard as the binary branch
    # above (R2 blocking finding): `reset --soft` removes the commit without
    # touching the index, then a pathspec-scoped `reset` unstages only the residual
    # paths — never the whole index, so a concurrently-staged unrelated file is
    # preserved. Branch the maintainer-facing claim on the SOFT reset's real exit
    # code instead of unconditionally asserting the commit was undone.
    local sec_resetrc=0
    git -C "$REPO" reset --soft "$old_head" >/dev/null 2>&1 || sec_resetrc=$?
    if [ "$sec_resetrc" -ne 0 ]; then
      gate "dirty-residual-undo-failed" \
        "The residual commit's ACTUAL committed content contains secret-shaped content (a password/token/key-looking string) in one of: $(printf '%s ' "${paths[@]}"), and the attempt to UNDO that commit FAILED ('git reset --soft ${old_head}' exited ${sec_resetrc} — a concurrent process may hold .git/index.lock). The flagged commit is STILL on HEAD (${new_head}); it was NOT removed, so the secret-shaped value is now in real git history and should be treated as exposed (rotate it). Reconcile by hand: reset HEAD back to ${old_head} yourself ('git reset ${old_head}'), remove or redact the sensitive content, then re-run."
    fi
    git -C "$REPO" reset -- "${pathspecs[@]}" >/dev/null 2>&1 || true
    gate "dirty-residual-sensitive" \
      "The residual commit's ACTUAL committed content contains secret-shaped content (a password/token/key-looking string) in one of: $(printf '%s ' "${paths[@]}") — introduced after the pre-commit scan (a pre-commit hook or a last-moment edit). The commit has been undone and the changes returned to your working tree, unstaged. Inspect 'git status'/'git diff' by hand, remove or redact the sensitive content (or commit it deliberately yourself if this is a false positive), then re-run."
  fi
  ok "commit-residual (committed as arc(${issue_id}): build output)"
}

# check_dirty_worktree_postbuild (mode: dirtycheck) — the enforced clean-tree
# ASSERTION that follows commit_residual (issue #62, D1, ruling
# d1-dirty-worktree-gate-surface). A dedicated subcommand, deterministic,
# fail-closed, matching the one-gate-per-concern pattern (seccheck/govcheck/
# speccheck each get their own). Deliberately a DIFFERENT token from
# check_dirty_worktree's own "dirty-worktree" (which fires at build/finish
# START and has a different remedy — "stash your changes"): this assertion
# fires mid-sequence, AFTER commit_residual already tried to commit
# everything committable, so its remedy is "something failed to stage",
# not "go stash".
#
# ARMOR A (d1-dirty-gate-armor-tier): this check NEVER receives a trust
# argument — no signature slot for one — so a future "thread trust through
# every new gate call" pass cannot silently wire a downgrade path through it.
# It always fires, at every trust level.
#
# CHANGELOG.md/VERSION stay excluded here too (same exact-path convention as
# commit_residual and check_fence) — the doc-release sweep leaves them
# legitimately dirty for /ship to own; this assertion must not fail on that.
check_dirty_worktree_postbuild() {
  local status
  status="$(git -C "$REPO" status --porcelain -- . ':!CHANGELOG.md' ':!VERSION')"
  if [ -n "$status" ]; then
    gate "dirty-worktree-postbuild" \
      "The working tree is still dirty after the commit-before-gates step (commit-residual) ran. This should not happen on a normal pass — inspect 'git status' by hand; only CHANGELOG.md/VERSION at the repo root are expected to still be dirty here (left for /ship)."
  fi
  ok "dirtycheck (working tree is clean, aside from any routine CHANGELOG.md/VERSION residual left for /ship)"
}

# check_rulings <issue> — the rulings-required gate with the RICHER schema
# (ruling rulings-artifact-schema = Option 2). The rulings file is
# .gstack/arc-rulings/<issue>-pr1-args.json. Validate:
#   - file exists                                   → missing-rulings
#   - parses as JSON                                → incomplete-rulings
#   - `issue` field's leading id matches the CLI
#     <issue> arg (no wrong-issue rulings)          → incomplete-rulings
#   - has non-empty `decisions` map                 → incomplete-rulings
#   - has `ruledAt` (timestamp)                      → incomplete-rulings
#   - has non-empty `discoveryRunId` string          → incomplete-rulings
#   - has non-empty `forkIds` array whenever
#     `decisions` is non-empty, every id keyed in
#     `decisions`                                    → incomplete-rulings
#   - EXCEPTION (issue #62, D2): `decisions:{}` + `forkIds:[]` together are
#     valid iff `noTierA === true` (strict boolean, not truthy) AND
#     `noTierAReason` is a non-empty single-line string — an explicit,
#     machine-written marker for a genuinely zero-Tier-A discovery. `ruledAt`
#     and non-empty `discoveryRunId` are still required unconditionally in
#     this shape too, so `check_stale` still applies unchanged.
check_rulings() {
  local issue="$1"
  if [ -z "$issue" ]; then
    gate "missing-rulings" \
      "No issue number passed to the rulings gate. Usage: arc-preflight.sh build <issue>."
  fi

  # Normalize the issue arg to its bare id BEFORE building the filename, so the
  # rulings path matches what discovery wrote regardless of arg form:
  # "4", "4 — scope", "owner/repo#4 — scope" all resolve to id "4". The shared
  # helper also enforces the bare id is numeric (traversal guard); a non-numeric
  # id exits 2 here, the same as in check_stale/check_spec/seccheck.
  local issue_id
  issue_id="$(normalize_issue_id "$issue")"

  local rulings="$REPO/.gstack/arc-rulings/${issue_id}-pr1-args.json"
  if [ ! -f "$rulings" ]; then
    gate "missing-rulings" \
      "No rulings for issue ${issue_id} (expected ${rulings#"$REPO"/}). Run '/arc discovery ${issue_id}' and rule the Tier-A forks first."
  fi

  if [ -z "$JSON_RUNNER" ]; then
    echo "arc-preflight: neither python3 nor node available to validate rulings JSON" >&2
    exit 2
  fi

  # Validate the schema in the chosen runner. Exit codes:
  #   0 = valid; 3 = parse error / schema-incomplete. Reason goes to stderr.
  local rc=0
  if [ "$JSON_RUNNER" = "python3" ]; then
    python3 - "$rulings" "$issue_id" <<'PY' || rc=$?
import json, re, sys

path = sys.argv[1]
cli_issue = sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except Exception as exc:  # parse error
    sys.stderr.write("rulings file is not valid JSON: %s\n" % exc)
    sys.exit(3)

if not isinstance(data, dict):
    sys.stderr.write("rulings root is not an object\n")
    sys.exit(3)

def issue_id(raw):
    # The issue may be "4", "4 — scope", or "dep0we/dev-process-kit#4 — scope".
    # Extract the issue id: the number after a '#' if present, else the first
    # run of digits. Returns None if no id can be extracted.
    s = str(raw).strip()
    m = re.search(r"#(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"\d+", s)
    if m:
        return m.group(0)
    return None

ruling_issue = data.get("issue")
if not ruling_issue:
    sys.stderr.write("rulings 'issue' field is missing\n")
    sys.exit(3)

want = issue_id(cli_issue)
got = issue_id(ruling_issue)
if want is None or got is None or want != got:
    sys.stderr.write(
        "rulings 'issue' (%r → id %r) does not match the build's issue arg (%r → id %r)\n"
        % (ruling_issue, got, cli_issue, want)
    )
    sys.exit(3)

decisions = data.get("decisions")
fork_ids = data.get("forkIds")
discovery_run_id = data.get("discoveryRunId")
no_tier_a = data.get("noTierA")
no_tier_a_reason = data.get("noTierAReason")

# D2 (issue #62, ruling notiera-shape-vs-deadlock): an explicit, machine-written
# marker for a genuinely zero-Tier-A discovery. Flat booleans + reason;
# decisions:{} is allowed ONLY when this shape is exactly right. Strict `is True`
# (never a truthy check) so a string "false"/1/"true" can never slip past this as
# if it were the real boolean (a negative control this gate must reject). This
# branch BYPASSES the decisions/forkIds-non-empty checks below, but every OTHER
# required field (ruledAt, discoveryRunId) still applies unconditionally
# (d2-required-fields-in-notiera-shape, opt2) — check_stale must still be able to
# bite on a noTierA rulings file exactly as it does on a decisions-bearing one.
if no_tier_a is True:
    if not isinstance(no_tier_a_reason, str) or no_tier_a_reason.strip() == "":
        sys.stderr.write("rulings 'noTierAReason' is required (a non-empty string) when 'noTierA' is true\n")
        sys.exit(3)
    if "\n" in no_tier_a_reason or "\r" in no_tier_a_reason:
        sys.stderr.write("rulings 'noTierAReason' must be a single line (no embedded newline)\n")
        sys.exit(3)
    if not isinstance(decisions, dict) or len(decisions) != 0:
        sys.stderr.write("rulings 'noTierA' is true but 'decisions' is not an empty object — noTierA requires decisions:{}\n")
        sys.exit(3)
    if not isinstance(fork_ids, list) or len(fork_ids) != 0:
        sys.stderr.write("rulings 'noTierA' is true but 'forkIds' is not an empty array — noTierA requires forkIds:[]\n")
        sys.exit(3)
    if not data.get("ruledAt"):
        sys.stderr.write("rulings 'ruledAt' timestamp is missing (re-run discovery to write the richer schema)\n")
        sys.exit(3)
    if not isinstance(discovery_run_id, str) or discovery_run_id.strip() == "":
        sys.stderr.write("rulings 'discoveryRunId' is missing or empty (re-run discovery to write the richer schema)\n")
        sys.exit(3)
    sys.exit(0)

if not isinstance(decisions, dict) or len(decisions) == 0:
    sys.stderr.write("rulings 'decisions' map is missing or empty\n")
    sys.exit(3)

if not data.get("ruledAt"):
    sys.stderr.write("rulings 'ruledAt' timestamp is missing (re-run discovery to write the richer schema)\n")
    sys.exit(3)

if not isinstance(discovery_run_id, str) or discovery_run_id.strip() == "":
    sys.stderr.write("rulings 'discoveryRunId' is missing or empty (re-run discovery to write the richer schema)\n")
    sys.exit(3)

if not isinstance(fork_ids, list):
    sys.stderr.write("rulings 'forkIds' array is missing (re-run discovery to write the richer schema)\n")
    sys.exit(3)

# Coverage of an empty forkIds list is vacuous: if there are decisions, there
# must be forks they answer.
if len(fork_ids) == 0:
    sys.stderr.write("rulings 'forkIds' is empty but 'decisions' is non-empty — coverage is vacuous (re-run discovery)\n")
    sys.exit(3)

missing = [fid for fid in fork_ids if fid not in decisions]
if missing:
    sys.stderr.write("rulings 'forkIds' not all covered by 'decisions': %s\n" % ", ".join(map(str, missing)))
    sys.exit(3)

sys.exit(0)
PY
  else
    node - "$rulings" "$issue_id" <<'NODE' || rc=$?
const fs = require("fs");
const path = process.argv[2];
const cliIssue = process.argv[3];
let data;
try {
  data = JSON.parse(fs.readFileSync(path, "utf8"));
} catch (exc) {
  process.stderr.write("rulings file is not valid JSON: " + exc.message + "\n");
  process.exit(3);
}
if (typeof data !== "object" || data === null || Array.isArray(data)) {
  process.stderr.write("rulings root is not an object\n");
  process.exit(3);
}
// The issue may be "4", "4 — scope", or "dep0we/dev-process-kit#4 — scope".
// Extract the id: the number after a '#' if present, else the first run of
// digits. Returns null if none.
function issueId(raw) {
  const s = String(raw).trim();
  let m = s.match(/#(\d+)/);
  if (m) return m[1];
  m = s.match(/\d+/);
  if (m) return m[0];
  return null;
}
const rulingIssue = data.issue;
if (!rulingIssue) {
  process.stderr.write("rulings 'issue' field is missing\n");
  process.exit(3);
}
const want = issueId(cliIssue);
const got = issueId(rulingIssue);
if (want === null || got === null || want !== got) {
  process.stderr.write(
    "rulings 'issue' (" + JSON.stringify(rulingIssue) + " → id " + JSON.stringify(got) +
    ") does not match the build's issue arg (" + JSON.stringify(cliIssue) + " → id " + JSON.stringify(want) + ")\n"
  );
  process.exit(3);
}
const decisions = data.decisions;
const forkIds = data.forkIds;
const discoveryRunId = data.discoveryRunId;
const noTierA = data.noTierA;
const noTierAReason = data.noTierAReason;

// D2 (issue #62, ruling notiera-shape-vs-deadlock): an explicit, machine-written
// marker for a genuinely zero-Tier-A discovery. Flat booleans + reason;
// decisions:{} is allowed ONLY when this shape is exactly right. Strict `=== true`
// (never a truthy check) so a string "false"/1/"true" can never slip past this as
// if it were the real boolean (a negative control this gate must reject). This
// branch BYPASSES the decisions/forkIds-non-empty checks below, but every OTHER
// required field (ruledAt, discoveryRunId) still applies unconditionally
// (d2-required-fields-in-notiera-shape, opt2) — check_stale must still be able to
// bite on a noTierA rulings file exactly as it does on a decisions-bearing one.
if (noTierA === true) {
  if (typeof noTierAReason !== "string" || noTierAReason.trim() === "") {
    process.stderr.write("rulings 'noTierAReason' is required (a non-empty string) when 'noTierA' is true\n");
    process.exit(3);
  }
  if (noTierAReason.includes("\n") || noTierAReason.includes("\r")) {
    process.stderr.write("rulings 'noTierAReason' must be a single line (no embedded newline)\n");
    process.exit(3);
  }
  if (typeof decisions !== "object" || decisions === null || Array.isArray(decisions) || Object.keys(decisions).length !== 0) {
    process.stderr.write("rulings 'noTierA' is true but 'decisions' is not an empty object — noTierA requires decisions:{}\n");
    process.exit(3);
  }
  if (!Array.isArray(forkIds) || forkIds.length !== 0) {
    process.stderr.write("rulings 'noTierA' is true but 'forkIds' is not an empty array — noTierA requires forkIds:[]\n");
    process.exit(3);
  }
  if (!data.ruledAt) {
    process.stderr.write("rulings 'ruledAt' timestamp is missing (re-run discovery to write the richer schema)\n");
    process.exit(3);
  }
  if (typeof discoveryRunId !== "string" || discoveryRunId.trim() === "") {
    process.stderr.write("rulings 'discoveryRunId' is missing or empty (re-run discovery to write the richer schema)\n");
    process.exit(3);
  }
  process.exit(0);
}

if (typeof decisions !== "object" || decisions === null || Array.isArray(decisions) || Object.keys(decisions).length === 0) {
  process.stderr.write("rulings 'decisions' map is missing or empty\n");
  process.exit(3);
}
if (!data.ruledAt) {
  process.stderr.write("rulings 'ruledAt' timestamp is missing (re-run discovery to write the richer schema)\n");
  process.exit(3);
}
if (typeof discoveryRunId !== "string" || discoveryRunId.trim() === "") {
  process.stderr.write("rulings 'discoveryRunId' is missing or empty (re-run discovery to write the richer schema)\n");
  process.exit(3);
}
if (!Array.isArray(forkIds)) {
  process.stderr.write("rulings 'forkIds' array is missing (re-run discovery to write the richer schema)\n");
  process.exit(3);
}
// Coverage of an empty forkIds list is vacuous: if there are decisions, there
// must be forks they answer.
if (forkIds.length === 0) {
  process.stderr.write("rulings 'forkIds' is empty but 'decisions' is non-empty — coverage is vacuous (re-run discovery)\n");
  process.exit(3);
}
const missing = forkIds.filter((fid) => !(fid in decisions));
if (missing.length) {
  process.stderr.write("rulings 'forkIds' not all covered by 'decisions': " + missing.join(", ") + "\n");
  process.exit(3);
}
process.exit(0);
NODE
  fi

  if [ "$rc" -eq 3 ]; then
    gate "incomplete-rulings" \
      "Rulings for issue ${issue} are incomplete or stale (see the reason above). Re-run '/arc discovery ${issue}' to regenerate the full ruling set."
  elif [ "$rc" -ne 0 ]; then
    echo "arc-preflight: rulings validator exited with code ${rc}" >&2
    exit 2
  fi
}

# snapshot_state — emit a deterministic fingerprint of the repo to stdout.
# Captures all six legs of the read-only invariant:
#   1. HEAD commit
#   2. porcelain-visible workspace state (tracked + untracked, staged + unstaged)
#   3. a content checksum of every tracked file (path + hash), NUL-safe.
#   4. a content checksum of the arc TOOLING files specifically. Under the self-contained
#      model (issue #47) these dirs (.claude/workflows/, .claude/skills/arc/) are COMMITTED
#      and porcelain-VISIBLE, so this leg OVERLAPS the TRACKED leg for committed files —
#      retained as install-shape-independent defense-in-depth, and load-bearingly because it
#      is the only leg that catches a freshly-CREATED untracked tooling file a discovery
#      sub-agent drops into those dirs (and it still uniquely covers any pre-#47/migration
#      shape where the dirs remain gitignored/porcelain-blind). Bounded to those exact paths
#      (NOT all ignored files — no node_modules). Skipped if absent. See the ARC-TOOLING
#      header in snapshot_state() below for the full model.
#   5. a content checksum of the arc RULINGS/gate files under .gstack/arc-rulings/ (EXCEPT
#      the append-only decision ledger). Gitignored → porcelain-blind, yet these are the gate
#      inputs a later /arc build trusts, so a sub-agent write must surface. See the ARC-RULINGS
#      header in snapshot_state() below.
#   6. a content checksum of the local trust-override file(s), .gstack/*.local.json — each
#      entry captured by ONE type-atomic pass as either a regular-file content hash or, for a
#      symlinked override, its readlink target text (`SYMLINK <path> <target>`; issue #118).
#      Gitignored → porcelain-blind, sits directly under .gstack/ (missed by
#      the arc-rulings leg) and is not .claude/ tooling (missed by the tooling leg), yet it
#      is a SECURITY-DECISION input the skill reads every /arc run. Without this leg a
#      discovery sub-agent could plant a solo-trust override undetected. See the
#      ARC-LOCAL-CONFIG header in snapshot_state() below.
snapshot_state() {
  # Pick a hasher once; both shasum (BSD/macOS) and sha256sum (GNU/Linux) accept
  # files on argv.
  local hasher
  if command -v shasum >/dev/null 2>&1; then
    hasher="shasum -a 256"
  elif command -v sha256sum >/dev/null 2>&1; then
    hasher="sha256sum"
  else
    echo "arc-preflight: no shasum/sha256sum available for the read-only checksum" >&2
    exit 2
  fi

  printf 'HEAD\t%s\n' "$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo NO-HEAD)"
  printf 'PORCELAIN\n'
  git -C "$REPO" status --porcelain
  printf 'TRACKED\n'
  # Hash each tracked file's content. ls-files -z is NUL-delimited so paths with
  # spaces/newlines are safe. Sort for determinism across platforms.
  # xargs -0 handles NUL-delimited paths and the no-files case (xargs runs the
  # command zero times if there's no input → empty checksum block, which is fine).
  git -C "$REPO" ls-files -z | ( cd "$REPO" && xargs -0 $hasher 2>/dev/null ) | LC_ALL=C sort

  printf 'ARC-TOOLING\n'
  # Fingerprint the arc tooling files specifically.
  # Under the self-contained model (issue #47) the kit COMMITS the
  # .claude/ tooling — INCLUDING the --link self-host entry points (mode-120000 symlinks,
  # tracked) — and removes the old .gitignore for these paths in ALL install shapes. So
  # the committed tooling is porcelain-VISIBLE and is ALSO hashed by the TRACKED leg above
  # (the leg follows each tracked symlink to its content). This leg is therefore defense-
  # in-depth OVERLAP on BOTH install shapes, not "the only leg that covers them."
  # Why keep it anyway: it is install-shape-INDEPENDENT, and — load-bearingly — it is the
  # leg that catches a NEWLY-CREATED untracked file a discovery sub-agent (cwd=REAL_REPO,
  # attacker-reachable) drops into .claude/workflows/ or .claude/skills/arc/. The TRACKED
  # leg only sees committed paths, so a brand-new untracked tooling file would be invisible
  # there. Do NOT remove this leg to "simplify" the redundant committed-file overlap.
  #
  # Bounded to: .claude/workflows/*.js, .claude/workflows/*.sh, and
  # .claude/skills/arc/ files (SKILL.md + arc-elicitation.md + any *.sh/*.js/*.json there).
  # Hash paths relative to REPO for stability; sort for determinism. Any present
  # file that changes content (or appears/disappears) shifts this block →
  # discovery-mutated.
  # NOTE: shadow-compare.js, decision-ledger.sh, and clarity-gate.js live in
  # .claude/workflows/ and are covered by the *.js / *.sh globs below.
  # arc-elicitation.md is covered by .claude/skills/arc/*.md. clarity-lint.js
  # (issue #145 — the live-chat pre-lint tool) and clarity-killlist.json (the
  # shared jargon wordlist) are the first non-*.md/*.sh files ever placed directly
  # in .claude/skills/arc/, so the *.js / *.json arms below were ADDED for them —
  # without those two arms, a discovery sub-agent (cwd=REAL_REPO, attacker-
  # reachable) could tamper with either file undetected by this leg (the TRACKED
  # leg above only catches an ALREADY-committed file's content changing, not a
  # brand-new untracked file). This ensures any tamper with the new clarity-gate
  # tooling is caught by discovery-verify without modifying this coverage comment.
  (
    cd "$REPO" || exit 0
    arc_tooling=()
    # SKILL.md is covered by .claude/skills/arc/*.md — do NOT also list it explicitly
    # (that hashed it twice). The *.sh glob covers any future arc skill scripts.
    for p in .claude/workflows/*.js .claude/workflows/*.sh .claude/skills/arc/*.md .claude/skills/arc/*.sh .claude/skills/arc/*.js .claude/skills/arc/*.json; do
      [ -f "$p" ] && arc_tooling+=("$p")
    done
    if [ "${#arc_tooling[@]}" -gt 0 ]; then
      # Dedupe defensively IN-SHELL so an overlapping glob can never double-count a file,
      # then emit each path NUL-delimited via `printf '%s\0'` straight into xargs -0. We
      # deliberately do NOT pipe through `printf '%s\n' | sort -u | tr '\n' '\0'`:
      # .claude/workflows/ is writable by a discovery sub-agent (cwd=REAL_REPO) in EVERY
      # install shape, so a NEWLINE-named file (e.g. evil$'\n'x.js) the sub-agent CREATES is
      # attacker-reachable regardless of whether the kit's own committed tooling is tracked.
      # (Under the self-contained model that committed tooling IS tracked and porcelain-
      # visible — see the ARC-TOOLING header above — so this NUL-safety is NOT about the
      # committed files; it is about a freshly-CREATED untracked newline-named file.) Keep
      # the NUL-safe path install-shape-independent — do NOT remove it to "simplify."
      # Routing paths through a newline
      # record separator would split a newline-named path into bogus fragments, xargs -0
      # would stat nonexistent paths and exit non-zero, and under pipefail that would
      # silently abort discovery_verify with no token. The glob already produced intact
      # array elements; in-shell dedupe + `%s\0` keeps embedded newlines whole. The dedupe
      # is a linear scan over a PLAIN array, NOT an associative array: macOS ships bash 3.2
      # (no `declare -A`), so an assoc-array dedupe would itself be a portability regression
      # — the exact class this leg is being hardened against. The list is a handful of
      # tooling files, so the O(n^2) scan is free. The trailing `LC_ALL=C sort` on the
      # HASH-LINE output supplies determinism, so the pre-hash path order is irrelevant.
      local arc_uniq=()
      local atp seen i
      for atp in "${arc_tooling[@]}"; do
        seen=0
        # Index-based scan (NOT `for prev in ${arc_uniq[@]}`): an unquoted array
        # expansion would word-split a path containing a space/newline and mis-compare —
        # the exact NUL-safety class this leg is hardening. Indexing dereferences each
        # element whole.
        for (( i=0; i<${#arc_uniq[@]}; i++ )); do
          [ "${arc_uniq[$i]}" = "$atp" ] && { seen=1; break; }
        done
        [ "$seen" -eq 0 ] && arc_uniq+=("$atp")
      done
      printf '%s\0' "${arc_uniq[@]}" | xargs -0 $hasher 2>/dev/null | LC_ALL=C sort
    fi
  )

  printf 'ARC-RULINGS\n'
  # Fingerprint the arc RULINGS/gate files (issue #6, P1). .gstack/arc-rulings/ is
  # gitignored → porcelain-blind, so without this leg a discovery sub-agent
  # (cwd=REAL_REPO) could permanently WRITE or modify future-issue gate files —
  # <N>-spec-receipt.json, <N>-security-receipt.json, <N>-pr1-args.json,
  # <N>-latest-discovery.json — and discovery-verify would never see it, because
  # those files are the exact inputs a later /arc build's maintainer/spec/staleness
  # gates trust. Hashing the dir's contents closes that hole. SAFE re-ordering
  # note: the trusted skill writes this issue's <N>-latest-discovery.json marker only
  # AFTER discovery-verify returns preflight-ok (SKILL.md step 5), so the marker write
  # happens past the fingerprint comparison and never self-trips the gate; every other
  # rulings/receipt file is written by a human or a build, never by the discovery fire.
  #
  # EXCLUDED: the decision ledger (decisions.jsonl). Per ADR-0007 the shadow-compare /
  # predict step "does NOT modify arc-discovery or its read-only contract" — the ledger
  # is an APPEND-ONLY artifact the TRUSTED skill writes as a legitimate decision-
  # delegation action, NOT a gate input a build's maintainer/spec/staleness gates trust.
  # Folding it into the read-only fingerprint would make a legitimate ledger append look
  # like a discovery mutation (the design tension that broke the gitignored-ledger
  # behavioral test). The ledger's integrity is guarded separately: it is append-only,
  # gitignored, and the trusted skill (never the discovery fire) owns its score/ruling
  # fields. So we hash every rulings/gate file EXCEPT decisions.jsonl.
  # Paths relative to REPO for stability; sort for determinism. Skipped if the dir is
  # absent (a repo that has never run /arc).
  (
    cd "$REPO" || exit 0
    if [ -d .gstack/arc-rulings ]; then
      # Emit NUL-delimited paths straight from the source (`find -print0`), then hash via
      # xargs -0, then sort the HASH-LINE output for determinism. We deliberately do NOT
      # use `sort -z` (a GNU coreutils extension absent on some minimal Linux/CI images;
      # inside this `$(...)` subshell a `sort -z` that exits non-zero is silently swallowed
      # under pipefail, emitting an EMPTY ARC-RULINGS block so discovery-pre/verify match on
      # ANY rulings change — voiding the gate). We ALSO do NOT reconstruct NUL delimiters
      # with `tr '\n' '\0'`: .gstack/arc-rulings/ is gitignored → porcelain-blind and
      # WRITABLE by a discovery sub-agent (cwd=REAL_REPO), so a maliciously NEWLINE-named
      # file is attacker-reachable; `tr '\n' '\0'` would split it into bogus path fragments,
      # `xargs -0` would stat nonexistent paths and exit non-zero, and under pipefail that
      # would SILENTLY abort discovery_verify with no token (violating the fail-LOUD gate
      # contract). `-print0` carries embedded newlines through intact, exactly like the
      # ls-files -z TRACKED leg above. Path order is irrelevant because the trailing
      # `LC_ALL=C sort` reorders the resulting hash lines deterministically — so portability
      # is satisfied without `sort -z` and newline-safety without `tr`. The decision ledger
      # is excluded by name (a legitimate trusted-skill append, not a discovery mutation —
      # see above); xargs runs the hasher zero times on an empty dir (empty block, fine).
      find .gstack/arc-rulings -type f ! -name 'decisions.jsonl' -print0 2>/dev/null \
        | xargs -0 $hasher 2>/dev/null \
        | LC_ALL=C sort
    fi
  )

  printf 'ARC-LOCAL-CONFIG\n'
  # Fingerprint the local trust-override file(s): .gstack/*.local.json (issue #118, L2).
  # This file is a SECURITY-DECISION input — its `trust` key downgrades the hardened
  # armor to solo (plain forgeable snapshot, thinned realpath containment, no worktree
  # scaffold) and the skill reads it on EVERY /arc invocation. But it sits DIRECTLY under
  # .gstack/ (maxdepth 1, NOT under arc-rulings/, so the ARC-RULINGS leg misses it), it is
  # gitignored via `.gstack/*.local.json` (porcelain-blind), and it is not .claude/ tooling
  # (the ARC-TOOLING leg misses it). Without this leg the file is in NO leg of the net-state
  # fingerprint, so a discovery sub-agent (cwd=REAL_REPO, attacker-reachable — the same class
  # the keyed MAC and the ARC-RULINGS leg were added to cover) could PLANT a solo-trust
  # override between discovery-pre and discovery-verify, discovery-verify would return
  # preflight-ok, and every subsequent /arc run would silently resolve trust=solo though the
  # maintainer never opted in. A LEGITIMATE maintainer opt-in file is stable across the
  # discovery run window, so discovery-pre and discovery-verify hash it identically and it
  # never false-trips; only a create/modify/delete DURING the run (the attack) shifts this
  # block → discovery-mutated. Same NUL-safe/-print0/LC_ALL=C-sort shape as the ARC-RULINGS
  # leg above: `.gstack/` is writable by a discovery sub-agent, so a newline-named
  # `*.local.json` is attacker-reachable and must be carried through intact (do NOT
  # reconstruct NUL with `tr`, do NOT use `sort -z`). maxdepth 1 keeps this bounded to the
  # top-level override, matching the skill's config-load (which reads only the top-level
  # file) and the gitignore pattern (`.gstack/*.local.json`, non-recursive). xargs runs the
  # hasher zero times when no such file exists (empty block, deterministic — no false trip).
  #
  # TYPE-ATOMIC single pass (issue #118): ONE `find` matches every `*.local.json` entry (no
  # `-type` filter), and each entry's type is classified INSIDE the read loop with a single
  # stat, so an entry is captured by exactly one branch based on its state at loop time. An
  # earlier shape used TWO separate `find` runs — `-type f` (content hash) then `-type l`
  # (symlink target text). A file that was a symlink when the first `find` ran and a regular
  # file when the second ran (or vice versa) could fall in the wide window BETWEEN the two
  # find process launches and appear in NEITHER scan, vanishing from this fingerprint though
  # it existed on disk with real, attacker-chosen content. The single pass closes that window
  # (only the microscopic single-stat TOCTOU shared by every other leg in this file remains).
  # `-L` is tested BEFORE `-f` because `[ -f ]` FOLLOWS a symlink: a symlink-to-regular-file
  # must classify as a link (captured by target TEXT, never dereferenced), matching the old
  # `-type l` behavior. Why capture symlinks at all: the skill's config-load REFUSES a
  # symlinked override outright (read-side `test -L` guard, SKILL.md trust-resolution step 3b),
  # so a symlink at that path never resolves trust and a content flip through a pre-existing
  # link cannot silently downgrade trust — that read-side refusal, NOT this leg, closes the
  # follow-the-link vector; this leg captures the link's target TEXT (not the content it
  # resolves to) purely to surface a link being CREATED, DELETED, or RETARGETED mid-window as
  # a discovery mutation. A dangling/self-referential link takes the SYMLINK branch and is
  # never handed to the hasher, so it cannot stat a nonexistent target and exit non-zero —
  # avoiding the pipefail-swallowed false-clean the ARC-RULINGS comment warns about. `readlink`
  # (no -f) reads the target text without dereferencing. Newline-named entries are
  # attacker-reachable, so `-print0` + a NUL-delimited read loop carry embedded newlines
  # through intact; both branches feed the SAME trailing `LC_ALL=C sort` for determinism.
  (
    cd "$REPO" || exit 0
    if [ -d .gstack ]; then
      find .gstack -maxdepth 1 -name '*.local.json' -print0 2>/dev/null \
        | while IFS= read -r -d '' entry; do
            if [ -L "$entry" ]; then
              printf 'SYMLINK\t%s\t%s\n' "$entry" "$(readlink "$entry" 2>/dev/null)"
            elif [ -f "$entry" ]; then
              $hasher "$entry" 2>/dev/null
            fi
          done \
        | LC_ALL=C sort
    fi
  )
}

# require_snapshot_key — resolve the per-run secret that authenticates the snapshot.
#
# THREAT (issue #6): the snapshot file lives in the git dir, which a discovery
# sub-agent (cwd=REAL_REPO) can WRITE. Storing a plaintext fingerprint there lets
# such an agent make a permanent tracked-file mutation, then overwrite the snapshot
# with a fingerprint matching the post-mutation state — discovery-verify would then
# pass and the mutation survives undetected. To close that, the snapshot stores only
# a keyed MAC of the fingerprint, where the key is a per-run secret the TRUSTED skill
# generates and passes via ARC_DISCOVERY_SNAPSHOT_KEY to BOTH discovery-pre and
# discovery-verify (same value across the two separate processes). The sandboxed
# discovery fire never receives that key, so a sub-agent cannot forge a MAC for its
# tampered state. Fail CLOSED if the key is absent — the security property must not be
# silently downgradable (matches the no-override gate posture).
require_snapshot_key() {
  if [ -z "${ARC_DISCOVERY_SNAPSHOT_KEY:-}" ]; then
    echo "arc-preflight: ARC_DISCOVERY_SNAPSHOT_KEY is required for discovery-pre/discovery-verify (the /arc skill generates a per-run secret and passes it to both; it authenticates the snapshot so a discovery sub-agent that can write the git dir cannot forge it). Refusing to run without it." >&2
    exit 2
  fi
}

# is_sha256_hex <str> — true iff <str> is exactly 64 lowercase hex chars (a full
# SHA-256 digest). Used to FAIL CLOSED on any MAC primitive that emits empty or
# malformed output, so the keyed-MAC integrity floor can never silently degrade to a
# vacuous empty==empty comparison.
is_sha256_hex() {
  printf '%s' "$1" | grep -Eq '^[0-9a-f]{64}$'
}

# plain_fingerprint — read the fingerprint on stdin, emit its UNKEYED sha256 hash to
# stdout. This is the trust=solo half of snapshot-keyed-mac-to-plain: no per-run key is
# minted or required, so discovery-pre/verify can run with zero setup on the
# maintainer's own trusted machine. It trades away only the anti-FORGERY property a
# keyed MAC buys (a hostile sub-agent could forge a matching plain hash for a tampered
# tree, whereas it cannot forge a MAC without the key) — the underlying "did anything
# change?" comparison this feeds is unchanged: any delta in the fingerprint still fails
# discovery-verify closed. Same trailing-newline-preserving capture as snapshot_mac
# (`body="$(cat; printf x)"` then strip the sentinel) so two fingerprints differing only
# in a trailing newline never hash identically.
plain_fingerprint() {
  local hasher
  if command -v shasum >/dev/null 2>&1; then
    hasher="shasum -a 256"
  elif command -v sha256sum >/dev/null 2>&1; then
    hasher="sha256sum"
  else
    echo "arc-preflight: no shasum/sha256sum available for the trust=solo plain discovery fingerprint" >&2
    exit 2
  fi
  local body
  body="$(cat; printf x)"
  body="${body%x}"
  local digest
  digest="$(printf '%s' "$body" | $hasher | awk '{print $1}')"
  # FAIL CLOSED on a present-but-broken hasher (a shimmed/corrupted shasum that exits 0 with
  # empty or constant stdout): validate the shape before emitting, mirroring snapshot_mac's
  # is_sha256_hex guard on the hardened path. Otherwise discovery-pre/verify could both store
  # the same empty/constant fingerprint and the net-state assertion — the one check the trust
  # rulings insist is never weakened at either trust level — would vacuously pass on a real
  # mutation.
  if ! is_sha256_hex "$digest"; then
    echo "arc-preflight: the trust=solo plain fingerprint hasher produced a non-SHA-256 result ('$digest'); refusing to emit a vacuous fingerprint that would void the net-state check." >&2
    exit 2
  fi
  printf '%s' "$digest"
}

# snapshot_mac <key> <algo> — read the fingerprint on stdin, emit its keyed MAC under
# the NAMED algorithm to stdout. <algo> is one of:
#   hmac     — real HMAC-SHA256 via openssl
#   envelope — H(key || body || key) via shasum/sha256sum (length-extension-resistant,
#              unlike a bare prefix MAC)
# The algorithm is chosen ONCE by discovery_pre and recorded in the snapshot file as a
# tag, so discovery_verify recomputes with the SAME primitive even if PATH changed
# between the two calls (openssl present for pre but not verify, or vice versa) — that
# previously produced a false discovery-mutated on a clean tree. Either way the MAC
# depends on the per-run key, so it is unforgeable without it.
#
# FAIL CLOSED (issue #6, P2): a present-but-broken openssl (FIPS policy rejecting HMAC,
# a build without -r, a shimmed/stripped install) can emit an EMPTY or malformed string
# and exit 0. The prior code took `command -v openssl` as proof the primitive works and
# unconditionally `return 0`'d the empty result — so discovery-pre stored "" and
# discovery-verify recomputed "" and they matched on ANY tree state, silently voiding
# the integrity floor. We now VALIDATE the output is a 64-hex digest before emitting; on
# a non-conforming result we exit 2 (a STOP the skill maps), never emit an empty MAC.
snapshot_mac() {
  local key="$1" algo="$2"
  local body mac
  # `body="$(cat)"` would strip ALL trailing newlines, so two fingerprints differing only
  # in trailing newlines would MAC identically (a "different inputs → same MAC" violation).
  # Append a sentinel before capture and strip exactly that one char, preserving every
  # byte of the snapshot_state output.
  body="$(cat; printf x)"
  body="${body%x}"
  case "$algo" in
    hmac)
      # discovery_pre pinned algo=hmac because openssl probed good THEN; if PATH drifted
      # and openssl is now ABSENT at verify, fail closed with a precise diagnostic rather
      # than letting the empty-output path below blame a "broken" openssl that isn't here.
      if ! command -v openssl >/dev/null 2>&1; then
        echo "arc-preflight: the discovery snapshot was MAC'd with algo=hmac (openssl probed good at discovery-pre), but openssl is not on PATH now at discovery-verify. The snapshot cannot be re-authenticated, so this is fail-closed. Restore openssl on PATH and re-run, or re-run discovery from discovery-pre in an environment where the MAC primitive is stable across both calls." >&2
        exit 2
      fi
      # `-r` gives `<hex> *stdin`; take field 1. Deterministic across platforms.
      mac="$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$key" -r 2>/dev/null | awk '{print $1}')"
      ;;
    envelope)
      local hasher
      if command -v shasum >/dev/null 2>&1; then
        hasher="shasum -a 256"
      elif command -v sha256sum >/dev/null 2>&1; then
        hasher="sha256sum"
      else
        echo "arc-preflight: no shasum/sha256sum available to authenticate the discovery snapshot under the 'envelope' MAC algorithm" >&2
        exit 2
      fi
      # Envelope MAC: H(key || body || key). Resists length extension because the body
      # is bracketed by the secret on both sides.
      mac="$(printf '%s%s%s' "$key" "$body" "$key" | $hasher | awk '{print $1}')"
      ;;
    *)
      echo "arc-preflight: internal error — unknown snapshot MAC algorithm '$algo'" >&2
      exit 2
      ;;
  esac
  if ! is_sha256_hex "$mac"; then
    echo "arc-preflight: the '$algo' MAC primitive produced no valid SHA-256 digest (got [${mac}], len ${#mac}). The discovery snapshot cannot be authenticated, so this is fail-closed (refusing to emit an empty/short MAC that would make discovery-verify pass on ANY tree). If algo=hmac, openssl is present (the absent case is caught earlier) but its HMAC is broken (FIPS policy, an old build without -r, or a shimmed install) — remove openssl from PATH to fall back to the shasum/sha256sum envelope MAC, or fix the openssl install." >&2
    exit 2
  fi
  printf '%s' "$mac"
}

# pick_mac_algo — choose the MAC primitive ONCE, by ACTUALLY EXERCISING it (not just
# checking presence). Prefer a real HMAC (openssl) but only if a probe HMAC over a
# known input yields a valid 64-hex digest; otherwise fall through to the envelope MAC.
# This closes the present-but-broken-openssl hole at selection time: a broken openssl is
# never SELECTED, so the run uses the working envelope hasher instead of failing closed.
# Echoes the chosen algo tag (hmac|envelope) to stdout. Exits 2 if neither works.
pick_mac_algo() {
  if command -v openssl >/dev/null 2>&1; then
    local probe
    probe="$(printf 'arc-preflight-probe' | openssl dgst -sha256 -hmac 'probe-key' -r 2>/dev/null | awk '{print $1}')"
    if is_sha256_hex "$probe"; then
      printf 'hmac'
      return 0
    fi
    # openssl present but its HMAC is broken — do NOT select it; fall through to envelope.
  fi
  if command -v shasum >/dev/null 2>&1 || command -v sha256sum >/dev/null 2>&1; then
    printf 'envelope'
    return 0
  fi
  echo "arc-preflight: no working MAC primitive (openssl HMAC failed a probe AND no shasum/sha256sum) to authenticate the discovery snapshot" >&2
  exit 2
}

# discovery_pre [trust] — clean check, then persist the snapshot inside the git dir
# (always present; never reported by porcelain).
#
# trust=hardened (default when omitted/unrecognized): only a keyed MAC is written,
# never the plaintext fingerprint, so a sub-agent that can read the git dir learns
# nothing it could replay and cannot forge a matching MAC without the per-run key
# (requires ARC_DISCOVERY_SNAPSHOT_KEY).
#
# trust=solo: no key is required or used. A PLAIN (unkeyed) fingerprint is stored under
# the "plain" tag instead — the maintainer's own trusted machine has no hostile
# sub-agent to defend the snapshot's forgeability against, so the anti-forgery layer is
# the one thing this thins (per the snapshot-keyed-mac-to-plain ruling). The underlying
# "did anything change?" comparison is NOT skipped or weakened — it always fails closed
# on any delta, at either trust level.
discovery_pre() {
  local trust="${1:-hardened}"
  check_dirty_worktree
  if is_solo "$trust"; then
    local mac
    mac="$(snapshot_state | plain_fingerprint)"
    printf 'plain:%s\n' "$mac" > "$SNAPSHOT_FILE"
    ok "discovery-pre (trust=solo — plain fingerprint, no key required)"
    return 0
  fi
  # Pick the MAC primitive ONCE (probing that it actually works), record it as a tag in
  # the snapshot file, and store the MAC under it. The stored line is "<algo>:<mac>" so
  # discovery-verify recomputes with the SAME algorithm even if PATH drifts between the
  # two processes — pinning the primitive is what stops a false discovery-mutated when
  # openssl is present for one call but not the other.
  require_snapshot_key
  local algo mac
  algo="$(pick_mac_algo)"
  mac="$(snapshot_state | snapshot_mac "$ARC_DISCOVERY_SNAPSHOT_KEY" "$algo")"
  printf '%s:%s\n' "$algo" "$mac" > "$SNAPSHOT_FILE"
  ok "discovery-pre"
}

# discovery_verify [trust] — recompute the fingerprint and compare to the stored
# snapshot. Any delta — or a tampered/forged snapshot file — fails closed to
# discovery-mutated.
#
# WHICH COMPARISON RUNS is decided by the algo TAG STORED IN THE SNAPSHOT FILE at
# discovery-pre time — NOT by this call's own trust argument. This is deliberate: if
# trust were re-read live and used to pick the comparison, a mid-run edit to the local
# trust override (or a plumbing bug that silently drops the arg on one of the two calls)
# could downgrade verification of a keyed (hardened) snapshot to the weaker plain
# comparison, or skip require_snapshot_key while still hitting the HMAC branch. Instead:
# the trust arg here is used ONLY to detect a REGIME MISMATCH against the stored tag —
# stored "plain" is acceptable only when this call's trust is "solo"; a stored
# "hmac"/"envelope" tag always demands the key and the full MAC recompute regardless of
# what trust says, and additionally requires this call's trust NOT be "solo" (both
# directions of the mismatch gate closed — a stale/foreign snapshot never silently
# passes under whichever regime happens to be convenient this call).
discovery_verify() {
  local trust="${1:-hardened}"
  if [ ! -f "$SNAPSHOT_FILE" ]; then
    gate "discovery-mutated" \
      "No discovery snapshot found (expected ${SNAPSHOT_FILE#"$REPO"/}). Run 'discovery-pre' before 'discovery-verify'."
  fi
  # The snapshot file is "<algo>:<mac>" — discovery_pre pinned the MAC primitive (or
  # "plain" under trust=solo) so we recompute with the SAME one even if PATH drifted
  # between the two processes. Parse the pinned algo off the stored line.
  local stored_line stored_algo stored_mac current_mac
  stored_line="$(cat "$SNAPSHOT_FILE")"
  stored_algo="${stored_line%%:*}"
  stored_mac="${stored_line#*:}"
  # Fail closed on a malformed/empty snapshot line (no colon, or an unknown algo tag):
  # never let a garbage tag reach snapshot_mac/plain_fingerprint or produce a vacuous
  # match. An attacker who rewrites the tag still cannot forge the keyed MAC branch.
  case "$stored_algo" in
    plain)
      if ! is_solo "$trust"; then
        gate "discovery-mutated" \
          "Discovery snapshot regime mismatch: the snapshot was stored as 'plain' (trust=solo at discovery-pre) but this discovery-verify call has trust='${trust}'. A plain snapshot can only be re-verified under trust=solo. Re-run discovery-pre and discovery-verify with a consistent trust value for this run."
      fi
      current_mac="$(snapshot_state | plain_fingerprint)"
      ;;
    hmac|envelope)
      if is_solo "$trust"; then
        gate "discovery-mutated" \
          "Discovery snapshot regime mismatch: the snapshot was stored as '${stored_algo}' (a keyed MAC, written under trust=hardened at discovery-pre) but this discovery-verify call has trust='solo'. A keyed snapshot always requires the key and a full MAC recompute — it is never re-verified as plain. Re-run discovery-pre and discovery-verify with a consistent trust value for this run."
      fi
      require_snapshot_key
      current_mac="$(snapshot_state | snapshot_mac "$ARC_DISCOVERY_SNAPSHOT_KEY" "$stored_algo")"
      ;;
    *)
      gate "discovery-mutated" \
        "Discovery snapshot is malformed or tampered (expected '<algo>:<mac>' with algo in {plain, hmac, envelope}, got tag '${stored_algo}'). Failing closed; review the tree before proceeding."
      ;;
  esac
  if [ "$current_mac" != "$stored_mac" ]; then
    gate "discovery-mutated" \
      "Discovery was supposed to make NO changes, but the main tree's snapshot did not match (HEAD, a tracked file's contents, porcelain state, an arc-tooling file, a local trust-override file (.gstack/*.local.json), OR a tampered snapshot file). Aborting; review the diff before proceeding."
  fi
  rm -f "$SNAPSHOT_FILE"
  ok "discovery-verify"
}

# check_governing_docs <base> <path>... — the enforced governing-doc gate (#9).
#
# DETERMINISTIC and STRICT: block on ANY change to a guarded path vs the base
# branch. No semantic judgment, no content exemptions — a pattern allowlist would
# be bypassable (a principle edit formatted to look routine slips through), so
# routine files belong OFF the guarded list instead, not exempted at runtime.
# The /arc skill resolves the path list from arc.config.jsonc and passes it here
# as args, so this script never parses config (one config parser = the skill).
check_governing_docs() {
  local base="${1:-}"
  if [ -z "$base" ]; then
    echo "arc-preflight: govcheck requires <base> as the first argument" >&2
    exit 2
  fi
  shift
  if ! git -C "$REPO" rev-parse --verify --quiet "${base}^{commit}" >/dev/null 2>&1; then
    echo "arc-preflight: govcheck base ref '${base}' is not resolvable" >&2
    exit 2
  fi
  if [ "$#" -eq 0 ]; then
    ok "govcheck (no governing-doc paths configured)"
    return 0
  fi
  # Non-blocking lint: a routine-edit file on the guarded list would block on
  # every routine edit. The invariant is "listed = blocks on any diff", so
  # routine files belong OFF the list. Warn; never fail on this.
  local p
  for p in "$@"; do
    case "$(basename -- "$p")" in
      CHANGELOG|CHANGELOG.md|CHANGELOG.txt|VERSION)
        echo "arc-preflight: warning — '${p}' looks like a routine-edit file but is on the governing-doc list; it will block on every routine edit. Consider removing it from governingDocs.paths." >&2
        ;;
    esac
  done
  # Convert each guarded path to a LITERAL pathspec so a configured value can never
  # smuggle pathspec magic (e.g. ':(exclude)docs/DECISIONS.md') that would carve a
  # file out of the gate — the exact runtime exemption the strict design forbids.
  local lit=() q
  for q in "$@"; do lit+=( ":(literal)${q}" ); done
  # Block on ANY change (committed or uncommitted) to a guarded path vs base.
  # GIT_NO_REPLACE_OBJECTS=1 so a planted replace-ref can't rewrite what base/HEAD
  # resolve to. Capture the exact diff status: 0 = clean, 1 = changed (gate),
  # >1 = git error (an env failure — exit 2, NOT a silent pass and NOT a mislabeled
  # governing-doc-edit token). Callers are expected to pass an immutable base SHA
  # (the /arc skill resolves it from the trusted pre-build / base state), so a
  # moved local ref cannot fool this.
  local rc=0
  GIT_NO_REPLACE_OBJECTS=1 git -C "$REPO" diff --quiet "$base" -- "${lit[@]}" || rc=$?
  if [ "$rc" -eq 1 ]; then
    local changed
    changed="$(GIT_NO_REPLACE_OBJECTS=1 git -C "$REPO" diff --name-only "$base" -- "${lit[@]}" | tr '\n' ' ')"
    gate "governing-doc-edit" \
      "A governing doc changed on this branch (${changed% }). A build may NOT amend a design principle or architectural decision unattended — that is yours to rule. If the change is real, ship it as its own docs PR; if a build made it, revert it from this branch, then re-run."
  elif [ "$rc" -ne 0 ]; then
    echo "arc-preflight: govcheck 'git diff' failed (exit ${rc}) for base '${base}'" >&2
    exit 2
  fi
  ok "govcheck"
}

# check_fence <base> <issue> — the enforced scope-fence gate (issue #70, ruling
# fence-enforcement). DETERMINISTIC: computes the ACTUAL changed paths itself via
# `git diff --name-only <base> HEAD` (never trusts a workflow's self-reported
# changedPaths/filesTouched for the ground truth of WHAT changed — that self-report-
# vs-ground-truth gap is the exact class of bug ADR-0012 and this gate's own P0 prep
# finding both call out) and checks every one against the ALLOWLIST recorded in the
# fence receipt the trusted skill wrote AFTER the build/finish run returned:
# .gstack/arc-rulings/<issue>-fence.json = { fenceFiles: [...], fenceException:
# [{file,reason}], ... }.
#
# No receipt file => no fence was configured for this run => "absent = no fence" (the
# fence-list ruling's own stated default) => pass. An EMPTY-but-present fenceFiles list
# behaves identically (documented in the JSON_RUNNER script below), so a receipt with
# `{}` is also a no-op pass, not a hard-block-everything. A PRESENT but malformed/
# unparseable receipt is NOT treated as "no fence" — that would let a corrupted or
# tampered receipt silently disable the gate — it fails CLOSED as fence-receipt-invalid.
#
# fenceFiles/fenceException values are NEVER handed to git as a pathspec anywhere in
# this function — unlike check_governing_docs (which must :(literal)-wrap its
# maintainer-authored guarded paths before filtering a `git diff`), this gate diffs
# UNFILTERED (`git diff --name-only <base> HEAD`, no `--` pathspec at all) and does the
# allowlist membership check entirely inside the JSON_RUNNER (python3/node) as plain
# string comparison. That removes the pathspec-injection attack surface rather than
# sanitizing it, which matters MORE here than at check_governing_docs: fenceFiles/
# fenceException are UNTRUSTED, self-reported by the build itself, whereas govcheck's
# guarded paths are maintainer-authored config.
#
# "Paired tests" (fence-list ruling: fenceFiles = the branch diff + paired tests) is
# enforced HERE, not precomputed by the skill: whenever a fence is active (fenceFiles
# non-empty), any changed path under `test/` or matching `*.test.js`/`*.spec.js` is
# auto-allowed. Biased toward false-inclusion on purpose (better to over-include than
# block a legitimate test edit) — this repo's own tests all live in ONE shared file
# under `test/`, which a naive per-source-file pairing rule would never match.
#
# Routine release-accounting files (CHANGELOG.md, VERSION) are ALWAYS allowed regardless
# of the fence, matched by EXACT repo-root path (NOT basename-anywhere, so a nested
# lib/CHANGELOG.md or src/VERSION is still fence-checked and cannot smuggle out-of-scope
# content past the gate under a reserved filename) — the doc-sweep phase is chartered to
# edit the ROOT ones on nearly every run, the identical reason they are deliberately kept
# off governingDocs.paths (see check_governing_docs's own routine-file warning above).
#
# Path normalization (both sides): strip a leading "./" and any trailing "/" before
# comparing. `git diff --name-only` output is already repo-relative and forward-slash
# normalized on the CHANGED side; the receipt's fenceFiles/fenceException values are
# self-reported by an LLM agent and may carry either form, so both sides are normalized
# identically before an EXACT, case-SENSITIVE string comparison (matching git's own path
# semantics — a case-insensitive match would be a false-safety net that passes locally on
# a case-insensitive filesystem like the default macOS one, but could be exploited to
# smuggle a distinct file past the gate on a case-sensitive CI runner).
#
# BASE must be an immutable ref (the skill's pinned BASE_SHA for build, origin/<base> for
# finish) — same contract as check_governing_docs/check_security, for the same TOCTOU
# reason documented on those gates.
check_fence() {
  local base="${1:-}"
  local issue="${2:-}"
  if [ -z "$base" ]; then
    echo "arc-preflight: fencecheck requires <base> as the first argument" >&2
    exit 2
  fi
  if ! git -C "$REPO" rev-parse --verify --quiet "${base}^{commit}" >/dev/null 2>&1; then
    echo "arc-preflight: fencecheck base ref '${base}' is not resolvable" >&2
    exit 2
  fi
  local issue_id; issue_id="$(normalize_issue_id "$issue")"
  if [ -z "$JSON_RUNNER" ]; then
    echo "arc-preflight: fencecheck requires python3 or node on PATH to parse the fence receipt" >&2
    exit 2
  fi

  # NOT TRUST-GATED — intentionally, do not sweep this into symlink-containment-thin.
  # The trust profile (L2, #118) only thins write_guarded/append_guarded_line's
  # containment check; check_fence's own fence-gate ruling requires the gate to "keep
  # being a HARD STOP" and its receipt-read containment stays at full hardened strength
  # at EVERY trust level, unconditionally — see the fence-gate-under-solo ruling. A
  # future "simplify all the symlink checks in this file" pass must not touch the block
  # below.
  #
  # Parent-directory hardening — mirror write_guarded()'s symlink refusal on the READ
  # side, BEFORE the "absent = no fence" pass below. write_guarded refuses to WRITE the
  # receipt through a symlinked `.gstack` or `.gstack/arc-rulings`; if fencecheck did NOT
  # mirror that, an agent with shell access could `rm -rf .gstack/arc-rulings &&
  # ln -s /some/empty/dir .gstack/arc-rulings` before the skill runs fencecheck. The real
  # receipt then becomes unreachable, the `[ ! -f "$receipt" ]` test sees no file at the
  # symlink target, and the absent-branch passes with the ENTIRE scope fence disabled — the
  # exact fail-OPEN this gate exists to prevent. `-L` is false for a non-existent path, so a
  # genuinely unconfigured repo (no `.gstack` yet) still falls through to the no-fence pass.
  if [ -L "$REPO/.gstack" ]; then
    echo "arc-preflight: fencecheck: $REPO/.gstack is a symlink — refusing to read through it" >&2
    exit 2
  fi
  if [ -L "$REPO/.gstack/arc-rulings" ]; then
    echo "arc-preflight: fencecheck: $REPO/.gstack/arc-rulings is a symlink — refusing to read through it" >&2
    exit 2
  fi
  # If the rulings dir exists, require it resolve (pwd -P, BOTH sides — same as
  # write_guarded) to exactly <resolved-repo>/.gstack/arc-rulings. Resolving both sides
  # catches a symlinked ANCESTOR of REPO too, which the per-leaf `-L` checks above would
  # miss. Read the receipt from the resolved path so the `-f` test and the JSON runner both
  # see the same real file. When the dir does NOT exist yet, skip this and fall through to
  # the absent = no-fence pass.
  local receipt_dir="$REPO/.gstack/arc-rulings"
  if [ -d "$REPO/.gstack/arc-rulings" ]; then
    local resolved_dir resolved_repo
    resolved_dir="$(cd "$REPO/.gstack/arc-rulings" 2>/dev/null && pwd -P)" || { echo "arc-preflight: fencecheck: cannot resolve arc-rulings dir" >&2; exit 2; }
    resolved_repo="$(cd "$REPO" 2>/dev/null && pwd -P)" || { echo "arc-preflight: fencecheck: cannot resolve repo root" >&2; exit 2; }
    if [ "$resolved_dir" != "$resolved_repo/.gstack/arc-rulings" ]; then
      echo "arc-preflight: fencecheck: arc-rulings resolved to '${resolved_dir}', expected '${resolved_repo}/.gstack/arc-rulings' — refusing to read (symlinked intermediate?)" >&2
      exit 2
    fi
    receipt_dir="$resolved_dir"
  fi

  local receipt="$receipt_dir/${issue_id}-fence.json"
  if [ -L "$receipt" ]; then
    echo "arc-preflight: fencecheck: ${receipt#"$REPO"/} is a symlink — refusing to read through it" >&2
    exit 2
  fi
  if [ ! -f "$receipt" ]; then
    ok "fencecheck (no fence receipt for issue ${issue_id} — absent = no fence)"
    return 0
  fi

  # Ground truth: the ACTUAL changed paths, computed independently — see the function
  # comment above for why this must never be the workflow's self-report. Diff base..HEAD
  # (committed bytes only, mirroring seccheck and the documented contract), NOT base..
  # working-tree: the fence ALLOWLIST (fenceFiles) is itself computed as `git diff
  # --name-only <BASE> HEAD` (fence-list ruling / SKILL.md), so grounding the check on the
  # working tree would compare a committed allowlist against a working-tree actual and
  # false-block the legitimate, UNCOMMITTED doc-release sweep (README/spec/package edits the
  # sweep leaves for /ship, none of them in the committed fenceFiles). #69 commits each fix
  # round's work AS IT LANDS, so a genuine out-of-fence crossing is committed and still
  # caught here; only the legitimate uncommitted sweep is intentionally out of view. This is
  # why fencecheck diffs HEAD while govcheck diffs the working tree: govcheck's guarded docs
  # are FORBIDDEN to the sweep, so it has no legitimate uncommitted edits to exempt, whereas
  # fencecheck covers the sweep's OWN files, so it MUST scope to committed HEAD.
  local cur_sha
  cur_sha="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || { echo "arc-preflight: fencecheck could not resolve HEAD" >&2; exit 2; }

  # Ground-truth changed paths, written NUL-delimited straight to a TEMP FILE.
  #
  # The path list is passed to the JSON_RUNNER as an argv FILE PATH, not via stdin:
  # `runner - <args> <<HEREDOC` already uses stdin to feed the SCRIPT SOURCE itself (the
  # `-` argument to python3/node means "read the program from stdin"), so a second data
  # stream on the SAME stdin would be swallowed by the heredoc — the script would run
  # against an ALWAYS-EMPTY `changed` list and this gate would never fire. Every other
  # JSON_RUNNER call in this file passes its input via a FILE PATH argv; this matches.
  #
  # --no-renames: without it, `git diff --name-only` reports a pure rename as ONLY the new
  # path — the vacated OLD path (which may be an out-of-fence file the agent silently
  # repurposed into an allowed fence path) never appears, so a rename could smuggle a file
  # out of scope with NO fence-crossing violation. --no-renames forces git to emit a rename
  # as a delete + add pair, so the vacated out-of-fence path is always in the ground truth.
  #
  # -z: NUL-terminate each path and emit it VERBATIM (never C-quoted), so filenames
  # containing spaces, newlines, or non-ASCII bytes survive intact and each path is one
  # unambiguous record. This is why the diff output is redirected STRAIGHT to the temp file
  # (bash variables cannot hold NUL bytes; a `$(...)` capture would drop them and corrupt
  # the delimiters) and why the runners split on \0, not \n, and do NOT strip interior/edge
  # whitespace that is part of a real filename. -z already disables path quoting, so
  # core.quotePath is moot under -z — kept `false` only for belt-and-suspenders parity with
  # the other gates. A trailing-space `CHANGELOG.md ` therefore stays DISTINCT from the
  # exempt `CHANGELOG.md` and cannot be swept into the routine allowlist by a stray space.
  local changed_file
  changed_file="$(mktemp "${TMPDIR:-/tmp}/arc-fence-changed.XXXXXX")" || { echo "arc-preflight: fencecheck: cannot create temp file for changed-paths list" >&2; exit 2; }
  local diff_rc=0
  GIT_NO_REPLACE_OBJECTS=1 git -C "$REPO" -c core.quotePath=false diff --no-renames -z --name-only "$base" "$cur_sha" > "$changed_file" || diff_rc=$?
  if [ "$diff_rc" -ne 0 ]; then
    rm -f "$changed_file"
    echo "arc-preflight: fencecheck 'git diff' failed (exit ${diff_rc}) for base '${base}'" >&2
    exit 2
  fi

  local result
  if [ "$JSON_RUNNER" = "python3" ]; then
    result="$(python3 - "$receipt" "$changed_file" <<'PY'
import json, sys

receipt_path = sys.argv[1]
changed_path = sys.argv[2]
with open(changed_path, "r", encoding="utf-8") as fh:
    # NUL-delimited (git diff -z): split on \0 and drop only genuinely empty records (the
    # trailing NUL yields one). Do NOT .strip() — leading/trailing spaces can be part of a
    # real git filename, and stripping them would let a distinct out-of-fence path that
    # differs only by whitespace collide with an in-fence entry (or with the CHANGELOG.md
    # routine exemption). norm() below normalizes only ./ prefix and trailing / for the
    # path comparison, identically on both sides.
    changed = [p for p in fh.read().split("\0") if p]

def norm(p):
    p = str(p)
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")

# Read the receipt bytes SEPARATELY from parsing them: an OS/IO read failure (e.g. a
# permission-denied receipt) must emit a PATH-FREE reason, never the raw OSError string,
# which embeds the absolute receipt path and would undo the repo-relative path stripping
# the shell does one token later on the same stderr line (the receipt-minus-REPO trim).
try:
    with open(receipt_path, "r", encoding="utf-8") as fh:
        raw_text = fh.read()
except OSError:
    print("invalid:receipt present but unreadable")
    sys.exit(0)

# Parse + STRUCTURE-validate. A PRESENT-but-wrong-TYPE fenceFiles/fenceException (e.g.
# fenceFiles as a string) is a MALFORMED receipt, not a "no fence" default: silently
# coercing it to [] would make fence_active=false and let a corrupted/tampered receipt
# disable the gate entirely, the exact fail-OPEN the function header forbids. Only an
# ABSENT key defaults to [] (the documented empty/absent = no-fence behavior).
try:
    d = json.loads(raw_text)
    if not isinstance(d, dict):
        raise ValueError("root is not an object")
    if "fenceFiles" in d and not isinstance(d["fenceFiles"], list):
        raise ValueError("fenceFiles is not a list")
    if "fenceException" in d and not isinstance(d["fenceException"], list):
        raise ValueError("fenceException is not a list")
except Exception as exc:
    print("invalid:%s" % exc)
    sys.exit(0)

raw_fence = d.get("fenceFiles", [])
raw_exc = d.get("fenceException", [])

fence_set = set()
for f in raw_fence:
    if isinstance(f, str) and f.strip():
        fence_set.add(norm(f))

# Fail CLOSED on a non-empty fenceFiles list that filters to an EMPTY fence_set (every
# element non-string or blank, e.g. [123, 456] or ["", "  "]): a present, non-empty
# allowlist that yields nothing is MALFORMED, not the documented empty/absent = no-fence
# default. Coercing it to no-fence (fence_active=false) would let a tampered receipt
# disable the gate — the SAME fail-OPEN the present-but-wrong-TYPE (non-list) branch above
# already rejects. Only a genuinely empty/absent fenceFiles ([] or omitted) stays a pass.
if len(raw_fence) > 0 and len(fence_set) == 0:
    print("invalid:fenceFiles present but has no valid string entries")
    sys.exit(0)

exc_set = set()
for e in raw_exc:
    if isinstance(e, dict):
        f = e.get("file")
        r = e.get("reason")
        if isinstance(f, str) and f.strip() and isinstance(r, str) and r.strip():
            exc_set.add(norm(f))

fence_active = len(fence_set) > 0
routine = {"CHANGELOG.md", "VERSION"}

def is_test_path(p):
    return p.startswith("test/") or p.endswith(".test.js") or p.endswith(".spec.js")

violations = []
for raw_p in changed:
    p = norm(raw_p)
    # Routine exemption is anchored to the EXACT repo-root path, not a basename-anywhere
    # match: only the root CHANGELOG.md / VERSION the doc-sweep charter edits are exempt.
    # A nested lib/CHANGELOG.md or src/VERSION is fence-checked like any other path, so a
    # reserved filename cannot be used to smuggle out-of-scope content past the fence.
    if p in routine:
        continue
    if p in fence_set or p in exc_set:
        continue
    if fence_active and is_test_path(p):
        continue
    if not fence_active:
        continue
    violations.append(raw_p)

if violations:
    print("violations:" + "|".join(violations))
else:
    print("ok")
PY
)"
  else
    result="$(node - "$receipt" "$changed_file" <<'NODE'
const fs = require("fs");
const receiptPath = process.argv[2];
const changedPath = process.argv[3];
// NUL-delimited (git diff -z): split on \0 and drop only genuinely empty records (the
// trailing NUL yields one). Do NOT .trim() — leading/trailing spaces can be part of a real
// git filename; stripping them would let a distinct out-of-fence path differing only by
// whitespace collide with an in-fence entry (or the CHANGELOG.md routine exemption). norm()
// normalizes only ./ prefix and trailing / for the comparison, identically on both sides.
const changed = fs.readFileSync(changedPath, "utf8").split("\0").filter(Boolean);
(function main() {
  function norm(p) {
    p = String(p);
    while (p.startsWith("./")) p = p.slice(2);
    return p.replace(/\/+$/, "");
  }
  // Read bytes separately from parse (see the python runner comment): an fs read
  // failure emits a PATH-FREE reason so the raw error, which embeds the absolute
  // receipt path, never reaches stderr and undoes the later abs-path stripping.
  let rawText;
  try {
    rawText = fs.readFileSync(receiptPath, "utf8");
  } catch (exc) {
    console.log("invalid:receipt present but unreadable");
    return;
  }
  let d;
  try {
    d = JSON.parse(rawText);
    if (typeof d !== "object" || d === null || Array.isArray(d)) throw new Error("root is not an object");
    // present-but-wrong-TYPE is a MALFORMED receipt, NOT a no-fence default (see python
    // runner): coercing to [] would let a tampered receipt disable the gate (fail-OPEN).
    if ("fenceFiles" in d && !Array.isArray(d.fenceFiles)) throw new Error("fenceFiles is not a list");
    if ("fenceException" in d && !Array.isArray(d.fenceException)) throw new Error("fenceException is not a list");
  } catch (exc) {
    console.log("invalid:" + exc.message);
    return;
  }
  const rawFence = Array.isArray(d.fenceFiles) ? d.fenceFiles : [];
  const rawExc = Array.isArray(d.fenceException) ? d.fenceException : [];
  const fenceSet = new Set();
  for (const f of rawFence) if (typeof f === "string" && f.trim()) fenceSet.add(norm(f));
  // Fail CLOSED on a non-empty fenceFiles list that filters to an EMPTY fenceSet (all
  // entries non-string/blank, e.g. [123,456]) — MALFORMED, not the empty/absent=no-fence
  // default; coercing to no-fence would let a tampered receipt disable the gate (fail-OPEN),
  // mirroring the wrong-type branch above. An empty/absent fenceFiles stays a no-fence pass.
  if (rawFence.length > 0 && fenceSet.size === 0) {
    console.log("invalid:fenceFiles present but has no valid string entries");
    return;
  }
  const excSet = new Set();
  for (const e of rawExc) {
    if (e && typeof e === "object" && typeof e.file === "string" && e.file.trim() && typeof e.reason === "string" && e.reason.trim()) {
      excSet.add(norm(e.file));
    }
  }
  const fenceActive = fenceSet.size > 0;
  const routine = new Set(["CHANGELOG.md", "VERSION"]);
  const isTestPath = (p) => p.startsWith("test/") || p.endsWith(".test.js") || p.endsWith(".spec.js");
  const violations = [];
  for (const rawP of changed) {
    const p = norm(rawP);
    // Routine exemption anchored to the EXACT repo-root path, not basename-anywhere (see
    // the python runner comment): only root CHANGELOG.md / VERSION are exempt; a nested
    // lib/CHANGELOG.md or src/VERSION is fence-checked like any other path.
    if (routine.has(p)) continue;
    if (fenceSet.has(p) || excSet.has(p)) continue;
    if (fenceActive && isTestPath(p)) continue;
    if (!fenceActive) continue;
    violations.push(rawP);
  }
  console.log(violations.length ? "violations:" + violations.join("|") : "ok");
})();
NODE
)"
  fi
  rm -f "$changed_file"

  case "$result" in
    ok)
      ok "fencecheck"
      ;;
    invalid:*)
      echo "arc-preflight: fencecheck: fence receipt at ${receipt#"$REPO"/} is present but invalid (${result#invalid:}) — failing closed rather than silently treating a malformed receipt as no-fence" >&2
      gate "fence-receipt-invalid" \
        "The fence receipt at .gstack/arc-rulings/${issue_id}-fence.json exists but could not be parsed. Re-run the build/finish (which regenerates it), or remove the stale file by hand if you are certain no fence should apply to this run."
      ;;
    violations:*)
      local vlist="${result#violations:}"
      gate "fence-crossing" \
        "This run touched file(s) outside its declared scope fence with no matching fenceException: ${vlist//|/, }. Either the change is genuinely out of scope (revert it from this branch and file it as its own issue), or it is a legitimate cross-fence edit that needs a fenceException entry (file + one-line reason) — re-run a fix round that declares it."
      ;;
    *)
      echo "arc-preflight: fencecheck: unexpected result from the JSON runner: ${result}" >&2
      exit 2
      ;;
  esac
}

# _arc_run_with_timeout <timeout_seconds> <out_file> <cmd_str> — run `cmd_str`
# in $REPO with ARC_TESTFAIL_CAPTURE_FILE stripped, exactly like the untimed
# path, but bounded by <timeout_seconds> (issue #62, D3). Sets two globals for
# the caller to read IMMEDIATELY after it returns (a plain bash function
# cannot return a structured value):
#   ARC_TIMEOUT_RC     the command's exit code (or the reserved fired-code, below)
#   ARC_TIMEOUT_FIRED  "1" iff OUR watchdog POSITIVELY fired the kill — never
#                       inferred from the command's OWN exit code (P1 prep
#                       finding: a legitimate rc=143 the test runner produced on
#                       its own must never be mistaken for a timeout, and a real
#                       timeout must never be missed just because the killed
#                       process happened to also exit with an ordinary-looking
#                       code). Ground truth is the POLLING SUBSHELL's OWN exit
#                       code: when the watchdog fires it exits a reserved code
#                       (ARC_TIMEOUT_FIRED_CODE) the untrusted command cannot
#                       forge — see the mechanism note above the body (issue #62,
#                       R2 P0: this replaces the earlier on-disk sentinel FILE,
#                       which a same-UID command could reach and truncate through
#                       an ANCESTOR process's still-open fd via /proc/<pid>/fd/N
#                       on Linux, defeating the gate; an exit code has no such
#                       filesystem path to attack).
#
# d3-process-group-kill (TIER-B): starts the command in its OWN process group
# via job control (`set -m` in a subshell — no `setsid` dependency; macOS, this
# kit's own primary dev environment, ships none) so a process-group kill
# (`kill -- -$pgid`) can never land on arc-preflight.sh's OWN process group.
# Verifies the child actually resolved to a distinct, valid (>0, not this
# script's own) pgid before ever sending a group kill; falls back to a
# documented single-PID kill (logged loudly, never silent) if that pgid read
# is unreadable, zero, non-numeric, or equal to this script's own. TERM first,
# then KILL after a short grace period, so a forking sleeper that ignores TERM
# is still reaped — both signals target the whole group, so descendants
# inherited into the same pgid die too, not just the direct child.
#
# DISCLOSED RESIDUAL LIMIT (issue #62, R2 P1): the group kill reaches only
# descendants that STAY in the launched command's process group. A test command
# that deliberately detaches a grandchild into its OWN session/group (e.g.
# `python3 -c 'import os; os.setsid(); ...'`) escapes both the TERM and the
# KILL, and — because such a wrapper can exit 0 immediately after backgrounding
# the detached process — the poll loop may even see the command "finish" before
# the timeout fires. That detached process can outlive this gate. This is an
# inherent property of ANY process-group approach: fully reaping a deliberately
# re-parented/re-sessioned process needs OS-level isolation (cgroups on Linux,
# a job object elsewhere), which is out of scope for a pure-bash, no-new-
# dependency kit (d3-timeout-mechanism-dependency). It is disclosed here rather
# than papered over with a fragile `ps`-for-leftovers scan, which would false-
# positive on unrelated same-cwd processes (an editor, a language server) and
# fail legitimate builds. The timeout's job is to bound a HUNG in-group command;
# a test that intentionally spawns a detaching daemon is the same untrusted-
# test-code exposure any runner has without process isolation.
#
# Teardown: there is nothing to tear down. The timer is an INLINE poll loop
# running in the SAME subshell that backgrounded the command (see the
# IMPLEMENTATION NOTE below) — not a separate backgrounded watchdog job — so no
# orphan timer can survive past the command's lifetime and later fire against a
# REUSED pid (the PID-reuse race a naive "start a sleep; kill $pid" watcher
# would invite). The loop simply exits once `kill -0 "$cmd_pid"` reports the
# command gone. Job-control status-change notices ("[1]+ Done"/"Terminated")
# CAN be printed by a non-interactive script under `set -m` (verified on macOS
# bash 3.2 and Linux bash 5.x — the notice is NOT interactive-only) — but the
# design is safe anyway because such a notice is a job-control message on the
# shell's own STDERR, never stdout, so it structurally cannot corrupt this
# script's `preflight-failed:<token>` stdout contract. The stderr-vs-stdout
# separation is load-bearing here; do not remove it on the mistaken belief the
# notice never fires. (`disown` is deliberately NOT
# used here: it removes a job from bash's internal job table, which also
# breaks `wait <pid>`'s ability to retrieve that job's real exit status —
# verified empirically; do not reintroduce it.)
#
# IMPLEMENTATION NOTE — why this is a POLL loop, not a second background
# "watchdog" process: an earlier version of this function ran the test command
# and the timeout timer as TWO concurrent background jobs (`cmd &` plus a
# `( sleep timeout; kill ... ) &` watchdog) and used `wait "$cmd_pid"` to learn
# when the foreground command finished. That worked when arc-preflight.sh's
# own stdout was a terminal, but under `set -m`, with TWO concurrent
# background jobs, `wait "$specific_pid"` was empirically observed (verified
# repeatedly, isolated down to this exact shape) to block until the OTHER
# job (the watchdog) ALSO changed state — i.e. a fast, already-finished test
# command would still make the whole gate wait out the FULL
# testTimeoutSeconds before returning, silently defeating the entire point of
# a short-lived config value and regressing every green build's wall-clock
# time to the timeout on any caller that captures this script's stdout via a
# pipe or command substitution (the /arc skill's own Bash tool call is exactly
# such a caller). A single foreground job, polled by the SAME shell that
# backgrounded it (no second concurrent background job in play), does not
# exhibit this: `kill -0 "$cmd_pid"` is a plain synchronous check, not a
# `wait`-driven job-control wakeup, so it cannot get stuck behind an unrelated
# job's own status transition. Do not "simplify" this back to a
# background-watchdog shape without re-verifying that regression is gone.
_arc_run_with_timeout() {
  local timeout_s="$1" out_file="$2" cmd_str="$3"
  local self_pgid
  self_pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"

  # Reserved exit code the POLLING SUBSHELL uses to signal "the watchdog fired"
  # (issue #62, R2 P0). This replaces the earlier on-disk sentinel FILE. The
  # sentinel was pinned by descriptor and unlinked, but on Linux the untrusted
  # test command could still reach the still-open write fd through an ANCESTOR
  # process's /proc/<pid>/fd/N (a same-UID-readable path regardless of unlink):
  # in a TERM trap it walked up the process tree to the long-lived polling
  # subshell, truncated that fd, and exited 0 — the fired-flag read back EMPTY
  # and a hung/malicious suite reported as PASSING. An exit code has NO
  # filesystem path to open, so that whole channel disappears. Ground truth for
  # "we fired" is now the polling subshell's OWN exit status, which the command
  # (a mere descendant) cannot write. 124 is git/coreutils' conventional
  # "timed out" code; it can ONLY be produced here by the did_fire branch below
  # (a genuine command exit of 124 is remapped away), so it is unforgeable.
  local ARC_TIMEOUT_FIRED_CODE=124

  ARC_TIMEOUT_RC=0
  ARC_TIMEOUT_FIRED=0
  (
    # This subshell inherits the script's `set -euo pipefail`. `wait`/`kill`
    # returning the target's real (possibly non-zero) status is the NORMAL,
    # expected case here — not an error — so every one that matters is
    # guarded with `|| ...` the same way the rest of this file captures a
    # command's rc under `set -e` (e.g. check_tests' own `... || rc=$?` a few
    # lines below). An unguarded `wait`/`kill` here would abort this subshell
    # via set -e BEFORE the exit code is ever captured.
    set -m
    ( cd "$REPO" && env -u ARC_TESTFAIL_CAPTURE_FILE bash -c "$cmd_str" ) >"$out_file" 2>&1 &
    cmd_pid=$!

    did_fire=0
    elapsed=0
    # 1-second poll granularity (matches the existing TERM→KILL grace period
    # elsewhere in this function) — simple, portable, no fractional bash
    # arithmetic. Worst-case added latency on a FAST command is under 1s.
    while kill -0 "$cmd_pid" 2>/dev/null; do
      if [ "$elapsed" -ge "$timeout_s" ]; then
        # POSITIVELY record that WE fired the kill (P1 prep finding), in this
        # subshell's OWN local — never inferred later from cmd_rc, which a
        # legitimately red test suite could produce on its own. The command
        # cannot reach or forge this local; it is consumed only by the exit
        # below.
        did_fire=1
        pgid="$(ps -o pgid= -p "$cmd_pid" 2>/dev/null | tr -d ' ')" || pgid=""
        case "$pgid" in
          ''|0|"$self_pgid"|*[!0-9]*)
            echo "arc-preflight: timeout: could not confirm a distinct process group for the test command (pgid='${pgid}') — falling back to a single-PID kill (may leave forked grandchildren behind)" >&2
            kill -TERM "$cmd_pid" 2>/dev/null || true
            sleep 1
            kill -KILL "$cmd_pid" 2>/dev/null || true
            ;;
          *)
            kill -TERM -- "-${pgid}" 2>/dev/null || true
            sleep 1
            kill -KILL -- "-${pgid}" 2>/dev/null || true
            ;;
        esac
        break
      fi
      sleep 1
      elapsed=$((elapsed + 1))
    done

    cmd_rc=0
    wait "$cmd_pid" 2>/dev/null || cmd_rc=$?
    if [ "$did_fire" -eq 1 ]; then
      # A fired timeout is ground truth regardless of what the killed command
      # reported (a TERM-trapping suite can exit 0). Signal it out-of-band via
      # the reserved code the command cannot produce.
      exit "$ARC_TIMEOUT_FIRED_CODE"
    fi
    # Not fired: report the command's REAL exit code — but never let a genuine
    # command exit of exactly the reserved code masquerade as a fired timeout.
    # Remapping it to a plain failure (still non-zero → still tests-failed)
    # keeps the reserved code UNFORGEABLE: it can only come from the branch
    # above.
    if [ "$cmd_rc" -eq "$ARC_TIMEOUT_FIRED_CODE" ]; then
      exit 1
    fi
    exit "$cmd_rc"
  ) || ARC_TIMEOUT_RC=$?
  # Same class of bug noted above the subshell: under this script's
  # `set -euo pipefail`, an UNGUARDED `( ... )` returning non-zero as a bare
  # statement would abort check_tests/the whole script before its exit code
  # could ever be read. `ARC_TIMEOUT_RC=0` was set right before the subshell;
  # `|| ARC_TIMEOUT_RC=$?` overwrites it with the real code ONLY on the
  # non-zero branch (a plain `|| true` here would have been WRONG — it
  # discards `$?`, leaving `ARC_TIMEOUT_RC` stuck at 0 on a failing/timed-out
  # run; do not "simplify" this back to that shape).
  # The subshell exiting the reserved code is the fired-flag: unforgeable by the
  # command (only the did_fire branch produces it; a real command exit of 124 is
  # remapped away above). No signal can yield 124 either (a signalled exit is
  # 128+N ≥ 129), so ARC_TIMEOUT_RC==124 uniquely means "our watchdog fired."
  if [ "$ARC_TIMEOUT_RC" -eq "$ARC_TIMEOUT_FIRED_CODE" ]; then
    ARC_TIMEOUT_FIRED=1
  fi
}

# check_tests <testCommand> [coverageCommand] [testTimeoutSeconds]: the
# enforced test gate (#15).
#
# Makes the test discipline a HARD gate, not just instruction + review: a build
# may not reach pr-ready unless the configured test command actually RAN and
# exited 0 this build. The /arc skill resolves testCommand (and the optional
# coverageCommand, and now the optional testTimeoutSeconds — all three from
# the SAME pre-build CONFIG snapshot, never re-read post-build) from
# arc.config.jsonc and passes them here, so this script never parses config
# (one config parser = the skill), mirroring govcheck.
#
#   - empty testCommand              → tests-unconfigured (fail-closed; never a silent pass)
#   - testCommand exits non-zero     → tests-failed
#   - coverageCommand exits non-zero → coverage-floor
#   - testCommand/coverageCommand runs past testTimeoutSeconds (issue #62, D3)
#     → tests-failed, distinguished ONLY by a timeout-specific remedy message
#     (d3-timeout-token: reuses the existing token so the skill's tests-failed
#     park-with-seed dispatch, #131, applies unchanged — no new token).
#   - testTimeoutSeconds present but not a positive integer within a sane
#     bound → tests-timeout-misconfigured (fail-closed; NEVER silently treated
#     as no-limit — d3-invalid-timeout-degrade).
#
# testTimeoutSeconds is OPTIONAL: unset/blank preserves today's no-limit
# behavior EXACTLY (the synchronous, non-backgrounded code path below is
# untouched in that case). When set, ONE value bounds BOTH testCommand and
# coverageCommand (d3-timeout-covers-coverage: a coverage hang is as wedging
# as a test hang; least config surface) — document that dual scope explicitly
# in arc.config.example.jsonc so the name is never misread as test-only.
#
# The command strings are the project's OWN configured commands (same trust as
# the build that just ran them); execute each via the shell in the repo root.
# Output is captured and shown only on failure (quiet success, like the other
# gates). For coverage, arc never parses output; the command's EXIT CODE is the
# gate, so a project self-enforces its own threshold with its own tooling.
check_tests() {
  local test_cmd="${1:-}"
  local coverage_cmd="${2:-}"
  local test_timeout="${3:-}"

  # Validate testTimeoutSeconds BEFORE anything else runs: fully-anchored
  # positive-integer check (never a loose "looks numeric" match — an
  # unanchored pattern reaching a later `sleep $timeout` construct would be a
  # command-injection surface, not just a parse nuisance). Bound the digit
  # COUNT before any numeric comparison, so a pathologically long digit
  # string can't overflow bash's integer arithmetic and crash the gate
  # instead of failing closed with a clean token.
  if [ -n "$test_timeout" ]; then
    case "$test_timeout" in
      ''|*[!0-9]*)
        gate "tests-timeout-misconfigured" \
          "testTimeoutSeconds is set to '${test_timeout}', which is not a valid positive whole number of seconds. Fix .gstack/arc.config.jsonc (unset it to disable the limit, or set a positive integer, e.g. 300) and re-run."
        ;;
    esac
    if [ "${#test_timeout}" -gt 7 ]; then
      gate "tests-timeout-misconfigured" \
        "testTimeoutSeconds is set to '${test_timeout}', which is unreasonably large. Set a smaller positive integer (seconds) and re-run."
    fi
    if [ "$test_timeout" -eq 0 ]; then
      gate "tests-timeout-misconfigured" \
        "testTimeoutSeconds is set to 0, which would kill the test command before it can ever run. Unset it to disable the limit, or set a positive integer (seconds), and re-run."
    fi
    if [ "$test_timeout" -gt 86400 ]; then
      gate "tests-timeout-misconfigured" \
        "testTimeoutSeconds is set to '${test_timeout}' seconds (over 24h), which defeats the point of the gate. Set a smaller positive integer and re-run."
    fi
  fi
  # Blank OR whitespace-only counts as unconfigured: `bash -c '   '` exits 0, so a
  # space-only testCommand would otherwise sail through as a silent pass.
  if [ -z "$(printf '%s' "$test_cmd" | tr -d '[:space:]')" ]; then
    gate "tests-unconfigured" \
      "No testCommand is set in .gstack/arc.config.jsonc, so the loop cannot prove this change was tested. Set testCommand (even a minimal one) and re-run. This gate is fail-closed on purpose: a build must not reach pr-ready with the test discipline unenforced."
  fi
  local out rc=0
  out="$(mktemp "${TMPDIR:-/tmp}/arc-tests.XXXXXX")" || { echo "arc-preflight: cannot create temp file for test output" >&2; exit 2; }
  # Pin two read fds to $out's inode BEFORE the UNTRUSTED test command runs (issue
  # #131, R1 — read-side TOCTOU). The command below inherits fd 1 already bound to
  # this inode, but it can discover $out's underlying PATH (lsof on macOS,
  # readlink /proc/$$/fd/1 on Linux) and, before it exits, delete that path and
  # recreate it as a symlink to an arbitrary readable file (SSH key, .env). Every
  # LATER read of the captured output goes through fd 3 / fd 4, which were opened
  # here (pre-swap) and stay bound to the ORIGINAL inode by descriptor — they never
  # re-resolve the path — so a path swap during the run cannot redirect the tail
  # display or the capture copy onto a planted target. This closes the read side
  # the same way the mv-based rename below closes the write side. Two fds (not one)
  # so the tail display and the full capture each read from offset 0 independently.
  exec 3<"$out" 4<"$out" || { echo "arc-preflight: cannot open read fds for test output" >&2; rm -f "$out"; exit 2; }
  # Strip ARC_TESTFAIL_CAPTURE_FILE from the UNTRUSTED test command's environment
  # (issue #131, R1). The test command comes from the branch under review (any
  # test file a build/fix agent wrote is in-fence and untrusted per this repo's
  # threat model); if it could read the capture path it could pre-position a
  # symlink there and turn the later copy into an arbitrary-file-overwrite. It
  # has no legitimate need to see this var, so `env -u` it out of scope entirely.
  local test_timed_out=0
  if [ -n "$test_timeout" ]; then
    _arc_run_with_timeout "$test_timeout" "$out" "$test_cmd"
    rc="$ARC_TIMEOUT_RC"
    test_timed_out="$ARC_TIMEOUT_FIRED"
  else
    ( cd "$REPO" && env -u ARC_TESTFAIL_CAPTURE_FILE bash -c "$test_cmd" ) >"$out" 2>&1 || rc=$?
  fi
  # Enter the failure branch on a non-zero rc OR a fired watchdog (R3 blocking
  # finding): a killed command can still report rc=0 — a graceful-shutdown TERM
  # handler that exits 0, or an adversarial build/fix agent adding `trap 'exit 0'
  # TERM` to its own (untrusted) test script specifically to neutralize the
  # timeout. The watchdog-fired signal (test_timed_out, carried out of the poll
  # subshell as its reserved exit code — see _arc_run_with_timeout), not the
  # killed command's self-reported exit code, is ground truth for "this suite
  # hung past testTimeoutSeconds and had to be forcibly killed" — a fired timeout
  # is an unconditional failure regardless of rc.
  if [ "$rc" -ne 0 ] || [ "$test_timed_out" -eq 1 ]; then
    if [ "$test_timed_out" -eq 1 ]; then
      echo "arc-preflight: test command exceeded testTimeoutSeconds=${test_timeout}s and was killed: ${test_cmd}" >&2
    else
      echo "arc-preflight: test command failed (exit ${rc}): ${test_cmd}" >&2
    fi
    echo "---- last 40 lines of test output ----" >&2
    tail -n 40 <&3 >&2
    # ARC_TESTFAIL_CAPTURE_FILE (issue #131, R1): an OPTIONAL env var only the
    # trusted /arc skill ever sets — never a human at an interactive terminal.
    # When present, copy the FULL (untruncated) captured test output to that
    # path BEFORE the temp file is removed, so the skill can pick up the entire
    # verbatim failure text (not just the tail-40 a human-facing terminal gets)
    # to seed the next `/arc finish` run. This is deliberately NOT a new
    # preflight MODE and NOT a new structured-output file format (D3 chose
    # stderr capture, no new mode/file) — it is an optional side channel on the
    # EXISTING `tests` mode, gated on an env var so a manual/interactive run
    # (which never sets it) still only ever sees the bounded 40-line tail on
    # its own terminal. Symlink/TOCTOU hardening (issue #131, R1) has THREE legs:
    # (1) the test command above ran with the var stripped from its env, so
    # untrusted test code never learns this path to pre-position a symlink at it;
    # (2) the SOURCE side — both the tail display above and the capture copy below
    # read back through fds 3/4, pinned to $out's inode BEFORE the untrusted
    # command ran, so a path swap on $out during the run cannot redirect either
    # read onto a planted target; and (3) the DESTINATION side mirrors
    # write_guarded()'s own discipline (see line ~2536) — write to a mktemp file
    # in the destination's directory, then `mv -f` it into place. `mv` is
    # rename(2): it replaces whatever is at the destination (including a symlink
    # planted AFTER the `-L` check below) atomically WITHOUT following it, so this
    # closes both the leaf-symlink-follow AND the check-then-cp TOCTOU race a plain
    # `cp` would reintroduce. The `-L` check stays only as a fast, loud early refusal.
    if [ -n "${ARC_TESTFAIL_CAPTURE_FILE:-}" ]; then
      if [ -L "$ARC_TESTFAIL_CAPTURE_FILE" ]; then
        echo "arc-preflight: ARC_TESTFAIL_CAPTURE_FILE='${ARC_TESTFAIL_CAPTURE_FILE}' is a symlink — refusing to write through it; the skill's seed will be missing this run's failure text" >&2
      else
        local capdir captmp
        capdir="$(dirname "$ARC_TESTFAIL_CAPTURE_FILE")"
        if captmp="$(mktemp "${capdir}/.arc-testfail.XXXXXX" 2>/dev/null)"; then
          # Read the captured output back through fd 4 (pinned to $out's inode before
          # the untrusted command ran), NOT by re-opening "$out" by pathname — a path
          # swap during the run cannot redirect this copy onto a planted symlink. The
          # trailing `-f && ! -L` check catches the one case `mv` reports success for
          # without writing the destination: if $ARC_TESTFAIL_CAPTURE_FILE is a
          # DIRECTORY, `mv -f "$captmp" "$dir"` moves the temp INSIDE it and exits 0,
          # so the seed file was never actually written — treat that as a write failure.
          if cat <&4 > "$captmp" 2>/dev/null && mv -f "$captmp" "$ARC_TESTFAIL_CAPTURE_FILE" 2>/dev/null \
             && [ -f "$ARC_TESTFAIL_CAPTURE_FILE" ] && [ ! -L "$ARC_TESTFAIL_CAPTURE_FILE" ]; then
            : # captured full output landed atomically
          else
            rm -f "$captmp" 2>/dev/null
            echo "arc-preflight: could not write full test output to ARC_TESTFAIL_CAPTURE_FILE='${ARC_TESTFAIL_CAPTURE_FILE}' — the skill's seed will be missing this run's failure text" >&2
          fi
        else
          echo "arc-preflight: could not create temp file beside ARC_TESTFAIL_CAPTURE_FILE='${ARC_TESTFAIL_CAPTURE_FILE}' — the skill's seed will be missing this run's failure text" >&2
        fi
      fi
    fi
    exec 3<&- 4<&-
    rm -f "$out"
    if [ "$test_timed_out" -eq 1 ]; then
      gate "tests-failed" \
        "The configured test suite exceeded testTimeoutSeconds=${test_timeout}s and was killed (testCommand: ${test_cmd}). This counts as a test failure (d3-timeout-token). Either the suite is genuinely hung (fix the hang), or the timeout is too tight for this project (raise testTimeoutSeconds in .gstack/arc.config.jsonc), then re-run."
    else
      gate "tests-failed" \
        "The configured test suite did not pass (testCommand: ${test_cmd}). Fix the failing tests, or the change that broke them, and re-run. A build may NOT reach pr-ready on a red suite."
    fi
  fi
  exec 3<&- 4<&-
  rm -f "$out"
  # A blank / whitespace-only coverageCommand means "no coverage floor" (it is
  # optional), so skip it rather than running `bash -c '   '` as a no-op pass.
  if [ -n "$(printf '%s' "$coverage_cmd" | tr -d '[:space:]')" ]; then
    local cout crc=0 coverage_timed_out=0
    cout="$(mktemp "${TMPDIR:-/tmp}/arc-coverage.XXXXXX")" || { echo "arc-preflight: cannot create temp file for coverage output" >&2; exit 2; }
    if [ -n "$test_timeout" ]; then
      # d3-timeout-covers-coverage: the SAME testTimeoutSeconds bounds this
      # command too, via the SAME shared watchdog helper (never a second
      # hand-maintained timeout implementation for the two commands).
      _arc_run_with_timeout "$test_timeout" "$cout" "$coverage_cmd"
      crc="$ARC_TIMEOUT_RC"
      coverage_timed_out="$ARC_TIMEOUT_FIRED"
    else
      ( cd "$REPO" && env -u ARC_TESTFAIL_CAPTURE_FILE bash -c "$coverage_cmd" ) >"$cout" 2>&1 || crc=$?
    fi
    # Same R3 blocking finding as the test path above: a fired watchdog counts as
    # a failure even when the killed coverage command reported crc=0 (a TERM-
    # trapping coverage runner that exits 0). Ground-truth is the fired signal
    # (the poll subshell's reserved exit code), not crc.
    if [ "$crc" -ne 0 ] || [ "$coverage_timed_out" -eq 1 ]; then
      if [ "$coverage_timed_out" -eq 1 ]; then
        echo "arc-preflight: coverage command exceeded testTimeoutSeconds=${test_timeout}s and was killed: ${coverage_cmd}" >&2
      else
        echo "arc-preflight: coverage command failed (exit ${crc}): ${coverage_cmd}" >&2
      fi
      echo "---- last 40 lines of coverage output ----" >&2
      tail -n 40 "$cout" >&2
      rm -f "$cout"
      if [ "$coverage_timed_out" -eq 1 ]; then
        gate "tests-failed" \
          "The configured coverageCommand exceeded testTimeoutSeconds=${test_timeout}s and was killed (coverageCommand: ${coverage_cmd}). A coverage hang is treated as a test failure, not a coverage-floor miss (d3-timeout-covers-coverage). Either the command is genuinely hung (fix the hang), or the timeout is too tight for this project (raise testTimeoutSeconds), then re-run."
      fi
      gate "coverage-floor" \
        "The configured coverageCommand exited non-zero, meaning coverage is below your project's floor (coverageCommand: ${coverage_cmd}). Add tests to raise coverage, or adjust the floor in your coverage tool's own config, then re-run. arc never reads the coverage number itself; your command's exit code is the gate."
    fi
    rm -f "$cout"
  fi
  ok "tests"
}

# check_security <base> <issue>: the enforced security-gate classifier (#8, PR1).
#
# The DETERMINISTIC, ENFORCED layer of the two-layer hybrid (the AI flag-helper in
# the workflow is PR2, advisory and can only RAISE a flag). It classifies the diff
# base..HEAD against a project-agnostic list of sensitive surfaces across three legs
# (changed paths, added-line content tokens, dependency-manifest changes). If ANY
# leg fires, the change is sensitive and the gate requires a security-review RECEIPT
# tied to the exact sha being shipped, or it blocks `needs-security-gate`. There is
# NO skip (per the ruling): risk is accepted AFTER the review, never by bypassing it.
#
# No agent judges this enforced layer (the govcheck precedent, #9). The surface list
# is a hard-coded named constant, biased to OVER-FLAG: a false flag costs a review,
# a miss ships an unreviewed sensitive change. The /arc skill passes <base> as the
# immutable pre-build BASE_SHA, so a moved ref / config edit can't change the diff.
#
# Receipt: .gstack/arc-rulings/<issue>-security-receipt.json { "reviewedSha": "<sha>" },
# written by the skill AFTER /cso + security-review ran on the diff. A receipt whose
# sha != current HEAD is stale (commits added since review) and does not clear it.
# TRUST: the build/workflow is untrusted and CAN write this gitignored path, so the
# skill DELETES any preexisting receipt before the first seccheck and writes one only
# itself after the review (the build finishes before the skill runs the gates, so it
# cannot re-seed it). The gate enforces sha-currency; the skill owns the lifecycle.
# classify_sensitive_surface <base> <cur_sha>
#
# PURE classifier — the SAME three legs (path / content / manifest) seccheck has
# always run, extracted into ONE shared function so seccheck and riskcheck (#119,
# L3) can never drift into two hand-maintained copies of "what counts as
# sensitive" (risky-surface-classifier-source ruling: reuse, never a second list).
# NO receipt-file logic lives here — that stays in check_security, the only caller
# that has a receipt lifecycle.
#
# Sets GLOBAL (not local) result vars for the caller to read IMMEDIATELY after
# the call returns — a plain bash function cannot return a structured value:
#   CLASSIFY_HIT             0 or 1
#   CLASSIFY_REASONS         human text, "| "-joined (empty iff CLASSIFY_HIT=0)
#   CLASSIFY_CHANGED_PATHS   newline-separated changed file paths (name-only diff)
#   CLASSIFY_DIFF_LINES      count of added+removed CONTENT lines (excludes the
#                            +++/--- file headers) — riskcheck's diff-size leg
#                            reuses this SAME capture, never a second git call
#                            (P1 prep finding: one shared diff-capture helper).
#   CLASSIFY_FILE_COUNT      count of changed files
#
# Fails CLOSED (exit 2) on ANY git diff error — an env failure must never
# silently resolve to "nothing sensitive". A caller that must NEVER exit
# non-zero (riskcheck) invokes this ONLY through a command-substitution
# subshell, so the exit only terminates that subshell — see riskcheck's own
# header note below.
classify_sensitive_surface() {
  local base="$1" cur_sha="$2"

  # The project-agnostic sensitive-surface rulebook (named constants, over-flag bias).
  # The path boundary allows /._- separators so "config-secret.yml" matches. Ultra-
  # generic structure words (route/controller/handler/api) are intentionally NOT here:
  # flagging every backend file trains rubber-stamping (alarm fatigue is worse for
  # security than a miss); the content greps catch auth logic, and PR2's AI flag-helper
  # is for the residual semantic cases.
  local SENSITIVE_PATH_RE='(^|[/._-])(auth|authn|authz|login|logout|session|oauth|saml|sso|jwt|password|passwd|credential|secret|token|apikey|api[_-]?key|\.env|permission|role|acl|rbac|migration|schema|models?|db|database|billing|payment|stripe|invoice|charge|refund|quota|pricing|webhook|crypto|cipher|encrypt|decrypt|signing|cors|csrf|sanitiz|cookie|upload|download|filesystem|archive|dockerfile|docker-compose|terraform|helm|kubernetes|k8s|ansible|\.github/workflows|decisions\.jsonl|arc[_-]?ledger|arc[_-]?rulings)'
  # NOTE on the decision-delegation additions (issue #32):
  #   decisions.jsonl, arc-ledger, arc-rulings: the decision ledger and the rulings/
  #   receipts dir are private, gitignored runtime state (operator rationale, ruling
  #   patterns) that should NEVER appear in a diff; if one did, it was likely committed
  #   by accident, so flagging it for review is the right call. Over-flag bias: a false
  #   flag costs a review, not a miss.
  #   DELIBERATELY EXCLUDED: arc-judgment-profile / arc-profile. The committed-vs-
  #   gitignored ruling for #32 makes the per-repo judgment profile
  #   (.gstack/arc-judgment-profile.json) COMMITTED project knowledge, like CLAUDE.md
  #   (which is also not flagged here). It is DESIGNED to appear in diffs and be
  #   reviewed as an ordinary doc; gating every profile edit behind a per-commit
  #   security-receipt would fire on the file's intended lifecycle and train rubber-
  #   stamping. The private *.local.json draft is gitignored (never committed), so it
  #   never reaches this diff scan.
  #   arc-kit-version. `.gstack/arc-kit-version.json` is committed benign provenance
  #   metadata (install source, kit version, opaque commit SHA, ISO dates). It is NOT in
  #   SENSITIVE_PATH_RE, so the PATH leg (Leg 1) trivially does not flag it — but be
  #   precise: that is a no-op, not a real exclusion, and Leg 2 still scans the manifest's
  #   added-line CONTENT like any other file. For most fields that is harmless: kitCommit
  #   (hex SHA), date/installedAt (ISO-8601) and source (the fixed strings git|version-file)
  #   can never match SENSITIVE_CONTENT_RE. kitVersion is the one operator-controlled field:
  #   the VERSION regex `^[a-zA-Z0-9][a-zA-Z0-9._+\-]*$` permits letters, so a version like
  #   `2.0-authorize` would both validate AND match the `authoriz` alternative. That is NOT
  #   a leak — seccheck fails CLOSED, so the worst case is a spurious halt on the
  #   install/upgrade commit (which the operator clears by confirming the diff), never a
  #   silent pass. So no path/content exclusion is added: the rare false halt is the safe
  #   side of the trade. The kit's free-text commit SUBJECT is deliberately NOT embedded in
  #   the manifest (issue #47) — precisely so a sensitive word in a commit message cannot
  #   reach this content scan.
  # Issue #62, D1: this pattern is now the SHARED ARC_SENSITIVE_CONTENT_RE global
  # (declared once, near the top of the file) — reused verbatim by
  # commit_residual()'s pre-commit scan so the two never drift apart.
  local SENSITIVE_CONTENT_RE="$ARC_SENSITIVE_CONTENT_RE"
  local MANIFESTS='package.json package-lock.json yarn.lock pnpm-lock.yaml requirements.txt pyproject.toml poetry.lock Pipfile Pipfile.lock go.mod go.sum Cargo.toml Cargo.lock Gemfile Gemfile.lock composer.json composer.lock pom.xml build.gradle build.gradle.kts'
  # --no-ext-diff / --no-textconv so a project's diff config can't reshape what we scan.
  local DIFF_OPTS='--no-ext-diff --no-textconv'

  local reasons=""

  # Leg 1: changed paths. Capture name-only diff with explicit rc (a git error is an
  # env failure → exit 2, never a silent "no sensitive surface" pass).
  local changed_paths diffrc=0
  changed_paths="$(GIT_NO_REPLACE_OBJECTS=1 git -C "$REPO" diff $DIFF_OPTS --name-only "$base" "$cur_sha")" || diffrc=$?
  if [ "$diffrc" -ne 0 ]; then
    echo "arc-preflight: classify_sensitive_surface 'git diff --name-only' failed (exit ${diffrc}) for base '${base}'" >&2
    exit 2
  fi
  local hit_paths
  hit_paths="$(printf '%s\n' "$changed_paths" | grep -iE "$SENSITIVE_PATH_RE" || true)"
  [ -n "$hit_paths" ] && reasons="${reasons}sensitive path(s): $(printf '%s' "$hit_paths" | head -n 5 | tr '\n' ' ')| "

  # Capture the FULL patch ONCE with explicit rc: a patch-generation failure must be an
  # env error (exit 2), NOT a swallowed "nothing sensitive" via || true.
  local full_diff patchrc=0
  full_diff="$(GIT_NO_REPLACE_OBJECTS=1 git -C "$REPO" diff $DIFF_OPTS "$base" "$cur_sha")" || patchrc=$?
  if [ "$patchrc" -ne 0 ]; then
    echo "arc-preflight: classify_sensitive_surface 'git diff' (patch) failed (exit ${patchrc}) for base '${base}'" >&2
    exit 2
  fi

  # Leg 2: added-line content tokens (only '+' lines, excluding the +++ file header).
  local added hit_content
  added="$(printf '%s\n' "$full_diff" | grep -E '^\+' | grep -vE '^\+\+\+' || true)"
  hit_content="$(printf '%s\n' "$added" | grep -iE "$SENSITIVE_CONTENT_RE" || true)"
  [ -n "$hit_content" ] && reasons="${reasons}sensitive content token(s) in added lines| "

  # Leg 3: a dependency manifest changed with added lines. Match the BASENAME at ANY
  # depth, so monorepo paths like services/api/package.json count (not just root).
  # Capture each per-file diff with explicit rc: a git error is exit 2, never a silent
  # "no new dependency" (the only || true here is the expected no-match grep).
  local f bn fdiff mrc mhits
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    bn="$(basename -- "$f")"
    case " $MANIFESTS " in
      *" $bn "*) : ;;
      *) continue ;;
    esac
    mrc=0
    fdiff="$(GIT_NO_REPLACE_OBJECTS=1 git -C "$REPO" diff $DIFF_OPTS "$base" "$cur_sha" -- ":(literal)$f")" || mrc=$?
    if [ "$mrc" -ne 0 ]; then
      echo "arc-preflight: classify_sensitive_surface 'git diff' for manifest '${f}' failed (exit ${mrc})" >&2
      exit 2
    fi
    mhits="$(printf '%s\n' "$fdiff" | grep -E '^\+[^+]' || true)"
    [ -n "$mhits" ] && reasons="${reasons}dependency manifest changed: ${f}| "
  done <<< "$changed_paths"

  # Diff-size capture (riskcheck-only consumer): derived from the SAME full_diff
  # already captured above for Leg 2 — never a second independent git invocation.
  local diff_lines
  diff_lines="$(printf '%s\n' "$full_diff" | grep -E '^[+-]' | grep -vcE '^(\+\+\+|---)' || true)"
  case "$diff_lines" in ''|*[!0-9]*) diff_lines=0 ;; esac
  local file_count
  file_count="$(printf '%s\n' "$changed_paths" | grep -c . || true)"
  case "$file_count" in ''|*[!0-9]*) file_count=0 ;; esac

  CLASSIFY_HIT=0
  CLASSIFY_REASONS=""
  if [ -n "$reasons" ]; then
    CLASSIFY_HIT=1
    CLASSIFY_REASONS="${reasons%| }"
  fi
  CLASSIFY_CHANGED_PATHS="$changed_paths"
  CLASSIFY_DIFF_LINES="$diff_lines"
  CLASSIFY_FILE_COUNT="$file_count"
}

check_security() {
  local base="${1:-}" issue="${2:-}"
  if [ -z "$base" ] || [ -z "$issue" ]; then
    echo "arc-preflight: seccheck requires <base> and <issue> arguments" >&2
    exit 2
  fi
  if ! git -C "$REPO" rev-parse --verify --quiet "${base}^{commit}" >/dev/null 2>&1; then
    echo "arc-preflight: seccheck base ref '${base}' is not resolvable" >&2
    exit 2
  fi
  # Normalize + traversal-guard the issue arg UP FRONT, before diff classification
  # and before the non-sensitive early return. A non-numeric issue arg (a possible
  # path-traversal sequence) must fail closed with exit 2 on EVERY path, not only
  # the sensitive branch where the receipt path is built — matching check_rulings /
  # check_spec / check_stale, which all normalize at the top. (Previously this ran
  # only at receipt-path construction on the sensitive branch, so a traversal arg on
  # a non-sensitive diff slipped through as a clean "no sensitive surface" pass.)
  local issue_id
  issue_id="$(normalize_issue_id "$issue")"
  local cur_sha
  cur_sha="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || { echo "arc-preflight: seccheck could not resolve HEAD" >&2; exit 2; }

  classify_sensitive_surface "$base" "$cur_sha"

  if [ "$CLASSIFY_HIT" -eq 0 ]; then
    ok "seccheck (no sensitive surface touched)"
    return 0
  fi

  # SENSITIVE → require a valid security-review receipt for the current sha.
  # issue_id was already normalized + traversal-guarded at the top of this function.
  local receipt="$REPO/.gstack/arc-rulings/${issue_id}-security-receipt.json"
  local why="$CLASSIFY_REASONS"
  if [ ! -f "$receipt" ]; then
    gate "needs-security-gate" \
      "This change touches a SENSITIVE surface (${why}) and has no security-review receipt. Run the security review (/cso + security-review) on this diff, address or accept each finding, record the receipt for the reviewed commit, then re-run. Fail-closed: a sensitive change may NOT reach pr-ready unreviewed, and there is NO skip."
  fi
  if [ -z "$JSON_RUNNER" ]; then
    echo "arc-preflight: neither python3 nor node available to read the security receipt" >&2
    exit 2
  fi
  local reviewed_sha=""
  if [ "$JSON_RUNNER" = "python3" ]; then
    reviewed_sha="$(python3 - "$receipt" <<'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(str(d.get("reviewedSha", "")).strip())
except Exception:
    print("")
PY
)"
  else
    reviewed_sha="$(node -e 'try{const d=require(process.argv[1]);process.stdout.write(String(d.reviewedSha||"").trim())}catch(e){process.stdout.write("")}' "$receipt" 2>/dev/null || true)"
  fi
  if [ "$reviewed_sha" != "$cur_sha" ]; then
    gate "needs-security-gate" \
      "This change touches a SENSITIVE surface (${why}). A security-review receipt exists but it is for a different commit (${reviewed_sha:-none} vs current ); commits were added since the review. Re-run the security review on the current diff, re-record the receipt, then re-run."
  fi
  ok "seccheck (sensitive surface reviewed; receipt valid for ${cur_sha})"
}

# check_spec <issue>: the enforced SPEC gate, the FRONT-half mirror of seccheck.
#
# The arc loop hard-gates the back half (govcheck / tests / seccheck) but planning was
# only a soft norm, so a build could reach discovery with no spec and the maintainer
# (who follows the presented path) skipped planning unasked. This makes "has a spec" a
# real, fail-closed gate at the front of the loop, before discovery.
#
# Mirrors seccheck's SHAPE (fail-closed, receipt, NO silent skip) but deliberately NOT
# its anti-forgery lifecycle: speccheck runs BEFORE any untrusted build output exists,
# so there is no build to forge a receipt, and the delete-before-check dance seccheck
# needs would be cargo-culting here. The receipt is issue-bound, NOT sha-bound: a spec
# precedes the code and describes intent, so it must NOT go stale when commits land
# (unlike the security receipt, which certifies a reviewed diff). State that on purpose
# so a later reader does not "fix" it into sha-binding.
#
# Receipt: .gstack/arc-rulings/<issue>-spec-receipt.json
#   { "issue":"<N>", "kind":"spec"|"trivial", "reason":"<required iff trivial>", "ruledAt":"<ISO>" }
# Two ways to satisfy: kind "spec" (a real spec / fleshed-out issue was authored), or
# kind "trivial" WITH a non-empty reason. The trivial path is an explicit, justified,
# dashboard-visible ruling ("too small to spec" is never a silent skip). The `kind`
# field is load-bearing: it lets the maintainer's metrics tell "spec'd" from
# "ruled-trivial", so overuse of the trivial path is visible, not hidden in a green gate.
# A missing/unreadable receipt, an unknown kind, or a reasonless trivial ruling all fail
# closed with `missing-spec`.
check_spec() {
  local issue="${1:-}"
  if [ -z "$issue" ]; then
    echo "arc-preflight: speccheck requires an <issue> argument" >&2
    exit 2
  fi
  local issue_id
  issue_id="$(normalize_issue_id "$issue")"
  local receipt="$REPO/.gstack/arc-rulings/${issue_id}-spec-receipt.json"
  if [ ! -f "$receipt" ]; then
    gate "missing-spec" \
      "No spec on file for issue ${issue_id}. Run /spec to turn this into a real, fleshed-out issue, or record an explicit 'too small to need a spec' ruling with a one-line reason. Planning is gated the same way security is: there is no silent skip."
  fi
  if [ -z "$JSON_RUNNER" ]; then
    echo "arc-preflight: neither python3 nor node available to read the spec receipt" >&2
    exit 2
  fi
  # Validate the receipt INSIDE the JSON runner and return exactly ONE token from a fixed
  # vocabulary (spec | trivial-ok | trivial-noreason | issue-mismatch | bad). The shell
  # never splits user-controlled strings (a "kind" with an embedded newline could
  # otherwise fail open through head -n1), so the only values the case below can see are
  # the controlled tokens. The runner enforces the full schema, fail-closed: the receipt's
  # own "issue" field must match the issue id this gate was called for (filename binding
  # is not enough on its own); "kind" must be EXACTLY "spec" or "trivial" (no case-fold,
  # no surrounding tolerance the docs don't promise); a "trivial" ruling needs a "reason"
  # that is a JSON string, non-empty after trim, and single-line (no CR/LF). Anything else
  # is "bad". A crash / unreadable file yields an empty string, which the case maps to bad.
  local status
  if [ "$JSON_RUNNER" = "python3" ]; then
    status="$(python3 - "$receipt" "$issue_id" <<'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("bad"); sys.exit(0)
want = sys.argv[2]
issue = d.get("issue")
if not isinstance(issue, (str, int)) or isinstance(issue, bool):
    print("issue-mismatch"); sys.exit(0)
s = str(issue).strip()
iss = s.split()[0] if s else ""
iss = iss.rsplit("#", 1)[-1]
if iss != want:
    print("issue-mismatch"); sys.exit(0)
kind = d.get("kind")
if kind == "spec":
    print("spec")
elif kind == "trivial":
    r = d.get("reason")
    if isinstance(r, str) and r.strip() and "\n" not in r and "\r" not in r:
        print("trivial-ok")
    else:
        print("trivial-noreason")
else:
    print("bad")
PY
)"
  else
    status="$(node -e '
try {
  const d = require(process.argv[1]);
  const want = process.argv[2];
  const issue = d.issue;
  if ((typeof issue !== "string" && typeof issue !== "number")) { process.stdout.write("issue-mismatch"); process.exit(0); }
  const s = String(issue).trim();
  let iss = s ? s.split(/\s+/)[0] : "";
  if (iss.includes("#")) iss = iss.slice(iss.lastIndexOf("#") + 1);
  if (iss !== want) { process.stdout.write("issue-mismatch"); process.exit(0); }
  const kind = d.kind;
  if (kind === "spec") { process.stdout.write("spec"); }
  else if (kind === "trivial") {
    const r = d.reason;
    if (typeof r === "string" && r.trim() && !r.includes("\n") && !r.includes("\r")) process.stdout.write("trivial-ok");
    else process.stdout.write("trivial-noreason");
  } else { process.stdout.write("bad"); }
} catch (e) { process.stdout.write("bad"); }
' "$receipt" "$issue_id" 2>/dev/null || true)"
  fi

  case "$status" in
    spec)
      ok "speccheck (spec on file for issue ${issue_id})"
      return 0
      ;;
    trivial-ok)
      ok "speccheck (issue ${issue_id} ruled trivial; no spec needed)"
      return 0
      ;;
    trivial-noreason)
      gate "missing-spec" \
        "Issue ${issue_id} has a 'trivial' spec ruling but no valid reason. A 'too small to need a spec' ruling must record a non-empty, single-line \"reason\" (a JSON string) so the skip is justified and visible, never silent. Add one (or run /spec), then re-run."
      ;;
    issue-mismatch)
      gate "missing-spec" \
        "The spec receipt at ${receipt#"$REPO"/} does not name issue ${issue_id} in its \"issue\" field (it must match the issue this build is for). Re-record it via /spec, or an explicit trivial ruling with a reason, then re-run."
      ;;
    *)
      gate "missing-spec" \
        "The spec receipt for issue ${issue_id} is unreadable or has an unrecognized kind (expected exactly \"spec\" or \"trivial\"). Re-record it via /spec, or an explicit trivial ruling with a reason, then re-run."
      ;;
  esac
}

# check_stale <issue>: the enforced staleness gate for issue #42.
#
# Proves the rulings in <N>-pr1-args.json were made against the LATEST discovery
# run by comparing both files' `discoveryRunId` fields. If they agree, the rulings
# are current; if they differ, a newer discovery ran since the maintainer ruled.
#
# Gate ordering contract: check_stale MUST be called AFTER check_rulings (which
# already validated the rulings file as parseable JSON with a non-empty
# discoveryRunId). Calling it before check_rulings risks a parse error that exits
# 2 instead of the expected incomplete-rulings token.
#
# Marker file: .gstack/arc-rulings/<N>-latest-discovery.json
#   Schema: { schemaVersion, issue, discoveryRunId, ranAt }
#   - schemaVersion: integer; missing == 1 for back-compat (mirrors the ledger).
#   - issue: the marker's issue id must match the CLI <N> arg. The CLI arg's path
#     is built from the bare id produced by normalize_issue_id (the shared %%/##
#     strip + numeric guard); the marker's own `issue` field is then cross-checked
#     by a REGEX extraction (#<n> then first digit-run) inside the runner, NOT the
#     bash normalization. They agree for every bare-numeric / owner/repo#N / "N —
#     scope" form that actually reaches this gate (non-numeric CLI ids exit 2 in
#     normalize_issue_id long before the runner sees them).
#   - discoveryRunId: compared to the rulings file's discoveryRunId (string equality
#     after trim). THIS IS A STRING EQUALITY CHECK, NOT A TIMESTAMP COMPARISON.
#     Do not add Date or timestamp logic here — freshness is proven by run-id
#     match, not by how recently the files were written.
#   - ranAt: informational only; not validated for staleness.
#
# Absent/malformed marker: A missing marker blocks with missing-latest-discovery.
# A malformed/unparseable marker is treated IDENTICALLY to an absent one (routed
# through the same missing-latest-discovery path). A malformed marker MUST NEVER
# produce exit 2 (a hard-crash / env error) — parse failures are not environment
# errors; they are absent-equivalent. A warning is logged to stderr before routing.
#
# Legacy-affirmed escape hatch: .gstack/arc-rulings/<N>-latest-discovery-affirmed.json
#   { issue, kind:"legacy-affirmed", reason, affirmedAgainstRunId, ruledAt }
#   Satisfies the missing-latest-discovery gate without a re-run of discovery, for
#   issues that predate the marker system. ENFORCED fields (gate-checked, fail-
#   closed): `issue` id matches; `kind` == "legacy-affirmed"; `reason` is a non-
#   empty single-line string; and `affirmedAgainstRunId` EQUALS the rulings file's
#   current discoveryRunId. That binding is the SELF-VOID control (mirrors
#   seccheck's reviewedSha == HEAD): the receipt is bound to the exact discovery
#   run it affirms, so the moment the maintainer re-rules against a NEWER discovery
#   (which rotates the rulings discoveryRunId), the old receipt no longer matches
#   and is rejected.
#
#   HONEST SCOPE — what the binding does NOT do: it does NOT make the receipt
#   un-forgeable by the build. The build (arc-execute) is fired with the saved
#   args, so it RECEIVES the current discoveryRunId in its own input, and its
#   downstream agents have write access to the gitignored .gstack/arc-rulings/
#   dir. A build can therefore delete the marker and write a correctly-bound
#   receipt by COPYING the present run id from its input — no prediction needed.
#   The binding only self-voids on the NEXT re-ruling against a newer discovery,
#   not against the build that already holds the current id. So the absent-marker
#   case's real defense is the skill's REQUIRED pre-build delete of any pre-
#   existing affirmed receipt (SKILL.md): the trusted skill removes a build-
#   written receipt before the gate ever consults it. This binding is the
#   second layer that catches a stale receipt the delete missed, NOT a
#   replacement for the delete. `ruledAt` is informational only; not validated.
#
# Exit codes follow the same contract as the rest of the file:
#   exit 0  (via return)    — gate passed
#   exit 1  (via gate())    — gated failure (missing-latest-discovery or stale-rulings)
#   exit 2  (direct)        — USAGE/env error only: a non-numeric issue id (caught by
#                             normalize_issue_id as a possible path traversal) OR no
#                             python3/node available. NOT used for parse failures — a
#                             malformed/unparseable marker is absent-equivalent and
#                             returns 1 via the gate, never exit 2.
check_stale() {
  local issue="${1:-}"
  if [ -z "$issue" ]; then
    # No issue arg: can't check, but check_rulings already gated on this. Skip.
    return 0
  fi

  # Normalize the issue arg to its bare numeric id via the shared helper, which
  # also enforces the traversal guard (non-numeric id → exit 2). This is the same
  # boundary check_rulings/check_spec/seccheck now use, so the guard is consistent
  # across every path-constructing gate, not just this one.
  local issue_id
  issue_id="$(normalize_issue_id "$issue")"

  local marker="$REPO/.gstack/arc-rulings/${issue_id}-latest-discovery.json"
  local affirmed="$REPO/.gstack/arc-rulings/${issue_id}-latest-discovery-affirmed.json"
  local rulings="$REPO/.gstack/arc-rulings/${issue_id}-pr1-args.json"

  if [ -z "$JSON_RUNNER" ]; then
    echo "arc-preflight: neither python3 nor node available to read the latest-discovery marker" >&2
    exit 2
  fi

  # Helper: validate the marker and return a controlled token from a fixed vocabulary:
  #   "match"       — marker is valid, issue matches, discoveryRunId matches rulings
  #   "stale"       — marker is valid, issue matches, discoveryRunId differs from rulings
  #   "absent"      — marker is missing, malformed, or issue-mismatched (all = absent path)
  # The shell never sees the raw discoveryRunId string — only the controlled token.
  # PARSE FAILURES (including valid JSON with wrong shape) must return "absent", never exit 2.
  local marker_status=""

  if [ "$JSON_RUNNER" = "python3" ]; then
    # Validation checklist (both runners must implement all steps identically):
    # (1) file must be readable JSON; malformed → warn stderr, return "absent"
    # (2) schemaVersion: absent == 1 (back-compat); present must be integer
    # (3) issue field must exist and its id must match CLI arg; mismatch → "absent"
    # (4) discoveryRunId must exist and be a non-empty string; missing → "absent"
    # (5) compare marker.discoveryRunId.strip() vs rulings.discoveryRunId.strip()
    #     — equal → "match"; different → "stale"
    # ranAt is informational; not validated.
    marker_status="$(python3 - "$marker" "$rulings" "$issue_id" <<'PY' || true
import json, sys

marker_path = sys.argv[1]
rulings_path = sys.argv[2]
cli_issue = sys.argv[3]

# Parse marker — malformed routes to "absent", never exit 2.
try:
    with open(marker_path, "r", encoding="utf-8") as fh:
        marker = json.load(fh)
    if not isinstance(marker, dict):
        sys.stderr.write("arc-preflight: marker parse warning: root is not an object\n")
        print("absent"); sys.exit(0)
except FileNotFoundError:
    print("absent"); sys.exit(0)
except Exception as exc:
    sys.stderr.write("arc-preflight: marker parse warning: %s\n" % exc)
    print("absent"); sys.exit(0)

# (2) schemaVersion back-compat: absent == 1.
# Canonical rule (BOTH runners): accept an integer-VALUED number, reject bool.
# JS has no distinct int/float, so a JSON `1.0` parses to the same value as `1`
# and Number.isInteger(1.0) is true. Python DOES distinguish: json `1.0` becomes a
# float, so a bare isinstance(sv, int) would REJECT 1.0 that node accepts. To keep
# the two contracted-identical validators in lockstep we accept an integer-valued
# float here too (sv.is_integer()), and still reject bool first (isinstance(True, int)
# is True in Python, but Number.isInteger(true) is false in node). Result: `1` and
# `1.0` accepted in both; `true` and `1.5` rejected in both.
sv = marker.get("schemaVersion", 1)
if isinstance(sv, bool) or not (isinstance(sv, int) or (isinstance(sv, float) and sv.is_integer())):
    sys.stderr.write("arc-preflight: marker parse warning: schemaVersion is not an integer\n")
    print("absent"); sys.exit(0)

# (3) issue cross-check. Use the EXACT same extractor as check_spec (first
# whitespace token -> segment after the last hash -> numeric-only guard), NOT a
# loose "hash-N anywhere, else first digit-run" search. The loose form is MORE
# PERMISSIVE than check_spec: for a marker issue like "38 superseded by hash42" it
# would pick "42" and accept the marker for a build of 42, even though the marker
# really describes issue 38 — a fail-open relative to its sibling gate. First-token
# extraction yields "38" and correctly rejects. A token with no bare-numeric id
# returns None (absent path). (Keep this heredoc body apostrophe-free: it lives
# inside a "$(... <<PY ...)" command substitution, where a lone apostrophe breaks
# bash parsing of the surrounding command substitution.)
def extract_id(raw):
    s = str(raw).strip()
    tok = s.split()[0] if s else ""
    tok = tok.rsplit("#", 1)[-1]
    return tok if tok.isdigit() else None

raw_issue = marker.get("issue")
if raw_issue is None:
    sys.stderr.write("arc-preflight: marker parse warning: issue field missing\n")
    print("absent"); sys.exit(0)
marker_id = extract_id(raw_issue)
cli_id = extract_id(cli_issue)
if marker_id is None or cli_id is None or marker_id != cli_id:
    sys.stderr.write("arc-preflight: marker parse warning: issue mismatch (marker=%r, cli=%r)\n" % (raw_issue, cli_issue))
    print("absent"); sys.exit(0)

# (4) discoveryRunId must be a non-empty string.
marker_run_id = marker.get("discoveryRunId")
if not isinstance(marker_run_id, str) or marker_run_id.strip() == "":
    sys.stderr.write("arc-preflight: marker parse warning: discoveryRunId missing or empty\n")
    print("absent"); sys.exit(0)

# (5) read rulings discoveryRunId and compare (string equality after trim).
try:
    with open(rulings_path, "r", encoding="utf-8") as fh:
        rulings = json.load(fh)
    rulings_run_id = rulings.get("discoveryRunId", "")
    if not isinstance(rulings_run_id, str):
        rulings_run_id = ""
except Exception:
    # If rulings cannot be read here, check_rulings already passed, so this is
    # an unexpected error -- treat as absent to fail safely.
    print("absent"); sys.exit(0)

if marker_run_id.strip() == rulings_run_id.strip():
    print("match")
else:
    print("stale")
PY
)"
  else
    # Node path — must implement the IDENTICAL validation checklist as python3 above.
    # Validation checklist:
    # (1) file must be readable JSON; malformed → warn stderr, return "absent"
    # (2) schemaVersion: absent == 1 (back-compat); present must be integer
    # (3) issue field must exist and its id must match CLI arg; mismatch → "absent"
    # (4) discoveryRunId must exist and be a non-empty string; missing → "absent"
    # (5) compare marker.discoveryRunId.trim() vs rulings.discoveryRunId.trim()
    #     — equal → "match"; different → "stale"
    # ranAt is informational; not validated.
    marker_status="$(node - "$marker" "$rulings" "$issue_id" <<'NODE' || true
const fs = require("fs");
const markerPath = process.argv[2];
const rulingsPath = process.argv[3];
const cliIssue = process.argv[4];

// EXACT parity with the check_spec extractor (and the python marker extractor):
// first whitespace token -> segment after the last hash -> numeric-only guard.
// NOT a loose "hash-N anywhere, else first digit-run" match, which is more
// permissive than check_spec and would accept a marker like "38 superseded by
// hash42" for a build of 42. Returns null when the first token has no bare id.
// (Keep this heredoc body apostrophe-free: it lives inside a "$(... <<NODE ...)"
// command substitution, where a lone apostrophe breaks bash parsing of the
// surrounding command substitution.)
function extractId(raw) {
  const s = String(raw).trim();
  let tok = s ? s.split(/\s+/)[0] : "";
  if (tok.includes("#")) tok = tok.slice(tok.lastIndexOf("#") + 1);
  return /^\d+$/.test(tok) ? tok : null;
}

// (1) Parse marker — malformed routes to "absent", never exit 2.
let marker;
try {
  marker = JSON.parse(fs.readFileSync(markerPath, "utf8"));
  if (typeof marker !== "object" || marker === null || Array.isArray(marker)) {
    process.stderr.write("arc-preflight: marker parse warning: root is not an object\n");
    process.stdout.write("absent"); process.exit(0);
  }
} catch (exc) {
  if (exc.code === "ENOENT") {
    process.stdout.write("absent"); process.exit(0);
  }
  process.stderr.write("arc-preflight: marker parse warning: " + exc.message + "\n");
  process.stdout.write("absent"); process.exit(0);
}

// (2) schemaVersion back-compat: absent == 1.
// Canonical rule (BOTH runners): accept an integer-VALUED number, reject bool.
// Number.isInteger(true) is false (JSON `true` rejected) and Number.isInteger(1.0)
// is true — JS has no distinct integer/float, so `1.0` IS `1`. The python branch
// matches this by accepting an integer-valued float (sv.is_integer()) in addition
// to int, and rejecting bool first. Both runners now agree on every operand:
// `1` and `1.0` accepted, `true` and `1.5` rejected.
const sv = marker.schemaVersion !== undefined ? marker.schemaVersion : 1;
if (typeof sv !== "number" || !Number.isInteger(sv)) {
  process.stderr.write("arc-preflight: marker parse warning: schemaVersion is not an integer\n");
  process.stdout.write("absent"); process.exit(0);
}

// (3) issue cross-check.
const rawIssue = marker.issue;
if (rawIssue === undefined || rawIssue === null) {
  process.stderr.write("arc-preflight: marker parse warning: issue field missing\n");
  process.stdout.write("absent"); process.exit(0);
}
const markerId = extractId(rawIssue);
const cliId = extractId(cliIssue);
if (markerId === null || cliId === null || markerId !== cliId) {
  process.stderr.write("arc-preflight: marker parse warning: issue mismatch (marker=" + JSON.stringify(rawIssue) + ", cli=" + JSON.stringify(cliIssue) + ")\n");
  process.stdout.write("absent"); process.exit(0);
}

// (4) discoveryRunId must be a non-empty string.
const markerRunId = marker.discoveryRunId;
if (typeof markerRunId !== "string" || markerRunId.trim() === "") {
  process.stderr.write("arc-preflight: marker parse warning: discoveryRunId missing or empty\n");
  process.stdout.write("absent"); process.exit(0);
}

// (5) read rulings discoveryRunId and compare.
let rulingsRunId = "";
try {
  const rulings = JSON.parse(fs.readFileSync(rulingsPath, "utf8"));
  rulingsRunId = typeof rulings.discoveryRunId === "string" ? rulings.discoveryRunId : "";
} catch (exc) {
  // Unexpected — check_rulings already passed. Fail safely.
  process.stdout.write("absent"); process.exit(0);
}

if (markerRunId.trim() === rulingsRunId.trim()) {
  process.stdout.write("match");
} else {
  process.stdout.write("stale");
}
NODE
)"
  fi

  case "$marker_status" in
    match)
      # Run IDs agree — rulings are current. Gate passes.
      return 0
      ;;
    stale)
      gate "stale-rulings" \
        "A newer discovery ran since you ruled; the rulings file's discoveryRunId differs from the latest-discovery marker. Re-run '/arc discovery ${issue_id}' and re-rule — the saved rulings may not cover the current forks."
      ;;
    *)
      # absent (or empty, which is the safe default on any unexpected failure).
      # Before gating, check for the legacy-affirmed escape hatch — but ONLY when
      # invoked by the trusted /arc skill, which sets ARC_PREFLIGHT_TRUSTED_CALLER=1
      # on its `build` preflight call right AFTER it has run the required pre-build
      # delete of any pre-existing affirmed receipt. A direct, untrusted
      # `arc-preflight.sh build <N>` (which bypasses the skill and so skips that
      # delete) must NOT be able to satisfy the gate with a pre-seeded receipt, so
      # when the var is unset we skip the hatch entirely and fall through to
      # missing-latest-discovery. `finish` mode never reaches check_stale, so it
      # neither needs nor sets the var.
      #
      # HONEST SCOPE: the env var is caller-controlled, so it only closes the
      # "bypass the skill entirely via direct invocation" path — a caller that can
      # set ARC_PREFLIGHT_TRUSTED_CALLER=1 is already trusted, and could also write
      # a correctly-bound receipt. The real defense against a build forging a
      # receipt remains the skill's REQUIRED pre-build delete (SKILL.md); this var
      # is the second layer that stops a no-skill direct call from using a stale
      # pre-seeded receipt, and the affirmedAgainstRunId binding below is the third.
      # The gate honors ONLY the exact value the skill writes (=1), so a value a
      # human reads as untrusted (=0, =false) fails closed instead of opening the
      # hatch on any non-empty string.
      if [ -f "$affirmed" ] && [ "${ARC_PREFLIGHT_TRUSTED_CALLER:-}" = "1" ]; then
        # Self-void binding (mirrors seccheck's reviewedSha == HEAD model): the
        # affirmed receipt is gate-checked, not trusted on skill prose alone. It
        # must carry `affirmedAgainstRunId` equal to the rulings file's CURRENT
        # discoveryRunId, so it is bound to the exact discovery run it affirms —
        # the moment the maintainer re-rules against a NEW discovery (which
        # rewrites the rulings discoveryRunId), the old receipt no longer matches
        # and is rejected. This binding does NOT make the receipt un-forgeable by
        # the build: the build receives the current run id in its own args and can
        # copy it into a receipt, and it has write access to this gitignored dir.
        # The absent-marker case's real defense is the skill's REQUIRED pre-build
        # delete of any pre-existing affirmed receipt (SKILL.md); this binding is
        # the second layer that catches a stale receipt the delete missed. We read
        # the rulings discoveryRunId here (check_rulings already proved it parses
        # and is a non-empty string) and pass it to the validator as the bound value.
        local rulings_run_id=""
        if [ "$JSON_RUNNER" = "python3" ]; then
          rulings_run_id="$(python3 - "$rulings" <<'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    v = d.get("discoveryRunId", "")
    print(v.strip() if isinstance(v, str) else "")
except Exception:
    print("")
PY
)"
        else
          # Use the same fs.readFileSync + JSON.parse idiom as every other node block
          # in this file (NOT require()): require() strips a leading UTF-8 BOM and parses
          # anyway, while JSON.parse(readFileSync) throws on a BOM — so require() would
          # read a BOM-prefixed rulings file as a real run id while the marker validator
          # reads it as absent, an avoidable parity gap in a security-relevant binding.
          # require() also resolves only absolute/`./` paths and module-caches; uniform
          # readFileSync removes the silent dependency on $REPO always being absolute.
          rulings_run_id="$(node -e 'try{const fs=require("fs");const d=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));const v=d.discoveryRunId;process.stdout.write(typeof v==="string"?v.trim():"")}catch(e){process.stdout.write("")}' "$rulings" 2>/dev/null || true)"
        fi

        local affirmed_status=""
        if [ "$JSON_RUNNER" = "python3" ]; then
          affirmed_status="$(python3 - "$affirmed" "$issue_id" "$rulings_run_id" <<'PY' || true
import json, sys

path = sys.argv[1]
cli_issue = sys.argv[2]
bound_run_id = sys.argv[3]

try:
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    if not isinstance(d, dict):
        sys.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: root is not an object\n")
        print("invalid"); sys.exit(0)
except Exception as exc:
    sys.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: %s\n" % exc)
    print("invalid"); sys.exit(0)

# EXACT parity with check_spec / the marker extractor: first whitespace token ->
# segment after last hash -> numeric-only guard. Keeps the escape-hatch issue check
# no more permissive than check_spec. (Heredoc body kept apostrophe-free: a lone
# apostrophe breaks bash parsing of the surrounding "$(... <<PY ...)".)
def extract_id(raw):
    s = str(raw).strip()
    tok = s.split()[0] if s else ""
    tok = tok.rsplit("#", 1)[-1]
    return tok if tok.isdigit() else None

raw_issue = d.get("issue")
if raw_issue is None:
    sys.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: issue field missing\n")
    print("invalid"); sys.exit(0)
affirmed_id = extract_id(raw_issue)
cli_id = extract_id(cli_issue)
if affirmed_id is None or cli_id is None or affirmed_id != cli_id:
    sys.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: issue mismatch\n")
    print("invalid"); sys.exit(0)

if d.get("kind") != "legacy-affirmed":
    sys.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: kind is not legacy-affirmed\n")
    print("invalid"); sys.exit(0)

reason = d.get("reason")
if not isinstance(reason, str) or reason.strip() == "" or "\n" in reason or "\r" in reason:
    sys.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: reason must be a non-empty single-line string\n")
    print("invalid"); sys.exit(0)

# Self-void binding: affirmedAgainstRunId must equal the CURRENT rulings
# discoveryRunId. A receipt with no binding, a wrong binding, or one that no
# longer matches (because the maintainer re-ruled against a newer discovery)
# is invalid. This catches a stale receipt the skill delete missed; it does NOT
# stop a build from copying the current run id into a fresh receipt (the REQUIRED
# pre-build delete in the skill is what guards that case).
# (NOTE: keep this heredoc body apostrophe-free. It lives inside a
# "$(... <<PY ...)" command substitution, where a lone apostrophe breaks
# bash parsing of the surrounding command substitution.)
affirmed_against = d.get("affirmedAgainstRunId")
if not isinstance(affirmed_against, str) or affirmed_against.strip() == "":
    sys.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: affirmedAgainstRunId missing or empty\n")
    print("invalid"); sys.exit(0)
if bound_run_id == "" or affirmed_against.strip() != bound_run_id.strip():
    sys.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: affirmedAgainstRunId (%r) does not match the rulings discoveryRunId (%r)\n" % (affirmed_against, bound_run_id))
    print("invalid"); sys.exit(0)

print("ok")
PY
)"
        else
          affirmed_status="$(node - "$affirmed" "$issue_id" "$rulings_run_id" <<'NODE' || true
const fs = require("fs");
const path = process.argv[2];
const cliIssue = process.argv[3];
const boundRunId = process.argv[4] || "";

// EXACT parity with check_spec / the marker extractor: first whitespace token ->
// segment after last hash -> numeric-only guard. Keeps the escape-hatch issue check
// no more permissive than check_spec. (Heredoc body kept apostrophe-free: a lone
// apostrophe breaks bash parsing of the surrounding "$(... <<NODE ...)".)
function extractId(raw) {
  const s = String(raw).trim();
  let tok = s ? s.split(/\s+/)[0] : "";
  if (tok.includes("#")) tok = tok.slice(tok.lastIndexOf("#") + 1);
  return /^\d+$/.test(tok) ? tok : null;
}

let d;
try {
  d = JSON.parse(fs.readFileSync(path, "utf8"));
  if (typeof d !== "object" || d === null || Array.isArray(d)) {
    process.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: root is not an object\n");
    process.stdout.write("invalid"); process.exit(0);
  }
} catch (exc) {
  process.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: " + exc.message + "\n");
  process.stdout.write("invalid"); process.exit(0);
}

const rawIssue = d.issue;
if (rawIssue === undefined || rawIssue === null) {
  process.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: issue field missing\n");
  process.stdout.write("invalid"); process.exit(0);
}
const affirmedId = extractId(rawIssue);
const cliId = extractId(cliIssue);
if (affirmedId === null || cliId === null || affirmedId !== cliId) {
  process.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: issue mismatch\n");
  process.stdout.write("invalid"); process.exit(0);
}
if (d.kind !== "legacy-affirmed") {
  process.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: kind is not legacy-affirmed\n");
  process.stdout.write("invalid"); process.exit(0);
}
const reason = d.reason;
if (typeof reason !== "string" || reason.trim() === "" || reason.includes("\n") || reason.includes("\r")) {
  process.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: reason must be a non-empty single-line string\n");
  process.stdout.write("invalid"); process.exit(0);
}
// Self-void binding: affirmedAgainstRunId must equal the CURRENT rulings
// discoveryRunId (mirrors the seccheck reviewedSha == HEAD model). Self-voids
// when the maintainer re-rules against a newer discovery. It does NOT make the
// receipt un-forgeable by the build (the build holds the current run id in its
// args) — the REQUIRED pre-build delete in the skill guards that; this is the
// backstop that catches a stale receipt the delete missed. (Keep this heredoc
// body apostrophe-free: it lives inside a
// "$(... <<NODE ...)" command substitution where a lone apostrophe breaks
// bash parsing of the surrounding command substitution.)
const affirmedAgainst = d.affirmedAgainstRunId;
if (typeof affirmedAgainst !== "string" || affirmedAgainst.trim() === "") {
  process.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: affirmedAgainstRunId missing or empty\n");
  process.stdout.write("invalid"); process.exit(0);
}
if (boundRunId === "" || affirmedAgainst.trim() !== boundRunId.trim()) {
  process.stderr.write("arc-preflight: legacy-affirmed receipt parse warning: affirmedAgainstRunId (" + JSON.stringify(affirmedAgainst) + ") does not match the rulings discoveryRunId (" + JSON.stringify(boundRunId) + ")\n");
  process.stdout.write("invalid"); process.exit(0);
}
process.stdout.write("ok");
NODE
)"
        fi

        if [ "$affirmed_status" = "ok" ]; then
          # Valid legacy-affirmed receipt — escape hatch satisfied.
          return 0
        else
          # Receipt present but invalid — log already went to stderr inside the runner.
          # Fall through to the gate.
          :
        fi
      fi

      gate "missing-latest-discovery" \
        "No valid latest-discovery marker found for issue ${issue_id}. Either re-run '/arc discovery ${issue_id}' to regenerate the marker, OR write a legacy-affirmed receipt: .gstack/arc-rulings/${issue_id}-latest-discovery-affirmed.json with {issue, kind:'legacy-affirmed', reason:'<why rulings are current>', affirmedAgainstRunId:'<the rulings discoveryRunId>', ruledAt:'<ISO>'}. The affirmedAgainstRunId MUST equal the rulings file's discoveryRunId — a mismatched or missing binding is rejected (the gate, not prose, enforces currency)."
      ;;
  esac
}

# write_guarded <relname>: the ONE shared, TESTED write-hardening helper (issue #69,
# ruling rounds-json-write-hardening). Writes STDIN atomically to
# $REPO/.gstack/arc-rulings/<relname>, reusing the EXACT same hardening SKILL.md's
# discovery-marker write already documents as prose: per-component symlink-checked
# mkdir (never a single `mkdir -p`, which would follow a pre-existing symlinked
# `.gstack` and create `arc-rulings` at the resolved EXTERNAL location before any
# check fires), realpath-both-sides EXACT-equality containment (not `startsWith`, which
# would accept a sibling dir or an in-repo-but-wrong directory), then a `mktemp` +
# `mv -f` atomic rename. Both the latest-discovery marker write AND the new
# `<issue>-rounds.jsonl` telemetry write call this SAME function — no more forking a
# third near-identical prose block per issue #69's needs-adr / write-hardening rulings.
#
# <relname> MUST be a bare filename directly under arc-rulings/ (no `/`, no `..`): the
# containment check above only protects the DIRECTORY, not the filename component — an
# unsanitized issue id (e.g. containing `/` or `..`) could otherwise escape via the
# concatenated path even though the directory itself is verified. Reject anything else.
#
# <trust> (OPTIONAL trailing arg, default "hardened" when omitted) switches the
# containment check's speed per symlink-containment-thin — see
# dir_ancestor_containment_guard's own doc comment and the TRUST PROFILE block at the
# top of this file. Deliberately trailing+optional (not inserted before relname) so any
# not-yet-migrated call site that only ever passes one arg keeps working exactly as
# before rather than silently corrupting its filename argument.
write_guarded() {
  local relname="$1"
  local trust="${2:-hardened}"
  case "$relname" in
    ''|*/*|*..*)
      echo "arc-preflight: write_guarded: relname '${relname}' must be a bare filename (no '/', no '..')" >&2
      exit 2
      ;;
  esac
  if ! printf '%s' "$relname" | grep -qE '^[A-Za-z0-9_.-]+$'; then
    echo "arc-preflight: write_guarded: relname '${relname}' contains unexpected characters (only [A-Za-z0-9_.-] allowed)" >&2
    exit 2
  fi

  # (1) per-component symlink-checked mkdir. Each component is created race-safely
  # (mkdir, tolerate an already-existing DIR, fail loud otherwise) THEN symlink-checked,
  # so mkdir never traverses a planted symlink at any level. The mkdir-first form is
  # atomic: a check-then-`mkdir` on a bare `[ -e ]` false could lose a race to a
  # concurrent arc run (this repo runs parallel arcs) and abort a legitimate write with
  # a bare "File exists". These plain `-L` checks run UNCONDITIONALLY at every trust
  # level — they are the "quick test ! -L" half of symlink-containment-thin, never
  # thinned further; only the elaborate ancestor-of-REPO resolution below (2) varies.
  mkdir "$REPO/.gstack" 2>/dev/null || [ -d "$REPO/.gstack" ] || { echo "arc-preflight: write_guarded: cannot create $REPO/.gstack (exists as a non-directory?)" >&2; exit 2; }
  if [ -L "$REPO/.gstack" ]; then
    echo "arc-preflight: write_guarded: $REPO/.gstack is a symlink — refusing to write through it" >&2
    exit 2
  fi
  mkdir "$REPO/.gstack/arc-rulings" 2>/dev/null || [ -d "$REPO/.gstack/arc-rulings" ] || { echo "arc-preflight: write_guarded: cannot create $REPO/.gstack/arc-rulings (exists as a non-directory?)" >&2; exit 2; }
  if [ -L "$REPO/.gstack/arc-rulings" ]; then
    echo "arc-preflight: write_guarded: $REPO/.gstack/arc-rulings is a symlink — refusing to write through it" >&2
    exit 2
  fi

  # (2) the ELABORATE half of symlink-containment-thin: realpath-both-sides
  # EXACT-equality containment (catches a symlinked ancestor of REPO, e.g. macOS /tmp ->
  # /private/tmp). Runs under trust=hardened; trust=solo skips this resolution entirely
  # and relies on the always-on (1)/(2b) `-L` checks — see
  # dir_ancestor_containment_guard's doc comment. Either way `resolved_dir` ends up
  # naming the same directory; solo just doesn't pay for the extra `pwd -P` round trips,
  # and its value is the RAW (unresolved) path rather than the canonicalized one.
  local resolved_dir
  resolved_dir="$(dir_ancestor_containment_guard "$REPO/.gstack/arc-rulings" ".gstack/arc-rulings" "$trust")"

  # (2b) leaf-symlink refusal for symmetry with append_guarded_line's read-side check.
  # `mv -f` at step (3) already de-symlinks the destination (replaces the link with the tmp
  # file), so the write itself never follows a symlinked leaf out of the dir — but refusing
  # loudly is more conservative than silently de-symlinking a planted link, and keeps both
  # guarded-write paths symmetric. A legitimate rounds/marker file is never a symlink.
  if [ -L "$resolved_dir/$relname" ]; then
    echo "arc-preflight: write_guarded: $resolved_dir/$relname is a symlink — refusing to write through it" >&2
    exit 2
  fi
  # (2c) refuse a non-regular-file leaf (e.g. a directory planted at the target path). The
  # symlink case is handled above, so anything here that exists but is not a regular file is a
  # dir/fifo/device: `mv -f "$tmp" "$dir/leaf"` would move the temp file INSIDE a directory leaf
  # instead of atomically replacing the intended file — a silent write-to-wrong-location. A real
  # rounds/marker file is always a regular file.
  if [ -e "$resolved_dir/$relname" ] && [ ! -f "$resolved_dir/$relname" ]; then
    echo "arc-preflight: write_guarded: $resolved_dir/$relname exists and is not a regular file — refusing to write" >&2
    exit 2
  fi

  # (3) write to a mktemp file INSIDE the verified dir, then atomic rename. The real
  # TOCTOU defense is step (2)'s containment check, not `mv -f` itself — `mv -f` only
  # gives atomic replace-at-destination, not symlink safety on its own.
  local tmp
  tmp="$(mktemp "$resolved_dir/.${relname}.XXXXXX")" || { echo "arc-preflight: write_guarded: mktemp failed in $resolved_dir" >&2; exit 2; }
  cat > "$tmp"
  mv -f "$tmp" "$resolved_dir/$relname"
}

# release_lock: the trap-dispatched lock releaser for append_guarded_line. Defined as a
# FUNCTION on purpose (issue #69) so the trap STRINGS below carry NO `$` at all — a trap
# body is re-parsed as a command when it fires, and any path data left in the trap string
# (even single-quoted `$lockdir`) is re-tokenized at fire time. Under bash 3.2.57 (the
# macOS default /bin/bash the kit's own suite runs under) that re-tokenization was observed
# to EXECUTE a `$(…)` command-substitution payload embedded in the repo path (via
# `git rev-parse --show-toplevel` -> $REPO -> $lockdir) — a low-frequency command injection.
# Dispatching to a named function removes every `$` from the trap string, so there is
# nothing to re-tokenize; `$lockdir` is expanded ONLY here, as an ordinary parameter
# expansion at call time (its value is never re-scanned for `$(…)`). `lockdir` is `local`
# to append_guarded_line, and release_lock only ever fires while that function is on the
# call stack (all early `exit`s that trip the traps happen inside it; the normal path
# clears the EXIT trap before returning), so dynamic scope makes `$lockdir` visible here.
# `${lockdir:-}` guards the theoretical set -u case where it is unset.
release_lock() {
  [ -n "${lockdir:-}" ] && rmdir "$lockdir" 2>/dev/null || true
}

# append_guarded_line <relname>: appends ONE line (read from STDIN, no trailing
# newline expected) to $REPO/.gstack/arc-rulings/<relname> — used for the append-only
# <issue>-rounds.jsonl telemetry file (ruling rounds-json-retention: one run-record per
# line, build then finish; NEVER a single-overwrite whole-object file). Reuses
# write_guarded() for the actual write (a read-modify-write of the FULL file content,
# per the ruling's explicit "reuse the full marker-write hardening" instruction — NOT a
# raw O_APPEND syscall, which would reopen the write_guarded containment gap this
# function exists to close). Because read-modify-write is not safe under CONCURRENT
# writers on its own (two overlapping callers could both read the pre-append content and
# the later `mv -f` would silently drop the earlier writer's line), this function
# additionally serializes callers with a per-file mkdir lock sentinel — an ADDITIVE
# safety measure around the ruled mechanism, not a substitution of it.
#
# <trust> (OPTIONAL trailing arg, default "hardened" when omitted, same contract as
# write_guarded) threads straight through to the write_guarded call below — this
# function's OWN upfront dir checks are already the plain `-L` form (there is no
# "elaborate" version of a single `-L` test to further thin), so the only place trust
# actually changes behavior here is inside the delegated write_guarded call.
append_guarded_line() {
  local relname="$1"
  local trust="${2:-hardened}"
  local line
  line="$(cat)"

  # Reuse the same bare-filename validation write_guarded enforces, so the lock
  # sentinel path (derived from relname) can't itself be used to escape the dir.
  case "$relname" in
    ''|*/*|*..*)
      echo "arc-preflight: append_guarded_line: relname '${relname}' must be a bare filename (no '/', no '..')" >&2
      exit 2
      ;;
  esac
  if ! printf '%s' "$relname" | grep -qE '^[A-Za-z0-9_.-]+$'; then
    echo "arc-preflight: append_guarded_line: relname '${relname}' contains unexpected characters (only [A-Za-z0-9_.-] allowed)" >&2
    exit 2
  fi

  # Ensure the dir exists + is safe BEFORE acquiring the lock (mirrors write_guarded's
  # own per-component symlink checks, so the lock dir itself is never created through a
  # planted symlink). Race-safe: mkdir-first, tolerate an already-existing DIR, fail
  # loud otherwise — a check-then-`mkdir` on a bare `[ -e ]` false could lose a race to a
  # concurrent arc run and abort a legitimate append with a bare "File exists".
  mkdir "$REPO/.gstack" 2>/dev/null || [ -d "$REPO/.gstack" ] || { echo "arc-preflight: append_guarded_line: cannot create $REPO/.gstack (exists as a non-directory?)" >&2; exit 2; }
  [ -L "$REPO/.gstack" ] && { echo "arc-preflight: append_guarded_line: $REPO/.gstack is a symlink — refusing" >&2; exit 2; }
  mkdir "$REPO/.gstack/arc-rulings" 2>/dev/null || [ -d "$REPO/.gstack/arc-rulings" ] || { echo "arc-preflight: append_guarded_line: cannot create $REPO/.gstack/arc-rulings (exists as a non-directory?)" >&2; exit 2; }
  [ -L "$REPO/.gstack/arc-rulings" ] && { echo "arc-preflight: append_guarded_line: $REPO/.gstack/arc-rulings is a symlink — refusing" >&2; exit 2; }

  local lockdir="$REPO/.gstack/arc-rulings/.${relname}.lock.d"
  local waited=0
  while ! mkdir "$lockdir" 2>/dev/null; do
    waited=$((waited + 1))
    if [ "$waited" -gt 100 ]; then
      echo "arc-preflight: append_guarded_line: could not acquire the append lock for '${relname}' after ~10s — a stale lock dir may remain at ${lockdir}; remove it by hand if no other append is genuinely in flight" >&2
      exit 2
    fi
    sleep 0.1
  done
  # Deterministic release below covers the normal path; these traps are belt-and-
  # suspenders for an early-exit or an interactive kill within this single process. A bash
  # EXIT trap does NOT fire on an untrapped SIGINT/SIGTERM, so INT/TERM are trapped explicitly
  # (release the lock, then exit 130) — otherwise a Ctrl-C mid-append would strand the lock dir
  # (recoverable via the stale-lock message at acquire, but a hard-kill SIGKILL still cannot be
  # trapped and remains the one uncovered case).
  # The trap STRINGS dispatch to release_lock (defined above) and carry NO `$` — nothing to
  # re-tokenize when the trap fires. $lockdir is expanded only inside release_lock, at call
  # time, as an ordinary parameter (its value is never re-parsed for `$(…)`). This is the
  # bash-3.2-robust form of the deferred-expansion fix; an inline `$lockdir` in the trap
  # string (even single-quoted) was observed to re-tokenize and execute a repo-path payload.
  trap release_lock EXIT
  trap 'release_lock; exit 130' INT TERM

  # Leaf-symlink refusal on the READ side. The dir checks above and write_guarded's
  # containment guard harden the DIRECTORY components, but `[ -f ]` and `cat` both FOLLOW a
  # symlinked LEAF — so a planted `<relname> -> ~/.ssh/id_rsa` (or an out-of-repo .env) would
  # be slurped by `cat` and rewritten into the guarded file (the later `mv -f` de-symlinks,
  # replacing the link with a regular file now holding the leaked bytes). Refuse to read
  # through a symlinked leaf, fail-closed, before touching it. The target is either absent,
  # or a real regular file — anything else halts.
  if [ -L "$REPO/.gstack/arc-rulings/$relname" ]; then
    echo "arc-preflight: append_guarded_line: $REPO/.gstack/arc-rulings/$relname is a symlink — refusing to read through it" >&2
    exit 2
  fi
  local existing=""
  if [ -f "$REPO/.gstack/arc-rulings/$relname" ]; then
    existing="$(cat "$REPO/.gstack/arc-rulings/$relname")"
  fi
  if [ -n "$existing" ]; then
    printf '%s\n%s\n' "$existing" "$line" | write_guarded "$relname" "$trust"
  else
    printf '%s\n' "$line" | write_guarded "$relname" "$trust"
  fi

  release_lock
  trap - EXIT
}

# ===========================================================================
# Decision inbox (issue #79, AU2) — the SOLE locked read-modify-write path for
# .gstack/arc-rulings/pending-decisions.json.
#
# pending-decisions.json is a single mutable KEYED-OBJECT document (not append-only
# JSONL), touched by several independent writers over a build's lifetime: discovery
# adding a Tier-A entry, a mid-build blocked-on-decision fork adding another, the
# notifier stamping notifiedAt after a successful digest push, build-dispatch clearing
# an entry once its ruling is consumed, and a defensive sweep clearing any entry whose
# issue already has a saved rulings file. write_guarded alone only guarantees an atomic
# WHOLE-FILE overwrite; it does not make a caller's own read-then-mutate-then-write
# cycle safe against another caller doing the same in an overlapping window. Since the
# discoveries-cap-enforcement ruling explicitly allows up to 2 concurrent discoveries,
# two real writers CAN race here — so, exactly like append_guarded_line's per-file mkdir
# lock (reused, not reinvented), every mutation below is serialized by ONE lock sentinel
# per file and every writer goes through this ONE function. Nothing else may write this
# file directly.
pending_decision_mutate() {
  local op="$1" issue_raw="${2:-}" trust="${3:-hardened}"
  local issue="-"
  if [ "$op" != "sweep" ]; then
    issue="$(normalize_issue_id "$issue_raw")"
  fi

  mkdir "$REPO/.gstack" 2>/dev/null || [ -d "$REPO/.gstack" ] || { echo "arc-preflight: pending_decision_mutate: cannot create $REPO/.gstack (exists as a non-directory?)" >&2; exit 2; }
  if [ -L "$REPO/.gstack" ]; then
    echo "arc-preflight: pending_decision_mutate: $REPO/.gstack is a symlink — refusing to write through it" >&2
    exit 2
  fi
  mkdir "$REPO/.gstack/arc-rulings" 2>/dev/null || [ -d "$REPO/.gstack/arc-rulings" ] || { echo "arc-preflight: pending_decision_mutate: cannot create $REPO/.gstack/arc-rulings" >&2; exit 2; }
  if [ -L "$REPO/.gstack/arc-rulings" ]; then
    echo "arc-preflight: pending_decision_mutate: $REPO/.gstack/arc-rulings is a symlink — refusing to write through it" >&2
    exit 2
  fi

  local relname="pending-decisions.json"
  local lockdir="$REPO/.gstack/arc-rulings/.${relname}.lock.d"
  local waited=0
  while ! mkdir "$lockdir" 2>/dev/null; do
    waited=$((waited + 1))
    if [ "$waited" -gt 100 ]; then
      echo "arc-preflight: pending_decision_mutate: could not acquire the lock for '${relname}' after ~10s — a stale lock dir may remain at ${lockdir}; remove it by hand if no other pending-decision write is genuinely in flight" >&2
      exit 2
    fi
    sleep 0.1
  done
  # Same deferred-expansion trap dispatch as append_guarded_line/risk_ratchet_write
  # (release_lock is a named function so the trap string carries no `$` to re-tokenize).
  trap release_lock EXIT
  trap 'release_lock; exit 130' INT TERM

  local existing_path="$REPO/.gstack/arc-rulings/$relname"
  if [ -L "$existing_path" ]; then
    echo "arc-preflight: pending_decision_mutate: $existing_path is a symlink — refusing to read through it" >&2
    exit 2
  fi

  # For "add", the payload (kind/preview/pointerFile/contentHash — all untrusted,
  # LLM/issue-authored text) is read from STDIN as a JSON object, never string-
  # interpolated into a shell command; it reaches the JSON_RUNNER script below as a
  # single argv element (argv is not shell-reparsed), never spliced into script text.
  local payload="{}"
  if [ "$op" = "add" ]; then
    payload="$(cat)"
  fi

  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if [ -z "$JSON_RUNNER" ]; then
    echo "arc-preflight: pending_decision_mutate: no python3 or node found — cannot safely mutate JSON" >&2
    exit 2
  fi

  local out rc=0
  if [ "$JSON_RUNNER" = "python3" ]; then
    out="$(python3 - "$existing_path" "$op" "$issue" "$now" "$payload" <<'PY'
import json, os, sys

existing_path, op, issue, now, payload_raw = sys.argv[1:6]

doc = {}
if os.path.isfile(existing_path):
    with open(existing_path, encoding="utf-8") as f:
        raw = f.read()
    # An EXISTING, non-empty file that fails to parse is CORRUPT, not empty: refuse to
    # overwrite it (that would silently destroy every waiting entry, contradicting the
    # present-but-corrupt-is-unreadable-never-empty contract that pending_decisions_read
    # already enforces on the read side). A truly empty/whitespace-only file is the
    # legitimate fresh-doc case (start from an empty object).
    if raw.strip():
        try:
            loaded = json.loads(raw)
        except Exception as e:
            print("arc-preflight: pending_decision_mutate: refusing to mutate a present-but-corrupt pending-decisions.json (%s) — treat it as UNREADABLE, not empty; inspect .gstack/arc-rulings/pending-decisions.json by hand before any write" % e, file=sys.stderr)
            sys.exit(2)
        if not isinstance(loaded, dict):
            print("arc-preflight: pending_decision_mutate: pending-decisions.json is present but not a JSON object — refusing to mutate/overwrite it; inspect it by hand", file=sys.stderr)
            sys.exit(2)
        # A present `entries` that is not an object is corrupt too (matching the strict
        # read side): resetting it to {} here would silently overwrite real data of an
        # unknown shape. A MISSING entries key is harmless (an empty/new doc) — reject
        # only a present-but-wrong-shape one.
        if "entries" in loaded and not isinstance(loaded.get("entries"), dict):
            print("arc-preflight: pending_decision_mutate: pending-decisions.json has a present-but-non-object `entries` field — treating it as corrupt and refusing to mutate/overwrite it; inspect it by hand", file=sys.stderr)
            sys.exit(2)
        doc = loaded
doc["schemaVersion"] = 1
if not isinstance(doc.get("entries"), dict):
    doc["entries"] = {}
entries = doc["entries"]

if op == "add":
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    prior = entries.get(issue) if isinstance(entries.get(issue), dict) else {}
    kind = payload.get("kind")
    if kind not in ("tier-a", "blocked-on-decision"):
        kind = prior.get("kind", "tier-a")
    content_hash = payload.get("contentHash") or prior.get("contentHash", "")
    entries[issue] = {
        "issue": issue,
        "kind": kind,
        "preview": payload.get("preview", prior.get("preview", "")),
        "pointerFile": payload.get("pointerFile", prior.get("pointerFile", "")),
        "contentHash": content_hash,
        "notifiedAt": prior.get("notifiedAt"),
        "notifiedContentHash": prior.get("notifiedContentHash"),
        "createdAt": prior.get("createdAt", now),
        "updatedAt": now,
    }
elif op == "clear":
    entries.pop(issue, None)
elif op == "stamp-notified":
    entry = entries.get(issue)
    if isinstance(entry, dict):
        entry["notifiedAt"] = now
        entry["notifiedContentHash"] = entry.get("contentHash")
elif op == "sweep":
    rulings_dir = os.path.dirname(existing_path)
    for iss in list(entries.keys()):
        safe_iss = iss if isinstance(iss, str) and iss.isdigit() else None
        if safe_iss is None:
            continue
        rulings_path = os.path.join(rulings_dir, safe_iss + "-pr1-args.json")
        # Proof of a saved ruling is a REAL regular file, not a symlink: os.path.isfile
        # follows symlinks, so a stray/planted `<issue>-pr1-args.json` symlink pointing at
        # any existing file would falsely mark this issue "already ruled" and silently
        # clear a genuinely-waiting entry from the sole waiting-on-you record. Mirror the
        # -L symlink guard the rest of this file applies to arc-rulings leaves.
        if os.path.isfile(rulings_path) and not os.path.islink(rulings_path):
            entries.pop(iss, None)
else:
    print("arc-preflight: pending_decision_mutate: unknown op '%s'" % op, file=sys.stderr)
    sys.exit(2)

print(json.dumps(doc))
PY
)" && rc=0 || rc=$?
  else
    out="$(node -e '
const fs = require("fs");
const path = require("path");
const [existingPath, op, issue, now, payloadRaw] = process.argv.slice(1);

// An EXISTING, non-empty file that fails to parse is CORRUPT, not empty: refuse to
// overwrite it (that would silently destroy every waiting entry, contradicting the
// present-but-corrupt-is-unreadable-never-empty contract pending_decisions_read
// enforces on the read side). A truly empty/whitespace-only file is the legitimate
// fresh-doc case.
let doc = {};
if (fs.existsSync(existingPath)) {
  const raw = fs.readFileSync(existingPath, "utf8");
  if (raw.trim()) {
    let loaded;
    try {
      loaded = JSON.parse(raw);
    } catch (e) {
      process.stderr.write("arc-preflight: pending_decision_mutate: refusing to mutate a present-but-corrupt pending-decisions.json (" + e.message + ") — treat it as UNREADABLE, not empty; inspect .gstack/arc-rulings/pending-decisions.json by hand before any write\n");
      process.exit(2);
    }
    if (!loaded || typeof loaded !== "object" || Array.isArray(loaded)) {
      process.stderr.write("arc-preflight: pending_decision_mutate: pending-decisions.json is present but not a JSON object — refusing to mutate/overwrite it; inspect it by hand\n");
      process.exit(2);
    }
    // A present `entries` that is not an object is corrupt too (matching the read side):
    // resetting it to {} would silently overwrite real data. A MISSING entries key is
    // harmless (empty/new doc); only reject a present-but-wrong-shape one.
    if (Object.prototype.hasOwnProperty.call(loaded, "entries") && (typeof loaded.entries !== "object" || loaded.entries === null || Array.isArray(loaded.entries))) {
      process.stderr.write("arc-preflight: pending_decision_mutate: pending-decisions.json has a present-but-non-object `entries` field — treating it as corrupt and refusing to mutate/overwrite it; inspect it by hand\n");
      process.exit(2);
    }
    doc = loaded;
  }
}
doc.schemaVersion = 1;
if (!doc.entries || typeof doc.entries !== "object" || Array.isArray(doc.entries)) doc.entries = {};
const entries = doc.entries;

if (op === "add") {
  let payload = {};
  try { payload = payloadRaw ? JSON.parse(payloadRaw) : {}; } catch (e) { payload = {}; }
  if (!payload || typeof payload !== "object") payload = {};
  const prior = (entries[issue] && typeof entries[issue] === "object") ? entries[issue] : {};
  let kind = payload.kind;
  if (kind !== "tier-a" && kind !== "blocked-on-decision") kind = prior.kind || "tier-a";
  const contentHash = payload.contentHash || prior.contentHash || "";
  entries[issue] = {
    issue,
    kind,
    preview: payload.preview !== undefined ? payload.preview : (prior.preview || ""),
    pointerFile: payload.pointerFile !== undefined ? payload.pointerFile : (prior.pointerFile || ""),
    contentHash,
    notifiedAt: prior.notifiedAt !== undefined ? prior.notifiedAt : null,
    notifiedContentHash: prior.notifiedContentHash !== undefined ? prior.notifiedContentHash : null,
    createdAt: prior.createdAt || now,
    updatedAt: now,
  };
} else if (op === "clear") {
  delete entries[issue];
} else if (op === "stamp-notified") {
  const entry = entries[issue];
  if (entry && typeof entry === "object") {
    entry.notifiedAt = now;
    entry.notifiedContentHash = entry.contentHash;
  }
} else if (op === "sweep") {
  const rulingsDir = path.dirname(existingPath);
  for (const iss of Object.keys(entries)) {
    if (!/^[0-9]+$/.test(iss)) continue;
    const rulingsPath = path.join(rulingsDir, iss + "-pr1-args.json");
    // Proof of a saved ruling is a REAL regular file, not a symlink: fs.existsSync
    // follows symlinks, so a stray/planted symlink here would falsely mark this issue
    // "already ruled" and silently clear a genuinely-waiting entry. lstatSync does not
    // follow the link, so a symlink reports isFile() === false (existsSync short-circuits
    // the lstat for a truly-absent path). Mirrors the python islink guard and the -L
    // guards elsewhere in this file.
    if (fs.existsSync(rulingsPath) && fs.lstatSync(rulingsPath).isFile()) delete entries[iss];
  }
} else {
  process.stderr.write("arc-preflight: pending_decision_mutate: unknown op \x27" + op + "\x27\n");
  process.exit(2);
}

process.stdout.write(JSON.stringify(doc));
' "$existing_path" "$op" "$issue" "$now" "$payload")" && rc=0 || rc=$?
  fi

  if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
    echo "arc-preflight: pending_decision_mutate: JSON mutation (op=${op}) failed" >&2
    exit 2
  fi

  printf '%s\n' "$out" | write_guarded "$relname" "$trust"

  release_lock
  trap - EXIT
}

# pending_decisions_read: read-only, symlink-guarded dump of pending-decisions.json for
# /arc resume and digest generation. Absent file -> the empty-but-valid document (this IS
# an empty inbox, not an error). A present-but-CORRUPT file is a DIFFERENT, louder case
# (issue #79 prep review): print nothing on stdout and exit 2 so the caller can tell
# "no entries" from "could not read the entries" and surface a warning instead of
# silently treating a corrupt file as an empty inbox.
pending_decisions_read() {
  local relname="pending-decisions.json"
  local existing_path="$REPO/.gstack/arc-rulings/$relname"
  # Guard the PARENT directories the same way pending_decision_mutate() (the write path
  # for this same file) and write_guarded() do — a leaf-only -L check is not enough. If
  # `.gstack` or `.gstack/arc-rulings` is a symlink to an empty/foreign directory, the
  # leaf check below passes (no leaf exists through the link) and the function would fall
  # through to the empty-inbox document, silently reporting zero waiting decisions — the
  # exact "silently report nothing waiting" failure this inbox exists to make impossible.
  if [ -L "$REPO/.gstack" ]; then
    echo "arc-preflight: pending_decisions_read: $REPO/.gstack is a symlink — refusing to read through it" >&2
    exit 2
  fi
  if [ -L "$REPO/.gstack/arc-rulings" ]; then
    echo "arc-preflight: pending_decisions_read: $REPO/.gstack/arc-rulings is a symlink — refusing to read through it" >&2
    exit 2
  fi
  if [ -L "$existing_path" ]; then
    echo "arc-preflight: pending_decisions_read: $existing_path is a symlink — refusing to read through it" >&2
    exit 2
  fi
  # A present-but-non-regular leaf (directory/FIFO/device) is UNREADABLE, not "absent" —
  # `[ ! -f ]` alone would treat a directory as an empty inbox, disagreeing with the mutate
  # side (write_guarded), which refuses a non-regular-file leaf. Exit 2 for parity so the
  # caller surfaces "could not read" rather than silently reporting an empty inbox.
  if [ -e "$existing_path" ] && [ ! -f "$existing_path" ]; then
    echo "arc-preflight: pending_decisions_read: $existing_path exists but is not a regular file — treat the inbox as UNREADABLE, not empty" >&2
    exit 2
  fi
  if [ ! -f "$existing_path" ]; then
    printf '{"schemaVersion":1,"entries":{}}\n'
    return 0
  fi
  if [ -z "$JSON_RUNNER" ]; then
    echo "arc-preflight: pending_decisions_read: no python3 or node found — cannot validate the file; treat the inbox as UNREADABLE, not empty" >&2
    exit 2
  fi
  if [ "$JSON_RUNNER" = "python3" ]; then
    python3 - "$existing_path" <<'PY' || exit 2
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), dict):
        raise ValueError("malformed pending-decisions.json shape")
    print(json.dumps(doc))
except Exception as e:
    print("arc-preflight: pending_decisions_read: %s" % e, file=sys.stderr)
    sys.exit(1)
PY
  else
    node -e '
const fs = require("fs");
try {
  const doc = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
  // `typeof [] === "object"` in JS, so without the Array.isArray exclusions an
  // `entries:[]` (or a top-level array) corrupt file would pass as a valid EMPTY inbox
  // on the node runner — the exact silent-empty failure the python read side and BOTH
  // mutate branches reject. Keep all three shape checks in parity.
  if (!doc || typeof doc !== "object" || Array.isArray(doc) || !doc.entries || typeof doc.entries !== "object" || Array.isArray(doc.entries)) {
    throw new Error("malformed pending-decisions.json shape");
  }
  process.stdout.write(JSON.stringify(doc));
} catch (e) {
  process.stderr.write("arc-preflight: pending_decisions_read: " + e.message + "\n");
  process.exit(1);
}
' "$existing_path" || exit 2
  fi
}

# ===========================================================================
# Global discovery concurrency lease (issue #79, AU2, ruling discoveries-cap-
# enforcement) — a MACHINE/ACCOUNT-level limiter, deliberately OUTSIDE any single
# repo (a lease taken while running discovery in repo A must also refuse a 3rd
# concurrent discovery started in repo B). Cap is fixed at 2 concurrent leases
# (memory: parallel-discoveries-throttle-opus — 3+ parallel discoveries rate-limit
# the Opus API). Each lease is a plain file (not a lock directory: multiple leases
# must coexist up to the cap) named with a random token under
# ${ARC_GLOBAL_LEASE_DIR:-$HOME/.claude/arc-discovery-leases}. Staleness is TTL-based
# (default 4 hours — generous: normal discovery runtimes are minutes, not hours) so a
# crashed/killed discovery session's lease self-expires rather than permanently
# starving the cap; a maintainer who confirms a lease is dead sooner can also remove
# the file by hand (the remedy message names the exact path).
ARC_GLOBAL_LEASE_CAP=2
ARC_GLOBAL_LEASE_TTL_SECONDS=$((4 * 60 * 60))

_arc_global_lease_dir() {
  printf '%s' "${ARC_GLOBAL_LEASE_DIR:-$HOME/.claude/arc-discovery-leases}"
}

# _arc_lease_is_stale <file>: true (exit 0) if the lease's mtime is older than the TTL.
# Portable mtime read: GNU stat (-c) then BSD/macOS stat (-f); if neither exists, fail
# safe by treating the lease as NOT stale (never falsely reclaim a live lease just
# because this environment lacks a stat we recognize).
_arc_lease_is_stale() {
  local f="$1" mtime now
  if mtime="$(stat -c '%Y' "$f" 2>/dev/null)"; then
    :
  elif mtime="$(stat -f '%m' "$f" 2>/dev/null)"; then
    :
  else
    return 1
  fi
  now="$(date -u +%s)"
  [ $((now - mtime)) -gt "$ARC_GLOBAL_LEASE_TTL_SECONDS" ]
}

# discovery_lease_acquire [label]: prints the lease token (its absolute file path) on
# stdout on success, exit 0. On refusal (cap reached with no stale lease to reclaim),
# prints nothing on stdout, a plain-language remedy on stderr, and exits non-zero — the
# skill maps this the same way as any other preflight-failed token.
discovery_lease_acquire() {
  local label="${1:-discovery}"
  local dir
  dir="$(_arc_global_lease_dir)"
  mkdir -p "$dir" 2>/dev/null || { echo "arc-preflight: discovery_lease_acquire: cannot create lease dir $dir" >&2; exit 2; }
  if [ -L "$dir" ]; then
    echo "arc-preflight: discovery_lease_acquire: $dir is a symlink — refusing to use it" >&2
    exit 2
  fi

  # The reap -> count -> mint sequence below is a check-then-act critical section: two
  # concurrent acquirers that both counted BEFORE either minted could each see a
  # sub-cap count and both mint, blowing past the cap the discoveries-cap-enforcement
  # ruling requires be actually ENFORCED (not advisory). Serialize it with the SAME
  # portable per-dir mkdir sentinel + release_lock-trap pattern append_guarded_line /
  # pending_decision_mutate already use (reused, not reinvented) — held ONLY for the
  # reap/count/mint window, released the instant the new lease exists (or on any early
  # exit via the trap), so live leases still coexist up to the cap. `lockdir` is the
  # name release_lock reads via dynamic scope.
  local lockdir="$dir/.leases.lock.d"
  local waited=0
  while ! mkdir "$lockdir" 2>/dev/null; do
    waited=$((waited + 1))
    if [ "$waited" -gt 100 ]; then
      echo "arc-preflight: discovery_lease_acquire: could not acquire the lease-dir lock after ~10s — a stale lock dir may remain at ${lockdir}; remove it by hand if no other discovery lease is genuinely being acquired right now" >&2
      exit 2
    fi
    sleep 0.1
  done
  trap release_lock EXIT
  trap 'release_lock; exit 130' INT TERM

  # Reap stale leases first — a crashed discovery's lease should never permanently
  # occupy a cap slot.
  local f
  for f in "$dir"/lease-*; do
    [ -e "$f" ] || continue
    if _arc_lease_is_stale "$f"; then
      rm -f "$f" 2>/dev/null || true
    fi
  done

  local live_count
  live_count=$(find "$dir" -maxdepth 1 -name 'lease-*' -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "${live_count:-0}" -ge "$ARC_GLOBAL_LEASE_CAP" ]; then
    echo "arc-preflight: discovery_lease_acquire: ${ARC_GLOBAL_LEASE_CAP} concurrent discoveries are already running across this machine (leases in $dir) — refusing a new one. If you're sure one is dead (crashed session), inspect the files there (each names its repo + start time) and remove the stale one by hand, or wait for its ${ARC_GLOBAL_LEASE_TTL_SECONDS}s TTL to expire." >&2
    exit 2
  fi

  local token now
  token="$(mktemp "$dir/lease-XXXXXX")" || { echo "arc-preflight: discovery_lease_acquire: mktemp failed in $dir" >&2; exit 2; }
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"label":%s,"repo":%s,"pid":%s,"startedAt":"%s"}\n' \
    "$(printf '%s' "$label" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip(chr(10))))' 2>/dev/null || printf '"%s"' "$label")" \
    "$(printf '%s' "$REPO" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip(chr(10))))' 2>/dev/null || printf '"%s"' "$REPO")" \
    "$$" "$now" > "$token"

  # The lease now exists and counts toward the cap for the NEXT acquirer — safe to drop
  # the critical-section lock before returning the token.
  release_lock
  trap - EXIT INT TERM
  printf '%s\n' "$token"
}

# discovery_lease_release <token>: idempotent delete-if-present, containment-checked so
# a malformed token can't be used to delete an arbitrary path.
discovery_lease_release() {
  local token="${1:-}"
  [ -z "$token" ] && return 0
  local dir
  dir="$(_arc_global_lease_dir)"
  local resolved_dir resolved_token
  resolved_dir="$(cd "$dir" 2>/dev/null && pwd -P)" || return 0
  resolved_token="$(cd "$(dirname "$token")" 2>/dev/null && pwd -P)/$(basename "$token")" || return 0
  case "$resolved_token" in
    "$resolved_dir"/lease-*) rm -f "$resolved_token" 2>/dev/null || true ;;
    *) echo "arc-preflight: discovery_lease_release: token '$token' is not inside the lease dir — refusing to delete" >&2 ;;
  esac
}

# ===========================================================================
# riskcheck — L3 (#119): right-size review CEREMONY to RISK, never to safety.
#
# Categorically DIFFERENT in kind from every gate above, and documented here
# separately on purpose so a future edit doesn't quietly "fix" it into a real
# gate (per the maintainer's own ruled `what-risk-controls`):
#   * riskcheck NEVER blocks pr-ready or the build. It never skips a pass/fail
#     safety check either — tests, coverage, seccheck, govcheck, and the
#     cross-family reviewer always run regardless of what riskcheck says.
#     Those stay locked at max (Armor A, docs/ARMOR-TAXONOMY.md). riskcheck
#     only ever turns review INTENSITY up or down (round count, panel size) —
#     it is advisory-only.
#   * riskcheck therefore does NOT follow this file's gate()/exit-1/exit-2
#     contract used by every OTHER mode. It ALWAYS exits 0 — even on an
#     internal failure (a bad diff, unreadable JSON, a missing JSON_RUNNER) it
#     resolves the token to "full" and reports the problem on stderr, rather
#     than halting. Reusing the gate()/exit-2 convention here would give a
#     mere environment hiccup the power to abort an entire build, which no
#     ruling authorizes and which `default-when-absent` explicitly forbids
#     (an unknown risk must make the review MORE thorough, never stop it).
#   * stdout carries EXACTLY one line: `risk-depth:<light|normal|full>`. No
#     diff-derived text (a sensitive filename, a diff hunk) ever reaches
#     stdout — that mirrors gate()/ok()'s own "stdout is a token channel
#     only" discipline, and closes an injection seam: a crafted filename or
#     added-line content with an embedded newline could otherwise forge a
#     second, attacker-chosen token line. All "why" reasoning is stderr-only.
#
# Composes THREE existing deterministic signals (risk-read-mechanism ruling —
# Option (a): NOT agent-judged, NOT SKILL.md prose, NOT a maintainer
# pre-declaration):
#   (1) sensitive-surface — reuses classify_sensitive_surface() above, the
#       SAME function seccheck itself calls (risky-surface-classifier-source
#       ruling: reuse, never a second hand-maintained list — "sensitive" has
#       ONE definition, owned by seccheck). DISCLOSED GAP: this repo has no
#       discrete public-facing-change classifier today (confirmed by search —
#       "public surface" exists only as free-text guidance inside LLM
#       scope-fence prompts elsewhere in this kit, not a deterministic
#       signal). Per the ruling's own note ("if no discrete public-surface
#       classifier exists yet, that is a build-time implementation detail —
#       surface it, don't invent a duplicate list"), this leg is
#       sensitive-surface ONLY; a public-signature-change detector is a
#       separate, undone future addition, not invented here.
#   (2) Tier-A ruled-decision count — forkIds.length read from the ALREADY
#       schema-validated rulings file (.gstack/arc-rulings/<id>-pr1-args.json)
#       that check_rulings enforces for `build` — never a raw re-scan of
#       discovery's own output, which could disagree with what was actually
#       ruled (see tier_a_fork_state's own comment for the 3-state read).
#   (3) diff size — the SAME diff capture classify_sensitive_surface already
#       made (added+removed content-line count), never a second git call.
#
# TWO subcommands, at two different points in the build (read-timing-pre-
# vs-post-implement ruling — Option 3, BOTH reads):
#
#   riskcheck pre-build <issue> [trust]
#     Runs BEFORE any diff exists — a fresh feature branch's pre-fire diff is
#     structurally empty (see arc-execute.js's own FENCE_FILES comment on the
#     identical timing gap). Signals (1) and (3) are diff-based and would
#     trivially read "clear" on an empty diff, so this call uses ONLY signal
#     (2), the Tier-A fork count. It resolves to just TWO outcomes:
#       * forks PRESENT or INCONCLUSIVE -> "full" (escalate — the real floor).
#       * forks genuinely CLEAR (forkIds:[]) -> "light". This is NOT a claim
#         that all three legs are clear (two are unmeasured here) — it is the
#         ratchet's IDENTITY element (max(light, x) == x), i.e. "the one leg I
#         can measure shows no escalation signal, so I impose NO floor; the
#         post-implement read against the REAL diff makes the real
#         light/normal/full call." Emitting "normal" here instead (the pre-#119
#         behavior) was the de-escalation bug: it planted a floor the genuine
#         all-three-clear post-implement read could never get below, so "light"
#         — the entire ceremony-reduction this issue exists to deliver — was
#         unreachable in any wired build (issue #119 round-1 finding). "light"
#         and "normal" share the SAME 2-round floor, so this changes NO round
#         budget; it only unblocks the post-implement panel-size trim.
#     Either way this is a PROVISIONAL FLOOR the real diff can only RAISE later
#     (post-implement ratchets UP on a large/sensitive/forky diff), never a
#     final answer, and never a floor that can force scrutiny DOWN.
#
#   riskcheck post-implement <base> <issue> [trust]
#     Runs AFTER Implement, against the real diff. Computes all three signals
#     and RATCHETS the result against whatever pre-build already stored: the
#     persisted depth can only move UP the light -> normal -> full ordering,
#     never down (trust-field-dependency ruling: "it only ever ADDS scrutiny,
#     never removes it" — see risk_ratchet_write). <base> MUST be the SAME
#     immutable BASE_SHA plumbing seccheck itself is handed (never a branch
#     name a build could move mid-run — see check_security's own BASE_SHA
#     note above): a moved base could otherwise make a sensitive change
#     appear to already exist in the base, defeating both diff-based legs.
#
# Escalate-vs-de-escalate ASYMMETRY (escalate-vs-deescalate-asymmetry
# ruling) — an explicit boolean AND/OR, NEVER a scored/weighted sum. A summed
# score would let a missed sensitive-surface hit be silently outvoted by two
# clean legs — exactly the "one wrong looks-trivial guess" the ruling exists
# to prevent:
#   FULL   if sensitive-surface HIT, OR Tier-A forks are PRESENT, OR the
#          Tier-A leg is INCONCLUSIVE (rulings file absent/unreadable — fail
#          toward full per default-when-absent). Diff SIZE ALONE never
#          escalates to full; it only ever blocks de-escalation (below).
#   LIGHT  only if ALL THREE are independently clear: no sensitive surface,
#          AND zero Tier-A forks (a GENUINE clear, not "inconclusive"), AND
#          the diff is small (below both hardcoded thresholds).
#   NORMAL otherwise — e.g. nothing sensitive/ruled, but the diff is large.
#
# Hardcoded constants (new-config-surface ruling, Option A: no new config
# knobs anywhere — matches ADR-0017's no-per-guard-dials precedent. Round
# counts per depth live in arc-execute.js/arc-finish.js, same reasoning).
RISK_DIFF_LINES_THRESHOLD=150
RISK_DIFF_FILES_THRESHOLD=8

# depth_ordinal / depth_ordinal_or_none — light < normal < full, used by the
# ratchet's max() comparison. An unrecognized value always maps to the
# FULL/none-losing end of its own scale (fail toward full / fail toward "no
# prior floor yet"), never silently to a middle ground.
depth_ordinal() {
  case "$1" in
    light) echo 0 ;;
    normal) echo 1 ;;
    full) echo 2 ;;
    *) echo 2 ;;
  esac
}
depth_ordinal_or_none() {
  case "$1" in
    none) echo -1 ;;
    light) echo 0 ;;
    normal) echo 1 ;;
    full) echo 2 ;;
    *) echo -1 ;;
  esac
}

# tier_a_fork_state <issue_id> — the Tier-A-ruled-decision-count leg.
#
# Sets globals: TIERA_STATE=absent|clear|risky ; TIERA_COUNT=<n>
#
# "absent" covers EVERY one of: rulings file missing, a symlinked path,
# unreadable, unparseable JSON, or a `forkIds` field that isn't an array.
# Every one of those is INCONCLUSIVE, never "zero forks" (a P1 prep finding:
# conflating file-absent with a genuine forkIds:[] would silently violate
# default-when-absent for, e.g., a `finish` run on a branch that never went
# through discovery — finish's own preflight has no rulings-required gate).
# Reads the SAME field check_rulings already schema-validates for `build`
# (forkIds), never a fresh/raw re-scan of discovery's own output, which could
# diverge from what the maintainer actually ruled on (reproducibility).
# NEVER calls `exit` — every failure path just leaves TIERA_STATE=absent and
# returns 0, so this is safe to call directly (no subshell wrapper needed).
tier_a_fork_state() {
  local issue_id="$1"
  local rulings="$REPO/.gstack/arc-rulings/${issue_id}-pr1-args.json"
  TIERA_STATE="absent"
  TIERA_COUNT=0
  if [ -L "$rulings" ]; then
    echo "arc-preflight: tier_a_fork_state: ${rulings} is a symlink — refusing to read through it; treating as inconclusive" >&2
    return 0
  fi
  if [ ! -f "$rulings" ] || [ -z "$JSON_RUNNER" ]; then
    return 0
  fi
  local count
  if [ "$JSON_RUNNER" = "python3" ]; then
    count="$(python3 - "$rulings" <<'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    fi = d.get("forkIds")
    print(len(fi) if isinstance(fi, list) else -1)
except Exception:
    print(-1)
PY
)"
  else
    count="$(node -e '
try {
  const d = require(process.argv[1]);
  const fi = d && d.forkIds;
  process.stdout.write(String(Array.isArray(fi) ? fi.length : -1));
} catch (e) { process.stdout.write("-1") }
' "$rulings" 2>/dev/null || true)"
  fi
  case "$count" in
    ''|*[!0-9-]*) return 0 ;;
  esac
  if [ "$count" -lt 0 ]; then
    return 0
  fi
  TIERA_COUNT="$count"
  if [ "$count" -eq 0 ]; then
    TIERA_STATE="clear"
  else
    TIERA_STATE="risky"
  fi
}

# compute_risk_pre_build <issue_id> — sets RISK_COMPUTED_DEPTH / RISK_COMPUTED_WHY.
# The PARTIAL pre-build read — see the header note above for why a genuine CLEAR
# resolves to "light" (the ratchet's identity element, imposing NO floor so the
# real post-implement read can reach light), while PRESENT/INCONCLUSIVE forks
# resolve to "full". It never resolves to "normal" — only the full post-implement
# read, which can measure the diff-size leg, ever produces "normal". NEVER calls
# `exit`.
compute_risk_pre_build() {
  local issue_id="$1"
  tier_a_fork_state "$issue_id"
  local depth why
  case "$TIERA_STATE" in
    risky) depth="full"; why="tierA-forks:${TIERA_COUNT}(risky)" ;;
    clear) depth="light"; why="tierA-forks:0(clear, partial pre-build read — imposes no floor; sensitive-surface and diff-size not yet measurable, resolved at post-implement)" ;;
    *) depth="full"; why="tierA-forks:inconclusive" ;;
  esac
  RISK_COMPUTED_DEPTH="$depth"
  RISK_COMPUTED_WHY="$why"
}

# _risk_post_implement_calc <base> <cur_sha> <issue_id> — INTERNAL helper.
#
# MUST be invoked by its caller through a command-substitution subshell
# (`$(...)`), never called directly: classify_sensitive_surface can `exit 2`
# on a git failure, and riskcheck's own contract is "never exit non-zero to
# the process" — routing through `$(...)` means that exit only terminates the
# subshell command substitution creates; the caller reads the exit code via
# `$?` and resolves to full on anything but a clean 0. Prints "<depth>\n<why>\n"
# on success (its ONLY stdout output, captured by the caller — never leaked to
# riskcheck's own stdout token channel).
_risk_post_implement_calc() {
  local base="$1" cur_sha="$2" issue_id="$3"
  classify_sensitive_surface "$base" "$cur_sha"
  tier_a_fork_state "$issue_id"

  local small=1
  if [ "$CLASSIFY_DIFF_LINES" -ge "$RISK_DIFF_LINES_THRESHOLD" ] || [ "$CLASSIFY_FILE_COUNT" -ge "$RISK_DIFF_FILES_THRESHOLD" ]; then
    small=0
  fi

  local depth
  if [ "$CLASSIFY_HIT" -eq 1 ] || [ "$TIERA_STATE" = "risky" ] || [ "$TIERA_STATE" = "absent" ]; then
    depth="full"
  elif [ "$TIERA_STATE" = "clear" ] && [ "$small" -eq 1 ]; then
    depth="light"
  else
    depth="normal"
  fi
  local sens_word="clear"
  [ "$CLASSIFY_HIT" -eq 1 ] && sens_word="hit"
  local why="sensitive-surface:${sens_word} tierA-forks:${TIERA_STATE}(${TIERA_COUNT}) diff:${CLASSIFY_DIFF_LINES}L/${CLASSIFY_FILE_COUNT}F(threshold ${RISK_DIFF_LINES_THRESHOLD}L/${RISK_DIFF_FILES_THRESHOLD}F)"
  printf '%s\n%s\n' "$depth" "$why"
}

# risk_ratchet_write <issue_id> <new_depth> <computed_at> <why> [trust]
#
# Read-compare-max-write, LOCK-PROTECTED (reuses the SAME mkdir-sentinel lock
# primitive + release_lock trap dispatch append_guarded_line already uses)
# so two overlapping riskcheck calls (a resumed build racing a still-running
# prior one, or the pre-build and post-implement calls landing close together
# in a fast pipeline) cannot each read the OLD stored depth and independently
# decide no escalation is needed — the later writer would otherwise stomp an
# earlier legitimate escalation (the exact race the trust-field-dependency
# ruling's required test locks in). Builds the COMPLETE JSON object in memory
# and writes it via ONE mktemp+mv (write_guarded) — never an incremental
# multi-write — so a crash mid-write can never leave a schema-valid-but-
# partial file that a later reader silently trusts.
#
# MUST be invoked by its caller through `$(...)`: every internal failure path
# here (a refused symlink, a lock-acquire timeout, write_guarded's own
# possible `exit 2`) is a bare `exit 2` — safe ONLY because it is scoped to
# the command-substitution subshell the caller wraps this call in, never the
# whole process. On success, prints the FINAL (post-ratchet) depth as its
# ONLY stdout line.
risk_ratchet_write() {
  local issue_id="$1" new_depth="$2" computed_at="$3" why="$4" trust="${5:-hardened}"
  local relname="${issue_id}-risk.json"

  mkdir "$REPO/.gstack" 2>/dev/null || [ -d "$REPO/.gstack" ] || { echo "arc-preflight: risk_ratchet_write: cannot create $REPO/.gstack (exists as a non-directory?)" >&2; exit 2; }
  if [ -L "$REPO/.gstack" ]; then
    echo "arc-preflight: risk_ratchet_write: $REPO/.gstack is a symlink — refusing to write through it" >&2
    exit 2
  fi
  mkdir "$REPO/.gstack/arc-rulings" 2>/dev/null || [ -d "$REPO/.gstack/arc-rulings" ] || { echo "arc-preflight: risk_ratchet_write: cannot create $REPO/.gstack/arc-rulings" >&2; exit 2; }
  if [ -L "$REPO/.gstack/arc-rulings" ]; then
    echo "arc-preflight: risk_ratchet_write: $REPO/.gstack/arc-rulings is a symlink — refusing to write through it" >&2
    exit 2
  fi

  local lockdir="$REPO/.gstack/arc-rulings/.${relname}.lock.d"
  local waited=0
  while ! mkdir "$lockdir" 2>/dev/null; do
    waited=$((waited + 1))
    if [ "$waited" -gt 100 ]; then
      echo "arc-preflight: risk_ratchet_write: could not acquire the lock for '${relname}' after ~10s — a stale lock dir may remain at ${lockdir}; remove it by hand if no other riskcheck is genuinely in flight" >&2
      exit 2
    fi
    sleep 0.1
  done
  trap release_lock EXIT
  trap 'release_lock; exit 130' INT TERM

  local existing_path="$REPO/.gstack/arc-rulings/$relname"
  local existing_depth="none"
  if [ -L "$existing_path" ]; then
    echo "arc-preflight: risk_ratchet_write: $existing_path is a symlink — refusing to read through it; treating prior depth as absent" >&2
  elif [ -f "$existing_path" ] && [ -n "$JSON_RUNNER" ]; then
    if [ "$JSON_RUNNER" = "python3" ]; then
      existing_depth="$(python3 - "$existing_path" <<'PY' || true
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    depth = d.get("depth")
    print(depth if d.get("schemaVersion") == 1 and depth in ("light", "normal", "full") else "none")
except Exception:
    print("none")
PY
)"
    else
      existing_depth="$(node -e '
try {
  const d = require(process.argv[1]);
  const ok = d && d.schemaVersion === 1 && ["light","normal","full"].includes(d.depth);
  process.stdout.write(ok ? d.depth : "none");
} catch (e) { process.stdout.write("none") }
' "$existing_path" 2>/dev/null || true)"
    fi
  fi
  case "$existing_depth" in light|normal|full|none) : ;; *) existing_depth="none" ;; esac

  local new_ord existing_ord final_depth
  new_ord="$(depth_ordinal "$new_depth")"
  existing_ord="$(depth_ordinal_or_none "$existing_depth")"
  if [ "$existing_ord" -ge "$new_ord" ]; then
    final_depth="$existing_depth"
  else
    final_depth="$new_depth"
  fi

  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # why is built entirely from our own fixed vocabulary + digits (see
  # _risk_post_implement_calc / compute_risk_pre_build) — no diff-derived
  # filenames or content ever reach it — but escape defensively anyway.
  local why_escaped
  why_escaped="$(printf '%s' "$why" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  printf '{"schemaVersion":1,"issue":"%s","depth":"%s","computedAt":"%s","why":"%s","ratchetedAt":"%s"}\n' \
    "$issue_id" "$final_depth" "$computed_at" "$why_escaped" "$now" \
    | write_guarded "$relname" "$trust"

  printf '%s\n' "$final_depth"
  release_lock
  trap - EXIT
}

# riskcheck_pre_build <issue> [trust]
riskcheck_pre_build() {
  local issue="${1:-}" trust="${2:-hardened}"
  if [ -z "$issue" ]; then
    echo "arc-preflight: riskcheck pre-build requires an <issue> argument — resolving to full (riskcheck never blocks)" >&2
    printf 'risk-depth:%s\n' "full"
    return 0
  fi
  local issue_id
  issue_id="$(normalize_issue_id "$issue" 2>/dev/null)" || {
    echo "arc-preflight: riskcheck pre-build: could not normalize issue id '${issue}' — resolving to full (riskcheck never blocks)" >&2
    printf 'risk-depth:%s\n' "full"
    return 0
  }

  compute_risk_pre_build "$issue_id"

  local ratchet_out rc=0
  ratchet_out="$(risk_ratchet_write "$issue_id" "$RISK_COMPUTED_DEPTH" "pre-build" "$RISK_COMPUTED_WHY" "$trust")" || rc=$?
  local final
  if [ "$rc" -eq 0 ] && [ -n "$ratchet_out" ]; then
    final="$ratchet_out"
  else
    # The ratchet write failed (lock-acquire timeout, refused symlink,
    # write_guarded exit). An un-persisted local compute is NOT ratcheted
    # against whatever floor is already on disk, so trusting it could silently
    # LOWER scrutiny below a floor a prior read established — a direct
    # trust-field-dependency violation ("only ever ADDS scrutiny, never removes
    # it"). Fail toward full, exactly like every other inconclusive-read path in
    # this file (issue #119 round-1 finding).
    echo "arc-preflight: riskcheck pre-build: could not persist the risk ratchet (exit ${rc}) — failing toward full (an un-persisted read must never be trusted to de-escalate below the on-disk floor)" >&2
    final="full"
  fi
  echo "arc-preflight: riskcheck pre-build depth=${final} (${RISK_COMPUTED_WHY})" >&2
  printf 'risk-depth:%s\n' "$final"
  return 0
}

# riskcheck_post_implement <base> <issue> [trust]
riskcheck_post_implement() {
  local base="${1:-}" issue="${2:-}" trust="${3:-hardened}"
  if [ -z "$base" ] || [ -z "$issue" ]; then
    echo "arc-preflight: riskcheck post-implement requires <base> and <issue> — resolving to full (riskcheck never blocks)" >&2
    printf 'risk-depth:%s\n' "full"
    return 0
  fi
  if ! git -C "$REPO" rev-parse --verify --quiet "${base}^{commit}" >/dev/null 2>&1; then
    echo "arc-preflight: riskcheck post-implement: base ref '${base}' is not resolvable — resolving to full (riskcheck never blocks)" >&2
    printf 'risk-depth:%s\n' "full"
    return 0
  fi
  local issue_id
  issue_id="$(normalize_issue_id "$issue" 2>/dev/null)" || {
    echo "arc-preflight: riskcheck post-implement: could not normalize issue id '${issue}' — resolving to full (riskcheck never blocks)" >&2
    printf 'risk-depth:%s\n' "full"
    return 0
  }
  local cur_sha
  cur_sha="$(git -C "$REPO" rev-parse HEAD 2>/dev/null)" || cur_sha=""
  if [ -z "$cur_sha" ]; then
    echo "arc-preflight: riskcheck post-implement: could not resolve HEAD — resolving to full (riskcheck never blocks)" >&2
    printf 'risk-depth:%s\n' "full"
    return 0
  fi

  local calc_out rc=0
  calc_out="$(_risk_post_implement_calc "$base" "$cur_sha" "$issue_id")" || rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$calc_out" ]; then
    echo "arc-preflight: riskcheck post-implement: internal computation failed (exit ${rc}) — resolving to full (riskcheck never blocks)" >&2
    printf 'risk-depth:%s\n' "full"
    return 0
  fi
  local computed_depth computed_why
  computed_depth="$(printf '%s\n' "$calc_out" | sed -n '1p')"
  computed_why="$(printf '%s\n' "$calc_out" | sed -n '2p')"
  case "$computed_depth" in light|normal|full) : ;; *) computed_depth="full" ;; esac

  local ratchet_out rrc=0
  ratchet_out="$(risk_ratchet_write "$issue_id" "$computed_depth" "post-implement" "$computed_why" "$trust")" || rrc=$?
  local final
  if [ "$rrc" -eq 0 ] && [ -n "$ratchet_out" ]; then
    final="$ratchet_out"
  else
    # The ratchet write failed. The freshly computed depth was NOT maxed against
    # any pre-build floor already on disk, so returning it could regress a
    # persisted "full" floor down to a fresh "light" — silently REMOVING
    # scrutiny the pre-build read already established. Fail toward full instead
    # (issue #119 round-1 finding; matches riskcheck_pre_build's identical
    # failure path).
    echo "arc-preflight: riskcheck post-implement: could not persist the risk ratchet (exit ${rrc}) — failing toward full (an un-persisted read is not ratcheted against the on-disk floor and must never de-escalate below it)" >&2
    final="full"
  fi
  echo "arc-preflight: riskcheck post-implement depth=${final} (${computed_why})" >&2
  printf 'risk-depth:%s\n' "$final"
  return 0
}

# ===========================================================================
# Reconcile the `died` state (issue #132, R2) — READ-ONLY detection of an arc
# build/finish run that was killed mid-flight, surfaced at the next
# `/arc resume`/`/arc status`. See the header note near the top of this file
# (RECONCILE-DIED IS NOT PART OF THIS CONTRACT EITHER) for the exit-code
# contract: this NEVER gates, it only ever reports.
#
# Design, per the maintainer's rulings for #132:
#   * logic-home: detection is deterministic code, here — the skill only
#     renders the JSON verdict and drives resume-side effects (never itself
#     re-derives "died").
#   * liveness-proxy (Option D): the per-repo build lock dir
#     (arc-build.lock.d, read-only `test -d`, NEVER mkdir/rmdir — probing by
#     acquiring the lock would itself leave an orphaned lock behind on a
#     crash, exactly the failure mode this feature exists to detect) and
#     build-queue.json's `active` entry must BOTH agree before either is
#     trusted; on disagreement, or an unreadable source, this reports
#     "cant-tell", never guesses a winner.
#   * candidate-enumeration: build-queue.json's `active` entry and git commit
#     history are each read for the ONE failure mode only that source can
#     catch (a stale active entry vs. an orphaned committed round).
#   * run-scoping (Option 3): scope to `git log --all --not <base>` — every
#     ref, minus anything already reachable from the caller-supplied <base>
#     ref — recomputed HERE at check time. There is no live BASE_SHA to
#     "reuse": a died run's process (and the variable it held in memory) is
#     long gone by the time `/arc resume` runs. The caller passes the SAME
#     kind of base ref build/finish already resolve (prefer the remote-
#     tracking ref, e.g. `origin/main`, so a not-yet-pulled remote merge is
#     correctly excluded).
#   * output-contract (Option B): one JSON object on stdout per call, never
#     persisted to disk (new-persisted-state ruling — no new tracking file;
#     this mode writes nothing, ever).
#   * read-path-safety: the parent-directory guard below is copied VERBATIM
#     from check_fence's read guard (leaf -L refusal + realpath-both-sides
#     containment) — no `trust` argument anywhere in this mode; it would
#     control nothing on a read-only path.
# ===========================================================================

# _reconcile_guard_rulings_dir — mirrors check_fence's read guard EXACTLY:
# unconditional (no trust arg — every trust level gets the full check), leaf
# `-L` refusal on `.gstack` and `.gstack/arc-rulings`, then (only if the dir
# exists) a realpath-both-sides exact-equality containment check. Echoes the
# resolved rulings dir (or the raw not-yet-existing path) to stdout on
# success; returns non-zero with a stderr diagnostic on any guard failure.
_reconcile_guard_rulings_dir() {
  if [ -L "$REPO/.gstack" ]; then
    echo "arc-preflight: reconcile-died: $REPO/.gstack is a symlink — refusing to read through it" >&2
    return 1
  fi
  if [ -L "$REPO/.gstack/arc-rulings" ]; then
    echo "arc-preflight: reconcile-died: $REPO/.gstack/arc-rulings is a symlink — refusing to read through it" >&2
    return 1
  fi
  local dir="$REPO/.gstack/arc-rulings"
  if [ -d "$dir" ]; then
    local resolved_dir resolved_repo
    resolved_dir="$(cd "$dir" 2>/dev/null && pwd -P)" || { echo "arc-preflight: reconcile-died: cannot resolve arc-rulings dir" >&2; return 1; }
    resolved_repo="$(cd "$REPO" 2>/dev/null && pwd -P)" || { echo "arc-preflight: reconcile-died: cannot resolve repo root" >&2; return 1; }
    if [ "$resolved_dir" != "$resolved_repo/.gstack/arc-rulings" ]; then
      echo "arc-preflight: reconcile-died: arc-rulings resolved to '${resolved_dir}', expected '${resolved_repo}/.gstack/arc-rulings' — refusing to read (symlinked intermediate?)" >&2
      return 1
    fi
    dir="$resolved_dir"
  fi
  printf '%s' "$dir"
  return 0
}

# _reconcile_common_dir — resolve the repo's COMMON git dir the SAME two-step
# way SKILL.md's isolation setup and build step 3 already do (never a path
# built off $REPO alone — a worktree-isolated build's real lock lives under
# the COMMON dir, not a per-worktree .git FILE). Echoes the resolved absolute
# path; returns non-zero on failure.
_reconcile_common_dir() {
  local gcd
  gcd="$(git -C "$REPO" rev-parse --git-common-dir 2>/dev/null)" || return 1
  case "$gcd" in
    /*) : ;;
    *) gcd="$REPO/$gcd" ;;
  esac
  (cd "$REPO" 2>/dev/null && realpath "$gcd" 2>/dev/null)
}

# _reconcile_extract_died_ids <json> — print (one per line) every issue id
# whose `verdict` is "died" in the given reconcile JSON payload. Used ONLY to
# decide whether the re-check pass (below) is needed.
_reconcile_extract_died_ids() {
  local json="$1"
  if [ "$JSON_RUNNER" = "python3" ]; then
    printf '%s' "$json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for item in (d.get("issues", []) if isinstance(d, dict) else []):
    if isinstance(item, dict) and item.get("verdict") == "died":
        print(item.get("issue", ""))
'
  else
    printf '%s' "$json" | node -e '
const fs = require("fs");
let d;
try { d = JSON.parse(fs.readFileSync(0, "utf8")); } catch (e) { process.exit(0); }
for (const item of (d && Array.isArray(d.issues) ? d.issues : [])) {
  if (item && item.verdict === "died") console.log(item.issue || "");
}
'
  fi
}

# _reconcile_downgrade_died <json> — print the given reconcile JSON with EVERY
# tentative "died" verdict downgraded to "cant-tell" and `overall` recomputed.
# Used when the two-pass re-check CANNOT complete (its mktemp/merge plumbing
# failed): the two-pass guarantee — never keep a single-read "died" a fresh
# look did not reconfirm — must hold even when the SECOND read's machinery
# fails, not only when the two reads disagree. A false "died" is worse than
# silence (liveness-proxy ruling). Reads from stdin only (no temp files), so it
# still works in exactly the low-disk / unwritable-TMPDIR condition that breaks
# the merge step. Exits non-zero (printing nothing) if the runner itself cannot
# re-serialize the payload, so the caller can fall back to a static cant-tell.
_reconcile_downgrade_died() {
  local json="$1"
  if [ "$JSON_RUNNER" = "python3" ]; then
    printf '%s' "$json" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for item in (doc.get("issues", []) if isinstance(doc, dict) else []):
    if isinstance(item, dict) and item.get("verdict") == "died":
        item["verdict"] = "cant-tell"
        item["reason"] = ("initial read showed died, but the re-check could not be "
                          "completed, so this was not confirmed — check by hand")
overall = "matches"
issues = doc.get("issues", []) if isinstance(doc, dict) else []
if any(isinstance(i, dict) and i.get("verdict") == "died" for i in issues):
    overall = "died"
elif any(isinstance(i, dict) and i.get("verdict") == "cant-tell" for i in issues) or doc.get("cantTellReasons"):
    overall = "cant-tell"
doc["overall"] = overall
doc["recheckIncomplete"] = True
print(json.dumps(doc))
'
  else
    printf '%s' "$json" | node -e '
const fs = require("fs");
let doc;
try { doc = JSON.parse(fs.readFileSync(0, "utf8")); } catch (e) { process.exit(1); }
const issues = (doc && Array.isArray(doc.issues)) ? doc.issues : [];
for (const item of issues) {
  if (item && item.verdict === "died") {
    item.verdict = "cant-tell";
    item.reason = "initial read showed died, but the re-check could not be completed, so this was not confirmed — check by hand";
  }
}
let overall = "matches";
if (issues.some(i => i && i.verdict === "died")) overall = "died";
else if (issues.some(i => i && i.verdict === "cant-tell") || (doc.cantTellReasons || []).length) overall = "cant-tell";
doc.overall = overall;
doc.recheckIncomplete = true;
process.stdout.write(JSON.stringify(doc));
'
  fi
}

# _reconcile_died_fallback <json> — safe fallback for reconcile_died when the
# two-pass re-check plumbing fails: downgrade tentative "died" to "cant-tell"
# via _reconcile_downgrade_died, and if even that cannot run, emit a static
# cant-tell result rather than ever printing an unconfirmed "died".
_reconcile_died_fallback() {
  local raw="$1" out
  if out="$(_reconcile_downgrade_died "$raw")" && [ -n "$out" ]; then
    printf '%s\n' "$out"
  else
    printf '%s\n' '{"schemaVersion":1,"overall":"cant-tell","issues":[],"cantTellReasons":["the died re-check could not be completed and its result could not be safely re-serialized — check by hand"],"buildQueueShape":{"valid":false,"state":"cant-tell","reason":"the died re-check could not be completed — build-queue.json shape was not evaluated"},"recheckIncomplete":true}'
  fi
}

# _reconcile_single_pass <base> — ONE full read+compute pass (no re-check).
# Prints the raw JSON result to stdout. This is the unit the "re-read once
# before concluding died" logic (reconcile_died, below) re-invokes wholesale —
# a fresh call, never a partial/incremental recompute, so pass 2 sees a
# genuinely fresh snapshot of every source (lock dir, build-queue.json, git
# history, every <issue>-rounds.jsonl).
_reconcile_single_pass() {
  local base="$1"
  local checked_at
  checked_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if [ -z "$base" ]; then
    echo "arc-preflight: reconcile-died requires a <base> ref (e.g. origin/main)" >&2
    exit 2
  fi
  if ! git -C "$REPO" rev-parse --verify --quiet "${base}^{commit}" >/dev/null 2>&1; then
    echo "arc-preflight: reconcile-died base ref '${base}' is not resolvable" >&2
    exit 2
  fi
  if [ -z "$JSON_RUNNER" ]; then
    echo "arc-preflight: reconcile-died requires python3 or node on PATH" >&2
    exit 2
  fi

  local common_dir lock_present
  if common_dir="$(_reconcile_common_dir)" && [ -n "$common_dir" ]; then
    if [ -L "$common_dir/arc-build.lock.d" ]; then
      # read-path-safety ruling: refuse a symlinked leaf, the SAME guard the
      # rulings-dir read applies. A swapped symlink here could point at any
      # existing dir, forging (or masking) the liveness signal — so treat it
      # as unreadable and report cant-tell for the WHOLE check rather than
      # trusting whatever it resolves to.
      printf '{"schemaVersion":1,"checkedAt":"%s","overall":"cant-tell","issues":[],"liveBuild":{"confirmed":false,"issue":null,"ambiguous":false},"cantTellReasons":["the build lock (arc-build.lock.d) is a symlink — refusing to read through it; cannot determine whether a build is live"],"buildQueueShape":{"valid":false,"state":"cant-tell","reason":"the build lock is a symlink — refusing to read through it; build-queue.json was not checked"}}\n' "$checked_at"
      return 0
    fi
    if [ -d "$common_dir/arc-build.lock.d" ]; then
      lock_present=1
    else
      lock_present=0
    fi
  else
    echo "arc-preflight: reconcile-died: could not resolve the common git dir — cannot read the build lock" >&2
    exit 2
  fi

  local rulings_dir
  if ! rulings_dir="$(_reconcile_guard_rulings_dir)"; then
    # Guard failure (a symlinked .gstack/arc-rulings): report cant-tell for the
    # WHOLE check rather than exit non-zero — this mode never gates (see the
    # header contract note), and "cannot read either source" is itself a
    # real, reportable outcome, not a script failure.
    printf '{"schemaVersion":1,"checkedAt":"%s","overall":"cant-tell","issues":[],"liveBuild":{"confirmed":false,"issue":null,"ambiguous":false},"cantTellReasons":["the .gstack/arc-rulings directory could not be safely read (see stderr) — build-queue.json and every <issue>-rounds.jsonl are unreadable"],"buildQueueShape":{"valid":false,"state":"cant-tell","reason":"the .gstack/arc-rulings directory could not be safely read (see stderr) — build-queue.json is unreadable"}}\n' "$checked_at"
    return 0
  fi

  # Ground truth: every arc(<id>): commit not yet reachable from <base>,
  # scanned across ALL refs (not just whatever branch is checked out right
  # now) — this closes the retried-issue blind spot (run-scoping ruling) and
  # catches an abandoned attempt even when nobody has that branch checked out.
  local log_file candidates_file unsafe_file shipped_file
  log_file="$(mktemp "${TMPDIR:-/tmp}/arc-reconcile-log.XXXXXX")" || { echo "arc-preflight: reconcile-died: cannot create temp file" >&2; exit 2; }
  candidates_file="$(mktemp "${TMPDIR:-/tmp}/arc-reconcile-cand.XXXXXX")" || { rm -f "$log_file"; echo "arc-preflight: reconcile-died: cannot create temp file" >&2; exit 2; }
  unsafe_file="$(mktemp "${TMPDIR:-/tmp}/arc-reconcile-unsafe.XXXXXX")" || { rm -f "$log_file" "$candidates_file"; echo "arc-preflight: reconcile-died: cannot create temp file" >&2; exit 2; }
  shipped_file="$(mktemp "${TMPDIR:-/tmp}/arc-reconcile-shipped.XXXXXX")" || { rm -f "$log_file" "$candidates_file" "$unsafe_file"; echo "arc-preflight: reconcile-died: cannot create temp file" >&2; exit 2; }

  # %cI = committer date, strict ISO 8601 — the COMMITTER date (this
  # machine's own clock at commit time), never the author date (which can be
  # arbitrary after a rebase).
  local log_rc=0
  # `--source` populates %S (the ref by which each commit was reached) so a
  # candidate commit carries its BRANCH identity, not just its issue id. Two
  # different unmerged branches both holding arc(<N>): commits for the same
  # issue number (a died first attempt orphaned on branch A, plus a later
  # retry on branch B) must NOT be silently merged into one issue entry whose
  # aggregate max commit time a single later telemetry line can bless — that
  # is the exact retried-issue blind spot this feature exists to close. The
  # raw %S value is a full ref, and git --source splits ONE logical branch
  # across two refs whenever the local branch is ahead of its pushed tip: the
  # already-pushed ancestors come back as refs/remotes/<remote>/<b> and the
  # local-only later commits as refs/heads/<b>. The compute step below
  # normalizes each %S to a bare branch name (branch_of / branchOf: strip
  # refs/heads/ and refs/remotes/<remote>/) BEFORE bucketing, so a local
  # branch and its own remote-tracking ref collapse into one group; only
  # genuinely distinct branch NAMES (attempt-A vs attempt-B) split.
  git -C "$REPO" log --source --all --not "$base" --extended-regexp \
    --grep='^arc\([A-Za-z0-9._/-]+\): (implement|round [0-9]+ fixes|seed fixes|build output)$' \
    --format='%H%x1f%cI%x1f%S%x1f%s' > "$log_file" 2>/dev/null || log_rc=$?
  if [ "$log_rc" -ne 0 ]; then
    # The orphaned-commit scan itself did not complete (corrupt/unreadable
    # ref, packed-refs/alternates problem, etc.). Do NOT fall through as if
    # zero candidates were found — that would collapse "couldn't check" into
    # "checked, nothing wrong" (matches), the exact distinction the
    # output-contract ruling says must stay separate. Report cant-tell.
    rm -f "$log_file" "$candidates_file" "$unsafe_file" "$shipped_file"
    printf '{"schemaVersion":1,"checkedAt":"%s","overall":"cant-tell","issues":[],"liveBuild":{"confirmed":false,"issue":null,"ambiguous":false},"cantTellReasons":["the orphaned-commit scan (git log --all --not <base>) exited %s — could not enumerate candidate commits, check by hand"],"buildQueueShape":{"valid":false,"state":"cant-tell","reason":"the orphaned-commit scan failed before build-queue.json could be checked"}}\n' "$checked_at" "$log_rc"
    return 0
  fi

  local sha cdate sref subject cid
  while IFS=$'\x1f' read -r sha cdate sref subject; do
    [ -z "$sha" ] && continue
    # Portable bash 3.2+ regex capture (this file always runs under an
    # explicit `bash script.sh` invocation — see the run_preflight test
    # helpers and every SKILL.md call site — so this is safe here even
    # though the rest of the file sticks to plain `[ ]`/case for other
    # matches).
    if [[ "$subject" =~ ^arc\(([A-Za-z0-9._/-]+)\):\ (implement|round\ [0-9]+\ fixes|seed\ fixes|build\ output)$ ]]; then
      cid="${BASH_REMATCH[1]}"
      # write_guarded's bare-filename charset: the guard excludes "/", so cid
      # can never contribute a path separator and traversal is impossible. (It
      # does NOT forbid a literal "..", but that is harmless here — cid only
      # ever appears suffixed as "<cid>-rounds.jsonl", so cid=".." yields the
      # file "..-rounds.jsonl", a plain filename, never a ".." path component.)
      # COMMIT_SCOPE itself permits "/" too, so an id containing one is
      # EXCLUDED from the path-based telemetry check (NEVER used to build a
      # path) and surfaced as its own cant-tell entry below instead.
      if [[ "$cid" =~ ^[A-Za-z0-9_.-]+$ ]]; then
        # cid \x1f sha \x1f cdate \x1f sourceRef — the ref keeps distinct
        # branches for the same issue id from collapsing into one entry.
        printf '%s\x1f%s\x1f%s\x1f%s\n' "$cid" "$sha" "$cdate" "$sref" >> "$candidates_file"
      else
        printf '%s\n' "$cid" >> "$unsafe_file"
      fi
    fi
  done < "$log_file"
  rm -f "$log_file"

  # exclude-already-shipped (run-scoping ruling, revisited for issue #132 R2).
  # This repo squash-merges (CLAUDE.md convention), so a SHIPPED issue's original
  # arc(<id>): commits are NEVER reachable from <base> — they live only on the
  # merged-but-undeleted feature branch's refs (esp. refs/remotes/origin/arc/*).
  # The all-refs orphaned-commit scan above therefore sweeps them up, and with no
  # covering telemetry they would read as `died` — a FALSE died on work that
  # actually shipped (the exact signal-eroding failure #132 forbids). But a
  # squash-merge/PR commit for that issue DOES land on <base>, carrying "(#<id>)"
  # in its message. If such a commit exists, the issue shipped: record it here so
  # the compute step below excludes it from any `died` verdict. A genuinely
  # orphaned crashed run (branch never merged) has NO such <base> commit and
  # stays flagged (correct); a retried-then-merged issue is excluded (correct); a
  # retried-still-in-progress issue has no merge commit yet and stays flagged
  # (correct). Detection is a FIXED-STRING grep of <base>'s history (parens are
  # literal, never a regex) — the ruled-on heuristic, matched across the full
  # commit message (subject + body) so a squash whose "(#<id>)" sits in the body
  # still counts.
  if [ -s "$candidates_file" ]; then
    local scid
    while IFS= read -r scid; do
      [ -z "$scid" ] && continue
      if git -C "$REPO" log "$base" --fixed-strings --grep="(#${scid})" --max-count=1 --format='%H' 2>/dev/null | grep -q .; then
        printf '%s\n' "$scid" >> "$shipped_file"
      fi
    done < <(cut -d$'\x1f' -f1 "$candidates_file" | sort -u)
  fi

  local result rc
  # NOTE: this python3 body runs inside a "$(... <<'PY' ... )" command
  # substitution, so bash tracks apostrophes through the whole block to find
  # its closing paren — an unbalanced (odd) apostrophe in the heredoc body
  # (comments included) breaks bash parsing (same gotcha the marker/rulings
  # heredocs above already call out). Keep apostrophes balanced; prefer
  # wording that avoids them (e.g. "each branch completed its run").
  if [ "$JSON_RUNNER" = "python3" ]; then
    set +e
    result="$(python3 - "$candidates_file" "$unsafe_file" "$rulings_dir" "$lock_present" "$checked_at" "$shipped_file" <<'PY'
import json, os, re, sys
from datetime import datetime, timezone

candidates_file, unsafe_file, rulings_dir, lock_present_s, checked_at, shipped_file = sys.argv[1:7]
lock_present = lock_present_s == "1"

def parse_iso(s):
    if not isinstance(s, str) or not s:
        return None
    # Cross-runner parity gate (issue #132). datetime.fromisoformat (python)
    # and `new Date` (node) accept DIFFERENT input sets on their own:
    # fromisoformat accepts an hour-only time ("...T15") and a seconds-bearing
    # offset ("...+00:00:00") that new Date rejects; new Date silently rolls
    # hour 24 ("...T24:00:00" -> next-day 00:00) and reads odd-length fractions
    # that 3.9/3.10 fromisoformat rejects. A telemetry line one runner reads as
    # a valid date and the other reads as "no date" would flip a died verdict
    # purely by which interpreter is on PATH. So gate BOTH runners on ONE
    # strict ISO-8601 shape, apply the SAME hour range check, and normalize the
    # fraction to 3 digits (milliseconds, the most new Date can represent) so an
    # accepted value parses to the SAME instant on either runner.
    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})"
        r"(?:[ T](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?"
        r"(?:[zZ]|[+-]\d{2}:?\d{2})?)?$",
        s,
    )
    if not m:
        return None
    # new Date rolls hour 24 into the next day; fromisoformat rejects it. Reject
    # it here so both agree. Minute/second 60 already yield NaN in new Date and
    # are rejected by fromisoformat, so only hour 24 needs an explicit guard.
    if m.group(4) is not None and int(m.group(4)) > 23:
        return None
    # Normalize the trailing UTC designator case-INSENSITIVELY. The shared shape
    # regex above admits BOTH uppercase Z and lowercase z; the node twin new Date
    # accepts a lowercase z natively, but a case-sensitive s.replace("Z", ...)
    # would leave a lowercase z in place, and datetime.fromisoformat rejects a
    # trailing lowercase z on Python <=3.10 -> None. That would flip the verdict
    # purely by which interpreter is on PATH for an input the regex explicitly
    # permits. Rewrite a trailing [zZ] so both runners land on the SAME instant.
    norm = re.sub(r"[zZ]$", "+00:00", s)
    # Pad/truncate the fractional field to 3 digits. Anchor on [ T] (not a
    # literal T) so a SPACE-separated datetime has its fraction normalized too;
    # otherwise 3.9/3.10 fromisoformat would reject e.g. "... 00:00:00.5".
    mf = re.match(r"^(.*[ T]\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", norm)
    if mf:
        frac = (mf.group(2) + "000")[:3]
        norm = "%s.%s%s" % (mf.group(1), frac, mf.group(3))
    norm = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", norm)  # +0500 -> +05:00
    try:
        dt = datetime.fromisoformat(norm)
    except Exception:
        return None
    # A valid but timezone-less ISO-8601 value (e.g. an LLM-authored
    # writtenAt of "2099-01-01T00:00:00") parses NAIVE; the git %cI commit
    # dates it is compared against are ALWAYS offset-aware, and mixing the two
    # raises TypeError. Treat a naive value as UTC (the node twin appends "Z"
    # for the identical convention) so the comparison never crashes and both
    # runners agree.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def branch_of(ref):
    # Collapse a local branch and its own remote-tracking ref to ONE logical
    # branch. git log --source attributes each commit to whichever ref reached
    # it, so when a local branch is ahead of its pushed tip the pushed
    # ancestors return as refs/remotes/<remote>/<b> and the local-only commits
    # as refs/heads/<b> — the SAME branch, split into two refs. Strip both
    # prefixes so they bucket together; genuinely distinct branch NAMES
    # (attempt-A vs attempt-B) still split, preserving the retried-issue
    # blind-spot fix.
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    if ref.startswith("refs/remotes/"):
        rest = ref[len("refs/remotes/"):]
        i = rest.find("/")
        return rest[i + 1:] if i != -1 else rest
    return ref

def _classify_state_file(path):
    # OS-level classification only — absent / symlink / not-a-regular-file /
    # regular. Content-level classification (parse-fail, shape) is layered on
    # top by each caller once "regular" comes back, since build-queue.json
    # (one JSON document) and <issue>-rounds.jsonl (newline-delimited JSON)
    # are read very differently. Shared by every state-file read in this pass
    # so /arc resume and the advisory build-queue shape check (build step 2)
    # always agree about the same file (corruption-state-taxonomy ruling,
    # issue #66 R6) — this is the ONE place that taxonomy is decided, not a
    # second lookalike copy per call site.
    if os.path.islink(path):
        return "symlink"
    if not os.path.lexists(path):
        return "absent"
    if not os.path.isfile(path):
        return "not-a-regular-file"
    return "regular"

def _usable_issue_string(v):
    # Parity helper (issue #66 R6): coerce v to a stripped string ONLY if it
    # is a JSON scalar (string / number / boolean); return None for every
    # container (object, array) and for null. This is the ONE coercion BOTH
    # the shape validator and the active-issue extraction go through, so:
    #   (a) no object value ever reaches a coercion that can throw — in JS,
    #       String({"toString":null}) raises TypeError and would crash the
    #       node arm outright (the arm runs OUTSIDE the JSON.parse try/catch),
    #       so both arms must refuse to coerce containers, not just rely on
    #       Python str() being always-safe;
    #   (b) Python str([]) == "[]" (truthy) can never diverge from JS
    #       String([]) == "" (blank) — every non-scalar is treated as blank
    #       on BOTH sides, giving one coherent verdict for the same bytes no
    #       matter which interpreter is on PATH (corruption-state-taxonomy /
    #       keep-the-two-arms-in-exact-parity rulings).
    # bool is a subclass of int in python, so it passes this isinstance check
    # (accepted, matching JS `typeof v === "boolean"`); a numeric issue id is
    # accepted, not just a string — nothing has ever enforced issue to be a
    # JSON string, and a fleet repo with a numeric id must not read as corrupt.
    #
    # PARITY (issue #66 R6): strip an EXPLICIT ASCII-whitespace set, NOT the
    # default str.strip(). Python's default str.strip() (str.isspace()) and JS
    # String.prototype.trim() disagree on which control/space codepoints count
    # as whitespace — e.g. U+001C-U+001F (File/Group/Record/Unit Separator) and
    # U+0085 (NEL) are stripped by Python but NOT by JS .trim(), and U+FEFF is
    # trimmed by JS but not by Python. Left unpinned, the SAME bytes would fold
    # to empty on one runner and stay non-empty on the other, giving opposite
    # corruption verdicts by interpreter. Both arms strip EXACTLY this set
    # (space, tab, LF, CR, FF, VT), identical in both languages, so the verdict
    # never depends on which interpreter is on PATH (keep-the-two-arms-in-exact-
    # parity ruling). The node twin's regex must stay character-for-character in
    # sync with this set.
    if isinstance(v, (str, int, float)):
        # PARITY (issue #66 R6): an integral-valued JSON float (e.g. 3.0) must
        # coerce to the SAME id string on both arms. Python str(3.0) == "3.0"
        # but JS String(3) == "3" — JSON 3.0 parses to a float in python and an
        # integer-valued number in node (JS has no int/float distinction). Left
        # unnormalized, the SAME bytes would extract "3.0" (fails the numeric-id
        # regex -> no active issue) on python3 vs "3" (matches -> an active
        # issue) on node: opposite verdicts by interpreter. Fold an integral
        # float to int so both emit "3"; a non-integral float (3.5) already
        # stringifies identically on both. bool is an int subclass, NOT a float,
        # so it never enters this branch. NaN/Infinity cannot reach here (the
        # JSON parse rejects them via _reject_json_const). The node twin needs
        # no mirror of this — its String() already renders an integral number
        # without a trailing ".0".
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        s = str(v).strip(" \t\n\r\f\v")
        return s or None
    return None

def _reject_json_const(tok):
    # Parity with node's JSON.parse (issue #66 R6): Python's json module accepts
    # the non-standard tokens NaN / Infinity / -Infinity as valid values, but JS
    # JSON.parse rejects them as a syntax error. Without this override the SAME
    # build-queue.json bytes would parse clean on python3 and fail on node,
    # giving opposite corruption verdicts by interpreter (corruption-state-
    # taxonomy / keep-the-two-arms-in-exact-parity rulings). Raising here routes
    # NaN/Infinity/-Infinity into the same parse-fail branch node reaches
    # natively, so both arms agree the file is corrupt.
    raise ValueError("non-standard JSON constant: " + tok)

def _validate_build_queue_shape(qdoc):
    # Lenient corruption tripwire (be liberal in what you accept), NOT a strict JSON-Schema
    # gate: build-queue.json is a hand-editable scratch cache (GitHub is
    # source of truth), so this tolerates any per-entry variation beyond the
    # checks below. Returns a plain-language reason string naming the
    # problem, or None if qdoc looks healthy. Never echoes a raw field VALUE
    # back (only key names / entry positions) — those values are written by
    # an untrusted build/fix agent, same threat model as fenceException.reason
    # elsewhere in this file.
    if not isinstance(qdoc, dict):
        return "the top-level value of build-queue.json is not a JSON object"
    for key in ("active", "queue", "held", "completed"):
        if key not in qdoc:
            return "build-queue.json is missing the required \"%s\" key" % key
    for key in ("queue", "held", "completed"):
        if not isinstance(qdoc[key], list):
            return "the \"%s\" section of build-queue.json is not a list" % key
    for key in ("held", "completed"):
        for idx, entry in enumerate(qdoc[key]):
            if not isinstance(entry, dict):
                return "the \"%s\" entry #%d of build-queue.json is not an object" % (key, idx)
            if _usable_issue_string(entry.get("issue")) is None:
                return "the \"%s\" entry #%d of build-queue.json is missing a usable \"issue\" field" % (key, idx)
    return None

candidates = {}
with open(candidates_file, encoding="utf-8") as fh:
    for line in fh:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        cid, sha, cdate, sref = parts
        candidates.setdefault(cid, []).append((sha, cdate, sref))

unsafe_ids = []
if os.path.isfile(unsafe_file):
    with open(unsafe_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and line not in unsafe_ids:
                unsafe_ids.append(line)

# Issues the bash caller proved ALREADY SHIPPED (a squash-merge/PR commit for
# them exists on <base>). Their orphaned arc(<id>): commits are pre-squash
# branch history, not a died run — excluded from any `died` verdict below.
shipped_ids = set()
if os.path.isfile(shipped_file):
    with open(shipped_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                shipped_ids.add(line)

queue_readable = True
queue_unreadable_reason = None
queue_active_issue = None
active_entry_present = False
queue_shape_state = "absent"
queue_shape_reason = None
if rulings_dir:
    qpath = os.path.join(rulings_dir, "build-queue.json")
    qstate = _classify_state_file(qpath)
    if qstate == "symlink":
        queue_shape_state = "symlink"
        queue_shape_reason = "build-queue.json is a symlink — refusing to read through it"
        queue_readable = False
        queue_unreadable_reason = queue_shape_reason
    elif qstate == "not-a-regular-file":
        # Exists but is not a regular file (e.g. a directory). The node twin
        # now classifies this explicitly too (see classifyStateFile) instead
        # of relying on an EISDIR catch, so both runners report the SAME
        # reason string, not just the same cant-tell bucket.
        queue_shape_state = "not-a-regular-file"
        queue_shape_reason = "build-queue.json could not be read (not a regular file)"
        queue_readable = False
        queue_unreadable_reason = queue_shape_reason
    elif qstate == "absent":
        queue_shape_state = "absent"
        # readable, nothing active
    else:  # "regular"
        try:
            # PARITY (issue #66 R6): decode with errors="replace" so an invalid
            # UTF-8 byte becomes U+FFFD instead of raising UnicodeDecodeError.
            # The node twin below reads via fs.readFileSync(qpath, "utf8"), which
            # silently replaces invalid bytes rather than throwing, so without
            # this the SAME bytes would read parse-fail on python3 but ok on node
            # (opposite corruption verdicts by interpreter). This matches the
            # stated lenient-tripwire, not-strict-schema stance
            # (keep-the-two-arms-in-exact-parity ruling); the JSON parse still
            # runs afterward, so genuinely malformed content is still caught.
            with open(qpath, encoding="utf-8", errors="replace") as fh:
                # parse_constant rejects NaN/Infinity/-Infinity so python3 fails
                # exactly where node's JSON.parse does (see _reject_json_const).
                qdoc = json.load(fh, parse_constant=_reject_json_const)
        except Exception:
            # Runner-neutral reason string (NOT the interpreter exception
            # class name — Python JSONDecodeError vs node SyntaxError): this
            # reason is surfaced verbatim to the maintainer at /arc resume and
            # folded into cantTellReasons, so it must read identically
            # regardless of which interpreter is on PATH (exact-parity
            # ruling), exactly like the shape reasons above.
            queue_shape_state = "parse-fail"
            queue_shape_reason = "build-queue.json could not be parsed as JSON"
            queue_readable = False
            queue_unreadable_reason = queue_shape_reason
        else:
            shape_problem = _validate_build_queue_shape(qdoc)
            if shape_problem:
                # New case (corruption-state-taxonomy ruling): parses, but
                # is missing the shape reconcile-died needs to trust it.
                # Classified into the SAME cant-tell/refuse class as the
                # other unreadable states above — never a 4th `overall`
                # value — by reusing queue_readable/queue_unreadable_reason,
                # the exact mechanism that already drives cant-tell below.
                queue_shape_state = "parses-but-missing-required-keys"
                queue_shape_reason = shape_problem
                queue_readable = False
                queue_unreadable_reason = shape_problem
            else:
                queue_shape_state = "ok"
                active = qdoc.get("active")
                if isinstance(active, dict):
                    active_entry_present = True
                    # Same scalar-only coercion the shape validator uses, so a
                    # container-valued active.issue can never reach a throwing
                    # coercion (node parity) and both arms agree on what counts
                    # as a usable id.
                    s = _usable_issue_string(active.get("issue"))
                    if s is not None:
                        # PARITY (issue #66 R6): split on the SAME explicit ASCII
                        # whitespace set the coercion strip uses, NOT bare
                        # str.split() (Python whitespace). Default str.split()
                        # treats U+001C-U+001F / U+0085 as separators while the
                        # node twin's /\s+/ does not — the very codepoints
                        # _usable_issue_string's comment names — so the SAME
                        # active.issue bytes would extract a different id (or
                        # none) by interpreter. Keep this class identical to the
                        # node twin's split regex below.
                        toks = [t for t in re.split(r"[ \t\n\r\f\v]+", s) if t]
                        s = toks[0] if toks else s
                        s = s.split("#")[-1]
                        if re.fullmatch(r"[0-9]+", s):
                            queue_active_issue = s

ambiguous_live = False
live_issue = None
stale_queue_issue = None
cant_tell_reasons = []

if not queue_readable:
    cant_tell_reasons.append(queue_unreadable_reason or "build-queue.json is unreadable")
    if lock_present:
        ambiguous_live = True
elif lock_present:
    if queue_active_issue is not None:
        live_issue = queue_active_issue
    else:
        ambiguous_live = True
        cant_tell_reasons.append(
            "a build lock (arc-build.lock.d) is held, but build-queue.json's active "
            "entry does not confirm which issue it belongs to"
        )
else:
    if active_entry_present:
        if queue_active_issue is not None:
            stale_queue_issue = queue_active_issue
        else:
            # An active entry is present but its issue field did not parse to a
            # bare issue id. Do NOT fabricate a died entry keyed on a placeholder
            # string (a phantom id SKILL.md would render as "#(unparseable
            # active.issue): DIED") — report cant-tell, matching how every other
            # unattributable-queue case is handled (liveness-proxy ruling: never
            # auto-conclude on unresolved ambiguity).
            cant_tell_reasons.append(
                "build-queue.json active entry is present but its issue field could not be parsed"
            )

def read_rounds_writtenats(issue_id):
    # Returns (ok, writtenAts, had_unparseable). A per-line JSON parse failure
    # (a run that died mid-append, or lost the append-lock race, can leave a
    # truncated final line) is EXCLUDED from matching — we cannot tell whether
    # it would have covered this issue — but its presence is tracked
    # separately so the caller can distinguish "genuinely no telemetry"
    # (-> died) from "a line existed that we could not confirm either way"
    # (-> cant-tell, never silently defaulted to died OR to matches).
    if not rulings_dir:
        return (True, [], False)
    path = os.path.join(rulings_dir, "%s-rounds.jsonl" % issue_id)
    fstate = _classify_state_file(path)
    if fstate == "symlink":
        return (False, None, False)
    if fstate == "absent":
        return (True, [], False)
    if fstate == "not-a-regular-file":
        # Exists but is not a regular file (e.g. a directory) -> cant-tell,
        # never "no telemetry" (which would drive a false died) — a false
        # died is worse than silence.
        return (False, None, False)
    # fstate == "regular"
    out = []
    had_unparseable = False
    try:
        # PARITY (issue #66 R6): same errors="replace" decode as the build-queue
        # read above — the node twin below reads via fs.readFileSync(p, "utf8"),
        # which replaces invalid UTF-8 bytes with U+FFFD rather than throwing, so
        # a rounds.jsonl carrying an invalid byte must not raise here (which the
        # outer except would fold to cant-tell) while node reads it and matches.
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    # PARITY (issue #66 R6): reject NaN/Infinity/-Infinity here
                    # too, exactly like the build-queue parser above. The python
                    # json.loads accepts these non-standard tokens by default but
                    # the node JSON.parse twin below rejects them as a syntax
                    # error — without this override the SAME rounds.jsonl line
                    # would set had_unparseable on node but not python3, and that
                    # flag feeds the died/matches/cant-tell verdict, giving an
                    # opposite trustworthiness answer by interpreter (keep-the-
                    # two-arms-in-exact-parity ruling).
                    rec = json.loads(line, parse_constant=_reject_json_const)
                except Exception:
                    had_unparseable = True
                    continue
                if not isinstance(rec, dict):
                    continue
                # PARITY / no-crash issue 66 R6: route the record issue field
                # through the SAME scalar-only coercion the build-queue.json sites
                # use, not a bare str. A container-valued issue field here
                # otherwise diverges between the two arms, and on node a bare
                # String on an object with a non-callable toString throws an
                # uncaught TypeError OUTSIDE the JSON.parse try/except, crashing
                # the whole arm and breaking the always-exits-0 contract. The
                # helper returns None for every container, so both arms simply
                # fail to match instead. See usableIssueString in the node twin.
                ris = _usable_issue_string(rec.get("issue"))
                if ris is not None and ris == str(issue_id):
                    wa = rec.get("writtenAt")
                    if isinstance(wa, str) and wa:
                        out.append(wa)
    except Exception:
        return (False, None, False)
    return (True, out, had_unparseable)

issues_out = []
for cid in sorted(candidates.keys()):
    commits = candidates[cid]
    entry = {
        "issue": cid,
        "commitSweepHit": True,
        "staleQueueHit": (stale_queue_issue == cid),
        "commits": ["%s %s" % (sha, cdate) for sha, cdate, _sref in commits],
    }
    # Bucket the candidate commits for this issue by the branch they came from,
    # normalizing each %S ref to a bare branch name first (branch_of) so a
    # local branch and its own remote-tracking twin collapse to ONE bucket.
    # newest_commit_dt is the max across ALL branches; ref_newest holds the
    # newest commit on each distinct branch, so telemetry coverage can be
    # judged PER BRANCH rather than once in aggregate — otherwise converged
    # telemetry from a later branch would silently bless commits on an earlier
    # orphaned branch (the retried-issue blind spot).
    newest_commit_dt = None
    ref_newest = {}
    for _sha, cdate, sref in commits:
        bref = branch_of(sref)
        dt = parse_iso(cdate)
        if dt is not None and (newest_commit_dt is None or dt > newest_commit_dt):
            newest_commit_dt = dt
        cur = ref_newest.get(bref)
        if dt is not None and (cur is None or dt > cur):
            ref_newest[bref] = dt
        elif bref not in ref_newest:
            ref_newest[bref] = cur  # keep the branch known even if its date was unparseable
    distinct_refs = sorted(ref_newest.keys())
    entry["newestCommitAt"] = newest_commit_dt.isoformat() if newest_commit_dt else None

    if ambiguous_live:
        entry["verdict"] = "cant-tell"
        entry["reason"] = "liveness could not be confirmed — this issue may be the currently-running build"
        entry["newestTelemetryAt"] = None
        issues_out.append(entry)
        continue

    if live_issue is not None and cid == live_issue:
        entry["verdict"] = "matches"
        entry["reason"] = "confirmed still running (build lock held + build-queue.json active entry agree)"
        entry["newestTelemetryAt"] = None
        issues_out.append(entry)
        continue

    ok, writtenats, had_unparseable = read_rounds_writtenats(cid)
    if not ok:
        entry["verdict"] = "cant-tell"
        entry["reason"] = "<issue>-rounds.jsonl could not be read (symlink or read error) — check by hand"
        entry["newestTelemetryAt"] = None
        issues_out.append(entry)
        continue

    newest_tel_dt = None
    tel_unparseable = False
    for wa in writtenats:
        dt = parse_iso(wa)
        if dt is None:
            # A telemetry record for THIS issue exists but its writtenAt did
            # not parse as a date (an LLM/hand-authored value, as this feature
            # documents). Track it separately so an unparseable date is never
            # collapsed into "no telemetry -> died".
            tel_unparseable = True
        elif newest_tel_dt is None or dt > newest_tel_dt:
            newest_tel_dt = dt
    entry["newestTelemetryAt"] = newest_tel_dt.isoformat() if newest_tel_dt else None

    if newest_tel_dt is None:
        if had_unparseable or tel_unparseable:
            # A telemetry line existed but could not be confirmed to cover this
            # issue — either bad JSON (had_unparseable) or a present-but-
            # unparseable writtenAt date (tel_unparseable). We cannot rule out
            # that IT was the covering record, so defaulting to either "died"
            # or "matches" here would be a guess. Report cant-tell.
            entry["verdict"] = "cant-tell"
            entry["reason"] = "<issue>-rounds.jsonl has a telemetry line that could not be confirmed to cover this run (bad JSON or an unparseable writtenAt) — check by hand"
        else:
            entry["verdict"] = "died"
            entry["reason"] = "committed arc(%s): work with no covering telemetry line and no live run signal" % cid
    else:
        # Telemetry exists. Judge coverage PER BRANCH, not once in aggregate.
        # A branch whose newest commit POSTDATES the newest telemetry line is
        # definitely uncovered -> died (unambiguous; not a guess). When more
        # than one distinct branch carries commits for this issue and none is
        # provably uncovered, a single per-issue telemetry stream cannot be
        # attributed to more than one branch, so we report cant-tell rather
        # than auto-pick which branch the telemetry blessed (liveness-proxy
        # ruling: never pick a winner on unresolved disagreement; a false
        # died is worse than silence).
        uncovered_refs = [r for r in distinct_refs
                          if ref_newest[r] is not None and ref_newest[r] > newest_tel_dt]
        if uncovered_refs:
            entry["verdict"] = "died"
            entry["reason"] = ("the newest telemetry line for this issue predates orphaned commit(s) on "
                               "branch(es) %s — a later attempt died mid-flight" % ", ".join(uncovered_refs))
        elif len(distinct_refs) > 1:
            entry["verdict"] = "cant-tell"
            entry["reason"] = ("arc(%s): commits exist on %d distinct branches (%s) but only one telemetry "
                               "stream covers this issue — cannot confirm each branch completed its run; "
                               "check by hand" % (cid, len(distinct_refs), ", ".join(distinct_refs)))
        else:
            entry["verdict"] = "matches"
            entry["reason"] = "telemetry covers this run's committed work"
    # exclude-already-shipped: a squash-merge/PR commit for this issue exists on
    # <base>, so its orphaned arc(<id>): commits are the pre-squash branch
    # history of shipped work, not a died run. Never flag it died (run-scoping
    # ruling, issue #132 R2). Applied AFTER the telemetry judgment so it overrides
    # BOTH died legs (no covering telemetry, and telemetry-predates-commit); a
    # non-died verdict (matches/cant-tell) is left untouched.
    if entry["verdict"] == "died" and cid in shipped_ids:
        entry["verdict"] = "matches"
        entry["reason"] = ("already shipped — a squash-merge/PR commit for #%s exists on the base ref; "
                           "the orphaned arc(%s): commits are that shipped branch's pre-squash history, "
                           "not a died run" % (cid, cid))
    if len(distinct_refs) > 1:
        entry["branches"] = [
            {"ref": r, "newestCommitAt": ref_newest[r].isoformat() if ref_newest[r] else None}
            for r in distinct_refs
        ]
    issues_out.append(entry)

for uid in unsafe_ids:
    if any(i["issue"] == uid for i in issues_out):
        continue
    issues_out.append({
        "issue": uid,
        "verdict": "cant-tell",
        "commitSweepHit": True,
        "staleQueueHit": False,
        "reason": "commit label id contains characters unsafe for a file path — cannot check telemetry, check by hand",
        "commits": [],
        "newestCommitAt": None,
        "newestTelemetryAt": None,
    })

if stale_queue_issue is not None and not any(i["issue"] == stale_queue_issue for i in issues_out):
    issues_out.append({
        "issue": stale_queue_issue,
        "verdict": "died",
        "commitSweepHit": False,
        "staleQueueHit": True,
        "reason": "build-queue.json's active entry names this issue but no build lock is held for it",
        "commits": [],
        "newestCommitAt": None,
        "newestTelemetryAt": None,
    })

overall = "matches"
if any(i["verdict"] == "died" for i in issues_out):
    overall = "died"
elif any(i["verdict"] == "cant-tell" for i in issues_out) or cant_tell_reasons:
    overall = "cant-tell"

result = {
    "schemaVersion": 1,
    "checkedAt": checked_at,
    "overall": overall,
    "issues": issues_out,
    "liveBuild": {"confirmed": live_issue is not None, "issue": live_issue, "ambiguous": ambiguous_live},
    "cantTellReasons": cant_tell_reasons,
    # buildQueueShape (issue #66 R6): a distinct, typed field a caller can
    # branch on deterministically (e.g. the advisory build-step-2 renderer),
    # in addition to the SAME reason string already folded into
    # cantTellReasons above for overall-verdict coherence — the two never
    # disagree about the same read, by construction. valid is true for both
    # "ok" (healthy, parses, has the required shape) and "absent" (no
    # build-queue.json yet — not corruption).
    "buildQueueShape": {
        "valid": queue_shape_state in ("ok", "absent"),
        "state": queue_shape_state,
        "reason": queue_shape_reason,
    },
}
print(json.dumps(result))
PY
)"
    rc=$?
    set -e
  else
    set +e
    result="$(node -e '
const fs = require("fs"), path = require("path");
// node -e with trailing args has NO placeholder for script identity (unlike
// running an actual script file, where argv[1] is the script path) — argv
// here is [nodeBinPath, arg1, arg2, ...], so exactly ONE leading slot is skipped.
const [ , candidatesFile, unsafeFile, rulingsDir, lockPresentS, checkedAt, shippedFile ] = process.argv;
const lockPresent = lockPresentS === "1";

function parseIso(s) {
  if (typeof s !== "string" || !s) return null;
  // Cross-runner parity (issue #132): python parse_iso uses
  // datetime.fromisoformat, which is strict ISO-8601. `new Date` is far more
  // lenient — it accepts "July 21, 2026 11:00 AM"/"Jul 21 2026", it silently
  // ROLLS OVER an impossible calendar date ("2026-02-30" -> Mar 2) AND the
  // out-of-range hour 24 ("...T24:00:00Z" -> next-day 00:00), and it reads
  // odd-length fractions / non-colon offsets python accepts too. Any of these
  // would let one runner read a valid date where the other reads none,
  // flipping the verdict purely by which runner is on PATH. So gate on the
  // SAME strict ISO-8601 shape the python twin gates on, then apply the SAME
  // out-of-range checks: calendar date via the Date.UTC probe, and hour 24 via
  // the explicit guard below (new Date does NOT reject hour 24 on its own).
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?(?:[zZ]|[+-]\d{2}:?\d{2})?)?$/.exec(s);
  if (!m) return null;
  const Y = +m[1], Mo = +m[2], Da = +m[3];
  const probe = new Date(Date.UTC(Y, Mo - 1, Da));
  if (probe.getUTCFullYear() !== Y || probe.getUTCMonth() !== Mo - 1 || probe.getUTCDate() !== Da) return null;
  // new Date rolls hour 24 into the next day; python fromisoformat rejects it.
  // Reject it here so both agree (minute/second 60 already yield NaN below).
  if (m[4] !== undefined && +m[4] > 23) return null;
  // A timezone-less ISO-8601 datetime (e.g. an LLM-authored writtenAt of
  // "2099-01-01T00:00:00") is parsed by `new Date` as LOCAL time, which would
  // diverge from the python twin (and shift the tel-vs-commit comparison by
  // the machine offset). Append "Z" for a bare datetime with no Z and no
  // numeric offset so it is read as UTC — matching python parse_iso, which
  // attaches tzinfo=UTC to a naive value.
  let norm = s;
  if (!/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) {
    if (/^\d{4}-\d{2}-\d{2} \d{2}:/.test(s)) {
      // Space-separated bare datetime (e.g. "2026-07-21 15:53:36"): python
      // fromisoformat accepts the space and (via the naive->UTC step) reads
      // it as UTC, but `new Date` reads a space-separated value as LOCAL.
      // Canonicalize the separator to "T" and mark UTC so both runners agree.
      norm = s.replace(" ", "T") + "Z";
    } else if (/T/.test(s)) {
      norm = s + "Z";
    }
  }
  const d = new Date(norm);
  return isNaN(d.getTime()) ? null : d;
}

function branchOf(ref) {
  // See the python3 twin (branch_of): collapse a local branch and its own
  // remote-tracking ref so one branch ahead of its pushed tip is not split
  // into two distinct "branches". Strip refs/heads/ and refs/remotes/<remote>/.
  if (ref.startsWith("refs/heads/")) return ref.slice("refs/heads/".length);
  if (ref.startsWith("refs/remotes/")) {
    const rest = ref.slice("refs/remotes/".length);
    const i = rest.indexOf("/");
    return i !== -1 ? rest.slice(i + 1) : rest;
  }
  return ref;
}

function classifyStateFile(p) {
  // OS-level classification only — absent / symlink / not-a-regular-file /
  // regular. Content-level classification (parse-fail, shape) is layered on
  // top by each caller once "regular" comes back, since build-queue.json
  // (one JSON document) and <issue>-rounds.jsonl (newline-delimited JSON)
  // are read very differently. Shared by every state-file read in this pass
  // so /arc resume and the advisory build-queue shape check (build step 2)
  // always agree about the same file (corruption-state-taxonomy ruling,
  // issue #66 R6) — this is the python3 twin'"'"'s _classify_state_file.
  let st;
  try { st = fs.lstatSync(p); } catch (e) { return "absent"; }
  if (st.isSymbolicLink()) return "symlink";
  if (st.isFile()) return "regular";
  return "not-a-regular-file";
}

function usableIssueString(v) {
  // Parity helper (issue #66 R6) — the node twin of python3'"'"'s
  // _usable_issue_string. Return v coerced to a trimmed string ONLY if it is
  // a JSON scalar (string / number / boolean); return null for every
  // container (object, array) and for null. This is the ONE coercion BOTH
  // the shape validator and the active-issue extraction go through, so:
  //   (a) no object value ever reaches String() — String({"toString":null})
  //       throws TypeError and, because this runs OUTSIDE the JSON.parse
  //       try/catch, would crash the whole node process; refusing to coerce
  //       containers is what keeps the "never crash, only degrade" contract;
  //   (b) node'"'"'s String([]) === "" (blank) can never diverge from python'"'"'s
  //       str([]) === "[]" (truthy) — every non-scalar is treated as blank
  //       on BOTH sides, one coherent verdict for the same bytes regardless
  //       of which interpreter is on PATH (exact-parity ruling). A numeric
  //       issue id is accepted, not just a string. (Integral-float parity —
  //       JSON 3.0 -> "3" not "3.0" — is handled on the python3 side, which
  //       distinguishes float from int; String() here already renders an
  //       integral number without a trailing ".0", so no mirror is needed.)
  //
  // PARITY (issue #66 R6): strip an EXPLICIT ASCII-whitespace set via regex,
  // NOT String.prototype.trim(). Python str.strip() and JS .trim() disagree on
  // control/space codepoints (U+001C-U+001F and U+0085 are stripped by Python
  // but not JS .trim(); U+FEFF is trimmed by JS but not Python), so the same
  // bytes would fold to empty on one runner and stay non-empty on the other —
  // opposite corruption verdicts by interpreter. This character class (space,
  // tab, LF, CR, FF, VT) is character-for-character identical to the python3
  // twin .strip(" \t\n\r\f\v") set; keep the two in sync.
  const t = typeof v;
  if (t === "string" || t === "number" || t === "boolean") {
    const s = String(v).replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, "");
    return s || null;
  }
  return null;
}

function validateBuildQueueShape(qdoc) {
  // Lenient corruption tripwire (be liberal in what you accept), NOT a
  // strict JSON-Schema gate — see the python3 twin
  // (_validate_build_queue_shape) for the full rationale. Returns a
  // plain-language reason string, or null if healthy. Never echoes a raw
  // field VALUE back, only key names / entry positions. Wording matches the
  // python3 twin EXACTLY (word-for-word, not just bucket-for-bucket) so a
  // caller never sees a runner-dependent message for the same file.
  if (!qdoc || typeof qdoc !== "object" || Array.isArray(qdoc)) {
    return "the top-level value of build-queue.json is not a JSON object";
  }
  for (const key of ["active", "queue", "held", "completed"]) {
    if (!Object.prototype.hasOwnProperty.call(qdoc, key)) {
      return "build-queue.json is missing the required \"" + key + "\" key";
    }
  }
  for (const key of ["queue", "held", "completed"]) {
    if (!Array.isArray(qdoc[key])) {
      return "the \"" + key + "\" section of build-queue.json is not a list";
    }
  }
  for (const key of ["held", "completed"]) {
    const arr = qdoc[key];
    for (let idx = 0; idx < arr.length; idx++) {
      const entry = arr[idx];
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
        return "the \"" + key + "\" entry #" + idx + " of build-queue.json is not an object";
      }
      if (usableIssueString(entry.issue) === null) {
        return "the \"" + key + "\" entry #" + idx + " of build-queue.json is missing a usable \"issue\" field";
      }
    }
  }
  return null;
}

const candidates = {};
for (const line of fs.readFileSync(candidatesFile, "utf8").split("\n")) {
  if (!line) continue;
  const parts = line.split("\x1f");
  if (parts.length !== 4) continue;
  const [cid, sha, cdate, sref] = parts;
  (candidates[cid] = candidates[cid] || []).push([sha, cdate, sref]);
}

let unsafeIds = [];
if (fs.existsSync(unsafeFile)) {
  unsafeIds = fs.readFileSync(unsafeFile, "utf8").split("\n").map(s => s.trim()).filter(Boolean);
  unsafeIds = [...new Set(unsafeIds)];
}

// Issues the bash caller proved ALREADY SHIPPED (a squash-merge/PR commit for
// them exists on <base>). Their orphaned arc(<id>): commits are pre-squash
// branch history, not a died run — excluded from any died verdict below (see
// the python3 twin for the full rationale).
let shippedIds = new Set();
if (shippedFile && fs.existsSync(shippedFile)) {
  shippedIds = new Set(fs.readFileSync(shippedFile, "utf8").split("\n").map(s => s.trim()).filter(Boolean));
}

let queueReadable = true, queueUnreadableReason = null, queueActiveIssue = null, activeEntryPresent = false;
let queueShapeState = "absent", queueShapeReason = null;
if (rulingsDir) {
  const qpath = path.join(rulingsDir, "build-queue.json");
  const qstate = classifyStateFile(qpath);
  if (qstate === "symlink") {
    queueShapeState = "symlink";
    queueShapeReason = "build-queue.json is a symlink — refusing to read through it";
    queueReadable = false;
    queueUnreadableReason = queueShapeReason;
  } else if (qstate === "not-a-regular-file") {
    // Exists but is not a regular file (e.g. a directory) — now classified
    // explicitly (not via an EISDIR catch), so this reason string matches
    // the python3 twin exactly instead of diverging to "could not be parsed".
    queueShapeState = "not-a-regular-file";
    queueShapeReason = "build-queue.json could not be read (not a regular file)";
    queueReadable = false;
    queueUnreadableReason = queueShapeReason;
  } else if (qstate === "absent") {
    queueShapeState = "absent";
    // readable, nothing active
  } else {
    // "regular"
    let qdoc;
    try {
      qdoc = JSON.parse(fs.readFileSync(qpath, "utf8"));
    } catch (e) {
      // Runner-neutral reason string (NOT e.constructor.name — node'"'"'s
      // SyntaxError vs python'"'"'s JSONDecodeError): surfaced verbatim to the
      // maintainer at /arc resume and folded into cantTellReasons, so it must
      // read identically regardless of interpreter (exact-parity ruling),
      // matching the python3 twin word-for-word like the shape reasons do.
      queueShapeState = "parse-fail";
      queueShapeReason = "build-queue.json could not be parsed as JSON";
      queueReadable = false;
      queueUnreadableReason = queueShapeReason;
      qdoc = undefined;
    }
    if (qdoc !== undefined) {
      const shapeProblem = validateBuildQueueShape(qdoc);
      if (shapeProblem) {
        // New case (corruption-state-taxonomy ruling): parses, but is
        // missing the shape reconcile-died needs to trust it. Classified into
        // the SAME cant-tell/refuse class as the other unreadable states
        // above — never a 4th `overall` value — by reusing
        // queueReadable/queueUnreadableReason, the exact mechanism that
        // already drives cant-tell below.
        queueShapeState = "parses-but-missing-required-keys";
        queueShapeReason = shapeProblem;
        queueReadable = false;
        queueUnreadableReason = shapeProblem;
      } else {
        queueShapeState = "ok";
        const active = qdoc.active;
        if (active && typeof active === "object" && !Array.isArray(active)) {
          activeEntryPresent = true;
          // Same scalar-only coercion the shape validator uses, so a
          // container-valued active.issue (e.g. {"toString":null}) can never
          // reach a throwing String() call — this line runs outside the
          // JSON.parse try/catch, so an uncaught throw here would crash the
          // whole node arm (see usableIssueString for the parity rationale).
          let s = usableIssueString(active.issue);
          if (s !== null) {
            // PARITY (issue #66 R6): split on the SAME explicit ASCII whitespace
            // set the coercion strip uses, NOT /\s+/ (JS whitespace). /\s+/ and
            // python3'"'"'s bare str.split() disagree on U+001C-U+001F / U+0085 —
            // the codepoints usableIssueString'"'"'s comment names — so the SAME
            // active.issue bytes would extract a different id (or none) by
            // interpreter. Keep this class identical to the python3 twin above.
            const toks = s.split(/[ \t\n\r\f\v]+/).filter(Boolean);
            s = toks.length ? toks[0] : s;
            const parts2 = s.split("#");
            s = parts2[parts2.length - 1];
            if (/^[0-9]+$/.test(s)) queueActiveIssue = s;
          }
        }
      }
    }
  }
}

let ambiguousLive = false, liveIssue = null, staleQueueIssue = null;
const cantTellReasons = [];
if (!queueReadable) {
  cantTellReasons.push(queueUnreadableReason || "build-queue.json is unreadable");
  if (lockPresent) ambiguousLive = true;
} else if (lockPresent) {
  if (queueActiveIssue !== null) {
    liveIssue = queueActiveIssue;
  } else {
    ambiguousLive = true;
    cantTellReasons.push("a build lock (arc-build.lock.d) is held, but build-queue.json'"'"'s active entry does not confirm which issue it belongs to");
  }
} else if (activeEntryPresent) {
  if (queueActiveIssue !== null) {
    staleQueueIssue = queueActiveIssue;
  } else {
    // Active entry present but its issue field did not parse to a bare id. Do
    // NOT fabricate a died entry keyed on a placeholder string (see the python3
    // twin) — report cant-tell, matching every other unattributable-queue case.
    cantTellReasons.push("build-queue.json active entry is present but its issue field could not be parsed");
  }
}

function readRoundsWrittenAts(issueId) {
  // Returns [ok, writtenAts, hadUnparseable] — see the python3 twin (the
  // compute script above, read_rounds_writtenats) for why an unparseable
  // line is tracked separately rather than
  // silently defaulted to either "died" or "matches".
  if (!rulingsDir) return [true, [], false];
  const p = path.join(rulingsDir, issueId + "-rounds.jsonl");
  const fstate = classifyStateFile(p);
  if (fstate === "symlink") return [false, null, false];
  if (fstate === "absent") return [true, [], false];
  if (fstate === "not-a-regular-file") return [false, null, false];
  // fstate === "regular"
  const out = [];
  let hadUnparseable = false;
  let text;
  try { text = fs.readFileSync(p, "utf8"); } catch (e) { return [false, null, false]; }
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    let rec;
    try { rec = JSON.parse(t); } catch (e) { hadUnparseable = true; continue; }
    if (!rec || typeof rec !== "object" || Array.isArray(rec)) continue;
    // PARITY / no-crash (issue #66 R6): the twin of the python3 site above.
    // Route rec.issue through usableIssueString, never a bare String(): a
    // container-valued issue field would otherwise make String(rec.issue) throw
    // a TypeError here (this runs OUTSIDE the JSON.parse try/catch) and crash
    // the whole node arm, breaking the reconcile-died always-exits-0 contract.
    const ris = usableIssueString(rec.issue);
    if (ris !== null && ris === String(issueId)) {
      if (typeof rec.writtenAt === "string" && rec.writtenAt) out.push(rec.writtenAt);
    }
  }
  return [true, out, hadUnparseable];
}

const issuesOut = [];
for (const cid of Object.keys(candidates).sort()) {
  const commits = candidates[cid];
  const entry = {
    issue: cid,
    commitSweepHit: true,
    staleQueueHit: staleQueueIssue === cid,
    commits: commits.map(([sha, cdate]) => sha + " " + cdate),
  };
  // Bucket the candidate commits by the branch they came from, normalizing
  // each %S ref to a bare branch name first (branchOf) so a local branch and
  // its own remote-tracking twin collapse to ONE bucket. newestCommitDt is the
  // max across ALL branches; refNewest holds each distinct branch own newest
  // commit so telemetry coverage is judged PER BRANCH — see the python3 twin
  // for why aggregating would reopen the retried-issue blind spot.
  let newestCommitDt = null;
  const refNewest = new Map();
  for (const [, cdate, sref] of commits) {
    const bref = branchOf(sref);
    const dt = parseIso(cdate);
    if (dt && (!newestCommitDt || dt > newestCommitDt)) newestCommitDt = dt;
    const cur = refNewest.has(bref) ? refNewest.get(bref) : null;
    if (dt && (!cur || dt > cur)) refNewest.set(bref, dt);
    else if (!refNewest.has(bref)) refNewest.set(bref, cur); // keep branch known even if its date was unparseable
  }
  const distinctRefs = [...refNewest.keys()].sort();
  entry.newestCommitAt = newestCommitDt ? newestCommitDt.toISOString() : null;

  if (ambiguousLive) {
    entry.verdict = "cant-tell";
    entry.reason = "liveness could not be confirmed — this issue may be the currently-running build";
    entry.newestTelemetryAt = null;
    issuesOut.push(entry);
    continue;
  }
  if (liveIssue !== null && cid === liveIssue) {
    entry.verdict = "matches";
    entry.reason = "confirmed still running (build lock held + build-queue.json active entry agree)";
    entry.newestTelemetryAt = null;
    issuesOut.push(entry);
    continue;
  }

  const [ok, writtenAts, hadUnparseable] = readRoundsWrittenAts(cid);
  if (!ok) {
    entry.verdict = "cant-tell";
    entry.reason = "<issue>-rounds.jsonl could not be read (symlink or read error) — check by hand";
    entry.newestTelemetryAt = null;
    issuesOut.push(entry);
    continue;
  }
  let newestTelDt = null;
  let telUnparseable = false;
  for (const wa of writtenAts) {
    const dt = parseIso(wa);
    if (!dt) {
      // A telemetry record for THIS issue exists but its writtenAt did not
      // parse as a date — track it separately so an unparseable date is never
      // collapsed into "no telemetry -> died" (see the python3 twin).
      telUnparseable = true;
    } else if (!newestTelDt || dt > newestTelDt) {
      newestTelDt = dt;
    }
  }
  entry.newestTelemetryAt = newestTelDt ? newestTelDt.toISOString() : null;

  if (!newestTelDt) {
    if (hadUnparseable || telUnparseable) {
      entry.verdict = "cant-tell";
      entry.reason = "<issue>-rounds.jsonl has a telemetry line that could not be confirmed to cover this run (bad JSON or an unparseable writtenAt) — check by hand";
    } else {
      entry.verdict = "died";
      entry.reason = "committed arc(" + cid + "): work with no covering telemetry line and no live run signal";
    }
  } else {
    // Telemetry exists. Judge coverage PER BRANCH (see the python3 twin): a
    // branch whose newest commit postdates the newest telemetry is definitely
    // uncovered -> died; when multiple distinct branches carry this issue and
    // none is provably uncovered, one per-issue telemetry stream cannot be
    // attributed to more than one branch -> cant-tell (never auto-pick a
    // winner; a false died is worse than silence).
    const uncoveredRefs = distinctRefs.filter(
      r => refNewest.get(r) && refNewest.get(r) > newestTelDt);
    if (uncoveredRefs.length) {
      entry.verdict = "died";
      entry.reason = "the newest telemetry line for this issue predates orphaned commit(s) on branch(es) " +
        uncoveredRefs.join(", ") + " — a later attempt died mid-flight";
    } else if (distinctRefs.length > 1) {
      entry.verdict = "cant-tell";
      entry.reason = "arc(" + cid + "): commits exist on " + distinctRefs.length +
        " distinct branches (" + distinctRefs.join(", ") + ") but only one telemetry stream covers this " +
        "issue — cannot confirm each branch completed its run; check by hand";
    } else {
      entry.verdict = "matches";
      entry.reason = "telemetry covers this run'"'"'s committed work";
    }
  }
  // exclude-already-shipped: overrides BOTH died legs when a squash-merge/PR
  // commit for this issue exists on <base> (see the python3 twin) — the
  // orphaned arc(<id>): commits are shipped pre-squash history, not a died run.
  if (entry.verdict === "died" && shippedIds.has(cid)) {
    entry.verdict = "matches";
    entry.reason = "already shipped — a squash-merge/PR commit for #" + cid +
      " exists on the base ref; the orphaned arc(" + cid + "): commits are that " +
      "shipped branch'"'"'s pre-squash history, not a died run";
  }
  if (distinctRefs.length > 1) {
    entry.branches = distinctRefs.map(r => ({
      ref: r,
      newestCommitAt: refNewest.get(r) ? refNewest.get(r).toISOString() : null,
    }));
  }
  issuesOut.push(entry);
}

for (const uid of unsafeIds) {
  if (issuesOut.some(i => i.issue === uid)) continue;
  issuesOut.push({
    issue: uid,
    verdict: "cant-tell",
    commitSweepHit: true,
    staleQueueHit: false,
    reason: "commit label id contains characters unsafe for a file path — cannot check telemetry, check by hand",
    commits: [],
    newestCommitAt: null,
    newestTelemetryAt: null,
  });
}

if (staleQueueIssue !== null && !issuesOut.some(i => i.issue === staleQueueIssue)) {
  issuesOut.push({
    issue: staleQueueIssue,
    verdict: "died",
    commitSweepHit: false,
    staleQueueHit: true,
    reason: "build-queue.json'"'"'s active entry names this issue but no build lock is held for it",
    commits: [],
    newestCommitAt: null,
    newestTelemetryAt: null,
  });
}

let overall = "matches";
if (issuesOut.some(i => i.verdict === "died")) overall = "died";
else if (issuesOut.some(i => i.verdict === "cant-tell") || cantTellReasons.length) overall = "cant-tell";

process.stdout.write(JSON.stringify({
  schemaVersion: 1,
  checkedAt: checkedAt,
  overall: overall,
  issues: issuesOut,
  liveBuild: { confirmed: liveIssue !== null, issue: liveIssue, ambiguous: ambiguousLive },
  cantTellReasons: cantTellReasons,
  // buildQueueShape (issue #66 R6): see the python3 twin result dict for
  // the full rationale — a distinct typed field, additive to (never
  // replacing) cantTellReasons.
  buildQueueShape: {
    valid: queueShapeState === "ok" || queueShapeState === "absent",
    state: queueShapeState,
    reason: queueShapeReason,
  },
}));
' "$candidates_file" "$unsafe_file" "$rulings_dir" "$lock_present" "$checked_at" "$shipped_file")"
    rc=$?
    set -e
  fi
  rm -f "$candidates_file" "$unsafe_file" "$shipped_file"
  if [ "$rc" -ne 0 ]; then
    echo "arc-preflight: reconcile-died: internal ${JSON_RUNNER} computation failed (exit ${rc})" >&2
    exit 2
  fi
  printf '%s\n' "$result"
}

# reconcile_died <base> — the public entry point. NEVER blocks (exit 0 on
# every successful check, exit 2 only on a genuine environment/usage failure
# — see the header contract note). Runs ONE pass; if that pass tentatively
# found any "died" issue, re-reads EVERYTHING once more and only keeps
# "died" for an issue BOTH passes independently agree on — a disagreement
# between the two downgrades that ONE issue to "cant-tell" (never silently
# drops it, never keeps a single-read "died" a fresh look didn't reconfirm).
# This is the "re-check once before concluding died" rule: a run finishing
# (or a new one starting) during this check's own read window must never
# produce a false "died".
reconcile_died() {
  local base="${1:-}"
  local raw1
  raw1="$(_reconcile_single_pass "$base")"

  local died1
  died1="$(_reconcile_extract_died_ids "$raw1")"
  if [ -z "$died1" ]; then
    printf '%s\n' "$raw1"
    return 0
  fi

  local raw2 died2
  # Guard the re-check pass the same way the merge step below is guarded. The
  # first pass already succeeded, so _reconcile_single_pass firing a bare
  # `exit 2` here (transient TMPDIR failure, a git-common-dir hiccup during a
  # concurrent gc/fetch, etc.) must NOT hard-abort the whole invocation — that
  # would break the two-pass guarantee this wrapper exists to provide and let
  # the exact class of transient failure the re-check was built to survive slip
  # through with no JSON at all. Downgrade every tentative "died" to cant-tell
  # instead (a false died is worse than silence, per the liveness-proxy ruling).
  set +e
  raw2="$(_reconcile_single_pass "$base")"
  local rc2=$?
  set -e
  if [ "$rc2" -ne 0 ]; then
    echo "arc-preflight: reconcile-died: the re-check pass failed to run — downgrading unconfirmed 'died' verdicts to cant-tell (a false died is worse than silence)" >&2
    _reconcile_died_fallback "$raw1"
    return 0
  fi
  died2="$(_reconcile_extract_died_ids "$raw2")"

  local raw1_file died1_file died2_file
  raw1_file="$(mktemp "${TMPDIR:-/tmp}/arc-reconcile-raw1.XXXXXX")" || { _reconcile_died_fallback "$raw1"; return 0; }
  died1_file="$(mktemp "${TMPDIR:-/tmp}/arc-reconcile-died1.XXXXXX")" || { rm -f "$raw1_file"; _reconcile_died_fallback "$raw1"; return 0; }
  died2_file="$(mktemp "${TMPDIR:-/tmp}/arc-reconcile-died2.XXXXXX")" || { rm -f "$raw1_file" "$died1_file"; _reconcile_died_fallback "$raw1"; return 0; }
  printf '%s' "$raw1" > "$raw1_file"
  printf '%s\n' "$died1" > "$died1_file"
  printf '%s\n' "$died2" > "$died2_file"

  local merged
  set +e
  if [ "$JSON_RUNNER" = "python3" ]; then
    merged="$(python3 - "$raw1_file" "$died1_file" "$died2_file" <<'PY'
import json, sys
raw1_file, died1_file, died2_file = sys.argv[1:4]
with open(raw1_file, encoding="utf-8") as fh:
    doc = json.load(fh)
with open(died1_file, encoding="utf-8") as fh:
    died1 = set(l.strip() for l in fh if l.strip())
with open(died2_file, encoding="utf-8") as fh:
    died2 = set(l.strip() for l in fh if l.strip())
for item in doc.get("issues", []):
    if item.get("verdict") == "died" and item.get("issue") in died1 and item.get("issue") not in died2:
        item["verdict"] = "cant-tell"
        item["reason"] = ("initial read showed died, but a re-check did not confirm it "
                           "(a concurrent run may have just finished or started) — check by hand")
overall = "matches"
if any(i.get("verdict") == "died" for i in doc.get("issues", [])):
    overall = "died"
elif any(i.get("verdict") == "cant-tell" for i in doc.get("issues", [])) or doc.get("cantTellReasons"):
    overall = "cant-tell"
doc["overall"] = overall
doc["rechecked"] = True
print(json.dumps(doc))
PY
)"
  else
    merged="$(node -e '
const fs = require("fs");
// Same node -e argv-offset note as the compute script above: ONE leading slot only.
const [ , raw1File, died1File, died2File ] = process.argv;
const doc = JSON.parse(fs.readFileSync(raw1File, "utf8"));
const died1 = new Set(fs.readFileSync(died1File, "utf8").split("\n").map(s => s.trim()).filter(Boolean));
const died2 = new Set(fs.readFileSync(died2File, "utf8").split("\n").map(s => s.trim()).filter(Boolean));
for (const item of (doc.issues || [])) {
  if (item.verdict === "died" && died1.has(item.issue) && !died2.has(item.issue)) {
    item.verdict = "cant-tell";
    item.reason = "initial read showed died, but a re-check did not confirm it (a concurrent run may have just finished or started) — check by hand";
  }
}
let overall = "matches";
if ((doc.issues || []).some(i => i.verdict === "died")) overall = "died";
else if ((doc.issues || []).some(i => i.verdict === "cant-tell") || (doc.cantTellReasons || []).length) overall = "cant-tell";
doc.overall = overall;
doc.rechecked = true;
process.stdout.write(JSON.stringify(doc));
' "$raw1_file" "$died1_file" "$died2_file")"
  fi
  local merge_rc=$?
  set -e
  rm -f "$raw1_file" "$died1_file" "$died2_file"

  if [ "$merge_rc" -ne 0 ] || [ -z "$merged" ]; then
    # The merge step itself failed. Do NOT fall back to raw1 verbatim — its
    # tentative "died" verdicts were never re-confirmed, and printing them would
    # break the two-pass guarantee and surface a false "died" (worse than
    # silence, per the liveness-proxy ruling). Downgrade every "died" to
    # "cant-tell" instead. Log loudly so this degrade is never silent.
    echo "arc-preflight: reconcile-died: the re-check merge step failed — downgrading unconfirmed 'died' verdicts to cant-tell (a false died is worse than silence)" >&2
    _reconcile_died_fallback "$raw1"
    return 0
  fi
  printf '%s\n' "$merged"
  return 0
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "$MODE" in
  build)
    check_dirty_worktree
    check_rulings "${2:-}"
    # The spec gate is also enforced HERE, not only before discovery: /arc build runs
    # against an EXISTING (or hand-written) rulings file, so a build could otherwise
    # reach pr-ready with rulings but no spec receipt. Discovery's speccheck is the early
    # UX prompt; this is the enforcement backstop (same defense-in-depth as govcheck).
    check_spec "${2:-}"
    # The stale gate runs AFTER check_rulings (which validated the rulings file as
    # parseable JSON with a non-empty discoveryRunId — required before check_stale can
    # safely read that field), and AFTER check_spec. Order matters for error legibility:
    # a build with both a missing rulings file AND a stale/missing marker should report
    # missing-rulings first; the maintainer sees one clear problem, not two interleaved.
    check_stale "${2:-}"
    ok "build"
    ;;
  finish)
    check_dirty_worktree
    ok "finish"
    ;;
  discovery-pre)
    shift
    discovery_pre "$@"
    ;;
  discovery-verify)
    shift
    discovery_verify "$@"
    ;;
  commit-residual)
    shift
    commit_residual "$@"
    ;;
  dirtycheck)
    check_dirty_worktree_postbuild
    ;;
  govcheck)
    shift
    check_governing_docs "$@"
    ;;
  fencecheck)
    shift
    check_fence "$@"
    ;;
  tests)
    shift
    check_tests "$@"
    ;;
  speccheck)
    shift
    check_spec "$@"
    ;;
  seccheck)
    shift
    check_security "$@"
    ;;
  write-guarded)
    shift
    write_guarded "$@"
    ;;
  append-guarded)
    shift
    append_guarded_line "$@"
    ;;
  pending-decision-add)
    shift
    pending_decision_mutate "add" "$@"
    ;;
  pending-decision-clear)
    shift
    pending_decision_mutate "clear" "$@"
    ;;
  pending-decision-stamp-notified)
    shift
    pending_decision_mutate "stamp-notified" "$@"
    ;;
  pending-decision-sweep)
    shift
    pending_decision_mutate "sweep" "-" "$@"
    ;;
  pending-decisions-list)
    pending_decisions_read
    ;;
  global-lease-acquire)
    shift
    discovery_lease_acquire "$@"
    ;;
  global-lease-release)
    shift
    discovery_lease_release "$@"
    ;;
  reconcile-died)
    shift
    reconcile_died "$@"
    ;;
  riskcheck)
    shift
    RISK_SUBMODE="${1:-}"
    case "$RISK_SUBMODE" in
      pre-build)
        shift
        riskcheck_pre_build "$@"
        ;;
      post-implement)
        shift
        riskcheck_post_implement "$@"
        ;;
      *)
        # riskcheck NEVER blocks (see its own header note above): an unknown
        # submode still resolves to the fail-toward-full token on stdout,
        # exit 0 — never this file's usual exit-2 usage-error path.
        echo "arc-preflight: riskcheck: unknown submode '${RISK_SUBMODE}' (expected 'pre-build' or 'post-implement') — resolving to full (riskcheck never blocks)" >&2
        printf 'risk-depth:%s\n' "full"
        ;;
    esac
    ;;
  *)
    cat >&2 <<USAGE
arc-preflight: unknown mode '${MODE}'
Usage:
  arc-preflight.sh build <issue>      dirty-worktree + rulings-required + stale-check + spec gates
  arc-preflight.sh finish <issue>     dirty-worktree gate
  arc-preflight.sh discovery-pre [trust]      (cwd=REAL_REPO) dirty-worktree gate, store main tree state before discovery fires. trust=hardened (default) needs ARC_DISCOVERY_SNAPSHOT_KEY and stores a keyed MAC; trust=solo stores a plain fingerprint
  arc-preflight.sh discovery-verify [trust]   (cwd=REAL_REPO) recompute main tree state; any delta vs. the stored snapshot → discovery-mutated. The comparison method is read off the snapshot's own stored tag, not this trust arg — see the TRUST PROFILE header note
  arc-preflight.sh commit-residual <issue>    (#62, D1) commit-before-gates catch-all: stage + commit whatever ADR-0012's per-round commits missed, as arc(<id>): build output (excludes CHANGELOG.md/VERSION). Fails closed (dirty-residual-sensitive) on secret-shaped content rather than committing it.
  arc-preflight.sh dirtycheck                 (#62, D1) enforced clean-tree assertion after commit-residual. Armor A — never trust-gated.
  arc-preflight.sh govcheck <base> <path>...  block if any guarded governing-doc path changed vs <base>
  arc-preflight.sh fencecheck <base> <issue>  block pr-ready if the diff crosses the run's declared scope fence with no matching fenceException (not trust-gated)
  arc-preflight.sh tests <testCommand> [coverageCommand] [testTimeoutSeconds]  block pr-ready unless the test command ran + exited 0. testTimeoutSeconds (#62, D3) is optional; bounds BOTH testCommand and coverageCommand; a timeout counts as tests-failed.
  arc-preflight.sh seccheck <base> <issue>  block pr-ready if the diff touches a sensitive surface with no security-review receipt
  arc-preflight.sh speccheck <issue>  block discovery if no spec receipt exists for the issue (planning gate)
  arc-preflight.sh write-guarded <relname> [trust]  write STDIN atomically to .gstack/arc-rulings/<relname> (symlink+containment guarded; trust=solo thins the containment check's speed, default hardened)
  arc-preflight.sh append-guarded <relname> [trust] append one STDIN line to .gstack/arc-rulings/<relname> (same guard, plus a per-file lock; trust threads through to write-guarded)
  arc-preflight.sh pending-decision-add <issue> [trust]            (#79, AU2) locked read-modify-write: upsert pending-decisions.json's entry for <issue> from a JSON payload on STDIN ({kind, preview, pointerFile, contentHash})
  arc-preflight.sh pending-decision-clear <issue> [trust]          (#79, AU2) locked read-modify-write: idempotent delete-if-present of pending-decisions.json's entry for <issue>
  arc-preflight.sh pending-decision-stamp-notified <issue> [trust] (#79, AU2) locked read-modify-write: stamp notifiedAt + notifiedContentHash on pending-decisions.json's entry for <issue> (only call after a successful notify)
  arc-preflight.sh pending-decision-sweep [trust]                  (#79, AU2) locked read-modify-write: clear every pending-decisions.json entry whose issue already has a saved <issue>-pr1-args.json rulings file
  arc-preflight.sh pending-decisions-list                          (#79, AU2) read-only dump of pending-decisions.json (the empty document if absent; exit 2 if present-but-corrupt — NOT the same as empty)
  arc-preflight.sh global-lease-acquire [label]                    (#79, AU2) machine-level discovery concurrency cap (2 concurrent); prints a lease token on stdout on success, refuses with a remedy on stderr at the cap
  arc-preflight.sh global-lease-release <token>                    (#79, AU2) release a lease acquired above (idempotent delete-if-present, containment-checked)
  arc-preflight.sh riskcheck pre-build <issue> [trust]         (#119, L3) ADVISORY ONLY, never blocks: prints risk-depth:<light|normal|full> to stdout, always exits 0. Partial pre-build floor (Tier-A fork count only — see the riskcheck header comment)
  arc-preflight.sh riskcheck post-implement <base> <issue> [trust]  (#119, L3) ADVISORY ONLY, never blocks: prints risk-depth:<light|normal|full> to stdout, always exits 0. Full composed read (sensitive-surface + Tier-A forks + diff-size), RATCHETED against any pre-build floor already stored — can only raise the depth, never lower it
  arc-preflight.sh reconcile-died <base>  (#132, R2; buildQueueShape added #66, R6) ADVISORY ONLY, never blocks (not trust-gated — no trust arg): prints ONE JSON object to stdout, always exits 0 on a completed check (exit 2 only if the check itself could not run). Read-only detection of an arc build/finish run killed mid-flight — a died run left committed arc(<id>): work with no covering telemetry line and no live signal (build lock + build-queue.json active entry). <base> is a resolvable ref (prefer a remote-tracking ref, e.g. origin/main) excluded via a git log --all --not <base> history scan. JSON shape: {schemaVersion, checkedAt, overall:"matches"|"died"|"cant-tell", issues:[{issue, verdict, commitSweepHit, staleQueueHit, reason, commits, newestCommitAt, newestTelemetryAt, branches?}], liveBuild:{confirmed, issue, ambiguous}, cantTellReasons, buildQueueShape:{valid, state:"ok"|"absent"|"symlink"|"not-a-regular-file"|"parse-fail"|"parses-but-missing-required-keys"|"cant-tell", reason}, rechecked?}. The optional branches field (present only when an issue commits span more than one distinct ref) lists ref + newestCommitAt per branch; rechecked:true is added when a tentative died verdict triggered the re-check pass. buildQueueShape is a LENIENT corruption tripwire for build-queue.json itself (four required keys present, three list sections are arrays, every held/completed entry carries a usable issue field) — NOT a strict schema gate; its reason (when present) is ALSO folded into cantTellReasons so overall never disagrees with it. See docs/arc-state-lifecycle.md.
USAGE
    exit 2
    ;;
esac
