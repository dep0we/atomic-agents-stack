"""Custom exceptions for the atomic_agents package."""


class AtomicAgentsError(Exception):
    """Base for all atomic_agents exceptions."""


class SchemaValidationError(AtomicAgentsError):
    """Frontmatter or capture failed validation per spec/03."""


class WritePathViolation(AtomicAgentsError):
    """Attempted write outside the agent's tools.md write paths."""


class LockBusy(AtomicAgentsError):
    """A LockBackend could not acquire the named lock within the timeout.

    Raised by ``atomic_agents.locks.LockBackend.acquire()`` when the
    deadline elapses without the lock being granted. Backend-agnostic:
    a ``FilesystemLockBackend`` raises it when another process holds the
    advisory ``flock``; a ``RedisLockBackend`` raises it when the
    Redis ``SET NX EX`` call returns nil within the wait window.

    Held lock identity is in the message text for human inspection;
    consumers branching on identity should query
    ``LockBackend.is_held(name)`` (racy by design — see spec/21).
    """


# Backwards-compat alias — was the pre-spec/21 name and is exported
# at the package top level. Existing ``except AgentLockBusy`` code paths
# keep working unchanged because the class identity is preserved.
AgentLockBusy = LockBusy


class LockLost(AtomicAgentsError):
    """A previously-held lease-backed lock expired mid-critical-section.

    Distinct from ``LockBusy`` (couldn't acquire) — ``LockLost`` means
    the caller HAD the lock and lost it because the lease expired before
    a renewal could land. Surfaces from heartbeat threads on
    lease-backed backends (``RedisLockBackend`` etc) and is checked by
    long-running call sites between iterations of their work loops so
    they can abort safely instead of writing under a lock another holder
    now owns.

    Deliberately NOT a subclass of ``LockBusy`` — code paths catching
    ``LockBusy`` for "couldn't start work" semantics should NOT
    accidentally swallow ``LockLost`` which signals "in-flight work
    must abort." Both share ``AtomicAgentsError`` as the common ancestor.

    Filesystem backends with ``supports_lease=False`` never raise this;
    operators using the filesystem default will not see it.
    """


class CostGuardrailBlocked(AtomicAgentsError):
    """Call blocked because the agent's daily/monthly cap was hit."""


class HelperBatchPartialFailure(AtomicAgentsError):
    """Some calls in helper_call_parallel succeeded; some failed.

    Attributes:
        failures: list of (index, exception) tuples
        partial_results: list of results, with exceptions in failed slots
    """

    def __init__(self, failures, partial_results):
        self.failures = failures
        self.partial_results = partial_results
        super().__init__(
            f"helper_call_parallel had {len(failures)} failures out of "
            f"{len(partial_results)} calls"
        )


class NoJudgeAvailable(AtomicAgentsError):
    """No judge model is reachable — check API keys."""


class CaptureParseError(AtomicAgentsError):
    """Could not parse a capture marker from agent response."""


class GoalCorrupted(AtomicAgentsError):
    """goal.md is missing required fields or invalid."""


class GoalConcurrentModification(AtomicAgentsError):
    """Raised by apply_transition() when expected_from_status is set and the
    sub-goal's current on-disk status differs — another writer moved the goal
    between the lock release and re-acquisition (re-dispatch or concurrent
    modification detected). Callers that need to detect this race catch
    GoalConcurrentModification; the coordinator lets it propagate.
    """


class OutcomeCorrupted(AtomicAgentsError):
    """result.json is present but cannot be parsed as a valid OutcomeResult."""


class JournalCorrupted(AtomicAgentsError):
    """A journal entry file is present but cannot be read or decoded as valid UTF-8."""


class NotInRoster(AtomicAgentsError):
    """Target agent not in the coordinator's roster.md."""


class SelfDelegationError(AtomicAgentsError):
    """An agent tried to delegate to itself — one-level delegation only."""


class NestedDelegationRefused(AtomicAgentsError):
    """A delegated agent tried to delegate again — nested delegation is forbidden.

    spec/15 enforces one-level delegation only (matching Anthropic's agent
    behaviour). A coordinator may delegate to specialists, but a specialist
    running under trigger='delegate' must not delegate further.
    """


class DreamInProgress(AtomicAgentsError):
    """A dream run is already in progress for this agent — lock held."""


