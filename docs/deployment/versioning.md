# Versioning policy

`atomic-agents-stack` follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
with the project-specific definitions below. Every release is tagged in git
(`vX.Y.Z`), published as a GitHub Release, and gets a dated entry in
[`CHANGELOG.md`](../../CHANGELOG.md) under `## [X.Y.Z] - YYYY-MM-DD`.

The CHANGELOG is the operator's source of truth — it is the only place where
"what changed and what does it require of me" is documented in
operator-facing language. Spec docs describe the contract; the CHANGELOG
describes the upgrade.

---

## What counts as Major / Minor / Patch

| Bump      | Trigger                                                                 | Operator impact                                                  |
|-----------|-------------------------------------------------------------------------|------------------------------------------------------------------|
| **Major** | Schema break, framework API break, default backend change.              | Mandatory work to upgrade. Read `### BREAKING`. Run `migrate`.   |
| **Minor** | New feature, new spec doc, new backend, new CLI subcommand.             | Drop-in upgrade. Existing agents keep working.                   |
| **Patch** | Bug fix, doc, test, internal refactor with no observable change.        | Drop-in upgrade. Safe to apply without reading the diff.         |

### Schema break

Any change to the on-disk frontmatter contract that existing vault content
no longer satisfies. Examples: renaming `confidence` to `certainty`,
flipping a default field's type, dropping a recognised note `type`,
changing the wikilink rewriting rule. Always paired with a migration
script (`atomic_agents.migrate`) and bumps `CURRENT_SCHEMA_VERSION`.

### Framework API break

Any change to the public Python surface (`AtomicAgent`, exception types,
`ToolDefinition`, `MemoryBackend` Protocol, etc.) that would make a
previously-working downstream agent fail to import or behave differently.
Removing or renaming an exported symbol is always a Major bump; *adding*
methods or optional kwargs is Minor.

### Default backend change

Switching the default `MemoryBackend` (or any future
`LockBackend`/`LogBackend`/etc.) from `filesystem` to something else.
Existing operators must explicitly opt back in to the old default — that
is operator work, so it is Major.

### Spec doc additions vs changes

A new `docs/spec/NN-*.md` file is Minor (additive — operators don't have
to do anything). A change to an existing spec doc that tightens a
contract (e.g., narrowing a type) is Major if it would invalidate
existing vault content; otherwise Minor.

---

## Pre-1.0 caveat

Per SemVer §4, while the Major digit is `0`, the public API is not
considered stable — anything *may* change. We use the Major/Minor/Patch
table above as our guide regardless, with one carve-out:

**Pre-1.0, a Minor release MAY include breaking changes.** A Major-shaped
change (schema break, API break, default backend change) does NOT bump
the leading 0 — it bumps the Minor digit and ships a `### BREAKING`
callout in the CHANGELOG. New additive features still bump Minor.
Bug fixes still bump Patch.

So pre-1.0:

| What changed                                | Bump      | Example          |
|---------------------------------------------|-----------|------------------|
| Schema break / API break / default change   | **Minor** | 0.9.0 → 0.10.0   |
| New feature, new module, new subcommand     | **Minor** | 0.9.0 → 0.10.0   |
| Bug fix, doc, test, internal refactor       | **Patch** | 0.10.0 → 0.10.1  |
| The Major digit                             | **stays at 0** until we ship 1.0.0 |

Major + new feature releases share the Minor slot pre-1.0; the only
signal that distinguishes them is the `### BREAKING` callout. That is
why every Minor pre-1.0 release MUST be read end-to-end before
upgrading. If the CHANGELOG entry has no `### BREAKING`, the release is
drop-in. If it does, follow the migration path it prescribes.

When we ship 1.0.0, the digits realign with standard SemVer and the
"Minor may break" carve-out goes away. The 1.0.0 release notes will
spell out the API surface that is then frozen behind Major.

---

## How a release happens

1. PRs land on `main` accumulating entries under `## [Unreleased]` in
   `CHANGELOG.md`. Each PR adds its own bullets — there is no "release
   notes meeting" later.
