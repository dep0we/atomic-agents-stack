"""ConversationBackend Protocol — the contract every conversation implementation satisfies.

This is the twentieth open Protocol in the protocol-pattern series (spec/47).
It provides per-principal conversation turn persistence so agent.call() can
inject prior turns into the messages array, enabling stateful multi-turn
exchanges without polluting the system prompt (T14 cacheable prefix).

Protocol method surface:

  Three operations ON the Protocol:
    load_turns(principal, conversation_id, budget_tokens)
        — load most-recent turns fitting within token budget (oldest-first eviction)
    write_turn(principal, conversation_id, turn)
        — atomically persist one turn (temp+fsync+rename)
    capabilities()
        — return ConversationCapabilities

  Plus the standard Protocol surface:
    export(query)     — spec/40 canonical export (all durable turn files)
    export_all()      — convenience wrapper (unbounded export)

Ownership model:
    Conversations are owned by the (principal, agent) pair. A conversation_id
    is scoped to a specific (principal.identifier, agent_root) combination.
    Cross-principal reads MUST fail-closed (ConversationAccessDenied or []).
    Cross-agent reads are NOT implicit — agent A cannot read principal P's
    conversation with agent B by default.

Filesystem layout:
    <agent_root>/conversations/<principal.identifier>/<conversation_id>/<ts>_<run_id>_<NN>_<role>.json
    (<NN> is the zero-padded per-call seq — user=00, assistant=01 — so the two
    turns one call() writes, which share run_id AND ts, do not collide.)

Token budget invariant (MUST 8):
    load_turns() MUST load the most-recent turns that fit within budget_tokens.
    The budget is derived from: model_context_limit - system_prompt_tokens
    - max_output_tokens - safety_margin. Oldest-first eviction when the window
    overflows. The DOLLAR guardrail gates whether the call runs at all (already
    checked before turn injection); the TOKEN budget gates how many turns fit.

Import boundary (circular-import safety):
    This module imports ONLY from .types, ..exceptions, and stdlib.
    No imports from ..agent, .._llm, .._costs, ..logs, or any module that
    transitively imports those. This keeps conversation/__init__.py importable
    without loading the LLM stack.

See docs/spec/47-conversation-backend.md for the full normative contract.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from .types import ConversationCapabilities, ConversationExport, Principal, Turn

_logger = logging.getLogger(__name__)


@runtime_checkable
class ConversationBackend(Protocol):
    """Contract every conversation backend implementation must satisfy.

    Implementations MUST NOT subclass this Protocol — it is structural.
    Implementations satisfy it by exposing the methods below with the
    documented behavior. The @runtime_checkable decorator enables
    isinstance(obj, ConversationBackend) to perform a method-presence check.

    Scope: bound at construction. FilesystemConversationBackend(agent_root)
    operates on <agent_root>/conversations/. This is an agent-scoped backend
    matching JournalBackend/IdempotencyBackend, NOT the project-scoped
    QueueBackend.

    The backend is STATELESS at the Protocol level — it holds agent_root only.
    All conversation-turn state is persisted on disk (filesystem backend) or
    in a shared database (future Postgres backend).
    """

    @property
    def backend_id(self) -> str:
        """Stable identifier — e.g. 'filesystem', 'postgres'.

        Used by the registry for lookup and by diagnostic tooling. Treat as a
        backwards-compatibility surface — operator deployments may pin against
        these strings.
        """
        ...

    def load_turns(
        self,
        principal: Principal,
        conversation_id: str,
        budget_tokens: int = 8000,
    ) -> list[Turn]:
        """Load most-recent turns for (principal, conversation_id) within budget.

        Returns turns in CHRONOLOGICAL order (oldest first), so prepending them
        to messages[] produces the correct [oldest_user, oldest_assistant, ...,
        work_item_user] sequence for the LLM.

        The budget_tokens parameter caps the total token count of all returned
        turns (character count / 4 approximation). Oldest-first eviction on
        overflow: if the full conversation history exceeds budget_tokens, the
        oldest turns are dropped first. This preserves the most recent context.

        Returns [] (authoritative empty) when:
        - conversations/ directory is absent (authoritative FRESH — no prior turns)
        - conversation_id subdirectory is absent (no prior turns for this conv)
        - budget_tokens is 0 or negative (caller explicitly requested empty window)

        MUST-NOT-RAISE on absent-directory condition. [] is authoritative, not
        an error.

        Principal isolation (MUST 2):
            MUST refuse to return turns when the requesting principal does not
            match the stored principal (ConversationAccessDenied). Two guards
            defend DIFFERENT classes — they are NOT symmetric:
              - the IDENTITY guard (resolved-basename comparison against
                principal.identifier) is the SOLE load-bearing guard for
                cross-principal isolation. Stripping it makes the cross-principal
                symlink negative control go RED.
              - the PERIMETER guard (path-escape outside conversations/) defends
                a different class and PASSES a sibling symlink bob -> alice, so
                it does NOT defend principal identity on its own.
            See spec/47 §"MUST 2" and FilesystemConversationBackend's module
            docstring for which guard each conformance control pins.

        Path traversal guard (MUST 3):
            MUST validate principal.identifier and conversation_id as bare
            filename components before any I/O. Raises PathTraversalError on
            invalid input (separators, NUL, '.', '..').

        Args:
            principal: the conversation owner. Only returns turns for this
                principal's directory.
            conversation_id: the conversation key. Caller-supplied; treated
                as an opaque string (validated as a bare path component).
            budget_tokens: token budget for loaded context. Turns are loaded
                oldest-first (lexicographic filename sort); the budget eviction
                pass then walks them newest-first, dropping the oldest turns once
                the accumulated token count would exceed this budget, and the kept
                window is returned in chronological order. Default 8000
                (conservative; callers supply a model-derived budget).

        Returns:
            list[Turn] in chronological order (oldest first). May be [] when
            no prior turns exist or all turns were evicted by the budget window.

        Raises:
            PathTraversalError: when principal.identifier or conversation_id
                contains path separators, is empty, '.' or '..'.
            ConversationAccessDenied: when the requesting principal does not
                match the stored conversation owner.
            ConversationBackendError: on unrecoverable I/O failure (distinct
                from absent-directory, which returns []).
        """
        ...

    def write_turn(
        self,
        principal: Principal,
        conversation_id: str,
        turn: Turn,
    ) -> None:
        """Atomically persist one turn (temp+fsync+rename) (spec/47 MUST 4).

        Writes a single Turn as a JSON file under:
            <agent_root>/conversations/<principal.identifier>/<conversation_id>/
                <iso_ts>_<run_id>_<NN>_<role>.json

        Filename is <iso_ts>_<run_id>_<NN>_<role>.json where:
        - iso_ts is turn.ts normalized to UTC, with colons replaced by dashes and
          '+' replaced by 'p' (path-safe ISO-8601)
        - run_id is turn.run_id (shared by the two turns one call() writes)
        - NN is the zero-padded turn.seq (user=00, assistant=01) — what makes the
          two same-call turns distinct (run_id alone is NOT unique within a call)
        - role is the turn role (informational)

        Uses atomic_write (temp+fsync+rename) — crash safety. A crash mid-write
        leaves a stale .tmp file that is excluded from load_turns() by the *.json
        glob. The .tmp file is safe to delete manually; doctor may sweep them.

        Principal isolation (MUST 2):
            Validates the write principal against the directory component BEFORE
            any I/O. A caller may not write to another principal's directory.

        Path traversal guard (MUST 3):
            Validates principal.identifier and conversation_id as bare filename
            components. Raises PathTraversalError on invalid input.

        Concurrency safety:
            An exclusive fcntl.flock on <agent_root>/conversations/<principal>/.conv.lock
            serializes concurrent write_turn() calls for a given principal across
            concurrent call() invocations (e.g. helper_call_parallel dispatching
            multiple agents that share a conversation_id), so two same-call()
            writers cannot interleave on the per-turn file. write_turn() is a pure
            single-file write — there is no read-modify-write cycle.

        Args:
            principal: the conversation owner.
            conversation_id: the conversation key.
            turn: the Turn to persist.

        Raises:
            PathTraversalError: when principal.identifier or conversation_id
                contains path separators, is empty, '.' or '..'.
            ConversationAccessDenied: when the write principal does not match
                the target directory's principal.
            ConversationBackendError: on unrecoverable I/O failure.
        """
        ...

    def export(self, query: Any = None) -> ConversationExport:
        """Export all durable turn files as a canonical ConversationExport (spec/40).

        INCLUDES: all *.json turn files under conversations/. These are durable
        (committed via atomic_write) and represent the full conversation history.

        EXCLUDES: stale .tmp files (from crashed writes, excluded by *.json glob).

        The export may be point-in-time inconsistent if a concurrent write_turn()
        completes between the directory listing and per-file reads. Callers that
        need strict consistency MUST hold the agent LockBackend before calling.

        Args:
            query: unused (reserved for future bounded-export filtering).

        Returns:
            ConversationExport with entries_with_bytes, backend_id, and scope.
        """
        ...

    def export_all(self) -> ConversationExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        ...

    def capabilities(self) -> ConversationCapabilities:
        """Backend capability declaration — see ConversationCapabilities.

        Conformance tests assert claim-vs-behavior parity. Honest capabilities
        let callers fail fast against incompatible backends.

        MUST: backend_id and supports_principal_isolation must be declared.
        A backend that claims supports_principal_isolation=False MUST NOT be
        used in production (the isolation guarantee is load-bearing for security).
        """
        ...