class DreamNotFound(AtomicAgentsError):
    """No dream with the given ID exists for this agent."""


# ──────────────────────────────────────────────────────────────────
# Custom tools exceptions (spec/17)


class ToolNotRegistered(AtomicAgentsError):
    """Model called a tool name that is not in the ToolRegistry."""


class ToolNameCollision(AtomicAgentsError):
    """Attempted to register a tool name already in the registry without allow_overwrite=True.

    Raised by ToolRegistry.register() when a duplicate name is detected.
    MCP registration uses default (refuse-to-overwrite) so namespace collisions
    surface loudly during development instead of silently winning.
    """


class ToolInputInvalid(AtomicAgentsError):
    """Tool input failed JSON Schema validation (required fields or type mismatch)."""


class ToolHandlerError(AtomicAgentsError):
    """Handler raised an exception; the result was captured in ToolCallResult.error."""


# ──────────────────────────────────────────────────────────────────
# Memory versioning exceptions (spec/02 versioning section)


class MemoryPreconditionFailed(AtomicAgentsError):
    """write_atomic_note expected_content_sha256 precondition did not match.

    Raised when the caller supplied an expected_content_sha256 that doesn't
    match the current on-disk sha256 of the target note (concurrent write
    detected), or when the caller supplied a precondition but the target note
    doesn't exist yet.

    Attributes:
        actual_sha256: the sha256 of the current on-disk content (or None
            when the file doesn't exist).
    """

    def __init__(self, message: str, actual_sha256: str | None = None):
        self.actual_sha256 = actual_sha256
        super().__init__(message)


# ──────────────────────────────────────────────────────────────────
# Skills exceptions (spec/18)


class SkillFileTraversal(AtomicAgentsError):
    """Attempted path traversal (../) in a skill file reference.

    Security parity with the capture path-traversal fix. load_skill_referenced_file
    raises this when a relative_path contains '..' or resolves outside the skill_dir.
    """


class PathTraversalError(AtomicAgentsError):
    """Resolved path escapes the expected root directory.

    Raised by safe_resolve_under() when user/operator-controlled input (roster
    names, CLI filenames, version names) resolves outside the intended root after
    joining with Path / and calling .resolve().

    Attributes:
        child: the raw input value that triggered the violation.
        root: the root directory the resolved path was expected to stay under.
    """

    def __init__(self, message: str, child: str = "", root: str = ""):
        self.child = child
        self.root = root
        super().__init__(message)


# ──────────────────────────────────────────────────────────────────
# MCP exceptions (spec/19)


class MCPServerConnectFailed(AtomicAgentsError):
    """An MCP server failed to connect or initialize.

    Raised (and caught/logged) by MCPClientPool.connect_all(). Does not
    block other servers from connecting — the agent runs with whatever
    servers connected successfully. Also raised at parse time when an env
    var reference in mcp.md cannot be resolved.
    """


class MCPServerNotConfigured(AtomicAgentsError):
    """Operator referenced an MCP server name that is not in mcp.md.

    Raised when code tries to route a call to a server that was not declared
    in the agent's mcp.md configuration.
    """


class MCPToolDispatchFailed(AtomicAgentsError):
    """A runtime failure occurred while routing a tool call to an MCP server.

    Raised by the tool handler when the MCP server returns an error or the
    async call fails. Caught by ToolRegistry.execute() and recorded in
    ToolCallResult.error — does not propagate up to the agent's call() loop.
    """


# ──────────────────────────────────────────────────────────────────
# MemoryBackend exceptions (spec/20)


