"""FilesystemConversationBackend — directory-tree reference implementation (spec/47).

This is the default backend for single-host deployments. It stores conversation
turn files under <agent_root>/conversations/<principal_id>/<conversation_id>/
as discrete JSON files (one file per turn).

Directory layout:
    <agent_root>/
      conversations/
        <principal_id>/               — one subdir per principal identifier
          .conv.lock                  — per-principal exclusive write lock
          <conversation_id>/                       — one subdir per conversation
            <iso_ts>_<run_id>_<NN>_<role>.json   — one file per turn

Where <iso_ts> is the turn timestamp normalized to UTC with colons replaced
by dashes and '+' replaced by 'p' (path-safe ISO-8601, e.g.
'2026-06-19T14-33-21.987654p00-00'), <run_id> is the parent call()'s run_id,
<NN> is the zero-padded per-call sequence index (turn.seq), and <role> is the
turn role. A single call() writes a user turn (seq=00) and an assistant turn
(seq=01) that share the SAME run_id AND ts — so run_id alone is NOT unique
within a call(); the seq component is what makes the two same-call turns map
to distinct files (and sort in write order: 00 before 01). Across calls,
run_id already differs, so the filename is collision-free at any timestamp
resolution.

Construction is side-effect-free (no filesystem I/O in __init__).

Chronological ordering:
    load_turns() lists and sorts *.json files lexicographically by name.
    ISO-8601 timestamps are lexicographically ordered (Z/+HH:MM suffix means
    fixed-width, sortable). The <iso_ts>_<run_id> prefix guarantees sort order
    matches write order.

Token-budget window (spec/47 MUST 8):
    load_turns() accepts a budget_tokens kwarg. It loads ALL turns first,
    then evicts oldest-first until the accumulated token estimate fits.
    Token count is approximated as (len(turn.role) + len(turn.content)) // 4 + 1
    (character-to-token approximation; sufficient for budget windowing).
    Callers supply a model-derived budget: model_context_limit - system_prompt_tokens
    - max_output_tokens. The DOLLAR guardrail gates whether the call runs
    (already checked before turn injection); this budget gates context fit.

Symlink containment (spec/47 security contract):
    Two-layer guard (mirrors FilesystemDedupLedger + FilesystemJournalBackend):

    Layer 1 — _conversations_dir(): resolves both agent_root AND
    agent_root/'conversations' and checks is_relative_to before trusting
    conversations/ as the containment root. A symlinked conversations/ DIRECTORY
    that points outside agent_root raises PathTraversalError on writes (fail-loud)
    and returns [] on reads (fail-soft, 'absent conversations' semantics).

    Layer 2 — _require_canonical_turn_path(): resolves BOTH the conversations
    root AND the per-entry turn file path, asserts the entry is_relative_to the
    root, and asserts the entry is NOT a symlink. Called at EVERY read/write sink.

Principal isolation (spec/47 MUST 2):
    The trust boundary is "a principal sees ONLY its own principal directory."
    Two guards run in BOTH load_turns() and write_turn(), defending DIFFERENT
    classes — they are NOT symmetric, and only ONE is load-bearing for the
    cross-principal-redirect case (per feedback_containment_reframe_not_whackamole:
    name what each guard actually defends rather than claiming symmetric
    independence the topology does not support):

    (1) safe_resolve_under(principal_dir, conversations_root) — PERIMETER guard.
        Defends path-escape OUTSIDE conversations/ (e.g. principal_dir resolving
        to /etc/). A sibling symlink conversations/bob -> conversations/alice
        PASSES this guard (alice IS under conversations/), so Guard (1) does NOT
        defend cross-principal identity.

    (2) _verify_principal_directory() — IDENTITY guard, the SOLE load-bearing
        guard for cross-principal isolation. Compares the RESOLVED principal
        directory's basename against the requesting principal.identifier. The
        sibling symlink bob -> alice resolves to basename 'alice' != 'bob', so
        ConversationAccessDenied is raised.

    Stripping Guard (2) makes the cross-principal symlink attack SUCCEED — the
    conformance suite's Guard-2-strip negative control (test_must2_*_guard2_*)
    goes RED (asserts the attack is blocked in shipped code; a separate
    documented-vulnerability test demonstrates the leak WITHOUT the guard).
    Guard (1) is defended by its own perimeter-escape test, not the
    cross-principal one.

Principal identifier validation (spec/47 MUST 3):
    principal.identifier, conversation_id, AND turn.run_id (interpolated raw
    into the on-disk turn filename) are validated as bare filename components
    via _validate_conversation_component() before ANY path arithmetic. The check is `value == Path(value).name` (plus empty/'.'/'..'/
    control-char rejection), so it refuses anything the OS treats as a path
    separator. On POSIX that is '/' only — a literal backslash is an ordinary
    filename char and is NOT rejected (it is harmless: it cannot escape the
    principal/conversation directory). A future cross-platform or Postgres
    backend that keys on these components may add explicit backslash rejection.

Concurrency safety:
    An exclusive fcntl.flock on <agent_root>/conversations/<principal>/.conv.lock
    serializes ALL turn writes for a given principal across concurrent call()
    invocations. The lock covers the full write_turn() operation. helper_call_parallel
    dispatches multiple agents that may share a (principal, conversation_id);
    the per-principal lock prevents the TOCTOU race on the per-turn file.

Atomic writes:
    write_turn() uses atomic_write() (temp + fsync + rename) for crash safety.
    A crash mid-write leaves a stale .<filename>.*.tmp file that is excluded
    from load_turns() by the *.json glob (the stale .tmp does not match *.json).
    Stale .tmp files are safe to delete manually; doctor MAY sweep them.

Fail-closed vs fail-open boundary:
    - conversations/ directory absent → [] (authoritative FRESH)
    - conversation_id subdir absent → [] (authoritative FRESH)
    - budget_tokens <= 0 → [] (caller requested empty window)
    - per-turn file absent between listing and open (TOCTOU) → skip (continue)
    - per-turn file present but unreadable (JSON parse error, missing field) →
      log WARNING, skip the corrupted turn (ConversationCorrupted, caught internally)
    - per-turn file read fails with a NON-ENOENT OSError (e.g. EACCES) →
      ConversationBackendError (fail-loud — a real I/O fault is not silently degraded)
    - symlink escape on directory check → PathTraversalError (fail-loud on write,
      [] on read — 'absent conversations' semantics)
    - symlink escape on per-entry check → PathTraversalError (raised at sink)
    - cross-principal access → ConversationAccessDenied

Crash recovery:
    Stale *.tmp files left by crashed atomic_write() are excluded from
    load_turns() (*.json glob does not match *.tmp). The .conv.lock sidecar
    persists but is not matched by *.json. Both are safe to leave on disk.
    Doctor checks MUST NOT treat their presence as corruption.

Import boundary (circular-import safety):
    Imports only from ..exceptions, .._io, .types — no imports from
    ..agent, .._llm, .._costs, ..logs, or any module that imports those.
    This keeps conversation/__init__.py importable without loading the LLM stack.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._io import atomic_write, safe_resolve_under
from ..exceptions import (
    ConversationAccessDenied,
    ConversationBackendError,
    ConversationCorrupted,
    PathTraversalError,
)
from .types import (
    TURN_SCHEMA_VERSION,
    ConversationCapabilities,
    ConversationExport,
    Principal,
    Turn,
)

_logger = logging.getLogger(__name__)

# Approximate chars-to-tokens ratio for token budget estimation.
# len(text) // _CHARS_PER_TOKEN gives a conservative token estimate.
_CHARS_PER_TOKEN = 4

# Maximum allowed length for principal.identifier and conversation_id.
# Advisory — prevents degenerate inputs from creating excessively long paths.
_MAX_COMPONENT_LEN = 512


def _validate_conversation_component(value: str, label: str) -> None:
    """Reject a caller-supplied principal_id or conversation_id that is unsafe as a path.

    Validates that value is a bare filename component — no separators, not empty,
    not '.' or '..', no NUL or control characters. Mirrors _validate_bare_component
    in queue/filesystem.py and _validate_key in idempotency/filesystem.py.

    Args:
        value: the caller-supplied string to validate.
        label: human-readable field name for error messages.

    Raises:
        PathTraversalError: on any invalid component.
    """
    if not value or value in (".", "..") or value != Path(value).name:
        raise PathTraversalError(
            f"{label} must be a bare component (no path separators, not empty, "
            f"not '.' or '..'): {value!r}",
            child=value,
            root=f"<{label} validation>",
        )
    if any(ord(ch) < 32 for ch in value):
        raise PathTraversalError(
            f"{label} must not contain NUL bytes or control characters: {value!r}",
            child=value,
            root=f"<{label} validation>",
        )
    if len(value) > _MAX_COMPONENT_LEN:
        raise PathTraversalError(
            f"{label} exceeds maximum length {_MAX_COMPONENT_LEN}: {len(value)!r}",
            child=value,
            root=f"<{label} validation>",
        )


def _normalize_utc_ts(ts: str) -> str:
    """Normalize an ISO-8601 timestamp to UTC +00:00 form.

    Parses the timestamp, converts to UTC, and returns isoformat() with
    explicit +00:00 suffix. This ensures:
    1. Non-UTC timestamps (e.g. -05:00) are converted to their UTC equivalent.
    2. Z-suffix timestamps ('2026-06-19T14:33:21Z') become '+00:00' form.
    3. All filenames sort lexicographically in chronological order.

    A WARNING is logged if the original ts was not already UTC.

    Args:
        ts: ISO-8601 timestamp string.

    Returns:
        Normalized UTC ISO-8601 string with +00:00 suffix.
    """
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            _logger.warning(
                "Turn.ts %r has no timezone; treating as UTC for sort correctness", ts
            )
            dt = dt.replace(tzinfo=timezone.utc)
        utc_dt = dt.astimezone(timezone.utc)
        normalized = utc_dt.isoformat()
        if normalized != ts and not (
            ts.endswith("Z") and normalized.endswith("+00:00")
        ):
            _logger.warning(
                "Turn.ts %r normalized to UTC %r for correct lexicographic sort order",
                ts,
                normalized,
            )
        return normalized
    except (ValueError, TypeError):
        # Malformed timestamp — return as-is, write_turn will let it through.
        return ts


def _turn_filename(turn: Turn) -> str:
    """Derive a collision-free, path-safe filename for a turn file.

    Format: <path_safe_ts>_<run_id>_<NN>_<role>.json

    Normalizes turn.ts to UTC (+00:00) FIRST, then replaces ':' with '-' and
    '+' with 'p' to make the timestamp path-safe while preserving sort order.
    UTC normalization ensures Z-suffix and +00:00-suffix turns sort identically
    at the same instant ('Z' ASCII 90 would otherwise sort before 'p' ASCII 112,
    corrupting chronological order for mixed-notation turns).

    Collision safety WITHIN a call(): a single call() writes BOTH a user turn
    and an assistant turn sharing one run_id AND one ts. run_id alone is NOT
    unique within a call(), so a <ts>_<run_id> filename would make the assistant
    turn overwrite the user turn (silent loss of every user turn). The <NN> seq
    component (zero-padded turn.seq) disambiguates them: user=00, assistant=01.
    Because the seq is the next-to-last filename component and is zero-padded,
    lexicographic sort keeps same-call turns in write order (00 before 01).
    The trailing <role> is informational (aids manual inspection); the seq is
    what makes the filename unique and sortable.

    Example: '2026-06-19T14-33-21.987654p00-00_RUN_00_user' from
    ts='2026-06-19T14:33:21.987654+00:00', run_id='RUN', seq=0, role='user'.
    """
    normalized_ts = _normalize_utc_ts(turn.ts)
    safe_ts = normalized_ts.replace(":", "-").replace("+", "p")
    return f"{safe_ts}_{turn.run_id}_{turn.seq:02d}_{turn.role}.json"


def _estimate_tokens(turn: Turn) -> int:
    """Rough token estimate for a single turn (chars / 4 approximation)."""
    return (len(turn.role) + len(turn.content)) // _CHARS_PER_TOKEN + 1


class FilesystemConversationBackend:
    """Reference implementation of ConversationBackend using the local filesystem.

    Stores turns as individual JSON files under:
        <agent_root>/conversations/<principal_id>/<conversation_id>/<iso_ts>_<run_id>_<NN>_<role>.json

    Construction is side-effect-free (no filesystem I/O in __init__). The
    conversations/ directory is created lazily by write_turn().

    Stale .{filename}.*.tmp files left by crashed atomic_write() calls are safe
    to delete and are excluded from load_turns() (the *.json glob does not match
    *.tmp files). Doctor may sweep them; load_turns() MUST NOT clean them up
    (that would side-effect a read-only operation).

    Import boundary: only imports from ..exceptions, .._io, .types — safe to
    import without triggering the full agent.py import chain.
    """

    def __init__(self, agent_root: Path) -> None:
        """Construct the backend for agent_root.

        Args:
            agent_root: path to the agent folder (e.g. /agents/caldwell/).
                The conversations/ subdirectory is created lazily on first write.

        No filesystem I/O in __init__ (side-effect-free construction, matching
        FilesystemDedupLedger / FilesystemJournalBackend convention).
        """
        self._agent_root = Path(agent_root)

    @property
    def backend_id(self) -> str:
        return "filesystem"

    # ──────────────────────────────────────────────────────────────
    # Symlink containment guards

    def _conversations_dir(self) -> Path:
        """Return the UNRESOLVED conversations/ dir after a resolved containment check.

        Resolves both agent_root and agent_root/'conversations' PURELY to run the
        is_relative_to containment check (a symlinked conversations/ pointing outside
        agent_root is refused). On success returns the UNRESOLVED
        self._agent_root / 'conversations' path — NOT the resolved path — so that
        caller-visible paths stay in the caller's own path representation.

        Mirrors FilesystemDedupLedger._ledger_root() and
        FilesystemJournalBackend._journal_dir() exactly.

        Returns:
            The UNRESOLVED agent_root/conversations/ path.

        Raises:
            PathTraversalError: when conversations/ resolves outside agent_root
                (symlinked ancestor escape), OR when either path cannot be
                resolved (symlink loop / inaccessible ancestor).
        """
        try:
            agent_root_resolved = self._agent_root.resolve()
            conv_resolved = (self._agent_root / "conversations").resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "conversations/ path could not be resolved "
                "(symlink loop or inaccessible ancestor)",
                child="conversations",
                root=str(self._agent_root),
            ) from exc
        if not conv_resolved.is_relative_to(agent_root_resolved):
            raise PathTraversalError(
                "conversations/ resolves outside agent_root (symlinked ancestor refused)",
                child="conversations",
                root=str(agent_root_resolved),
            )
        return self._agent_root / "conversations"

    @staticmethod
    def _require_canonical_turn_path(
        turn_file_path: Path,
        conversations_root: Path,
    ) -> None:
        """Single consolidated containment invariant for all turn read/write sinks.

        Subsumes: (a) regular-file invariant (no symlink leaf), (b) resolved path
        is strictly under conversations_root.resolve(), (c) symlinked-parent
        rejection via canonical equality.

        Call at EVERY write/read sink BEFORE touching the path. Write operations
        raise on violation. Read operations return [] on violation (absent semantics).

        Mirrors FilesystemDedupLedger._require_canonical_ledger_path() exactly.

        Raises:
            PathTraversalError: on any containment violation.
        """
        try:
            cr = conversations_root.resolve()
            fp = turn_file_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "turn path could not be resolved",
                child=str(turn_file_path),
                root=str(conversations_root),
            ) from exc
        if not fp.is_relative_to(cr):
            raise PathTraversalError(
                "turn path escapes conversations/ root",
                child=str(turn_file_path),
                root=str(conversations_root),
            )
        if turn_file_path.is_symlink():
            raise PathTraversalError(
                "turn file is a symlink",
                child=str(turn_file_path),
                root=str(conversations_root),
            )

    def _verify_principal_directory(
        self,
        principal_dir: Path,
        principal: Principal,
        conversations_root: Path,
    ) -> None:
        """Guard (2): verify RESOLVED principal directory name matches the requesting principal.

        This is the CROSS-PRINCIPAL ISOLATION guard (independent from path traversal).
        It uses the RESOLVED path's basename, not the unresolved path's basename.
        This makes it genuinely independent of Guard (1) (safe_resolve_under):

        Symlink attack: conv_root/bob -> conv_root/alice
          - principal_dir.name == 'bob' (unresolved — matches identifier — PASSES Guard (1))
          - principal_dir.resolve().name == 'alice' (resolved — differs from 'bob' — FAILS here)
          => ConversationAccessDenied raised (correct, cross-principal access blocked)

        Legitimate access: conv_root/alice/ is a real directory
          - principal_dir.resolve().name == 'alice' == principal.identifier => OK

        This is the SOLE load-bearing guard for cross-principal isolation.
        Stripping it makes the symlink cross-principal attack succeed (the
        conformance Guard-2-strip negative control goes RED). Guard (1)
        (safe_resolve_under) is a PERIMETER guard for path-escape outside
        conversations/ and does NOT defend principal identity for a sibling
        symlink — see the module docstring "Principal isolation" section.

        Args:
            principal_dir: the unresolved principal subdir path.
            principal: the requesting Principal.
            conversations_root: the unresolved conversations/ path (for context).

        Raises:
            ConversationAccessDenied: when the RESOLVED directory's basename
                does not match principal.identifier (catches symlink redirections).
            PathTraversalError: when the resolved path cannot be determined
                (symlink loop or inaccessible ancestor).
        """
        expected_name = principal.identifier
        try:
            resolved_name = principal_dir.resolve().name
        except (OSError, RuntimeError) as exc:
            raise PathTraversalError(
                "principal directory path could not be resolved "
                "(symlink loop or inaccessible ancestor)",
                child=str(principal_dir),
                root=str(conversations_root),
            ) from exc
        if resolved_name != expected_name:
            raise ConversationAccessDenied(
                f"Principal identifier mismatch: expected {expected_name!r}, "
                f"got {resolved_name!r} from resolved path. Cross-principal access denied."
            )

        # Robust cross-principal identity for case-insensitive / unicode-
        # normalizing filesystems (macOS APFS, Windows NTFS). There the OS
        # collapses 'Alice'/'alice' and the NFC/NFD forms of one name to a
        # SINGLE on-disk directory, and Path.resolve() returns the CALLER's
        # spelling rather than the on-disk name — so the resolve().name check
        # above is bypassable on those filesystems (verified: identifier='alice'
        # read+wrote the directory created by 'Alice'). Defend by INODE identity:
        # if conversations_root/<identifier> resolves to an existing entry whose
        # REAL on-disk name (the entry sharing that inode) is not byte-equal to
        # identifier, the caller is aliasing another principal's storage — deny.
        # A byte-exact entry, or no existing entry (a fresh principal, including
        # every case on a case-sensitive filesystem where no collision exists),
        # passes. Cost is one stat + one scandir of conversations/ per call,
        # O(#principals); negligible at home scale.
        ondisk_name = self._ondisk_principal_name(conversations_root, expected_name)
        if ondisk_name is not None and ondisk_name != expected_name:
            raise ConversationAccessDenied(
                f"Principal identifier {expected_name!r} aliases on-disk "
                f"directory {ondisk_name!r} (case-insensitive or unicode-"
                f"normalizing filesystem collision). Cross-principal access denied."
            )

    def _ondisk_principal_name(
        self, conversations_root: Path, identifier: str
    ) -> str | None:
        """Return the REAL on-disk directory-entry name that the OS resolves
        ``conversations_root/<identifier>`` to, or None if no such directory
        exists yet (a fresh principal — including every case on a case-sensitive
        filesystem, where no collision exists).

        On a case-insensitive / unicode-normalizing filesystem the returned name
        may differ in BYTES from ``identifier`` ('Alice' for 'alice', NFC for an
        NFD request) — that difference is exactly the cross-principal aliasing
        the Guard (2) caller refuses. Found by inode (st_ino, st_dev) identity,
        because Path.resolve() returns the caller's spelling, not the on-disk
        name. Extracted as a seam so the conformance negative control can strip
        precisely this check without disturbing Path.glob (which also uses
        os.scandir).

        Raises:
            PathTraversalError: if conversations/ cannot be scanned.
        """
        try:
            target_stat = (conversations_root / identifier).stat()
        except (OSError, ValueError):
            return None
        target_id = (target_stat.st_ino, target_stat.st_dev)
        try:
            with os.scandir(conversations_root) as it:
                for entry in it:
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if (est.st_ino, est.st_dev) == target_id:
                        return entry.name
        except OSError as exc:
            raise PathTraversalError(
                "conversations/ could not be scanned for principal-identity "
                "verification",
                child=str(conversations_root / identifier),
                root=str(conversations_root),
            ) from exc
        return None

    # ──────────────────────────────────────────────────────────────
    # Public Protocol methods

    def load_turns(
        self,
        principal: Principal,
        conversation_id: str,
        budget_tokens: int = 8000,
    ) -> list[Turn]:
        """Load most-recent turns for (principal, conversation_id) within token budget.

        Returns turns in chronological order (oldest first). Implements oldest-first
        eviction when the accumulated token estimate exceeds budget_tokens.

        Absent directory → [] (authoritative FRESH, not an error).
        Corrupted turn files → skip + WARNING (ConversationCorrupted caught internally).
        budget_tokens <= 0 → [] (caller requested empty window).
        """
        if budget_tokens <= 0:
            return []

        # Guard (0): validate inputs as bare components before any path arithmetic.
        _validate_conversation_component(principal.identifier, "principal.identifier")
        _validate_conversation_component(conversation_id, "conversation_id")

        # Layer 1: conversations/ directory containment.
        try:
            conv_root = self._conversations_dir()
        except PathTraversalError:
            # Symlinked conversations/ escapes agent_root — treat as absent.
            return []

        # Construct the conversation directory.
        principal_dir = conv_root / principal.identifier
        conv_dir = principal_dir / conversation_id

        # Guard (1): path traversal — safe_resolve_under ensures principal_dir
        # stays inside conv_root (belt-and-suspenders after bare-component check).
        try:
            safe_resolve_under(principal_dir, conv_root)
        except PathTraversalError:
            return []

        # Guard (2): cross-principal isolation — resolved-path check. This is
        # the SOLE load-bearing guard for cross-principal identity (Guard (1)
        # above is a perimeter-escape guard and PASSES a sibling symlink
        # bob -> alice). Compare the RESOLVED principal directory's name against
        # principal.identifier. A symlink conv_root/bob -> conv_root/alice
        # produces principal_dir.resolve().name == 'alice', which differs
        # from 'bob', so ConversationAccessDenied is raised. For a real
        # directory, resolved.name == unresolved.name, so legitimate calls
        # are unaffected.
        try:
            self._verify_principal_directory(principal_dir, principal, conv_root)
        except ConversationAccessDenied:
            raise  # propagate — caller maps to [] or WARNING

        # Guard (3): conv_dir containment. conversation_id is a bare component,
        # but conv_dir ITSELF can be a pre-staged SYMLINK
        # (conversations/alice/link -> ../bob/secret) that resolves OUT of
        # principal_dir into another principal's subtree. Without this, the
        # glob below would enumerate the other principal's turn files and the
        # conv_root-scoped per-entry guard would pass them (still under
        # conversations/) — a cross-principal READ leak (verified). Resolve
        # conv_dir and require it strictly under principal_dir, mirroring
        # write_turn()'s pre-mkdir guard. Read semantics: containment violation
        # is "absent" → [].
        try:
            safe_resolve_under(conv_dir, principal_dir)
        except PathTraversalError:
            return []

        # Absent conversation directory → authoritative FRESH.
        if not conv_dir.exists():
            return []

        # Collect all turn files, sorted lexicographically (ISO-8601 prefix → chronological).
        try:
            turn_files = sorted(conv_dir.glob("*.json"))
        except OSError as exc:
            raise ConversationBackendError(
                f"Failed to list turns in {conv_dir}: {exc}"
            ) from exc

        if not turn_files:
            return []

        # Load turns (newest-first for eviction pass, then reverse for chronological output).
        raw_turns: list[Turn] = []
        for turn_file in turn_files:
            # Outer try catches ConversationCorrupted raised by the inner parse
            # block, logs the branch-distinctive WARNING, and continues.
            # Conformance tests can assert: (a) the ConversationCorrupted type,
            # (b) the branch-distinctive "Skipping corrupted" log line, and
            # (c) strip the ConversationCorrupted raise → test goes RED.
            try:
                # Layer 2: per-entry containment guard. conv_root scope is
                # sufficient here: the conv_dir-symlink escape is already refused
                # by Guard 3 above, and an individual symlinked turn FILE is
                # refused by this guard's is_symlink() leaf check regardless of
                # the root scope.
                try:
                    self._require_canonical_turn_path(turn_file, conv_root)
                except PathTraversalError as exc:
                    _logger.warning(
                        "Skipping turn file with containment violation: %s — %s",
                        turn_file,
                        exc,
                    )
                    continue
                try:
                    data = json.loads(turn_file.read_text(encoding="utf-8"))
                    turn = Turn(
                        role=data["role"],
                        content=data["content"],
                        ts=data["ts"],
                        run_id=data["run_id"],
                        # seq defaults to 0 for forward-compat with any turn file
                        # written before the seq field existed (single-turn files).
                        seq=data.get("seq", 0),
                        schema_version=data.get("schema_version", TURN_SCHEMA_VERSION),
                    )
                    # spec/47: a JSON-parseable turn file may still carry an
                    # invalid schema — a bad role, non-string content, or a
                    # negative/non-int seq — from a corrupt write or a misbehaving
                    # custom backend. role/content are injected verbatim into the
                    # provider messages array by agent.call(); an out-of-contract
                    # role would trigger a provider 400 or inject an unexpected
                    # role. The Literal["user","assistant"] hint is not enforced
                    # at runtime, so validate here and treat a violation as
                    # corruption: skip + WARN (same path as a JSON error). bool is
                    # an int subclass, so reject it explicitly for seq.
                    if (
                        turn.role not in ("user", "assistant")
                        or not isinstance(turn.content, str)
                        or isinstance(turn.seq, bool)
                        or not isinstance(turn.seq, int)
                        or turn.seq < 0
                    ):
                        raise ConversationCorrupted(
                            f"Turn file {turn_file} has an invalid schema "
                            f"(role={turn.role!r}, seq={turn.seq!r})"
                        )
                    raw_turns.append(turn)
                except (KeyError, json.JSONDecodeError, TypeError) as exc:
                    # Raise ConversationCorrupted (the typed exception class) so the
                    # outer except here logs the branch-distinctive message and tests
                    # can catch and assert the type independently (not a false-green
                    # broad-except that swallows both corrupted-turn and real I/O errors).
                    raise ConversationCorrupted(
                        f"Corrupted turn file {turn_file}: {exc}"
                    ) from exc
                except OSError as exc:
                    # TOCTOU: file vanished between glob and open — skip.
                    if hasattr(exc, "errno") and exc.errno == 2:  # ENOENT
                        continue
                    raise ConversationBackendError(
                        f"Failed to read turn file {turn_file}: {exc}"
                    ) from exc
            except ConversationCorrupted as exc:
                # Branch-distinctive WARNING for the corrupted-turn path.
                # Tests assert this log message + exception type; the broad
                # OSError/ConversationBackendError path produces a different message.
                _logger.warning(
                    "Skipping corrupted turn file %s: %s",
                    turn_file,
                    exc,
                )
                continue

        if not raw_turns:
            return []

        # Token-budget eviction: accumulate tokens newest-first, evict oldest.
        # O(n) implementation: iterate newest-first appending to a list (O(1)
        # per append), then reverse at the end (O(n) single pass). The previous
        # kept.insert(0, turn) was O(n) per insert = O(n²) total for n turns.
        #
        # One-turn floor: when the single newest turn alone exceeds budget_tokens,
        # we return [] — the caller (agent.call()) uses a conservative budget so
        # this is unlikely in practice. Documented in spec/47 MUST 8.
        accumulated = 0
        kept_reversed: list[Turn] = []
        for turn in reversed(raw_turns):
            est = _estimate_tokens(turn)
            if accumulated + est > budget_tokens:
                # This turn and all older turns exceed budget — stop.
                break
            accumulated += est
            kept_reversed.append(turn)

        return list(reversed(kept_reversed))

    def write_turn(
        self,
        principal: Principal,
        conversation_id: str,
        turn: Turn,
    ) -> None:
        """Atomically persist one turn (temp+fsync+rename).

        Acquires a per-principal exclusive flock before writing to serialize
        concurrent call() invocations (helper_call_parallel race prevention).

        Raises:
            PathTraversalError: on invalid principal.identifier or conversation_id.
            ConversationAccessDenied: on cross-principal write attempt.
            ConversationBackendError: on I/O failure.
        """
        # Guard (0): validate inputs as bare components before any path arithmetic.
        _validate_conversation_component(principal.identifier, "principal.identifier")
        _validate_conversation_component(conversation_id, "conversation_id")
        # turn.run_id is interpolated raw into the on-disk filename
        # (_turn_filename) and is caller-influenced for custom callers invoking
        # write_turn() directly. Validate it as a bare component too — the
        # Layer-2 resolved-path guard (_require_canonical_turn_path below) would
        # also catch an escape, but defense-in-depth keeps parity with the
        # principal/conversation_id guards rather than relying on Layer 2 alone.
        _validate_conversation_component(turn.run_id, "turn.run_id")

        # Layer 1: conversations/ directory containment.
        conv_root = self._conversations_dir()

        # Construct the target path.
        principal_dir = conv_root / principal.identifier
        conv_dir = principal_dir / conversation_id
        filename = _turn_filename(turn)
        turn_file = conv_dir / filename

        # Guard (1): path traversal on principal directory.
        safe_resolve_under(principal_dir, conv_root)

        # Guard (2): cross-principal isolation (independent of path traversal).
        self._verify_principal_directory(principal_dir, principal, conv_root)

        # Acquire per-principal lock (covers the full write operation).
        lock_file = principal_dir / ".conv.lock"

        # Ensure the principal directory exists (needed for the lock file).
        try:
            principal_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConversationBackendError(
                f"Failed to create principal directory {principal_dir}: {exc}"
            ) from exc

        try:
            lock_fd = open(lock_file, "w")  # noqa: WPS515 — write mode for flock anchor
        except OSError as exc:
            raise ConversationBackendError(
                f"Failed to open per-principal lock file {lock_file}: {exc}"
            ) from exc

        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                # Verify conv_dir stays inside principal_dir BEFORE mkdir.
                # A pre-staged symlink conv_dir -> /etc/ would pass mkdir
                # (POSIX allows mkdir through a symlinked component) and
                # create files at the symlink target BEFORE the per-entry
                # guard fires. Check containment first.
                safe_resolve_under(conv_dir, principal_dir)

                # Ensure the conversation directory exists INSIDE the flock.
                # exist_ok=True is safe under concurrent callers; an OSError here
                # is caught by the `except OSError` below and converted to
                # ConversationBackendError while the lock is still held (released
                # in the outer finally).
                conv_dir.mkdir(parents=True, exist_ok=True)

                # Layer 2: per-entry containment guard (BEFORE the write).
                # Containment is checked against conv_dir (the actual target
                # directory), NOT conv_root. turn.ts and turn.role are
                # interpolated raw into the filename by _turn_filename(); a
                # custom caller passing turn.ts='../../etc/evil' or
                # turn.role='../sibling' would, under a conv_root-scoped check,
                # land the file in a SIBLING principal/conversation subtree
                # (still is_relative_to(conv_root)) and poison another
                # principal's history — bypassing MUST 2 isolation. Scoping the
                # single canonical-path invariant to conv_dir catches ts AND
                # role AND any future filename-component injection in one place
                # (per feedback_containment_reframe_not_whackamole: one
                # canonical-path invariant, not N per-field checks). The
                # run_id bare-component check above remains as defense-in-depth.
                self._require_canonical_turn_path(turn_file, conv_dir)

                # Serialize the turn as JSON. Normalize ts to UTC in the BODY too
                # (not just the filename) so the on-disk ts and the filename-derived
                # ts are identical — any reload reproduces the same filename, and the
                # spec/40 export round-trip stays coherent (P2: ts round-trip).
                data: dict[str, Any] = {
                    "role": turn.role,
                    "content": turn.content,
                    "ts": _normalize_utc_ts(turn.ts),
                    "run_id": turn.run_id,
                    "seq": turn.seq,
                    "schema_version": turn.schema_version,
                }
                content = json.dumps(data, ensure_ascii=False)

                # Atomic write (temp + fsync + rename).
                atomic_write(turn_file, content)

            except (PathTraversalError, ConversationAccessDenied):
                raise  # propagate typed errors
            except OSError as exc:
                raise ConversationBackendError(
                    f"Failed to write turn {turn_file}: {exc}"
                ) from exc
        finally:
            # Separate try/finally blocks so lock_fd.close() always runs
            # even when flock(LOCK_UN) raises (rare: NFS / unexpected errors).
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                _logger.warning("Failed to unlock per-principal flock %s", lock_file)
            finally:
                lock_fd.close()

    def export(self, query: Any = None) -> ConversationExport:
        """Export all durable turn files as a canonical ConversationExport.

        Enumerates *.json files under conversations/ (excludes stale *.tmp).
        Returns (relative_path_str, raw_bytes) tuples relative to agent_root.

        Principal isolation under export: rather than a single conv_root.rglob()
        (which would, on Python 3.13+ where rglob follows directory symlinks by
        default, traverse a cross-principal symlink conversations/bob ->
        conversations/alice and emit alice's turns a SECOND time under a
        conversations/bob/... relative path — aliasing one principal's bytes
        into another's exported namespace and double-counting in the spec/40
        canonical export), export iterates principal directories EXPLICITLY and
        runs the same IDENTITY guard (_verify_principal_directory) the read path
        uses before enumerating each subtree. A redirecting principal symlink is
        skipped, so export() enforces the SAME cross-principal isolation
        invariant advertised via supports_principal_isolation=True. The
        conversation-level enumeration below stays a per-principal rglob (no
        principal dimension remains inside it to alias across).

        NOTE: this is NOT a verbatim mirror of FilesystemDedupLedger /
        FilesystemJournalBackend export — those backends have no per-principal
        subdirectory dimension, so their flat rglob cannot alias across
        principals. ConversationBackend is the first principal-scoped filesystem
        backend, so its export carries this extra per-principal identity check.
        """
        entries: list[tuple[str, bytes]] = []
        try:
            conv_root = self._conversations_dir()
        except PathTraversalError:
            # Symlinked conversations/ — treat as empty.
            return ConversationExport(
                entries_with_bytes=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        if not conv_root.exists():
            return ConversationExport(
                entries_with_bytes=[],
                backend_id=self.backend_id,
                scope=str(self._agent_root),
            )

        try:
            principal_dirs = sorted(p for p in conv_root.iterdir() if p.is_dir())
        except OSError as exc:
            raise ConversationBackendError(
                f"Failed to enumerate conversations/ for export: {exc}"
            ) from exc

        for principal_dir in principal_dirs:
            # IDENTITY guard: skip a principal subdir whose RESOLVED basename
            # does not match its on-disk name (a redirecting symlink bob ->
            # alice resolves to basename 'alice' != 'bob' and is skipped). This
            # is the same guard the read path runs, applied per principal so
            # export cannot alias one principal's turns into another's namespace.
            principal = Principal(
                identifier=principal_dir.name, derivation_source="local"
            )
            try:
                self._verify_principal_directory(principal_dir, principal, conv_root)
            except (ConversationAccessDenied, PathTraversalError) as exc:
                _logger.warning(
                    "Skipping cross-principal / unresolvable directory during "
                    "export: %s — %s",
                    principal_dir,
                    exc,
                )
                continue

            try:
                turn_files = sorted(principal_dir.rglob("*.json"))
            except OSError as exc:
                _logger.warning(
                    "Skipping principal subtree during export: %s — %s",
                    principal_dir,
                    exc,
                )
                continue

            for turn_file in turn_files:
                try:
                    # Scope per-entry containment to THIS principal_dir, not
                    # conv_root. On Python 3.13+ (where rglob follows directory
                    # symlinks — see method docstring) a nested symlink
                    # conversations/alice/stolen -> ../bob would otherwise
                    # enumerate bob's files under alice's export and the
                    # conv_root-scoped guard would pass them (still under
                    # conversations/). principal_dir scoping refuses any file
                    # resolving outside the principal it is attributed to. Latent
                    # on 3.12 (rglob does not follow the symlink there); defended
                    # ahead of the 3.13 runtime shift.
                    self._require_canonical_turn_path(turn_file, principal_dir)
                    rel = str(turn_file.relative_to(self._agent_root))
                    entries.append((rel, turn_file.read_bytes()))
                except (PathTraversalError, OSError) as exc:
                    _logger.warning(
                        "Skipping turn file during export: %s — %s", turn_file, exc
                    )
                    continue

        return ConversationExport(
            entries_with_bytes=sorted(entries),
            backend_id=self.backend_id,
            scope=str(self._agent_root),
        )

    def export_all(self) -> ConversationExport:
        """Convenience wrapper — unbounded export (equivalent to export(None))."""
        return self.export(None)

    def capabilities(self) -> ConversationCapabilities:
        return ConversationCapabilities(
            backend_id=self.backend_id,
            single_host_only=True,
            supports_canonical_export=True,
            supports_principal_isolation=True,
            supports_token_budget_load=True,
        )
