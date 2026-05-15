"""REVISE state-machine helpers: amend ActionProposal, re-validate.

Implements spec/28 §"Revise". Two cycles consume this module:

1. **Judge-driven REVISE** (in ``agent.py:_run_ensemble``): a judge
   returns ``Judgment(outcome=REVISE, amendment=ProposalAmendment)``;
   the framework calls ``amend_proposal``, ``validate_amended_args``,
   ``enforce_amended_write_paths``, then re-runs the ensemble against
   the amended proposal. Bounded at ``max_revise_iterations=1`` per
   spec/28:276.
2. **Operator-driven REVISE** (in ``escalation.py``'s resolution path):
   operator writes ``### Revised by <op>`` block with embedded YAML
   amendment to a PENDING file; the framework parses
   ``parse_operator_amendment`` and routes through the same amend +
   validate primitives. For ``high_risk``, a fresh ensemble re-judges
   the amended proposal; for non-``high_risk``, schema/policy
   validation alone is sufficient. The non-vs-high-risk gate keys on
   the **recomputed** classification — otherwise an operator-revise
   can upgrade ``reversible_write`` → ``delete_files`` and skip judge
   eyes.

Validation scope is gated by ``judges.md``'s top-level ``validation:``
field, NOT by transitive import availability of ``jsonschema``:

- ``validation: weakened`` (default) — tool registered + args
  dict-shaped + ``arguments_hash`` recomputes. Operators see a
  one-shot per-agent log warning the first time amendment validation
  runs in this mode, pointing at the ``[validation]`` extra.
- ``validation: strict`` (opt-in, PR 5b of #112) — runs
  ``jsonschema.validate(args, registered.input_schema)`` after the
  weakened checks pass. Requires the ``[validation]`` extra installed;
  the parser checks for ``jsonschema`` importability at agent-load
  and raises ``JudgePolicyInvalid`` LOUD when the operator flips
  strict without installing the extra. Exception taxonomy:

  * ``jsonschema.ValidationError`` → ``JudgeAmendedProposalRejected``
    (per-amendment rejection; normal failure_policy flow).
  * ``jsonschema.SchemaError`` / ``RefResolutionError`` →
    ``JudgePolicyInvalid`` (operator authoring bug — the tool's
    own ``input_schema`` is malformed or has broken ``$ref``s).
  * ``ImportError`` / ``AttributeError`` / ``TypeError`` (runtime
    jsonschema API surprise) → ``JudgeAmendedProposalRejected``
    with a descriptive message.

The ``re_judged: bool`` field on the audit record is framework-set,
not operator-supplied: operators express intent via the amendment;
the framework decides whether re-judge fires based on the AMENDED
classification (gate on amended, not original).
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from typing import Any, Literal

from ..exceptions import JudgeAmendedProposalRejected, JudgePolicyInvalid
from .proposal import (
    compute_arguments_hash,
    compute_tool_definition_hash,
    _proposal_id as _generate_proposal_id,
)
from .types import (
    ActionClass,
    ActionProposal,
    Evidence,
    ProposalAmendment,
    Reversibility,
)


_logger = logging.getLogger("atomic_agents.judge._revise")

# Module-level flag so the jsonschema-gap warning fires exactly once
# per process (not per amendment). Keyed by agent name so multi-agent
# processes still warn per-agent. Set initialized lazily.
_jsonschema_warned_agents: set[str] = set()


# ──────────────────────────────────────────────────────────────────
# Amendment application


def amend_proposal(
    *,
    original: ActionProposal,
    amendment: ProposalAmendment,
    tool_registry,
    tool_classifications: dict[str, str],
) -> ActionProposal:
    """Merge a ``ProposalAmendment`` with the original ``ActionProposal``
    and produce a framework-bound amended proposal.

    Per spec/28:272: the framework controls these recomputed fields
    so the judge cannot bypass class policy by editing the
    ``classification`` field — it doesn't have access to it.

    - ``tool_name``: amendment overrides, else original.
    - ``tool_arguments``: amendment overrides, else original.
    - ``target_audience``, ``expected_consequence``, ``reversibility``,
      ``rollback_path``: amendment overrides, else original.
    - ``evidence``: ``original.evidence + amendment.appended_evidence``.
    - ``reason``, ``authorization``: **carried verbatim** from original;
      judge cannot rewrite these (spec/28:267-271).
    - ``classification``: **framework-recomputed** from the new
      ``tool_name`` via tools.md / mcp.md lookup. CLASS NON-DOWNGRADE
      BY EXPLOIT defense.
    - ``tool_definition_hash``: **framework-recomputed** from the new
      tool's input_schema + handler at execution time. May raise
      JudgeAmendedProposalRejected if the new tool_name is not in
      the registry.
    - ``arguments_hash``: **framework-recomputed** from amended args.
    - ``proposal_id``, ``proposal_ts``: **framework-set fresh**.

    Args:
        original: The first-judgment proposal.
        amendment: The judge's amendment payload.
        tool_registry: Used to resolve the (possibly new) tool's
            input_schema + handler so the recomputed
            tool_definition_hash reflects the current registered shape.
        tool_classifications: tools.md / mcp.md mapping of
            tool_name → ActionClass.value string. Used to recompute
            ``classification`` from the (possibly new) tool name.

    Returns:
        A new frozen ``ActionProposal`` with framework-set fields.
    """
    new_tool_name = amendment.tool_name or original.tool_name
    new_args = amendment.tool_arguments if amendment.tool_arguments is not None else original.tool_arguments

    # Recompute tool_definition_hash from the current registered tool
    # (input_schema + handler may have changed since the original
    # proposal). When the new tool is not registered, hash falls back
    # to empty schema + None handler — the validate step below will
    # raise JudgeAmendedProposalRejected.
    registered = tool_registry.get(new_tool_name) if tool_registry is not None else None

    # Resolve classification from the recomputed tool_name. Lookup
    # order mirrors agent.py:_resolve_classification: registered
    # ToolDefinition.classification first (tools.py), then
    # tools.md mapping, then default. Required so operator amendments
    # that swap tool_name to a code-classified high_risk tool actually
    # trigger the high_risk re-judge gate (Codex round-1 P1-4).
    cls_str = None
    classification_source = "default_unknown"
    if registered is not None and registered.classification:
        cls_str = registered.classification
        classification_source = "tools.py"
    if cls_str is None:
        cls_str = tool_classifications.get(new_tool_name)
        if cls_str is not None:
            classification_source = "tools.md"
    if cls_str:
        try:
            new_classification = ActionClass(cls_str)
        except ValueError:
            new_classification = ActionClass.EXTERNAL_SIDE_EFFECT
            classification_source = "default_unknown"
    else:
        new_classification = ActionClass.EXTERNAL_SIDE_EFFECT
        classification_source = "default_unknown"
    input_schema = registered.input_schema if registered else {}
    handler = registered.handler if registered else None
    new_tdef_hash = compute_tool_definition_hash(new_tool_name, input_schema, handler)

    new_args_hash = compute_arguments_hash(new_args)

    new_evidence = list(original.evidence) + list(amendment.appended_evidence or [])

    new_proposal_id = _generate_proposal_id()
    new_proposal_ts = datetime.now(tz=timezone.utc).isoformat()

    return ActionProposal(
        tool_name=new_tool_name,
        tool_arguments=new_args,
        tool_call_id=original.tool_call_id,
        tool_definition_hash=new_tdef_hash,
        arguments_hash=new_args_hash,
        classification=new_classification,
        classification_source=classification_source,
        actor_agent=original.actor_agent,
        actor_run_id=original.actor_run_id,
        proposal_id=new_proposal_id,
        proposal_ts=new_proposal_ts,
        actor_model_id=original.actor_model_id,
        delegate_chain=list(original.delegate_chain),
        loaded_skills=list(original.loaded_skills),
        mcp_server=original.mcp_server,
        side_channel_for_tool_call_id=original.side_channel_for_tool_call_id,
        reason=original.reason,
        evidence=new_evidence,
        authorization=original.authorization,
        expected_consequence=(
            amendment.expected_consequence
            if amendment.expected_consequence is not None
            else original.expected_consequence
        ),
        reversibility=(
            amendment.reversibility
            if amendment.reversibility is not None
            else original.reversibility
        ),
        rollback_path=(
            amendment.rollback_path
            if amendment.rollback_path is not None
            else original.rollback_path
        ),
        target_audience=(
            amendment.target_audience
            if amendment.target_audience is not None
            else original.target_audience
        ),
    )


# ──────────────────────────────────────────────────────────────────
# Validation


def validate_amended_args(
    amended: ActionProposal,
    tool_registry,
    *,
    agent_name: str = "",
    validation_mode: Literal["weakened", "strict"] = "weakened",
) -> None:
    """Validate amended args before the framework executes the bound
    action.

    Always-on weakened checks:

    - ``tool_name`` resolves to a registered handler in
      ``tool_registry``. Unknown tool → ``JudgeAmendedProposalRejected``.
    - ``tool_arguments`` is a dict (not None, not a list, not a
      scalar). Raises on wrong shape.
    - ``arguments_hash`` recomputes successfully (defends against
      args that fail canonical_sha256 — e.g. non-serializable values).

    Then branches on ``validation_mode``:

    - ``weakened`` (default) — emits a one-shot warning per agent
      pointing at the ``[validation]`` extra, then returns.
    - ``strict`` — runs ``jsonschema.validate(args, input_schema)``
      against the registered tool's ``input_schema``. Empty dict
      (``{}``) and ``None`` schemas pass (no constraint). Exception
      taxonomy per the module docstring: ``ValidationError`` raises
      ``JudgeAmendedProposalRejected``; ``SchemaError`` /
      ``RefResolutionError`` raise ``JudgePolicyInvalid`` (operator
      authoring bug); ``ImportError`` / ``AttributeError`` /
      ``TypeError`` raise ``JudgeAmendedProposalRejected`` with a
      descriptive message (runtime jsonschema API failure).
    """
    if tool_registry is None or tool_registry.get(amended.tool_name) is None:
        raise JudgeAmendedProposalRejected(
            f"amended tool_name {amended.tool_name!r} is not registered "
            "in the tool_registry"
        )
    if not isinstance(amended.tool_arguments, dict):
        raise JudgeAmendedProposalRejected(
            f"amended tool_arguments must be a dict; got "
            f"{type(amended.tool_arguments).__name__}"
        )
    # Recompute arguments_hash — raises on non-canonicalizable values
    # (e.g., a dict containing a set, a custom class, etc).
    try:
        recomputed = compute_arguments_hash(amended.tool_arguments)
    except Exception as exc:  # noqa: BLE001
        raise JudgeAmendedProposalRejected(
            f"amended tool_arguments not canonicalizable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if recomputed != amended.arguments_hash:
        # Caller should always pass a freshly-amend_proposal'd
        # ActionProposal where these match — but defend against
        # callers who bypass amend_proposal.
        raise JudgeAmendedProposalRejected(
            f"amended arguments_hash mismatch: recomputed "
            f"{recomputed[:12]}... != stored {amended.arguments_hash[:12]}..."
        )

    if validation_mode == "strict":
        _run_strict_jsonschema_validation(amended, tool_registry)
        return

    _warn_jsonschema_gap_once(agent_name)


def _run_strict_jsonschema_validation(
    amended: ActionProposal,
    tool_registry,
) -> None:
    """Validate amended tool_arguments against the registered tool's
    input_schema using jsonschema.

    Defensive re-import: the parser already probed jsonschema
    importability at agent-load (via
    ``judges_md._check_jsonschema_importable``). Re-importing here
    lets us produce a ``JudgeAmendedProposalRejected`` with a
    descriptive runtime message if jsonschema's API changes shape at
    runtime — that's a per-amendment rejection (normal flow), not
    operator policy invalid.
    """
    registered = tool_registry.get(amended.tool_name)
    # Empty / None schema → no constraint, no validation work.
    #
    # Be precise about what counts as "no schema": ``None`` and ``{}``
    # both mean "the tool doesn't declare a schema." Per /ship Step 11
    # adversarial review (PR 5b), ``not schema`` truthiness would ALSO
    # short-circuit on ``False`` (a legitimate JSON-Schema construct
    # meaning "reject all instances") and ``[]`` (malformed shape that
    # SHOULD trigger ``SchemaError → JudgePolicyInvalid``). Use
    # explicit identity / equality checks so those cases reach
    # ``jsonschema.validate`` and route through the correct exception
    # branch below.
    schema = registered.input_schema if registered is not None else None
    if schema is None or schema == {}:
        return

    try:
        import jsonschema
        from jsonschema import ValidationError, SchemaError
        # jsonschema 4.18+ deprecated ``RefResolutionError`` in favor of
        # ``referencing.exceptions.Unresolvable``; both shapes are
        # in-the-wild. Prefer the modern path; fall back to the
        # legacy class on older installs. ``getattr`` (not
        # ``import``) on the legacy name avoids triggering the
        # DeprecationWarning when both paths are available.
        try:
            from referencing.exceptions import Unresolvable as _RefErr
        except ImportError:
            _RefErr = getattr(jsonschema.exceptions, "RefResolutionError", None)
    except (ImportError, AttributeError) as exc:
        raise JudgeAmendedProposalRejected(
            f"validation: strict configured but jsonschema runtime "
            f"surface unavailable: {type(exc).__name__}: {exc}"
        ) from exc

    schema_authoring_errors: tuple[type[Exception], ...] = (SchemaError,)
    if _RefErr is not None:
        schema_authoring_errors = (SchemaError, _RefErr)

    try:
        jsonschema.validate(amended.tool_arguments, schema)
    except ValidationError as exc:
        path = list(exc.absolute_path) or ["<root>"]
        raise JudgeAmendedProposalRejected(
            f"amended tool_arguments failed jsonschema validation at "
            f"{path}: {exc.message}"
        ) from exc
    except schema_authoring_errors as exc:
        raise JudgePolicyInvalid(
            f"tool {amended.tool_name!r} input_schema is malformed "
            f"(operator authoring bug — fix the schema or remove the "
            f"tool from the registry): {type(exc).__name__}: {exc}"
        ) from exc
    except (TypeError, AttributeError) as exc:
        raise JudgeAmendedProposalRejected(
            f"validation: strict configured but jsonschema.validate "
            f"raised an unexpected runtime error: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _warn_jsonschema_gap_once(agent_name: str) -> None:
    """Emit a one-shot per-agent warning that full JSON-Schema
    validation is not performed.

    Operators reading the framework logs (or the dashboard's warning
    pane) see this on the first amendment validation; thereafter
    silent. CLAUDE.md rule #13: docs match reality — the gap is
    surfaced where operators can see it, not buried in spec/28.
    """
    if agent_name in _jsonschema_warned_agents:
        return
    _jsonschema_warned_agents.add(agent_name)
    _logger.warning(
        "agent %r: REVISE amendment validation is running in "
        "``validation: weakened`` mode (tool registration, dict "
        "shape, args_hash recompute). To enable full JSON-Schema "
        "validation of amended tool_arguments, install "
        "``atomic-agents-stack[validation]`` and set "
        "``validation: strict`` in judges.md.",
        agent_name,
    )


def enforce_amended_write_paths(
    amended: ActionProposal,
    write_paths: list,
    read_only_paths: list,
) -> None:
    """Re-run write-path enforcement on amended tool_arguments.

    Mirrors ``PolicyJudge._check_write_path_violations`` logic but
    operates on the ``ActionProposal`` post-amendment instead of inside
    the rule engine's evaluate flow. Raises
    ``JudgeAmendedProposalRejected`` on violation.

    Spec/28:275 mandates this re-check: an amendment may change
    ``tool_arguments`` such that a previously-clean path now hits a
    read-only directory or escapes the allowed write_paths set.
    """
    # Import here to avoid circular import (rules.py imports types
    # from this package's __init__).
    from .rules import PATH_ARG_KEYS

    if not write_paths and not read_only_paths:
        return  # No paths configured — nothing to enforce.

    args = amended.tool_arguments or {}
    for key, value in args.items():
        if key not in PATH_ARG_KEYS:
            continue
        if not isinstance(value, str):
            continue
        # Same heuristic as PolicyJudge: only validate values that
        # look path-shaped (contain / or \\ or start with ~).
        if "/" not in value and "\\" not in value and not value.startswith("~"):
            continue
        from pathlib import Path

        try:
            resolved = Path(value).expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        # Read-only path check: refuse if amended args land inside
        # any read_only directory.
        for ro in read_only_paths or []:
            try:
                ro_resolved = Path(ro).expanduser().resolve()
                resolved.relative_to(ro_resolved)
                raise JudgeAmendedProposalRejected(
                    f"amended args' {key}={value!r} resolves into "
                    f"read-only path {ro!r}"
                )
            except ValueError:
                continue
            except OSError:
                continue
        # Write-path allow-list: if write_paths configured, amended
        # args must land inside one of them.
        if write_paths:
            allowed = False
            for wp in write_paths:
                try:
                    wp_resolved = Path(wp).expanduser().resolve()
                    resolved.relative_to(wp_resolved)
                    allowed = True
                    break
                except ValueError:
                    continue
                except OSError:
                    continue
            if not allowed:
                raise JudgeAmendedProposalRejected(
                    f"amended args' {key}={value!r} resolves outside "
                    "allowed write_paths"
                )


# ──────────────────────────────────────────────────────────────────
# Operator-revise amendment parser


_AMENDMENT_BLOCK_RE = re.compile(
    r"amendment\s*:\s*\n+[ \t]*```yaml\s*\n(?P<body>.*?)\n[ \t]*```",
    re.DOTALL,
)


def parse_operator_amendment(resolution_block_body: str) -> ProposalAmendment | None:
    """Extract an embedded ``amendment:`` YAML block from an operator's
    ``### Revised by <op>`` resolution block body.

    Operator authors:

        ### Revised by alice
        resolved_at: 2026-05-13T12:00:00Z
        note: stripping attachment per security review
        amendment:
          ```yaml
          judge_note: "operator stripped attachment per security review"
          tool_arguments:
            to: "x@y"
            body: "hi"
          ```

    Returns the parsed ``ProposalAmendment`` or ``None`` if no
    ``amendment:`` block is present. Raises ``JudgeAmendedProposalRejected``
    on malformed YAML.

    Nested-fence handling: the regex matches ``amendment:`` line
    followed by a ```yaml fence, accumulating until the closing ```.
    Operators in markdown editors may inadvertently nest backticks;
    the regex requires the OUTER block be the resolution block in the
    PENDING file (which is markdown) and the INNER block be the YAML
    fence. PR 3c's strict-parser rule keeps this from breaking — if
    the operator's editor adds quad-backticks, parsing fails LOUD.
    """
    if not resolution_block_body:
        return None
    m = _AMENDMENT_BLOCK_RE.search(resolution_block_body)
    if not m:
        return None
    raw_yaml = m.group("body")
    if not raw_yaml.strip():
        return None
    # Operators commonly indent the YAML block beneath the
    # ``amendment:`` key when authoring in a markdown editor (the
    # block is visually nested under its key). Strip common leading
    # whitespace so PyYAML's indent-sensitive parser sees a clean
    # document.
    import textwrap

    raw_yaml = textwrap.dedent(raw_yaml)
    import yaml

    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise JudgeAmendedProposalRejected(
            f"operator amendment YAML failed to parse: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise JudgeAmendedProposalRejected(
            f"operator amendment must be a mapping; got "
            f"{type(data).__name__}"
        )
    return _amendment_from_dict(data)


def _amendment_from_dict(data: dict[str, Any]) -> ProposalAmendment:
    """Coerce a YAML-loaded dict into a ``ProposalAmendment`` instance.

    Spec/28's ProposalAmendment shape. Unknown fields are rejected
    (strict parser). ``judge_note`` is REQUIRED in the dataclass; if
    missing in operator YAML, fall back to a generic note.
    """
    allowed = {
        "judge_note",
        "tool_name",
        "tool_arguments",
        "target_audience",
        "expected_consequence",
        "reversibility",
        "rollback_path",
        "appended_evidence",
    }
    extras = set(data.keys()) - allowed
    if extras:
        raise JudgeAmendedProposalRejected(
            f"operator amendment has unknown fields {sorted(extras)}; "
            f"allowed: {sorted(allowed)}"
        )

    judge_note = data.get("judge_note") or "operator amendment"
    reversibility_raw = data.get("reversibility")
    reversibility = None
    if reversibility_raw is not None:
        try:
            reversibility = Reversibility(reversibility_raw)
        except ValueError as exc:
            raise JudgeAmendedProposalRejected(
                f"operator amendment.reversibility must be one of "
                f"{[r.value for r in Reversibility]}; got "
                f"{reversibility_raw!r}"
            ) from exc

    # Evidence list: each item must be a mapping that constructs an Evidence.
    evidence_raw = data.get("appended_evidence") or []
    if not isinstance(evidence_raw, list):
        raise JudgeAmendedProposalRejected(
            f"operator amendment.appended_evidence must be a list; got "
            f"{type(evidence_raw).__name__}"
        )
    evidence_objs: list[Evidence] = []
    for item in evidence_raw:
        if not isinstance(item, dict):
            raise JudgeAmendedProposalRejected(
                f"operator amendment.appended_evidence items must be "
                f"mappings; got {type(item).__name__}"
            )
        try:
            evidence_objs.append(Evidence(**item))
        except TypeError as exc:
            raise JudgeAmendedProposalRejected(
                f"operator amendment.appended_evidence item is malformed: {exc}"
            ) from exc

    tool_arguments = data.get("tool_arguments")
    if tool_arguments is not None and not isinstance(tool_arguments, dict):
        raise JudgeAmendedProposalRejected(
            f"operator amendment.tool_arguments must be a mapping; got "
            f"{type(tool_arguments).__name__}"
        )

    return ProposalAmendment(
        judge_note=str(judge_note),
        tool_name=data.get("tool_name"),
        tool_arguments=tool_arguments,
        target_audience=data.get("target_audience"),
        expected_consequence=data.get("expected_consequence"),
        reversibility=reversibility,
        rollback_path=data.get("rollback_path"),
        appended_evidence=evidence_objs,
    )