class MemoryBackendError(AtomicAgentsError):
    """Raised by a MemoryBackend on an unrecoverable I/O or protocol failure.

    NOT raised for domain-level conditions (note not found → None returned;
    orphan-recovery → Case 3 path; CAS precondition mismatch →
    MemoryPreconditionFailed; collision → SchemaValidationError). It signals
    that the backend cannot service the request due to an unrecoverable
    backend-level failure (e.g. a connection failure after reconnect retry, or
    a schema migration failure at connection time).

    Which reference impls raise it, today (#258 PR1):
    * ``PostgresMemoryBackend`` raises this on unrecoverable connection/read
      failure after its reconnect retry — its read paths wrap and re-raise as
      MemoryBackendError.
    * ``FilesystemBackend`` does NOT currently raise it. Its per-note parse
      paths swallow malformed-file errors (return None / skip the note).
      Directory-enumeration reads (``list_notes`` / ``list_pinned`` /
      ``list_by_type``) do NOT wrap ``glob`` / ``scandir``, so an
      unrecoverable dir-level OSError (EACCES / EIO) propagates raw
      (unwrapped) — it is caught by doctor's broad ``except Exception``
      liveness gate but NOT surfaced as MemoryBackendError. Wiring the
      filesystem impl to convert I/O failures to MemoryBackendError is
      future work. So catching MemoryBackendError catches the failure
      surface of the backends that raise it (Postgres today), not the
      filesystem default.

    Framework call sites that adopt fail-closed handling for a swapped backend
    catch MemoryBackendError (the base class) to distinguish backend failure
    from domain conditions, preserving the original type via ``type(exc)(...)``.
    As of #258 PR1 the only in-framework catch site is doctor's memory-backend
    liveness probe (check_memory_backend) — the runtime hot-path catch sites
    (agent recall, cost-style fail-closed gates) are wired in a follow-up
    (#258 PR2+). Subclasses may be added by reference implementations; catching
    MemoryBackendError catches the backend-failure surface of any impl that
    raises it, regardless of subclass. Modeled on LogBackendReadError.
    """


class BackendNotRegistered(AtomicAgentsError):
    """Operator declared a backend that isn't registered.

    Future-facing: only triggered when memory.md config selects a backend
    that was not registered via register_backend().
    """


class VersionNotFound(AtomicAgentsError):
    """Version token resolution failed.

    Raised by resolve_version_token() when the supplied token does not
    correspond to any known version for the named note.
    """


class StagingNotApplied(AtomicAgentsError):
    """Operation on a staging area that has already been applied or discarded.

    Raised when a caller tries to write to or apply a StagedMemory after
    apply_staging() or discard_staging() has already been called.
    """


# LLMBackend exceptions (spec/31 — PR #87 PR-1)


class UnknownModelError(AtomicAgentsError):
    """No registered LLMBackend claims the requested model id.

    Raised by ``find_backend_for_model(model_id)`` when zero backends'
    ``supports_model(model_id)`` returned True. Operators should either
    register a backend that handles the model or pick a model id that
    a registered backend handles.
    """


class AmbiguousBackendError(AtomicAgentsError):
    """Multiple registered LLMBackends claim the same model id.

    Raised by ``find_backend_for_model(model_id)`` when more than one
    backend's ``supports_model(model_id)`` returned True and the caller
    did not specify ``preferred_provider`` to disambiguate. The
    ``candidates`` attribute lists the conflicting ``provider_id``
    values so the operator can pick one.

    Once ``model.md``'s ``provider:`` field is parsed (lands with the
    AnthropicLLMBackend follow-up to PR 1 of issue #87), operators can
    resolve the ambiguity by adding a ``provider: <id>`` line. Until
    then, callers must pass ``preferred_provider`` explicitly.
    """

    def __init__(self, model: str, candidates: list[str]):
        self.model = model
        self.candidates = candidates
        super().__init__(
            f"multiple backends claim model {model!r}: "
            f"{candidates}. Pass `preferred_provider=<id>` to "
            f"find_backend_for_model() to disambiguate (parsing the "
            f"`provider:` field of model.md lands with the "
            f"AnthropicLLMBackend impl — issue #87)."
        )

    def __reduce__(self):
        # Default exception __reduce__ returns (cls, (args[0],)) — the
        # formatted message — which then crashes when pickle.loads calls
        # __init__(message). Restore the full constructor args so this
        # exception can cross process boundaries (multiprocessing pools,
        # concurrent.futures workers).
        return (type(self), (self.model, self.candidates))


# ──────────────────────────────────────────────────────────────────
# JudgeBackend exceptions (spec/28 — issue #112 PR 1 of 4)
#
# Distinct from ``NoJudgeAvailable`` above — that exception belongs to the
# existing eval-framework judge (``atomic_agents.eval``). The exceptions
# below are for the runtime *judge layer* per spec/28: a pre-action
# validation surface that runs between LLM tool_use emission and tool
# handler dispatch. Both judges coexist; they cover different surfaces.


