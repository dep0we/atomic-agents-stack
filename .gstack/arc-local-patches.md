# arc-local-patches — dev-process-kit adoption notes

Notes from adopting the dev-process-kit's arc engine (v0.2.0) onto this repo
(chore/adopt-dev-process-kit). No changes were made to the kit's engine files
themselves (`.claude/workflows/arc-*.js`, `arc-preflight.sh`,
`.claude/skills/arc/SKILL.md` are byte-identical to the kit source) — this
file records the operational decisions that made that possible, plus one
verified non-issue, so a future session doesn't re-litigate them.

## 1. How this repo produces the spec-receipt for `arc-preflight.sh speccheck`

The kit gates `/arc discovery <N>` on a spec receipt existing at
`.gstack/arc-rulings/<N>-spec-receipt.json` (see `check_spec()` in
`.claude/workflows/arc-preflight.sh`). This repo's spec discipline predates
the kit and stays as-is: **author a numbered, LOCKED `docs/spec/NN-*.md` doc
(or a normative amendment to an existing one) BEFORE running `/arc discovery`
— never the gstack `/spec` skill, which only files a GitHub issue.** The
kit's receipt schema does not care *how* the spec was produced, only that a
receipt on file asserts `kind: "spec"` for the matching issue number, so no
engine change was needed to keep that discipline — the receipt is just the
kit's required paper trail for a step this repo was already doing.

**The manual step**, run once discovery's governing spec doc is authored and
locked, before `/arc discovery <N>`:

```bash
python3 - <<'PY'
import json, datetime, sys
issue = "<N>"                                   # bare numeric issue id
spec_doc = "docs/spec/NN-<slug>.md"              # the spec doc/amendment just authored
receipt = {
    "issue": issue,
    "kind": "spec",
    "reason": f"{spec_doc} authored and locked before discovery",
    "ruledAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
path = f".gstack/arc-rulings/{issue}-spec-receipt.json"
json.dump(receipt, open(path, "w"), indent=2)
print(f"wrote {path}")
PY
```

(`reason` is only *required* by the gate for `kind: "trivial"` receipts, but
recording which spec doc satisfied the gate costs nothing and gives the
receipt file the same audit value as the rest of this repo's paper trail —
Principle #5.) For the rare issue genuinely too small to need a spec, use
`"kind": "trivial"` with a real one-line `"reason"` instead — that path is
explicit and dashboard-visible, never a silent skip.

## 2. `install.sh`'s exit 3 is a git quirk, not a real problem (verified)

Running `install.sh` (both the initial run and the `--force` re-run) reported
exit code 3 — "N tooling path(s) are still git-ignored after being placed" —
flagging `.gstack/arc.config.jsonc` and `.gstack/arc-kit-version.json`, both
matched by their own `!`-negation rule in `.gitignore` (`!.gstack/arc.config.jsonc`,
`!.gstack/arc-kit-version.json`).

This is a **false positive**, verified empirically (per CLAUDE.md Principle
#12 — verify before claim, don't accept a tool's exit code by plausibility):
`git check-ignore <path> -v --no-index` (the exact invocation the D9 probe
uses) returns exit 0 whenever the *last matching* `.gitignore` rule for a
path is a negation (`!pattern`) — even though the path is correctly
NOT ignored. Reproduced three independent ways:

- `git check-ignore -q` (no `-v`) on the same path returns 1 (not ignored) —
  correct.
- Default `check-ignore` (no `-v`, no `-q`) also returns 1 — correct.
- A from-scratch isolated repo with the identical `.gstack/*` +
  `!.gstack/arc.config.jsonc` pattern reproduces the same `-v`-only
  discrepancy, independent of `--no-index` or tracked/untracked state.

Both files are genuinely tracked (`git ls-files` lists them; `git add`
succeeded; `git status` shows them as staged/committed, not ignored) — the
D9 probe's own premise ("exists on disk but git will never track it") does
not hold here. No engine patch was made or needed; this note exists purely
so a future `install.sh` re-run (e.g. an upgrade) isn't mistaken for a real
regression in the `.gitignore` narrowing done in this migration.

## 3. Governing-doc (#753) tripwire — kit verification, no patch needed

Verified by reading the kit's adopted `arc-execute.js` / `arc-finish.js` +
`.claude/skills/arc/SKILL.md` (not by running a live build). The kit's
mechanism is a **stricter superset** of this repo's #753 fix, not a
regression:

- `arc-execute.js` / `arc-finish.js` each run an in-workflow
  `governingDocTripwire`-equivalent agent check as defense-in-depth
  (diffs against the workflow's `BASE` — a branch name from
  `CONFIG.baseBranch` — noted in the kit's own comments as advisory only).
- The **enforced** authority is `arc-preflight.sh govcheck <BASE_SHA> <paths>`,
  called by the `/arc` skill (not by the workflow itself) with an
  **immutable SHA resolved from the live remote before the build/finish
  fires** — exactly the #753 fix, applied earlier and more strictly:
  - `/arc build` (SKILL.md step 3): `git checkout <base> && git pull`, then
    `BASE_SHA=$(git rev-parse <base>)` — the `pull` guarantees local `<base>`
    matches `origin/<base>` before the SHA is captured.
  - `/arc finish` (SKILL.md step 2, the exact multi-worktree case #753 was
    about): `git fetch origin <base>`, then
    `BASE_SHA=$(git rev-parse origin/<base>)` explicitly — "a
    remote-tracking ref a local convergence pass can't move... Do NOT read
    paths or the base ref from the parked branch's working tree."
  - `governingDocs.paths` is resolved from `arc.config.jsonc` **at that same
    trusted `BASE_SHA`** (`git show <BASE_SHA>:.gstack/arc.config.jsonc`),
    so a build can't shrink its own guard by editing the config file
    in-flight.
- `governingDocs.paths` / `tripDescription` in this repo's
  `.gstack/arc.config.jsonc` are read by the kit (`CONFIG.governingDocs.paths`,
  falling back to the kit's generic default only if unset) — CLAUDE.md +
  docs/TENSIONS.md are correctly the guarded set, with the routine-table-edit
  exception preserved in `tripDescription`.

No patch made to the kit's engine.
