"""Canonical types for the ConversationBackend Protocol (spec/47).

ConversationBackend is the twentieth backend Protocol in the atomic-agents
framework (v1.5 wave). It provides per-principal conversation turn persistence
so that agent.call() can inject prior turns into the messages array, enabling
stateful multi-turn exchanges without polluting the system prompt (T14 cache).

This module is a dependency-free leaf: it imports only from stdlib and
.._export_base (which is itself dependency-free). It MUST NOT import from
..agent, .._llm, .._costs, ..logs, or any module that transitively imports
those. This keeps conversation/__init__.py importable without pulling in the
full LLM stack (circular-import safety).

Principal vs caller_identity:
    caller_identity (spec/37) is an unverified HTTP header value passed
    through to the audit trail. Principal is the authorization key for
    conversation ownership. These are SEPARATE data-flow paths:
    - Home-user shape: Principal(identifier='local', derivation_source='local',
      is_verified=True) — trusted by construction, zero config.
    - Org shape: a verified token (JWT/OIDC/mTLS) is decoded at the serve
      boundary and becomes a Principal; caller_identity stays the raw header.
    The serve layer MAY derive a Principal from a verified token. It MUST NOT
    pass caller_identity raw as a Principal.identifier without a verification
    step.

Turn schema:
    Minimal: {role, content, ts, run_id, schema_version}. Rich fields
    (tool_calls, sources, cost) stay in the RunRecord JSONL keyed by run_id.
    This avoids a parallel audit path (CLAUDE.md Principle #5).

See docs/spec/47-conversation-backend.md for the full normative contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .._export_base import ExportableResult


# ──────────────────────────────────────────────────────────────────
# Principal primitive

# Extensible derivation source vocabulary. The Literal is advisory;
# implementations MUST NOT reject unknown strings (forward compat).
DerivationSource = Literal["local", "jwt", "oidc", "mtls"]

# Schema version for on-disk Turn JSON. Increment when the field set
# changes in a backward-incompatible way. Conformance tests assert value==1.
TURN_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Principal:
    """Thin typed authorization key for conversation ownership (spec/47).

    Identifies who is participating in a conversation with an agent. The
    (principal.identifier, agent_name) pair owns a conversation_id — cross-
    principal reads fail-closed; cross-agent reads are not implicit.

    Fields:
        identifier: stable string identifier for this principal.
            MUST be a bare filename component: no OS path separator ('/' on
            POSIX), not empty, not '.' or '..', no NUL/control chars. (A literal
            backslash is an ordinary char on POSIX and is not rejected; see the
            FilesystemConversationBackend validation note.)
            Home-user shape: 'local' (single trusted local principal).
            Org shape: a stable opaque id derived from the verified token
            (e.g. sub claim from JWT, email address). NOT the raw JWT.
        derivation_source: how this principal's identity was established.
            'local' for home-user (trusted by construction, zero config).
            'jwt'/'oidc'/'mtls' for org deployments (verified at serve layer).
            Advisory; implementations MUST NOT gate behavior on this field
            alone (use is_verified for enforcement decisions).
        is_verified: True when the derivation_source performed actual
            cryptographic verification (signature check, cert validation, etc.).
            Home-user shape: True by construction (the caller IS the operator).
            Org shape: True only after the serve layer has validated the token.
            Default False so constructing a Principal from an unverified header
            does not accidentally inherit trust.

    MUST NOT be confused with caller_identity (spec/37 MUST 6), which is an
    explicitly-unverified HTTP header value passed through to the audit trail.
    """

    identifier: str
    derivation_source: str = "local"  # open vocabulary; Literal is advisory
    is_verified: bool = False


# Home-user default: a single trusted local principal with zero config.
# Construction-time trust (is_verified=True) because the home-user IS the
# operator — there is no separate verification step needed.
LOCAL_PRINCIPAL = Principal(
    identifier="local",
    derivation_source="local",
    is_verified=True,
)


# ──────────────────────────────────────────────────────────────────
# Core Protocol types


@dataclass(frozen=True)
class Turn:
    """One conversation turn persisted by a ConversationBackend (spec/47).

    Minimal schema: {role, content, ts, run_id, schema_version}.
    Rich fields (tool_calls, sources, cost_usd) stay in the RunRecord JSONL
    keyed by run_id — no parallel audit path (CLAUDE.md Principle #5).

    Fields:
        role: 'user' or 'assistant'. Matches the LLM API message role.
        content: turn text content (UTF-8). May be empty string for
            assistant turns that only emitted tool calls (no text response).
        ts: ISO-8601 UTC timestamp when this turn was written. Used for
            chronological ordering within a conversation. The filesystem
            backend includes this in the filename for lexicographic ordering.
        run_id: the agent.call() run_id that produced or received this turn.
            Links to the full RunRecord in the JSONL audit trail. Unique per
            call() invocation. NOTE: a single call() writes BOTH a user turn and
            an assistant turn sharing one run_id AND one ts; run_id alone is NOT
            unique within a call(). The `seq` field disambiguates them so the
            assistant turn does not overwrite the user turn's file.
        seq: monotonic per-call sequence index distinguishing turns written in
            the same call() invocation (which share run_id and ts). The user
            turn is seq=0, the assistant turn is seq=1. The filesystem backend
            includes a zero-padded seq in the filename so two same-call turns
            map to distinct files and sort in write order. Default 0 (a
            single-turn write needs no disambiguation).
        schema_version: on-disk schema version. Always TURN_SCHEMA_VERSION
            (currently 1). Conformance tests assert this value; a migration
            that changes fields bumps the constant. Default 1.
    """

    role: Literal["user", "assistant"]
    content: str
    ts: str  # ISO-8601 UTC, e.g. '2026-06-19T14:33:21.987654+00:00'
    run_id: str
    seq: int = 0
    schema_version: int = TURN_SCHEMA_VERSION


# ──────────────────────────────────────────────────────────────────
# Capability dataclass


@dataclass(frozen=True)
class ConversationCapabilities:
    """Per-backend capability declaration for ConversationBackend (spec/47).

    All capability booleans default to False so new fields can be appended
    without breaking existing instantiation sites (backward-compat pattern
    from LogCapabilities / JournalCapabilities).

    Fields:
        backend_id: stable backend identifier (required, no default).
        single_host_only: True when the backend is safe ONLY for single-host
            deployments. FilesystemConversationBackend claims True (filesystem
            atomicity does not extend across hosts).
        supports_canonical_export: True when the backend implements the
            spec/40 Exportable Protocol. FilesystemConversationBackend=True.
        supports_principal_isolation: True when the backend enforces cross-
            principal read/write isolation (fail-closed). All conforming
            backends MUST advertise True (MUST 2). Absence of this capability
            is a conformance failure.
        supports_token_budget_load: True when load_turns() accepts a
            budget_tokens kwarg and truncates oldest-first on overflow.
            FilesystemConversationBackend=True.
    """

    backend_id: str  # required, no default
    single_host_only: bool = False
    supports_canonical_export: bool = False
    supports_principal_isolation: bool = False
    supports_token_budget_load: bool = False


# ──────────────────────────────────────────────────────────────────
# Export result type


@dataclass
class ConversationExport(ExportableResult):
    """Canonical export from a ConversationBackend (spec/40 §"Per-backend export contracts").

    Exports all durable turn files as (relative_path, raw_bytes) tuples.
    The relative path is relative to agent_root (e.g.
    'conversations/local/<conv_id>/<iso_ts>_<run_id>_<NN>_<role>.json').

    Stale .tmp files are excluded (rglob '*.json' skips them).
    In-flight turns (if any) are included — a turn write-back is either
    atomic (committed) or absent (stale .tmp excluded by the glob).

    Fields:
        entries_with_bytes: list of (relative_path_str, raw_bytes) tuples.
        backend_id: stable backend identifier.
        scope: agent root path as a string.
    """

    entries_with_bytes: list[tuple[str, bytes]]
    backend_id: str
    scope: str