class JudgeError(AtomicAgentsError):
    """Base for all spec/28 judge-layer exceptions.

    Each subclass maps to a configurable default judgment outcome via
    ``judges.md``'s ``failure_policy`` block (default: ``block`` for all,
    i.e. fail-closed). Operators may override per-exception-type per-class.
    """


class JudgeUnavailable(JudgeError):
    """Backend cannot respond (timeout, network, provider outage)."""


class JudgePolicyInvalid(JudgeError):
    """``judges.md`` or ``tools.md`` cannot be parsed, or a project-floor
    relax violation was detected at policy-source load time."""


class JudgeBudgetExhausted(JudgeError):
    """Per-action or per-period judge cost cap was hit."""


class JudgeProposalInvalid(JudgeError):
    """Proposal is missing fields required for its action class — e.g.,
    a side-effectful tool_use arrived without the actor's side-channel
    marker, or ``side_channel_for_tool_call_id`` did not match the
    bound ``tool_call_id``."""


class JudgeAmendedProposalRejected(JudgeError):
    """A ``REVISE`` outcome's amended proposal failed framework
    re-validation (schema, policy, classification recompute, or
    write-path enforcement) before the second judgment cycle."""


class UnknownJudgeBackendError(JudgeError):
    """Operator referenced a judge backend name that is not registered.

    Raised by ``atomic_agents.judge.get_backend(name)`` when the name
    has not been ``register_backend()``-ed. Distinct from
    ``BackendNotRegistered`` which covers the memory-backend registry.
    """


# ──────────────────────────────────────────────────────────────────
# AgentProfileBackend exceptions (spec/24 — issue #63 PR 1 of 4)


class AgentProfileNotFound(AtomicAgentsError):
    """``load_profile(agent_id)`` was called with an id the backend
    does not know about.

    The filesystem reference impl raises this when the agent directory
    is missing or has no ``persona/IDENTITY.md`` sentinel. Database /
    registry backends raise this when the row is absent.

    Distinct from ``BackendNotRegistered`` (operator pinned a backend
    string that nobody registered) — this exception means the BACKEND
    is fine, the AGENT ID is not.
    """


class AgentProfileExists(AtomicAgentsError):
    """``clone(source, target)`` or equivalent refused to overwrite an
    existing agent.

    Profile backends MUST refuse silent overwrites; operators who want
    to replace an existing profile call ``save_profile()`` directly
    (which is documented to overwrite). ``clone`` and other create-
    flavored operations raise this.
    """


# ──────────────────────────────────────────────────────────────────
# IdempotencyBackend exceptions (spec/45 — issue #520 PR2)


class IdempotencyBackendError(AtomicAgentsError):
    """Raised by an IdempotencyBackend on unrecoverable I/O failure.

    NOT raised for duplicate-detection (FRESH/IN_FLIGHT/COMPLETED) — those
    are expressed as DedupDecision value objects. Only raised when the backend
    cannot determine the key's state due to a disk error, permission denial,
    or symlink escape.

    Callers that need to distinguish dedup from backend failure catch this
    exception; callers that want to fail-closed on any error catch Exception.
    This is the base class; ``FilesystemDedupLedger`` raises this directly.
    Mirrored from ``atomic_agents.idempotency.filesystem`` for uniform import
    in ``agent.py``, ``serve/_app.py``, etc. without a deep-module import.
    """


class DedupInFlight(AtomicAgentsError):
    """Raised by agent.call() when the idempotency key is already IN_FLIGHT.

    A peer to ``LockBusy``: signals that a concurrent call is already
    executing under the same idempotency key. The caller should wait and
    retry, or use the Queue DLQ / max-attempts stop valve for deterministic
    failure.

    Attributes:
        prior_run_id: the run_id of the call that currently holds the
            IN_FLIGHT lease, or ``None`` when the lease is unreadable
            (fail-closed — the key is still treated as in-flight). Callers
            that want to correlate the refusal with the in-flight JSONL record
            can use this id.

    The serve layer catches this and returns HTTP 409 Conflict carrying
    ``prior_run_id`` so the HTTP caller can correlate. The queue consumer
    should treat 409 as a signal to back off and retry.
    """

    def __init__(self, message: str, prior_run_id: str | None = None) -> None:
        self.prior_run_id = prior_run_id
        super().__init__(message)


