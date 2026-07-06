"""Typed exception ladder for the manage layer (spec/55 — Tier B implementation note).

Each guard has a distinct exception class so the negative-control tests can assert
a strip-testable failure per guard. The ladder is:

  ManageError (base)
    ManageValidationError     — M4: field unknown / invalid enum / format error
      ManageUnknownFieldError    — unknown field name
      ManageInvalidEnumError     — invalid enum value for a known field
      ManageInvalidDateError     — malformed date for a date field (created_at / updated_at)
      ManageControlCharRefused   — value carries a newline / control char (would corrupt on write)
      ManageNestedPathRefused    — dotted path / bare nested sub-record name (reserved, not in PR1)
      ManageListMutationRefused  — --add / --remove / --set-json flag (reserved, not in PR1)
    ManageGovernanceInvalidError — PRESENT_INVALID governance.md (parse_errors non-empty)

Most validation subclasses are raised during S2 step 1 and propagate to the verb
as non-zero, no-write refusals. ManageListMutationRefused is the exception: it is
constructed (not raised) as a pre-S1 scope refusal in ``run_govern`` — the reserved
list-mutation flags are rejected before the registry resolve. Audit-drop is NOT
modelled as an exception: the
audit path uses ``append_management_audit()``'s ``(ok, warnings)`` tuple return
(a dropped audit write warns and still exits 0 on an applied write, per M8), so
there is no audit-drop exception to catch.

``error_type`` on each subclass is the canonical JSON key used by ``--json``
structured refusal output (spec/55 S3).
"""

from __future__ import annotations


class ManageError(Exception):
    """Base class for all manage-layer errors (spec/55)."""

    error_type: str = "manage_error"


class ManageValidationError(ManageError):
    """M4: input validation failure — refused before any write."""

    error_type: str = "validation_error"


class ManageUnknownFieldError(ManageValidationError):
    """Unknown --set field name; not in the PR1 flat-scalar allowlist."""

    error_type: str = "unknown_field"

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"Unknown governance field: {field!r}")


class ManageInvalidEnumError(ManageValidationError):
    """Invalid enum value for a known field (spec/55 M4)."""

    error_type: str = "invalid_enum"

    def __init__(self, field: str, value: str, allowed: set) -> None:
        self.field = field
        self.value = value
        self.allowed = allowed
        super().__init__(
            f"Invalid value {value!r} for field {field!r}. Allowed: {sorted(allowed)}"
        )


class ManageInvalidDateError(ManageValidationError):
    """Malformed date value for a date field (created_at / updated_at) — M4.

    Date fields MUST be ISO-8601 ``YYYY-MM-DD``. A malformed date is refused
    before write (spec/55 M4 + the conformance outline's 'malformed date' control).
    """

    error_type: str = "invalid_date"

    def __init__(self, field: str, value: str) -> None:
        self.field = field
        self.value = value
        super().__init__(
            f"Invalid date {value!r} for field {field!r}. "
            "Expected an ISO-8601 date (YYYY-MM-DD), e.g. 2026-06-24."
        )


class ManageControlCharRefused(ManageValidationError):
    """Value cannot be emitted to YAML and read back byte-identical — M4.

    Governance scalars are single-line identifiers. A value containing a folding
    or PyYAML-rejected character (newline, carriage return, U+0085 NEL, NUL,
    vertical tab, ...) cannot be emitted losslessly: a single-quoted YAML scalar
    folds an embedded line break into a space on re-read (and PyYAML refuses NUL
    outright), so the persisted value would diverge from BOTH what the operator
    set AND what the ``--json`` ``after`` payload reports. The guard decides
    against PyYAML itself (emit-then-reload round-trip), not a hand-rolled
    control-char blocklist. Rather than silently mangle it, the value is refused
    before any write (the module's refuse-for-a-documented-reason posture).
    """

    error_type: str = "control_char"

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(
            f"Value for field {field!r} contains a newline or control character. "
            "Governance fields are single-line; edit governance.md directly for "
            "multi-line content."
        )


class ManageNestedPathRefused(ManageValidationError):
    """Dotted path or a bare nested sub-record name (reserved, not in PR1).

    Raised for a dotted path (``review.reviewer=...``) or a bare nested
    sub-record name (``review`` / ``risk`` / ``sources`` / ``actions``) used
    without a dot. These are named but unimplemented in PR1; list-mutation
    flags are a separate refusal (``ManageListMutationRefused``). The error
    message directs the operator to edit governance.md directly.
    """

    error_type: str = "nested_path_refused"

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(
            f"Nested / list field {field!r} is not yet settable via CLI; "
            "edit governance.md directly."
        )


class ManageListMutationRefused(ManageValidationError):
    """List-mutation flag (``--add`` / ``--remove`` / ``--set-json``) — reserved, not in PR1.

    The grammar for these flags is pinned NOW (spec/55 CLI-surface grammar) so the
    recognized-vs-unrecognized status of the flag never shifts in a later PR — an
    unimplemented path returns THIS clean structured refusal rather than argparse's
    ``unrecognized arguments`` parser error (which would also bypass the ``--json``
    contract). PR2 changes only the semantics, not whether the flag parses.
    """

    error_type: str = "list_mutation_unsupported"

    def __init__(self, flag: str, value: str) -> None:
        self.flag = flag
        self.value = value
        super().__init__(
            f"List mutation via {flag} ({value!r}) is not yet settable via CLI; "
            "edit governance.md directly."
        )


class ManageGovernanceInvalidError(ManageError):
    """governance.md has parse_errors — write refused (spec/55 M4 / PRESENT_INVALID guard).

    A PRESENT_INVALID governance.md has schema errors that prevent the surgical
    editor from operating safely. The operator must fix the YAML block first.
    """

    error_type: str = "governance_invalid"

    def __init__(self, agent_id: str, parse_errors: tuple[str, ...]) -> None:
        self.agent_id = agent_id
        self.parse_errors = parse_errors
        errors_str = "; ".join(parse_errors)
        super().__init__(
            f"governance.md for {agent_id!r} has validation errors: {errors_str}. "
            "Fix the YAML block before using --set (run --show for details)."
        )
