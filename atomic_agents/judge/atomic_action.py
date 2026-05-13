"""Side-channel marker tool for the JudgeBackend Protocol (spec/28).

The judge inspects an `ActionProposal` (spec/28:99-130). The proposal's
*framework-introspected* fields (`tool_name`, `tool_arguments`,
`tool_call_id`, hashes, classification, delegate chain, loaded skills)
come straight from the runtime — the actor cannot forge them. The
*actor-supplied* fields (`reason`, `evidence`, `authorization`,
`expected_consequence`, `reversibility`, `rollback_path`,
`target_audience`) come from a side-channel marker the actor emits in
the same turn as the side-effectful tool call.

Spec/28:95 says the side-channel marker "mirrors the `atomic_capture`
marker pattern per spec/05". This module is the concrete implementation
of that pattern for the judge layer:

- The actor calls a new always-available tool named ``atomic_action``
  in the same turn as a side-effectful custom tool call.
- The ``atomic_action`` input has a required ``for_tool_call_id`` field
  that binds the marker to the specific tool_use it justifies.
- The framework extracts atomic_action markers before the custom-tool
  partition (mirrors how ``atomic_capture`` is extracted), groups them
  by ``for_tool_call_id``, and the proposal-assembly pass binds each
  side-effectful tool_use to its corresponding marker.

Field-presence rules per spec/28:177-183:

- ``read_only`` actions: marker not required (proposal not built or
  minimal proposal in audit mode).
- ``reversible_write`` / ``external_side_effect`` / ``high_risk``:
  marker REQUIRED. Missing / mismatched / duplicate marker raises
  ``JudgeProposalInvalid``; ``failure_policy`` resolves (default
  ``block``).

PR 2a of #112 — the side-channel marker mechanism ships now so PR 2b's
LLMJudgeBackend and PR 3's MandateCheck specialist have a real payload
to consume.
"""

from __future__ import annotations

from typing import Any

from ..exceptions import JudgeProposalInvalid

# JSON Schema for the atomic_action tool. The structure mirrors
# CAPTURE_TOOL_SCHEMA (spec/05) but the fields are spec/28's
# actor-supplied side-channel fields. Required: just for_tool_call_id —
# everything else is optional because spec/28 lets the actor omit
# fields it genuinely has no information for (the *judge* decides
# whether the omission is acceptable per its policy).
ATOMIC_ACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "for_tool_call_id": {
            "type": "string",
            "description": (
                "The tool_call_id of the side-effectful tool_use this "
                "marker justifies. MUST match exactly (case-sensitive). "
                "One marker per tool_call_id; duplicates raise "
                "JudgeProposalInvalid."
            ),
        },
        "reason": {
            "type": "string",
            "description": (
                "Why you are proposing this action. Free prose — the "
                "judge weighs it against the cited evidence and the "
                "action class. This is the most-important judgment input."
            ),
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Note name, conversation ref, or skill name "
                            "that supports the claim below."
                        ),
                    },
                    "claim": {
                        "type": "string",
                        "description": (
                            "What this source supports — one sentence."
                        ),
                    },
                    "provenance": {
                        "type": "string",
                        "enum": [
                            "legacy",
                            "observed",
                            "inferred",
                            "generated",
                            "confirmed",
                            "disputed",
                            "superseded",
                        ],
                        "description": (
                            "How the source was obtained. See spec/28's "
                            "memory-provenance section."
                        ),
                    },
                    "source_hash": {
                        "type": ["string", "null"],
                        "description": (
                            "sha256 of source content at citing time "
                            "(optional; lets the judge detect "
                            "post-citation tampering)."
                        ),
                    },
                },
                "required": ["source", "claim", "provenance"],
            },
            "description": (
                "Supporting references for the action. May be empty for "
                "low-risk proposals; the judge weights weight-of-evidence "
                "into its decision."
            ),
        },
        "authorization": {
            "type": ["object", "null"],
            "properties": {
                "granted_by": {
                    "type": "string",
                    "description": (
                        "Who granted you authority — typically 'operator', "
                        "'policy', or 'delegated_from:<coordinator-agent>'."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Plain-language description of the scope of "
                        "authorization."
                    ),
                },
                "granted_at": {
                    "type": "string",
                    "description": "ISO-8601 timestamp of when authority was granted.",
                },
                "expires_at": {
                    "type": ["string", "null"],
                    "description": (
                        "ISO-8601 expiry if time-bounded; null if open-ended."
                    ),
                },
            },
            "required": ["granted_by", "scope", "granted_at"],
        },
        "expected_consequence": {
            "type": "string",
            "description": (
                "Plain-language description of what will happen if the "
                "action runs. The judge compares this against the tool's "
                "actual effect class."
            ),
        },
        "reversibility": {
            "type": "string",
            "enum": [
                "reversible",
                "reversible_with_artifact",
                "irreversible",
            ],
            "description": (
                "Your assessment of whether this action can be undone. "
                "Heuristic, not a guarantee — the judge weighs it."
            ),
        },
        "rollback_path": {
            "type": ["string", "null"],
            "description": (
                "If reversible, how would the rollback work? Free prose. "
                "Required when reversibility is reversible_with_artifact."
            ),
        },
        "target_audience": {
            "type": "string",
            "description": (
                "Who sees this action's effect. 'internal' for vault-only "
                "writes; 'external:<surface>' for outward-facing actions "
                "(e.g., 'external:github_pr', 'external:email')."
            ),
        },
    },
    "required": ["for_tool_call_id"],
}