class SnapshotNotFound(AtomicAgentsError):
    """``restore(agent_id, snapshot_id)`` referenced an unknown snapshot.

    Snapshot ids are backend-issued by ``snapshot()``; this exception
    fires when an operator passes a stale id, a typo, or a snapshot id
    from a different agent.
    """


# ──────────────────────────────────────────────────────────────────
# ToolRegistryBackend exceptions (spec/25 — issue #64 PR 1 of 4)


class ToolNotInRegistry(AtomicAgentsError):
    """``ToolRegistryBackend.load_tool(name)`` was called with an unknown name.

    Raised by the discovery-layer backend (filesystem walks
    ``<agent>/tools/`` for descriptors; SQLite queries the catalog
    table). Distinct from ``ToolNotRegistered`` which is raised by
    the in-memory ``ToolRegistry.execute`` when the LLM emits a
    tool_use whose ``name`` isn't in the dispatch registry.

    The two cover different layers — ``ToolNotInRegistry`` is the
    catalog miss; ``ToolNotRegistered`` is the dispatch miss. They
    are NOT interchangeable — an operator catching one and expecting
    to also catch the other will be surprised. Spec/25 keeps the
    distinction explicit so the layering composes cleanly with the
    existing tools.py.
    """


class ToolHandlerImportFailed(AtomicAgentsError):
    """A handler module could not be imported during ``load_tool`` / ``validate``.

    Filesystem backends raise this when ``<agent>/tools/<name>.py``
    is missing, fails to import (top-level exception), or imports
    cleanly but doesn't expose a callable named ``handler``. Wraps
    the underlying import error in the exception message for
    operator triage.

    Surfaced via ``ValidationResult.errors`` from ``validate(name)``
    so operators reviewing a catalog see import failures alongside
    descriptor parse errors. Propagates as an exception from
    ``load_tool(name)`` because the caller expects a callable
    handler back.
    """


class ToolDescriptorInvalid(AtomicAgentsError):
    """A tool descriptor (``<name>.md`` frontmatter) is malformed.

    Filesystem backends raise this for: missing YAML frontmatter
    delimiters, frontmatter YAML parse errors, frontmatter root that
    isn't a dict, ``input_schema`` field that isn't a dict, descriptor
    ``name`` field that doesn't match the file stem.

    Same surface contract as ``ToolHandlerImportFailed`` — surfaced
    via ``ValidationResult.errors`` from ``validate(name)``, raised
    from ``load_tool(name)``. Operators triaging "tool isn't being
    discovered" run ``validate(name)`` first to read the specific
    parse error.
    """


class ToolAlreadyInstalled(AtomicAgentsError):
    """``ToolRegistryBackend.install(source, version)`` collided on tool name.

    Raised by backends declaring ``supports_install=True`` (SQLite #64
    PR 3; future PyPI / git) when a tool with the same name already
    exists in the catalog. Mirrors the ``AgentProfileExists`` shape
    spec/24 established -- install is a safe-create primitive; operators
    wanting overwrite call ``uninstall`` first.

    Spec/25 MUST #7 -- install is atomic at the tool level; concurrent
    install calls with the same name resolve exactly one winner; the
    others raise this exception. Reserved at the exception level in
    PR 1 even though no backend in PR 1 raises it (filesystem doesn't
    support install). The SQLite backend (#64 PR 3) is the first
    implementer.
    """


# ──────────────────────────────────────────────────────────────────
# PersonaBackend exceptions (spec/33 -- issue #62 PR 1 of 4)
#
# PersonaError and its subclasses live here (not in persona/types.py)
# because PersonaOwnershipConflict is raised by profile/filesystem.py
# and profile/sqlite.py (D2a), and PersonaLinkInvalid is raised by the
# persona_link_md.py parser (D-ER-4). Cross-module placement matches
# the convention used by AgentProfileNotFound, ToolNotInRegistry, etc.
# D-PI-1 (pre-impl prep amendment 2026-05-26).


class PersonaError(AtomicAgentsError):
    """Base class for persona subsystem errors.

    Subclasses cover load, save, snapshot, and ownership-conflict
    scenarios. Operators catching AtomicAgentsError catch persona
    failures too; operators catching PersonaError catch only the
    persona subset.
    """


