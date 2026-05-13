"""Custom exceptions for the atomic_agents package."""


class AtomicAgentsError(Exception):
    """Base for all atomic_agents exceptions."""


class SchemaValidationError(AtomicAgentsError):
    """Frontmatter or capture failed validation per spec/03."""


class WritePathViolation(AtomicAgentsError):
    """Attempted write outside the agent's tools.md write paths."""


class AgentLockBusy(AtomicAgentsError):
    """Could not acquire the agent's lock — another process holds it."""


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