ATOMIC_ACTION_DESCRIPTION = (
    "Side-channel marker for the judge layer (spec/28). Emit this tool "
    "call in the SAME turn as any side-effectful tool call to justify "
    "the action: reason, evidence, authorization, expected_consequence, "
    "reversibility, rollback_path, target_audience. The framework binds "
    "the marker to the side-effectful tool via for_tool_call_id, and the "
    "judge inspects the bound ActionProposal before the action runs. "
    "Required for reversible_write / external_side_effect / high_risk "
    "actions; optional for read_only."
)


def canonical_tool_definition():
    """Return the atomic_action tool definition as a canonical ``LLMToolDefinition``.

    Mirrors ``_capture.canonical_tool_definition`` for the atomic_capture
    tool. Import is local to avoid the circular import
    ``judge/atomic_action → llm.types → ...``.
    """
    from ..llm.types import LLMToolDefinition

    return LLMToolDefinition(
        name="atomic_action",
        description=ATOMIC_ACTION_DESCRIPTION,
        input_schema=ATOMIC_ACTION_SCHEMA,
    )


def extract_atomic_action_markers(
    tool_uses: list[dict],
) -> dict[str, dict[str, Any]]:
    """Pull every ``atomic_action`` marker out of a list of tool_use blocks.

    Returns a dict keyed by ``for_tool_call_id``. Raises
    ``JudgeProposalInvalid`` on duplicate ``for_tool_call_id`` (one
    marker per bound tool_call), or on a marker missing the required
    ``for_tool_call_id`` field.

    Markers without ``for_tool_call_id`` are a parser-level error
    (schema validation should catch them before this point in production,
    but the framework's runtime defends against malformed input from
    backends that didn't validate). The judge layer's failure_policy
    resolves the resulting JudgeProposalInvalid per spec/28.
    """
    markers: dict[str, dict[str, Any]] = {}
    for tu in tool_uses:
        if tu.get("name") != "atomic_action":
            continue
        payload = tu.get("input", {}) or {}
        bind_id = payload.get("for_tool_call_id")
        if not bind_id or not isinstance(bind_id, str):
            raise JudgeProposalInvalid(
                "atomic_action marker missing required for_tool_call_id; "
                f"got {payload!r}"
            )
        if bind_id in markers:
            raise JudgeProposalInvalid(
                f"duplicate atomic_action marker for tool_call_id={bind_id!r}; "
                "exactly one marker per side-effectful tool call"
            )
        markers[bind_id] = payload
    return markers