class PersonaNotFound(PersonaError):
    """``PersonaBackend.load_persona(persona_id)`` was called with an id
    the backend does not know about.

    Raised by the filesystem reference impl when the persona directory
    is absent under ``<personas_root>/<persona_id>/``. Database backends
    raise this when the persona row is missing.

    Distinct from ``BackendNotRegistered`` (operator pinned a backend
    string that nobody registered) -- this exception means the BACKEND
    is fine, the PERSONA ID is not.
    """


class PersonaExists(PersonaError):
    """``PersonaBackend.save_persona(persona_id, ...)`` or
    ``PersonaBackend.clone(source_id, target_id)`` refused to overwrite
    an existing persona.

    Persona backends refuse silent overwrites by default. Operators who
    want to replace an existing persona call ``save_persona(...,
    overwrite=True)`` explicitly. ``clone`` and other create-flavored
    operations raise this when the target persona_id already exists.
    """


class PersonaSnapshotNotFound(PersonaError):
    """``PersonaBackend.restore(persona_id, snapshot_id)`` referenced an
    unknown snapshot.

    Snapshot ids are backend-issued by ``PersonaBackend.snapshot()``.
    This exception fires when an operator passes a stale id, a typo, or
    a snapshot id belonging to a different persona. Cross-persona
    snapshot isolation is enforced at the backend level.
    """


class PersonaOwnershipConflict(PersonaError):
    """Both ``<agent>/persona.link.md`` and ``<agent>/persona/IDENTITY.md``
    exist at agent construction (D2a).

    Raised by ``FilesystemAgentProfileBackend.load_profile()`` and
    ``SQLiteAgentProfileBackend.load_profile()`` when both the shared-
    persona reference file and the legacy three-file layout are present
    for the same agent. Operators must choose one layout: remove
    ``persona.link.md`` to keep the legacy layout, or remove the
    ``persona/IDENTITY|SOUL|USER.md`` files to use the shared-persona
    reference.
    """


class PersonaLinkInvalid(PersonaError):
    """The ``persona.link.md`` file is malformed or references an unknown
    persona record.

    Raised by the ``persona_link_md.py`` parser (D-ER-4) when:
    - The YAML code block cannot be parsed (malformed YAML).
    - The ``kind:`` field is missing.
    - The ``kind:`` value is not a supported kind (v1 supports only
      ``shared``; future: ``template``, ``git``, ``vault``).
    - The ``persona_id:`` field is missing.
    - The ``persona_id:`` value fails the charset pattern
      ``[a-zA-Z0-9_.+@-]+``.

    Distinct from ``PersonaNotFound`` -- this exception means the
    REFERENCE FILE is malformed; ``PersonaNotFound`` means the file
    parsed correctly but the referenced persona record does not exist.
    """


class PersonaCorrupted(PersonaError):
    """A persona record exists on disk but its contents are corrupt or
    structurally invalid.

    Raised by ``FilesystemPersonaBackend.load_persona`` when the persona
    directory is present but one of the following holds:
    - ``metadata.json`` contains invalid JSON.
    - ``metadata.json`` is missing a required key (``version``,
      ``created_at``).
    - A body file (``IDENTITY.md``, ``SOUL.md``, ``USER.md``) contains
      non-UTF-8 bytes.
    - The ``schema_version`` field in ``metadata.json`` names a version
      this release of atomic-agents-stack does not support.

    Distinct from ``PersonaNotFound`` -- the persona directory EXISTS but
    its data cannot be interpreted. Operators need to repair or remove the
    corrupt record before the persona is usable.
    """


# ──────────────────────────────────────────────────────────────────
# CorpusBackend exceptions (spec/34 -- issue #65 PR 1 of 4)
#
# CorpusError and its subclasses live here (not in corpus/types.py)
# per pre-impl prep finding M2 (Subagent 1, 2026-05-29): exception
# hierarchy MUST mirror the PersonaError / JudgeError base-class pattern
# with cross-module placement. CorpusBackendNotRegistered is raised by
# get_corpus_backend(); CorpusInvalidName is raised by the filesystem
# reference impl's _validate_corpus_name() at API boundary.


class CorpusError(AtomicAgentsError):
    """Base class for CorpusBackend subsystem errors (spec/34).

    All CorpusBackend reference implementations raise subclasses of this
    exception; operators may ``except CorpusError`` to catch the entire
    corpus failure surface without enumerating individual subtypes.
    Subclasses cover page-not-found, collision, precondition-failed,
    version-not-found, invalid-name, backend-registry, embedding-provider,
    and structural-corruption scenarios.
    """


