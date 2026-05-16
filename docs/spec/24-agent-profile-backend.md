# 24 — AgentProfileBackend Protocol

**Status:** **DRAFT** (PR 3 of #63 — locked at PR 4).
**Origin:** [#63](https://github.com/dep0we/atomic-agents-stack/issues/63).
**Shipping plan across four PRs:** PR 1 (Protocol scaffolding + `FilesystemAgentProfileBackend` reference impl + conformance suite + DRAFT spec — merged at #192), PR 2 (wire `AtomicAgent.__init__` + `_load_config()` through the profile backend + per-runner kwargs + `doctor.check_agent_profile_backend` coherence check + cascade unblocker — merged at #193), **PR 3 (second reference impl — `SQLiteAgentProfileBackend` — with parametrized conformance suite + snapshot trio implementation + `supports_skills` capability addition — this PR)**, PR 4 (spec lock-in + `Implementer contract for registry-backed backends` documented + README/CLAUDE.md status refresh).

## Overview

Today an Atomic Agent IS a directory. `AtomicAgent.__init__(name, agents_root, ...)` derives `self.agent_root = agents_root / name`, raises if the directory doesn't exist, then `_load_config()` (`agent.py:1897`) walks it for:

- `model.md` — parsed via `_model.parse_model_md`
- `tools.md` — parsed via `_tools.parse_tools_md` + `_tools.parse_tool_classifications`
- `judges.md` — parsed via `judges_md.load_judges_config` (cascade-aware)
- `roster.md` — parsed via `_roster.parse_roster_md`
- `mcp.md` — parsed via `mcp.parse_mcp_md`
- `persona/IDENTITY.md`, `persona/SOUL.md`, `persona/USER.md` — raw `read_text()`
- `goal.md` — raw `read_text()` for prompt assembly, structured round-trip via `GoalManager`
- `skills/<name>/SKILL.md` — discovered via `skills.discover_skills`

This works on a single host. It **breaks** the moment the deployment shape becomes:

- **SaaS UI editing.** An admin saves edits to IDENTITY.md in a web form and the next agent run picks them up. Filesystem-write-from-web-UI is racy.
- **Multi-tenant agent registry.** "List all agents in this tenant, query by tag, load by ID" — there's no protocol-shaped way to do that today; the framework only knows directory walks.
- **Hot reload.** A long-running coordinator wants to pick up a model.md change without process restart.
- **Snapshots / clones.** "Duplicate scout to scout-v2 with the new tools.md" needs to be one atomic operation; today it's a hand-coded cp-then-edit script.
- **Distributed deployments.** Cloud Run / Kubernetes replicas all reading from the same S3-mounted profile registry.

`AgentProfileBackend` is one of the open protocols in the protocol-pattern series alongside the shipped `MemoryBackend` (spec/20), `LLMBackend` (spec/31), `JudgeBackend` (spec/28), `LockBackend` (spec/21), and `LogBackend` (spec/22). Profile is the deepest bootstrap abstraction in the framework — every other backend Protocol lives downstream of the agent already being constructed. Sealing the layer unblocks SaaS, multi-tenant, UI-driven editing, agent registry queries, and the v1.0 stable surface plan.

The Protocol is **not** a generic identity-store API. It is the minimal contract the framework needs to satisfy the markdown-as-config aesthetic (CLAUDE.md §7) and the audit trail invariant (CLAUDE.md §5). Backends that meet this contract participate fully in the agent bootstrap without forking core.

## Module layout

```
atomic_agents/profile/
├── __init__.py        # registry: register_profile_backend / get_profile_backend / list_profile_backends + get_default_profile_backend factory
├── types.py           # canonical types: AgentProfile, ProfileSnapshot, ProfileCapabilities + AGENT_MODE_* constants
├── backend.py         # AgentProfileBackend Protocol contract
├── filesystem.py      # FilesystemAgentProfileBackend reference implementation
└── sqlite.py          # SQLiteAgentProfileBackend reference implementation (#63 PR 3)
```

Mirrors `atomic_agents/locks/{__init__.py, types.py, backend.py, filesystem.py}` and `atomic_agents/logs/{__init__.py, types.py, backend.py, filesystem.py}`. The split into `types.py` separate from `backend.py` matches the precedent — canonical types ship without pulling in the Protocol contract or any reference implementation.

**PR 1 does NOT ship a `renderers.py`** — see §"Decision 1" below for why.

## Load-bearing design decisions

These decisions are surfaced by reading the actual parser implementations and validated against the prior arc patterns. PR 4 locks them. Each has a "Why" that future contributors and alternate backend authors need before changing the shape.

### Decision 1: Typed shadow + raw text — both stored on `AgentProfile`

The `AgentProfile` dataclass carries **both** the structured form (`model_config: dict`, `tool_config: dict`, etc.) **and** the raw markdown text (`model_md_raw: str`, `tools_md_raw: str`, etc.) for every config file. The filesystem backend writes raw text; the structured form is derived on load. Future database backends MAY store structured columns for query purposes but the canonical reconstruction is via re-parse of raw text.

**Why:** the existing parsers are lossy in three places that make a structured-only profile unsafe to round-trip:

1. **`mcp.py:parse_mcp_md`** resolves `$VAR_NAME` env references at parse time (line 560). A `save_profile()` rendering from `MCPServerSpec` would bake those resolved values — **including secrets** like `GITHUB_PAT=ghp_real_token` — into the on-disk `mcp.md`. This is a security issue. Raw-text storage is the only honest round-trip.
2. **`_tools.py:parse_tools_md`** (line 95-99) strips operator comments after the first separator (` — `, `,`, `(`) and tilde-expands paths. Operators writing `~/scout/data — operator's notes dir` lose the annotation.
3. **`_roster.py:parse_roster_md`** strips everything after the first separator on each bullet. `editor — proofreads drafts` becomes `editor`.

**`renderers.py` is dropped from PR 1 entirely.** The filesystem backend writes raw text via `_io.atomic_write`. A future canonical render layer (PR 3+ if the SQLite backend needs to export back to markdown for migration) is the right place for renderers, with eyes-open about the loss surface.

### Decision 2: Skills are separate Protocol methods, not a profile field

`AgentProfileBackend.list_skills(agent_id)` and `load_skill_body(agent_id, skill_name)` are separate Protocol methods. `AgentProfile` does NOT carry a `skills: list[SkillManifest]` field.

**Why:**
1. `SkillManifest` (`skills.py:71`) holds `skill_dir: Path` and `skill_md_path: Path` — filesystem-specific values that don't translate to non-filesystem backends. A database backend would have to fake these paths or strip the fields.
2. Skills are lazy-loaded by design (spec/18 + CLAUDE.md §6). Only metadata lives in the system prompt; bodies load on demand via the `load_skill` tool. Embedding `list[SkillManifest]` with bodies in `AgentProfile` violates this principle.
3. A future `DatabaseAgentProfileBackend` naturally stores skills in a `skills` table per-agent — separate methods let it return rows directly without a blob column.

The filesystem reference delegates `list_skills` to `discover_skills()` and `load_skill_body` to existing `skills.load_skill_body()` helpers.

### Decision 3: Snapshot trio uses `profile.to_dict()` JSON, NOT `shutil.copytree`

**PR 3 shipped (was deferred in PR 1):** `snapshot(agent_id, label)` / `restore(agent_id, snapshot_id)` / `list_snapshots(agent_id)` are implemented on both reference backends. **Both backends declare `supports_snapshot=True`.**

The implementation shape is structurally identical across backends — `snapshot()` loads the current `AgentProfile` and serializes `profile.to_dict()` as JSON; `restore()` reconstructs the profile via `AgentProfile.from_dict()` and writes via the backend's normal `save_profile()` path:

- **Filesystem:** writes `<scope_root>/.snapshots/<agent_id>/<snapshot_id>/profile.json` + `metadata.json` via `_io.atomic_write`. The snapshot dir is created with `exist_ok=False` for collision detection.
- **SQLite:** inserts into a `profile_snapshots` table with columns `(snapshot_id PK, agent_id, label, created_at, profile_json)` and the `idx_snapshots_agent_id` + `idx_snapshots_created_at` indexes.

**Why JSON, not directory copy** (Plan-subagent fix during PR 3 design): `shutil.copytree` is not atomic at the agent level. A crash mid-copy leaves the agent partially snapshotted; restore via `copytree(snapshot_dir, agent_root, dirs_exist_ok=True)` has no good atomicity story either. The JSON shape round-trips through `AgentProfile.to_dict / from_dict` and `restore()` writes via `save_profile()`, which already has the per-file atomic_write discipline. Same shape on both backends; future Postgres / git backends mirror it directly.

**Snapshot ID format:** `snap_<YYYY-MM-DDTHHMMSS+TZ>_<6hex>` — timestamp prefix gives free lexicographic-sort fallback, the 6-hex random tail eliminates same-second collisions, no colons in the time field for Windows-share portability. Filesystem backend additionally validates the shape via `_validate_snapshot_id` (refuses any operator-supplied path-traversal token) before touching disk; SQLite's `restore()` filters on `snapshot_id` as an opaque string in the WHERE clause.

**Cross-agent isolation:** `restore(agent_id, snapshot_id)` enforces that the snapshot belongs to `agent_id`. SQLite enforces via the `WHERE snapshot_id = ? AND agent_id = ?` clause; filesystem enforces via path-scoping (`relative_to(snapshots_root)`) plus a `metadata.agent_id == agent_id` cross-check. A `SnapshotNotFound` raised by either path is indistinguishable to the caller — the rule is "snapshots scope per-agent" regardless of mechanism.

**Skills are NOT snapshotted.** They aren't part of `AgentProfile` (Decision 2), so `to_dict()` doesn't carry them. For the filesystem backend, this means snapshotting an agent with operator-edited skills under `skills/` will not preserve them in the snapshot; restore reverts only the profile fields. Operators wanting skill-snapshotting need the future `save_skill` Protocol method (see §"Reserved future capabilities").

### Decision 4: `AgentProfile` is `@dataclass(frozen=True)`

`AgentProfile` is frozen — field reassignment raises `dataclasses.FrozenInstanceError`. Mutable nested types (`dict`, `list`, the `JudgesConfig` itself which contains a `failure_policy: dict[ActionClass, dict[str, str]]`) work fine within a frozen dataclass because Python's frozen check only prevents *field* reassignment, not nested mutation. This matches the `JudgesConfig` precedent (`judges_md.py:122`) and the `RunRecord` precedent (`logs/types.py:92`).

**Why:** the framework / backend / consumer boundary is the load-bearing immutability surface. Backends MUST NOT mutate a passed-in profile (a backend that adds a field to `profile.extra` would silently corrupt the caller's data). `frozen=True` enforces that at the language level. Users wanting modifications use `dataclasses.replace()` or the `profile.replace(...)` convenience wrapper.

### Decision 5: Cascade carve-out — `AgentProfile` captures the post-merge view; save writes instance-layer only

For cascaded agents (`<system>/projects/<project>/agents/<role>/`), `FilesystemAgentProfileBackend.load_profile()` calls `_cascade.detect_cascade(agent_root)` and reads `tools_md_raw`/`model_md_raw` as the merged text from `_cascade.resolve_tools_md()` / `_cascade.resolve_model_md()`. The `AgentProfile` captures the effective post-merge config as of load time.

**`save_profile()` writes only instance-layer files.** Project-floor `judges.md`, role `tools.md`/`model.md`, project `canon.md`/`style_guide.md`/`policy.md` are read paths only from the profile backend's perspective. The filesystem backend conservatively only writes the instance file when one already exists — operators wanting to override a role-layer file create the instance file first, then save.

**Why:** the role layer is shared across multiple instances. A `save_profile` that wrote to the role-layer file would corrupt every other instance silently. Operators editing role/project layers do so out-of-band; the profile backend exists for instance-layer mutation.

### Decision 6: `agent_mode` is documented-derived

`AgentProfile.agent_mode` is a top-level field but its source of truth is `persona_identity`. `FilesystemAgentProfileBackend.load_profile()` derives it via `goal.parse_agent_mode(identity_path)`. `save_profile()` **ignores the field** — only `persona_identity` is written, and the next `load_profile()` re-derives.

**Why:** `parse_agent_mode` reads the `## Operating mode` section of IDENTITY.md prose (`goal.py:186-213`). It's a substring of `persona_identity`, not a separate file or frontmatter key. Database backends MAY persist `agent_mode` as an indexable column for registry queries, but they MUST re-derive on update to avoid divergence. Documented asymmetry is cleaner than dropping the field — DB query patterns want the column.

### Decision 8: Skills are filesystem-only in PR 3 — `supports_skills` capability gates skill content (#63 PR 3)

`ProfileCapabilities` carries a `supports_skills: bool` field. `FilesystemAgentProfileBackend` declares `True` (walks `<agent>/skills/<name>/SKILL.md`); `SQLiteAgentProfileBackend` declares `False` — `list_skills` returns `[]` for present agents, `load_skill_body` always raises `FileNotFoundError`, and `clone` does not copy skills (there are none to copy).

**Why skills stay filesystem-only in PR 3:** `SkillManifest` carries filesystem-specific `Path` fields (`skill_dir`, `skill_md_path`). Storing skill bodies in the database would require a new `save_skill` Protocol method — a Protocol expansion that PR 3 scope-disciplines away. PR 3's goal is the second reference backend, not the second Protocol surface.

**Conformance test gating:** the parametrized conformance suite checks the capability before exercising skill content. `test_list_skills_returns_metadata`, `test_load_skill_body_returns_body_without_frontmatter`, `test_clone_copies_skills_directory`, and `test_load_skill_body_refuses_skill_name_traversal` all skip with `supports_skills=False`. Empty-skill tests (`test_list_skills_empty`) pass on both backends because non-supporting backends return `[]` from `list_skills`. `test_list_skills_missing_agent_raises_not_found` + `test_load_skill_body_missing_agent_raises_not_found` (GAP-11) pass on both because both backends check agent existence before any skill operation.

**Operator surface for skills-on-SQLite:** operators running SQLite-backed profiles who need skills today have two options — (1) keep skills filesystem-only alongside the SQLite profile registry (works because skill registration happens at agent-construction time via the `load_skill` tool, not via `load_profile`), OR (2) wait for the future `save_skill` Protocol method tracked under §"Reserved future capabilities".

### Decision 7: `wiki/INDEX.md` is NOT part of `AgentProfile`

`_load_indexes()` (`agent.py:2031`) reads `wiki/INDEX.md` for system-prompt assembly, but this is memory-layer state, not identity-layer config. Memory is already abstracted via `MemoryBackend` (spec/20).

**Why:** scope discipline. `AgentProfile` is the identity/config layer; including wiki content would conflate config with memory state and create two backends with overlapping write responsibilities. Memory backend stays the source of truth for `wiki/`, `memory/`, `journal/`. Profile backend stays for `persona/`, config files, skills.

## Canonical types

### `AgentProfile` — the unit of load/save

```python
@dataclass(frozen=True)
class AgentProfile:
    # Required identity
    name: str
    agent_mode: str  # "reactive" | "goal-driven" | "hybrid" — derived

    # Structured config (for DB-backend query / inspection)
    model_config: dict[str, Any]              # _model.parse_model_md shape
    tool_config: dict[str, Any]               # _tools.parse_tools_md shape
    tool_classifications: dict[str, str]      # tool name → action class
    judges_config: JudgesConfig | None
    roster: list[str]
    mcp_servers: list[MCPServerSpec]          # parser-resolved env refs

    # Raw markdown bodies (source of truth for persona/goal)
    persona_identity: str
    persona_soul: str
    persona_user: str
    goal_text: str

    # Raw markdown text for structured config files
    # (source of truth for filesystem write-back — see Decision 1)
    model_md_raw: str
    tools_md_raw: str                          # post-cascade-merge
    judges_md_raw: str | None
    roster_md_raw: str
    mcp_md_raw: str                            # PRESERVES $VAR refs
```

`AgentProfile.to_dict()` / `from_dict()` round-trip is byte-shape preserving for raw-text fields; structured fields reconstruct via the underlying parser's idempotency.

### `ProfileSnapshot`

```python
@dataclass(frozen=True)
class ProfileSnapshot:
    snapshot_id: str       # backend-issued
    label: str             # operator-supplied
    created_at: str        # ISO-8601 with tz
    agent_id: str          # scope check
```

### `ProfileCapabilities`

```python
@dataclass(frozen=True)
class ProfileCapabilities:
    supports_save: bool        # False for read-only template libraries
    supports_clone: bool
    supports_snapshot: bool    # False on PR 1 filesystem (Decision 3)
    supports_subscribe: bool   # reserved future capability
    durable: bool
```

Conformance tests assert claim-vs-behavior parity (mirrors `LogCapabilities` precedent).

## `AgentProfileBackend` Protocol surface

```python
@runtime_checkable
class AgentProfileBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    # Core (always implemented)
    def load_profile(self, agent_id: str) -> AgentProfile: ...
    def save_profile(self, agent_id: str, profile: AgentProfile) -> None: ...
    def list_agents(self) -> list[str]: ...
    def exists(self, agent_id: str) -> bool: ...

    # Skills — separate methods (Decision 2)
    def list_skills(self, agent_id: str) -> list[SkillManifest]: ...
    def load_skill_body(self, agent_id: str, skill_name: str) -> str: ...

    # Capability-gated (MAY raise NotImplementedError when capability False)
    def clone(self, source_id: str, target_id: str,
              overrides: dict[str, Any] | None = None) -> None: ...
    def snapshot(self, agent_id: str, label: str) -> str: ...
    def restore(self, agent_id: str, snapshot_id: str) -> None: ...
    def list_snapshots(self, agent_id: str) -> list[ProfileSnapshot]: ...

    def capabilities(self) -> ProfileCapabilities: ...
```

### `load_profile` semantics

- MUST raise `AgentProfileNotFound` when the agent doesn't exist. Empty-profile defaults are NOT the same as "agent missing."
- MUST populate every required field. `name` mirrors the argument; `agent_mode` is derived (Decision 6).
- MUST populate raw-text shadow fields verbatim from source. Cascaded agents get the post-merge text for `tools_md_raw` / `model_md_raw` (Decision 5).
- MUST populate structured fields by re-parsing raw text via the existing parsers.

### `save_profile` semantics

- MUST persist before returning (fsync, transaction commit, server ack).
- MUST overwrite silently — `clone()` is the safe-create primitive; `save_profile` is updates-allowed.
- For raw-text fields, writes verbatim to corresponding paths. Structured fields are IGNORED on save (raw text is source of truth).
- `agent_mode` is IGNORED on save (re-derived from `persona_identity` on next load).
- For cascaded agents, writes ONLY instance-layer files (Decision 5).
- MAY raise `NotImplementedError` when `supports_save=False`.

### `list_agents` semantics

- Returns ids only, lexicographic order.
- MUST exclude backend-internal storage (filesystem: hidden dirs starting with `.`).
- MUST exclude entries failing the "is this an agent?" sentinel check. Filesystem requires `persona/IDENTITY.md`.

### `exists` semantics

- MUST be O(1)-ish — no full profile load for a presence check.
- MUST return False (NOT raise) for missing agents.
- MUST use the same sentinel as `list_agents()`.

### `list_skills` / `load_skill_body` semantics

- `list_skills` returns metadata only — no bodies. Lazy-load via `load_skill_body`.
- `load_skill_body` returns body without frontmatter (matches `skills.load_skill_body` precedent).
- Both raise `AgentProfileNotFound` for missing agents.

### `clone` semantics

- MUST raise `AgentProfileNotFound` for missing source.
- MUST raise `AgentProfileExists` for existing target (refuses silent overwrite).
- `overrides` keys must match `AgentProfile` field names; unknown raises `ValueError`.
- MUST be atomic at the agent level.
- MUST copy skills along with the profile.
- MAY raise `NotImplementedError` when `supports_clone=False`.

### Snapshot trio semantics

- `snapshot(agent_id, label)` returns a backend-issued unique id.
- `restore(agent_id, snapshot_id)` overwrites current state with snapshot contents. Raises `SnapshotNotFound` for unknown ids OR ids belonging to a different agent (cross-tenant safety).
- `list_snapshots(agent_id)` returns `ProfileSnapshot[]` in chronological order.
- All three MAY raise `NotImplementedError` when `supports_snapshot=False`.

## Reference implementations

### `FilesystemAgentProfileBackend`

Conforms to the Protocol with the constructor signature `FilesystemAgentProfileBackend(scope_root)`.

- `scope_root` is the parent directory containing agent subdirectories — `agents_root` in the framework's existing vocabulary.
- `scope_root` MUST exist at construction; the constructor raises `ValueError` otherwise.
- `agent_id` MAY contain `/` for cascade agents (`muse/projects/the-unfinished/agents/writer`); the `relative_to(scope_root)` resolved-path check enforces the actual security boundary. Empty `agent_id`, leading `.`, backslash, and `..` are refused up-front.
- `load_profile()` walks the directory via the existing parsers; cascade-aware via `_cascade.detect_cascade()`.
- `save_profile()` writes raw-text fields via `_io.atomic_write`; instance-layer only for cascaded agents.
- `list_agents()` enumerates `scope_root` subdirs containing `persona/IDENTITY.md`; skips hidden dirs.
- `clone()` does load → `dataclasses.replace` overrides → save → copy skills directory tree.
- `snapshot()` writes `<scope_root>/.snapshots/<agent_id>/<snapshot_id>/profile.json` + `metadata.json` (Decision 3). `restore()` validates `snapshot_id` shape, path-scope checks the snapshot dir via `relative_to`, verifies `metadata.agent_id == agent_id`, and writes via `save_profile`. `list_snapshots()` enumerates `<scope_root>/.snapshots/<agent_id>/` chronologically.

Capabilities: `supports_save=True, supports_clone=True, supports_snapshot=True, supports_subscribe=False, durable=True, supports_skills=True`.

### `SQLiteAgentProfileBackend` (#63 PR 3)

Conforms to the Protocol with the constructor signature `SQLiteAgentProfileBackend(db_path: Path | str)`. Constructed from the `sqlite://` URL family via `make_sqlite_profile_backend_from_url(url)`.

- **No optional dependency** — stdlib `sqlite3` only.
- **Schema:** `agents(name PK, agent_mode indexed, profile_json, updated_at)`, `profile_snapshots(snapshot_id PK, agent_id indexed, label, created_at indexed, profile_json)`, `meta(key PK, value)`. Schema version tracked in `meta('schema_version', '1')`; cold-start race mitigated via `INSERT OR IGNORE` (mirrors spec/22's `SQLiteLogBackend` cold-start fix).
- **JSON blob + indexed scalars** (Decision 1 of PR 3): `AgentProfile.to_dict()` serializes to the `profile_json` column with `default=str` (PosixPath coercion); `agent_mode` is duplicated as an indexed scalar for registry-by-mode queries. `from_dict` re-derives structured fields from raw text on load, so the JSON shape is forward-compatible with new AgentProfile fields without `ALTER TABLE`.
- **`save_profile()` re-derives `agent_mode`** from `persona_identity` before writing (Decision 6 — same invariant the filesystem backend enforces via its load-from-disk parser path; SQLite enforces at write time).
- **Concurrency:** `threading.local` connection pool gives each thread its own `sqlite3.Connection`. WAL journal mode + `synchronous=NORMAL` for multi-process safety on local filesystems. **Network-mounted filesystems (NFS, SMB) NOT supported** — SQLite WAL on NFS is documented-broken upstream.
- **Snapshot trio:** writes to `profile_snapshots` table; cross-agent isolation enforced at the SQL `WHERE` clause (`WHERE snapshot_id = ? AND agent_id = ?` in `restore()`).
- **Skills:** `supports_skills=False` (Decision 8). `list_skills` returns `[]` for present agents (raises `AgentProfileNotFound` for missing); `load_skill_body` always raises `FileNotFoundError`.
- **In-memory mode:** `SQLiteAgentProfileBackend(":memory:")` or URL `sqlite::memory:` / `sqlite:///:memory:` constructs a non-persistent backend; emits `RuntimeWarning` and reports `durable=False`. Test-only.

Capabilities: `supports_save=True, supports_clone=True, supports_snapshot=True, supports_subscribe=False, durable=True (False for :memory:), supports_skills=False`.

## Exception surface

- `AgentProfileNotFound` — agent id doesn't exist in the backend.
- `AgentProfileExists` — `clone()` refused to overwrite existing target.
- `SnapshotNotFound` — `restore()` referenced an unknown snapshot id.
- `BackendNotRegistered` — operator-pinned backend_id isn't in the registry.
- `ValueError` — invalid agent_id (path separator, empty, escapes scope), or `clone()` overrides has an unknown field name.
- `NotImplementedError` — capability-gated method on a backend that doesn't support that capability.

## Registry

```python
from atomic_agents.profile import (
    register_profile_backend, get_profile_backend, list_profile_backends,
)

register_profile_backend("filesystem", FilesystemAgentProfileBackend)
cls = get_profile_backend("filesystem")            # → FilesystemAgentProfileBackend
backend = cls(agents_root)                          # caller instantiates with scope
ids = list_profile_backends()                       # ["filesystem"]
```

The registry stores **classes**, not instances (matches LogBackend spec/22 §Registry + LockBackend spec/21 §Registry). Profile backends carry per-scope construction arguments; the registry's role is the operator-pin lookup that resolves a backend_id to a class.

The default `"filesystem"` registration happens at import time inside `atomic_agents/profile/__init__.py`.

## Operator surface

Profile backend choice is a **deployment-level** decision (the whole framework instance picks "filesystem" or "sqlite" or "git"), not an agent-author-level decision. Contrast with:

- `judges.md` — per-agent because judge policy is per-agent-author concern.
- `model.md`'s `provider:` — per-agent because model choice is per-agent.

There is no `profile.md` markdown config — the profile backend is the layer that READS the markdown config files; circular.

PR 2 of #63 will expose the choice via TWO paths (parallel to the LogBackend / LockBackend operator surfaces):

1. **Constructor kwarg** — programmatic operators (Python entry-points wiring the framework into Cloud Run, Kubernetes deployments with custom database connections) pass `AtomicAgent(..., profile_backend=DatabaseAgentProfileBackend(...))` to bypass env-var resolution entirely.

2. **Environment variables** — deployment-config operators (Docker, launchd, Cloud Run env, systemd units) set:
   - `ATOMIC_AGENTS_PROFILE_BACKEND` — backend id (default `filesystem`). Recognized: `filesystem`, `sqlite`.
   - `ATOMIC_AGENTS_PROFILE_BACKEND_URL` — connection / path string for non-filesystem backends. SQLite shape: `sqlite:///absolute/path/to/profiles.db` (three-slash convention; empty netloc), `sqlite::memory:` / `sqlite:///:memory:` for in-memory (test-only, warns). When `ATOMIC_AGENTS_PROFILE_BACKEND=sqlite` is set without a URL, the backend defaults to `<scope_root>/.profile.db`.

   **Credential safety**: `get_default_profile_backend` sanitizes the `ATOMIC_AGENTS_PROFILE_BACKEND` value before echoing it in error messages — strips anything following `://` and truncates at 32 chars — so an operator who accidentally pastes a URL credential into `ATOMIC_AGENTS_PROFILE_BACKEND` (instead of `..._URL`) does not see the credential echoed in the resulting `BackendNotRegistered` exception text. Mirrors the same fix from `logs/__init__.py:316` and `locks/__init__.py:194`.

The constructor kwarg ALWAYS wins. Operator-config layering: env vars are deployment-level (per-instance, per-host); the kwarg is per-agent-construction. A test that constructs an `AtomicAgent` with an explicit `profile_backend=` bypasses any env vars the deployment may have set.

## What PR 1 did NOT do (now done in PR 2)

PR 1 shipped pure scaffolding — Protocol, filesystem reference impl, conformance suite, DRAFT spec — with **zero call-site changes**. PR 2 wires the bootstrap path.

## What PR 2 wires

**Core (`atomic_agents/agent.py`):**

- `AtomicAgent.__init__` accepts `profile_backend: AgentProfileBackend | None = None` as a keyword-only kwarg (after `lock_backend` + `log_backend`, mirroring the established pattern). When unset, resolves via `get_default_profile_backend(self.agents_root)` (env-var-aware default). Stored as the **public** `self.profile_backend` for diagnostic + runner reuse, matching `self.lock_backend` / `self.log_backend`.
- `self._profile = self.profile_backend.load_profile(self.name)` snapshots the agent's config ONCE at init — private cache, not a stable operator surface.
- `_load_config()` is now a thin adapter reading fields from `self._profile` instead of file reads. **Cascade branch deleted** (Decision 7): `FilesystemAgentProfileBackend.load_profile` handles cascade internally. `self.cascade` is preserved at init for downstream uses (`_load_role_prompt`, `_load_project_layer_text`, `_load_tools_text`, tool-registration paths) — those load call-time content, not init-time config.
- `_load_persona()` and `_load_goal_text()` read from `self._profile` instead of re-reading the files.
- `agent_mode` is derived from the profile snapshot's `agent_mode` field (re-derived from `persona_identity` via the profile backend's `goal.parse_agent_mode_text`).

**Cascade unblocker (`atomic_agents/profile/filesystem.py:_agent_root`):**

- Removed the `/` refusal that PR 1 added as overly-restrictive belt-and-suspenders. Cascade agents have multi-segment `agent_id` values like `"muse/projects/the-unfinished/agents/writer"`. The `.resolve() + .relative_to(scope_root)` check is the actual security boundary; it catches `..` traversal after path resolution. All other validations (empty rejection, leading `.` refusal, backslash refusal, explicit `..` refusal) stay. **This unblocks all 9 cascade integration tests under PR 2.**

**Runner threading (`outcome.py`, `eval.py`, `dream.py`):**

- `OutcomeRunner.__init__` adds `profile_backend` kwarg; threaded to internal `AtomicAgent` at `run()`.
- `EvalRunner.__init__` adds `log_backend` AND `profile_backend` kwargs (Decision 3: fixed the pre-existing `log_backend` drop-trap simultaneously).
- `DreamRunner.__init__` adds `profile_backend` kwarg; pre-resolves `self._profile = self._profile_backend.load_profile(self.agent_name)` at init. **The Step 11 P1#3 trap**: `DreamRunner` reads `model.md` in TWO call sites — `dream.py:1128` (pipeline) AND `dream.py:672` (`_check_cap` cost-guardrail). PR 2 routes BOTH through the profile backend:
  - **Site 1** (`__init__:1128`): `self._model = self._profile.model_config["default_model"]` (replaces direct `_model.parse_model_md` read)
  - **Site 2** (`_check_cap:672`): the function now accepts `model_config: dict | None = None` kwarg (Decision 2 — passing pre-resolved config beats threading `profile_backend` + re-resolving the agent_name from `agent_root`). `DreamRunner.start()` passes `model_config=self._profile.model_config` so the cost-guardrail uses the same source the rest of DreamRunner uses. Legacy fallback (`_model.parse_model_md`) preserved for callers who don't pass `model_config`.

**Doctor (`atomic_agents/doctor.py:check_agent_profile_backend`):**

- New function mirroring `check_log_backend` shape (PASS/WARN/FAIL ladder, URL credential redaction via `urlparse + _replace`, capability + agent-count probe).
- Wired into `run_doctor()` between `check_log_backend` and `check_memory_backend`.
- Added `"profile-backend"` to the skip-names tuple so it emits a SKIP entry when no agent is supplied (parity with `memory-backend`).

**Public surface:**

- `atomic_agents.__init__` already exported the AgentProfileBackend surface in PR 1.
- `outcome.py`, `eval.py`, `dream.py` now import `AgentProfileBackend` (under `TYPE_CHECKING` in dream.py to match existing pattern).

**What PR 2 still does NOT do:**

- No second backend — that's PR 3 (`SQLiteAgentProfileBackend` likely).
- No `renderers.py` — Decision 1; deferred to PR 3 if a DB backend needs canonical markdown export.
- No snapshot implementation — Decision 3; lands with PR 3's second backend.
- No `subscribe` / hot-reload — capability flag reserved; deferred indefinitely.
- No CLI flag for `--profile-backend` — env var path (`ATOMIC_AGENTS_PROFILE_BACKEND`) covers operator config; CLI flag would add API surface without a use case.

PR 3 ships the second reference impl (likely `SQLiteAgentProfileBackend` — registry table + per-agent row + snapshots table) and parametrizes the conformance suite. PR 4 locks this spec and adds the `§"Implementer contract for registry-backed backends"` section below.

### PR 3 implementer note — synchronous load_profile at __init__ blocks construction

`AtomicAgent.__init__` calls `self.profile_backend.load_profile(self.name)` UNCONDITIONALLY and SYNCHRONOUSLY at construction time (`agent.py:308`). For `FilesystemAgentProfileBackend`, this is a handful of file reads — sub-millisecond. For a future `SQLiteAgentProfileBackend` or `DatabaseAgentProfileBackend` on a remote server, this is a network round-trip on every `AtomicAgent()` construction site (96 existing test sites today; production fleet deployments may construct hundreds per process). #63 PR 2 Step 11 adversarial Finding 2 flagged this as a PR 3 trap shape preview.

PR 3 backend implementers MUST EITHER (a) ensure `load_profile` completes within a sub-second budget under typical network conditions, OR (b) propose a Protocol extension that allows lazy-loading (e.g., a `lazy_load_profile` flag on the backend, or a `@cached_property` shape inside `AtomicAgent`). The latter is a Protocol change and would land via PR 3's spec-doc updates, not as a code-only addition.

Filesystem-default operators see no impact; this affects the deployment surface for non-filesystem backends.

### Known gaps deferred to follow-up issues

The #63 PR 1 Step 11 adversarial review surfaced two filesystem-backend rough edges that don't block scaffolding but should land before PR 4 locks:

- **`clone()` TOCTOU race** — two concurrent `clone(source, target)` calls can both pass the `exists(target)` check before either writes, then both proceed and the second silently overwrites the first. `clone()`'s contract promises `AgentProfileExists` for an existing target, but the guarantee is window-wide rather than atomic. Single-host typical deployments rarely trip this; Cloud Run / Kubernetes multi-replica deployments will. Fix shape: replace the check-then-mkdir pattern with `os.mkdir(target_root, mode=0o755)` which raises `FileExistsError` atomically as the sentinel.

- **`atomic_write` destroys symlinks** — `_io.atomic_write` uses `os.replace`, which atomically replaces the target including any symlink. Operators with `persona/SOUL.md` symlinked to a shared fleet-wide persona file would have the symlink silently converted to a regular file on first `save_profile`. The agent continues to work normally (reads the now-regular file), but the shared-persona update path breaks without any error. Fix shape: in `save_profile`, detect `path.is_symlink()` before writing and either refuse (with `WritePathViolation`) or follow the symlink (write to target rather than replace the symlink). Document the chosen behavior in the Implementer contract.

## Implementer contract for registry-backed backends

*Placeholder — locked in #63 PR 4. The section will mirror spec/22 §"Implementer contract for queryable backends" in shape: 6-8 normative MUSTs derived from the PR 3 review-pass adversarial findings. Likely topics:*

- *Atomic schema initialization across processes (Decision 4 of spec/22's contract — applies here too).*
- *Multi-tenant scoping for shared-backend deployments.*
- *Cross-tenant snapshot isolation (`SnapshotNotFound` rule).*
- *Round-trip preservation guarantees for raw-text fields.*
- *Re-parse-on-load discipline for the `agent_mode` derivation invariant.*
- *Skill-list pagination / streaming for large agent fleets.*

## Reserved future capabilities

These are not committed in PR 1–3 but are reserved in the namespace so future expansions don't need a breaking Protocol change:

- **`AsyncAgentProfileBackend`** — async variant for HTTP-served deployments. Same shape; `load_profile` / `save_profile` become `async def`.
- **`SubscribeProfileBackend`** — adds `subscribe(agent_id, callback) -> handle` for backends that push profile-change events. `ProfileCapabilities.supports_subscribe` is the flag.
- **`TemplateProfileBackend`** — read-only library of starter personas / canned agent configs. Sets `supports_save=False`.
- **`MigrationProfileBackend`** — adds `migrate(from_version, to_version)` for schema evolution at the backend layer.
- **`save_skill(agent_id, skill_name, body)` Protocol method** — lets database backends store skill bodies natively. Flips `supports_skills=True` for `SQLiteAgentProfileBackend` (and any future SaaS profile backend) once the method lands. Required before SaaS UI editing of skill bodies. Conformance tests for skill content are already gated on `supports_skills`, so adding the method + capability flip will exercise existing test coverage on the new backends.

## Conformance test surface

The conformance suite (PR 3 — parametrized across both reference backends):

- `tests/test_profile_protocol_conformance.py` — **44 tests × 2 backends = 88 parametrized invocations** (with capability-gated skips for skill-content tests on SQLite and filesystem-shape tests on SQLite). Parametrized via a `backend_factory` fixture; third-party backends import the `BACKEND_FACTORIES` list to verify their own conformance. Tests cover: Protocol surface, every load/save edge case, round-trip raw byte preservation, MCP `$VAR` preservation (Decision 1), agent_mode derivation asymmetry (Decision 6), `list_agents` filtering, `exists` correctness, skill listing + body loading (gated on `supports_skills`), skill_name path-traversal refusal (Step 9.1 multi-specialist finding F-A, gated), `from_dict` narrow except (Step 9.1 finding F-B), clone + overrides + skills directory copy (gated), capability parity for snapshot trio, `AgentProfile.to_dict / from_dict` round-trip, list_skills/load_skill_body on missing agent (GAP-11), **7 new snapshot conformance tests** (round-trip, list-empty, chronological ordering, label preservation, restore-unknown, cross-agent isolation, snapshot_id uniqueness — added in #63 PR 3).
- `tests/test_profile_filesystem_backend.py` — 23 filesystem-specific tests: on-disk path mapping (matches `agent.py:_load_config` expectations exactly), hidden-directory exclusion, path-traversal refusal, atomic save (no .tmp leftovers), registry resolution, `get_default_profile_backend` env-var dispatch + credential redaction + long-value truncation, cascade carve-out (load picks up role layer; save writes instance-layer only — Decision 5; cascade floor judges.md does NOT materialize ghost instance shadow — Step 11 adversarial finding P1#1), `from_dict` raises loudly when judges_config is dict-shape without raw text (Step 11 finding P1#2).
- `tests/test_profile_sqlite_backend.py` — **36 SQLite-specific tests** (#63 PR 3): schema creation + version row + idempotent cold-start race + version mismatch detection, WAL journal mode probe, in-memory `RuntimeWarning` + non-durable capability, JSON blob round-trip preserves raw fields, `save_profile` agent_mode normalization (Decision 6), indexed columns probe (`idx_agents_mode`, `idx_snapshots_agent_id`, `idx_snapshots_created_at`), skill semantics (`list_skills=[]`, `load_skill_body` always FileNotFoundError, `supports_skills=False`), snapshot table inserts + cross-agent WHERE-clause isolation + restore cross-agent refusal + empty `snapshot_id` refusal, clone via `save_profile` re-normalizes agent_mode, URL parsing (all 6 edge cases: absolute path, in-memory shorthand, three-slash memory, wrong scheme, netloc rejection, empty path), registry resolution + env-var dispatch (default path, with URL, filesystem default, unknown id, credential redaction), constructor parent-dir creation.

Total: **~150 AgentProfileBackend-arc tests** verifying the Protocol contract across both reference backends. The parametrized conformance suite (88 invocations) is the contract; the filesystem-specific + SQLite-specific suites (59 tests combined) cover backend-shape lessons.

## Related

- spec/20 — `MemoryBackend` (the original Protocol pattern; this spec mirrors its shape).
- spec/21 — `LockBackend` (immediate-sibling template; this spec mirrors its `types.py`/`backend.py`/registry split and operator-surface rationale).
- spec/22 — `LogBackend` (freshest template; this spec follows its DRAFT → lock cadence + Implementer contract structure).
- spec/28 — `JudgeBackend` (third-template; the profile arc adopts the same "lock spec at PR 4" discipline).
- spec/31 — `LLMBackend` (second-template; this spec mirrors its types/backend split).
- CLAUDE.md §1 — "The vault is the source of truth." The profile backend's raw-text-shadow design (Decision 1) is in service of this rule.
- CLAUDE.md §7 — "Markdown config or no config." The profile backend exists to let operators keep markdown-as-config while the framework lifts the bootstrap path to a Protocol.
- Issue [#63](https://github.com/dep0we/atomic-agents-stack/issues/63) — motivation + acceptance criteria.
