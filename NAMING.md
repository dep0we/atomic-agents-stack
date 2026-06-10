# Naming candidate: Atomeria

**Status:** Draft. Captured 2026-06-05. Not yet decided. The repo is still `atomic-agents-stack` until the rename is executed.

## The candidate

**Atomeria** is a coined word built from `atom` + the Latin/Romance suffix `-eria` ("place of" / "establishment for"), pattern-matching to Cafeteria, Pizzeria, Galleria, Pomerania. Reads as "place of atoms" or "the atomic place." No pre-existing dictionary meaning. The README's opening paragraph defines what it stands for.

Pronunciation: ah-toh-MEER-ee-ah.

## Why this candidate

Two constraints had to be satisfied at once:
1. Preserve "atomic" as architectural vocabulary (atomic notes, atomic writes, atomic captures stay as the framework's internal language). Avoids a full doc/spec rewrite cost.
2. Find a top-level brand name with all key namespaces actually claimable, so the project can be grown under it.

Atomeria is the only candidate from a wide sweep where every namespace is open.

## Availability (verified 2026-06-05)

| Namespace | Status |
|---|---|
| `github.com/atomeria` (org) | FREE |
| `pypi.org/project/atomeria` | Reserved slot but ZERO releases (dormant squat, no actual product behind it) |
| `atomeria.com` | FREE |
| `atomeria.ai` | FREE |
| `atomeria.io` | FREE |
| `atomeria.dev` | FREE |
| `atomeria.app` | FREE |

PyPI publication strategy: claim the GitHub org as the brand anchor; publish to a hyphenated PyPI name like `atomeria-agents` or `atomeria-framework`. This is the same pattern LangChain, Letta, Mem0, and most modern AI frameworks use (GitHub org as the brand, hyphenated PyPI distribution name).

## Backup option: Atomweave

If Atomeria turns out to have an obscure existing use that the namespace data didn't catch:

| Namespace | Status |
|---|---|
| `github.com/atomweave` (org) | FREE |
| `pypi.org/project/atomweave` | Reserved slot, ZERO releases |
| `atomweave.com` | TAKEN (AWS-hosted, content unverified) |
| `atomweave.ai` | FREE |
| `atomweave.io` / `.dev` / `.app` | FREE |

More verb-y in feel (atom + weave). Workable. The .com situation is a minor wrinkle.

## Names ruled out

| Name | Reason |
|---|---|
| `atomic-agents-stack` (current) | Dan dislikes the "-stack" suffix; redundant since framework is one thing, not a multi-product umbrella |
| `atomic-agents` | Active brand collision: `BrainBlend-AI/atomic-agents` has 5,958 stars, owns PyPI `atomic-agents`, uses the same "atomicity" concept. Same-category competitor |
| `helix` | Same-category collision with the Helix code editor (huge in dev tooling); also Helix genomics company |
| `reactor` | Same-category collision with Project Reactor (Spring's reactive streams library, backbone of Spring WebFlux) |
| `atomera` | Active brand collision with Atomera Inc., publicly-traded semiconductor company (NASDAQ: ATOM); atomera.com is a live business site |
| `tessera` / `tesserae` | `github.com/tessera` org-name held; `tesserae` PyPI is a "Typed LLM wiki graph pipeline" (direct same-category competitor) |
| `orbital` | Workable but generic; no distinctiveness in a crowded AI naming space |
| Anything ending in `-forge` | Overused pattern across AI tooling brands |
| `iota` | IOTA cryptocurrency owns the name |
| `alembic` | Confusion with the standard Python schema-migration library |

## Decision criteria used

1. Top-level GitHub org namespace must be claimable (no rent on the brand name).
2. PyPI status must be either available or dormant-with-zero-releases (no active competitor with the bare name).
3. At least one of .com or .ai must be available.
4. No active same-category brand collision (AI agent frameworks or developer libraries).
5. Atomic theme preserved so internal architectural vocabulary doesn't have to change.
6. Reads as a "regular name" rather than a contrived technical artifact.

Atomeria satisfies all six.

## Migration plan (if approved)

A weekend of work, roughly. Order matters: claim before announcing.

1. Register `github.com/atomeria` as an organization.
2. Register `atomeria.com` and `atomeria.ai` (under $100 total at standard registrars).
3. Reserve the `atomeria-agents` PyPI distribution name with a placeholder release if planning to publish soon. Otherwise hold off until publication time.
4. Reserve social handles (X/Twitter `@atomeria`, etc.) for brand consistency.
5. Sweep the codebase for `atomic-agents-stack` references. Update:
   - `pyproject.toml` URLs
   - README badges and links
   - `CHANGELOG.md` historical references (keep as-is for accuracy; add a renaming note at top)
   - Documentation cross-references
   - `docs/methodology.md`, `docs/architecture.md`, `docs/GOVERNANCE.md`
   - Deployment runbooks
   - Spec docs (most reference "Atomic Agents" the framework name, not the repo name; those should largely be untouched)
6. Update the path reference in Meridian-Stack ADR 0015 (which currently points to `~/Projects/atomic-agents-stack/`).
7. Add a `### Renamed` note to `CHANGELOG.md` `[Unreleased]` capturing the brand transition.
8. Move local project directory: `mv ~/Projects/atomic-agents-stack ~/Projects/atomeria` and update git remote: `git remote set-url origin git@github.com:atomeria/atomeria.git`.
9. GitHub provides automatic redirects from old URLs to new ones, so external links keep working.

What stays unchanged:
- Architectural vocabulary: atomic notes, atomic writes, atomic captures, INDEX-driven recall
- Spec doc numbering and content
- The Python module name `atomic_agents` (this is a separate decision; could stay as `atomic_agents` or transition to `atomeria` over time)

## Outstanding verifications before claiming

1. **Web search for "Atomeria"** to surface any obscure existing use (fiction franchise, indie band, minor company, non-English usage). Namespaces being all-free is a strong signal, but a 30-second sweep closes the loop.
2. **Trademark check** in relevant USPTO categories (computer software, online services).
3. **Pronunciation test** with a couple of people to confirm "ah-toh-MEER-ee-ah" reads cleanly.

## References

- Conversation captured 2026-06-05.
- Wide availability sweep ran across ~50 candidate names.
- All five "PyPI taken" finalist candidates had zero releases (dormant squats), so the brand collision risk on PyPI is effectively nil regardless of which name is picked.