class CorpusPageNotFound(CorpusError):
    """``CorpusBackend.read_page(name, corpus)`` was called with a name
    that does not exist in the specified corpus.

    Raised by filesystem and SQLite reference impls when no page file or
    SQL row matches ``(name, corpus)``. Also raised by
    ``restore_version(name, corpus, version_ref, policy)`` and
    ``snapshot(name, corpus)`` when the named page has been deleted before
    the versioning operation completes.

    Distinct from ``CorpusVersionNotFound`` -- this exception means the
    current page itself is absent; the version history may still exist.
    """


class CorpusPageExists(CorpusError):
    """``CorpusBackend.write_page()`` Case 4 collision: the page exists,
    its content differs from the proposed write, and no
    ``expected_content_sha256`` was supplied.

    The backend refuses silent overwrites by default. Operators who want
    to update an existing page must supply ``expected_content_sha256``
    matching the current on-disk SHA-256 to opt into the CAS (compare-
    and-swap) overwrite path. Without it, the backend raises this
    exception so concurrent or accidental overwrites surface loudly
    rather than destroying content silently.
    """


class CorpusPreconditionFailed(CorpusError):
    """``CorpusBackend.write_page()`` Case 4 collision: the page exists,
    its content differs, ``expected_content_sha256`` was provided, but the
    supplied hash does not match the current on-disk content hash.

    Mirrors ``MemoryPreconditionFailed`` at the equivalent CAS boundary in
    MemoryBackend (spec/20:318). Indicates a concurrent write landed
    between the caller's read and its write attempt; the caller should
    re-read the page, re-derive the hash, and retry. The ``actual_sha256``
    of the current on-disk content is included in the exception message to
    assist triage.
    """


class CorpusVersionNotFound(CorpusError):
    """``CorpusBackend.read_version(version_ref)`` could not access the
    version body for the supplied ``VersionRef``.

    Raised by the SQLite reference impl when the SQL snapshot row exists
    but the on-disk body file is missing or unreadable under the hybrid
    storage shape (SQL stores metadata; bodies live on disk). Also raised
    by the filesystem reference impl when the ``.versions/`` snapshot file
    has been externally deleted. The SQL row existing without a body is an
    independent failure mode distinct from a page-not-found condition.
    """


class CorpusInvalidName(CorpusError):
    """A ``name`` or ``corpus`` parameter failed charset or path-traversal
    validation at the CorpusBackend API boundary.

    Raised by ``_validate_corpus_name()`` in the filesystem reference impl
    when ``name`` contains path-traversal sequences (``..``, ``/``),
    control characters, a leading dot, or characters outside the allowed
    charset ``[a-zA-Z0-9_.+@-]+`` (per pre-impl prep finding S1 --
    mirrors Persona's ``_validate_persona_id`` pattern verbatim). Also
    raised when ``corpus`` is not one of the allowed ``Literal["wiki",
    "raw"]`` values.
    """


class CorpusBackendNotRegistered(CorpusError):
    """``get_corpus_backend(backend_id)`` was called with an id that has
    not been registered via ``register_corpus_backend(backend_id, cls)``.

    Raised when operator config (``ATOMIC_AGENTS_CORPUS_BACKEND`` env var
    or constructor kwarg) names a backend string that no implementation
    has registered. Distinct from a page-not-found condition -- this
    exception means the REGISTRY does not know the backend; the corpus
    storage itself has not been contacted.
    """


class CorpusEmbeddingProviderUnavailable(CorpusError):
    """The configured embedding provider is not reachable for a backend
    that advertises ``supports_semantic_search=True``.

    Raised by ``CorpusBackend.query()`` when the semantic-search code
    path cannot contact the embedding provider (network failure, auth
    error, missing or unreachable model). Reserved in v1.0 for the
    ``PgvectorCorpusBackend`` implementation shipping in the coordinated
    #258 Postgres-adapter family release alongside ``PgvectorMemoryBackend``;
    filesystem and SQLite reference impls never raise it because both
    advertise ``supports_semantic_search=False``.
    """