2. When ready to cut a release:
   - Decide bump level (Major / Minor / Patch) per the rules above.
   - Update `pyproject.toml`'s `version` field.
   - Promote `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`.
     Leave a fresh empty `[Unreleased]` above it for the next cycle.
   - Commit + push: `chore(release): vX.Y.Z`.
3. Tag and publish. The GitHub Release notes must be the CHANGELOG entry
   verbatim — that is the operator's source of truth, including any
   `### BREAKING` callouts. Extract the section, then create the release
   from it:

   ```bash
   # Substitute the version you're releasing.
   VERSION=0.10.0

   # Extract that section out of CHANGELOG.md into a temp file.
   # awk prints lines after the matching ## header until the next ## header.
   awk -v ver="$VERSION" \
       '$0 ~ "^## \\[" ver "\\]" {flag=1; next} /^## \[/{flag=0} flag' \
       CHANGELOG.md > "/tmp/release-notes-v${VERSION}.md"

   # Sanity check: the file should be non-empty and contain the bullets you expect.
   cat "/tmp/release-notes-v${VERSION}.md"

   # Tag and push.
   git tag -a "v${VERSION}" -m "v${VERSION}"
   git push origin "v${VERSION}"

   # Create the GitHub Release with the CHANGELOG entry as the body.
   gh release create "v${VERSION}" \
       --title "v${VERSION}" \
       --notes-file "/tmp/release-notes-v${VERSION}.md"
   ```

   Do **not** use `--notes-from-tag` — that uses only the tag's `-m`
   message (`vX.Y.Z`), not the CHANGELOG section, and operators would
   miss `### BREAKING` callouts. Do **not** use `--generate-notes` as
   the source — it autogenerates from commits, which is noisy and
   doesn't follow the section conventions; if you want to seed a draft,
   run it then hand-edit the output before publishing.
4. The release is now installable via `pip install atomic-agents-stack==X.Y.Z`
   (once PyPI publishing is wired) and pinnable in operator code.

---

## Per-PR expectations

Every PR that lands on `main` adds at least one bullet to `[Unreleased]`
under the appropriate section heading. The `/ship` skill does this
automatically; manual PRs are responsible for the same.

If a PR's diff would force operators to do work to upgrade, the PR title
starts with `feat!:` (or `refactor!:`, `fix!:`) per Conventional
Commits, and the PR body includes the `### BREAKING` callout text
verbatim — ready to paste into the next release's CHANGELOG entry.

---

## Protocol surface breaking-change policy

The Backend Protocol surface (the methods declared on each `Protocol` class) has its own SemVer rules after v1.0.

**Adding a new required method to any Backend Protocol is a Major bump.**
Existing third-party implementations (operators who wrote their own `CorpusBackend` or `LockBackend` etc.) will fail Protocol conformance checks at construction time if they have not added the new method. This is a breaking change for implementers, even though existing callers still work.

**Adding a new optional capability method with a `False` default is a Minor bump.**
If the new method has a default implementation (typically `return False` for `capabilities.supports_X` and `raise NotImplementedError` for the method body), implementers who do not override it advertise `False` capability -- which is protocol-compliant. Callers that check `capabilities.supports_X` before calling the method will not break. This is the established pattern for `install`, `uninstall`, `supports_audit`, and similar optional surfaces.

**Removing or renaming any Backend Protocol method is always a Major bump**, even if no existing conformance test exercises it.

This policy applies to all twelve v1.0 Backend Protocols: MemoryBackend, LLMBackend, JudgeBackend, LockBackend, LogBackend, AgentProfileBackend, ToolRegistryBackend, MandateBackend, PolicyBackend, PersonaBackend, CorpusBackend, MCPServerRegistryBackend.

---

## What this policy does NOT cover

- **PyPI publishing** — separate concern, tracked elsewhere. The release
  pipeline above stops at `gh release`.
- **Pre-release tags** (`vX.Y.Z-rc1`) — not used yet. When introduced,
  they will follow SemVer §9 (`-pre.N` suffix, lower precedence than
  `vX.Y.Z`).
- **Backporting fixes to old majors** — not supported pre-1.0. Operators
  on `v0.x` upgrade forward; there is no `v0.1.x` patch line.

See [`upgrading.md`](upgrading.md) for the operator-facing runbook that
turns a release into a successful upgrade on a running host.