class CorpusCorrupted(CorpusError):
    """A corpus page exists on disk or in the SQL store but its contents
    are structurally invalid and cannot be interpreted.

    Raised by ``FilesystemCorpusBackend.read_page()`` when the page file
    is present but contains malformed YAML frontmatter (parse error,
    missing delimiters, non-dict root), non-UTF-8 bytes, or a
    ``schema_version`` value this release does not support. Also raised
    by ``SQLiteCorpusBackend.read_page()`` when the on-disk body file
    exists but cannot be parsed. Distinct from ``CorpusPageNotFound`` --
    the page EXISTS but its data cannot be interpreted without operator
    intervention.
    """


# ──────────────────────────────────────────────────────────────────
# EmbeddingBackend exceptions (spec/46 — issue #200)


class EmbeddingError(AtomicAgentsError):
    """Base class for all EmbeddingBackend failures.

    Raised internally by EmbeddingBackend implementations for structured error
    context and logging. NOT propagated from ``embed()`` or ``embed_batch()``
    to callers -- those methods convert all exceptions to ``None`` return
    values per the MUST-NOT-RAISE invariant (spec/46 MUST 4).

    NOTE (NON-NORMATIVE): this exception hierarchy is for internal logging
    only. ``embed()`` and ``embed_batch()`` MUST return ``None``, not raise
    ``EmbeddingError`` or any subclass, as their public contract.
    """


class EmbeddingProviderUnavailable(EmbeddingError):
    """The embedding provider is temporarily or permanently unreachable.

    Raised internally when the SDK returns an authentication error, rate-limit
    error, or service-unavailable response. Caught inside ``embed()`` and
    converted to ``None`` return with a branch-distinctive WARNING log line.

    Distinct from ``CorpusEmbeddingProviderUnavailable`` -- do NOT reuse that
    class (it is scoped to corpus-query failures, not the embedding Protocol
    itself). This hierarchy lives under ``AtomicAgentsError`` like every other
    backend exception family (JudgeError, PersonaError, CorpusError, etc.).
    """


# ──────────────────────────────────────────────────────────────────
# LogBackend exceptions (spec/22 read-failure posture addendum — issue #497)


class LogBackendReadError(AtomicAgentsError):
    """Raised by ``query()`` / ``tail()`` / ``aggregate()`` on an unrecoverable
    read failure — disk corruption, I/O error, or lost database connection after
    all retry attempts are exhausted.

    **Not** raised for absent or empty backend state (that returns ``[]``).
    The boundary rule (per spec/22 read-failure posture addendum; see that
    section's boundary table for the canonical version):

    * ``log_dir`` does not exist / db is empty → return ``[]``
    * directory-level ``ENOENT`` (``log_dir`` / a month dir vanished AFTER the
      ``.exists()`` check — TOCTOU with retention cleanup) → return ``[]`` /
      skip; this is the absent-state contract, NOT a read failure
    * ``log_dir.iterdir()`` raises a NON-ENOENT ``OSError`` (``PermissionError``,
      ``NotADirectoryError``, ``EIO``) → raise ``LogBackendReadError``
    * per-file ``ENOENT`` inside a directory walk (file vanished between listing
      and ``open()``) → skip (``continue``)
    * per-file non-ENOENT ``OSError`` (``EIO``, ``EACCES``) → raise
      ``LogBackendReadError``
    * SQLite ``DatabaseError`` (the base class — covers ``OperationalError``,
      which is how sqlite3 reports disk I/O errors) at the SELECT ``execute()``
      OR at connection/schema setup on a corrupt ``.db`` file → raise
      ``LogBackendReadError``. SQLite ``RuntimeError`` from ``_ensure_schema``
      propagates uncaught — covers BOTH the schema-version mismatch (config
      error) AND the defensive "row missing after INSERT OR IGNORE — corruption
      suspected" near-unreachable branch; neither is a ``sqlite3.DatabaseError``
      so the narrow wrap does not catch it.
    * psycopg error surviving the one-shot reconnect → raise
      ``LogBackendReadError``. A Postgres ``ValueError`` (could-not-connect) /
      ``RuntimeError`` (schema mismatch) propagates uncaught — config error.

    ``stats()`` is **EXEMPT** — it is a racy diagnostic surface and MUST NOT
    be used for control flow per spec/22's locked ``stats()`` contract.

    Raised by conforming ``LogBackend`` implementations. Importable from both
    ``atomic_agents`` (top-level caller surface) and ``atomic_agents.logs``
    (backend-implementer surface).
    """
