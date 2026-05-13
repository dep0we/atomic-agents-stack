"""``PolicyJudge`` — rule-engine reference implementation of ``JudgeBackend`` (spec/28).

The deterministic baseline judge. Always-on; runs FIRST in the default
ensemble before any LLM judge. Microsecond latency. Inspects:

- ``tools.md`` write paths (reuses ``_capture.enforce_write_path`` for
  parity with handler-side enforcement — divergent path-matching is a
  vulnerability surface we explicitly refuse)
- Per-class policy from ``ClassPolicySnapshot`` (``BYPASS`` /
  ``ALLOW_WITH_AUDIT`` / ``JUDGE_REQUIRED`` / ``ESCALATE``)

PR 2a of #112. The rule engine ships with a minimal set of checks; PR 3
layers in operator-configurable rules from ``judges.md``.

Supported outcomes in PR 2a: ``{ALLOW, BLOCK}`` only. Class-policy
``ESCALATE`` self-maps to ``BLOCK`` with reason
``escalate_pending_polling_unimplemented`` because the operator-resolution
polling loop doesn't land until PR 3 alongside ``judges.md``. Shipping
ESCALATE-write-but-no-read would create orphan PENDING files with no
operator-visible resolution path (spec/28:340 polling cycle).
"""

from __future__ import annotations

import re
from pathlib import Path

from .._capture import enforce_write_path
from ..exceptions import WritePathViolation
from .backend import Judgment, JudgmentOutcome
from .proposal import compute_policy_version
from .types import (
    ActionClass,
    ClassPolicyValue,
    JudgmentContext,
    ActionProposal,
)


# Tool-argument keys that, by convention, hold a filesystem path the
# tool will write to. PolicyJudge checks each present key against the
# allowed write_paths from tools.md. Operators with custom tools that
# use a different key name can register a tool whose handler enforces
# its own write-path check; PolicyJudge's check is additive, not
# load-bearing — the handler-side check stays authoritative.
PATH_ARG_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "target_path",
    "destination",
    "destination_path",
    "to",
    "write_to",
    "output_path",
)


class PolicyJudge:
    """Rule-engine reference ``JudgeBackend`` (spec/28 §"Protocol surface").

    Constructed with the allowed write paths and read-only paths from
    ``tools.md``. ``policy_version`` is the sha256 of the source tools.md
    content at construction — operators detect policy drift by
    comparing ``policy_version`` across audit log entries.

    Typical wiring (PR 2a default):

    .. code-block:: python

        rules = PolicyJudge(
            tools_md_text=tools_md.read_text(),
            allowed_write_paths=[Path("/vault/memory")],
            read_only_paths=[Path("/vault/persona")],
        )
        register_backend("rules", rules)

    PR 3 widens ``supported_outcomes`` to include ``ESCALATE`` once the
    operator-resolution polling loop lands. Until then,
    ``supported_outcomes`` advertises ``{ALLOW, BLOCK}`` and
    class-policy ``ESCALATE`` self-maps to ``BLOCK``.
    """

    def __init__(
        self,
        *,
        tools_md_text: str = "",
        allowed_write_paths: list[Path] | None = None,
        read_only_paths: list[Path] | None = None,
        judge_id: str = "rules-default",
    ) -> None:
        self._tools_md_text = tools_md_text
        self._allowed_write_paths: list[Path] = list(allowed_write_paths or [])
        self._read_only_paths: list[Path] = list(read_only_paths or [])
        self._judge_id = judge_id
        # policy_version computation is centralized in
        # ``judge.proposal.compute_policy_version`` so every JudgeBackend
        # producing this string for the same (tools.md, judges.md)
        # snapshot agrees byte-for-byte. judges.md text stays None in
        # PR 2a/2b (parser lands in PR 3); ``compute_policy_version``
        # writes ``judges.md@sha256:absent`` in that case.
        self._policy_version = compute_policy_version(tools_md_text, None)

    # ── JudgeBackend Protocol surface ──────────────────────────────

    def evaluate(
        self,
        proposal: ActionProposal,
        context: JudgmentContext,
    ) -> Judgment:
        """Apply the rule-engine checks to ``proposal``.

        Outcome flow (in order):

        1. Class-policy ``BYPASS`` → ``ALLOW`` (judge should not have
           been invoked, but if it was, allow with a note).
        2. Class-policy ``ALLOW_WITH_AUDIT`` → ``ALLOW`` with note;
           framework records the event with
           ``enforcement_action="audit_bypass"`` (handled in agent.py).
        3. Class-policy ``ESCALATE`` → ``BLOCK`` with reason
           ``escalate_pending_polling_unimplemented`` (PR 2a deferral
           per plan; PR 3 widens supported_outcomes).
        4. Class-policy ``JUDGE_REQUIRED`` → run the per-class checks
           below.
        5. Write-path check on the proposal's tool_arguments. If any
           PATH_ARG_KEYS field resolves outside allowed write paths or
           into a read-only path → ``BLOCK`` with the
           ``WritePathViolation`` reason text.
        6. All checks pass → ``ALLOW``.

        Does NOT mutate ``proposal`` or ``context.policy`` (spec/28
        idempotency invariant).
        """
        # Per spec/28 §"Capability advertisement", a backend that
        # reaches into context.runtime to influence evaluate behavior
        # fails conformance. PolicyJudge intentionally consults ONLY
        # context.policy (class_policy + tools_md_entry).
        cls_policy = self._class_policy_for(proposal, context)

        if cls_policy == ClassPolicyValue.BYPASS:
            return self._allow(
                proposal,
                reason=(
                    "class policy is bypass; judge should not have been "
                    "invoked for this class"
                ),
            )

        if cls_policy == ClassPolicyValue.ALLOW_WITH_AUDIT:
            return self._allow(
                proposal,
                reason=(
                    "class policy is allow_with_audit; recording without "
                    "enforcement"
                ),
            )

        if cls_policy == ClassPolicyValue.ESCALATE:
            # PR 2a deferral. PR 3 widens supported_outcomes once polling
            # ships.
            return self._block(
                proposal,
                reason=(
                    "class policy is escalate but operator-resolution "
                    "polling is not yet implemented (escalate_pending_"
                    "polling_unimplemented). Action blocked to avoid "
                    "orphan PENDING files. Widens to ESCALATE in PR 3 "
                    "of #112 once judges.md + polling loop ship."
                ),
            )

        # cls_policy == JUDGE_REQUIRED — run the real checks.
        violation = self._check_write_path_violations(proposal)
        if violation is not None:
            return self._block(proposal, reason=violation)

        return self._allow(proposal, reason="all rule-engine checks passed")

    def supported_outcomes(self) -> set[JudgmentOutcome]:
        # PR 2a ships ALLOW + BLOCK only. PR 3 adds ESCALATE.
        return {JudgmentOutcome.ALLOW, JudgmentOutcome.BLOCK}

    def supports_read_audit(self) -> bool:
        # Rule engine is deterministic and free — supports audit mode.
        return True

    def supports_specialist_composition(self) -> bool:
        # PolicyJudge composes happily into ensembles per spec/28.
        return True

    @property
    def judge_id(self) -> str:
        return self._judge_id

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def close(self) -> None:
        # No resources to release.
        return None

    # ── Internals ──────────────────────────────────────────────────

    def _class_policy_for(
        self,
        proposal: ActionProposal,
        context: JudgmentContext,
    ) -> ClassPolicyValue:
        """Look up the operative class-policy value for this proposal's
        ``ActionClass`` from the context's snapshot."""
        snapshot = context.policy.class_policy
        return {
            ActionClass.READ_ONLY: snapshot.read_only,
            ActionClass.REVERSIBLE_WRITE: snapshot.reversible_write,
            ActionClass.EXTERNAL_SIDE_EFFECT: snapshot.external_side_effect,
            ActionClass.HIGH_RISK: snapshot.high_risk,
        }[proposal.classification]

    def _check_write_path_violations(self, proposal: ActionProposal) -> str | None:
        """Reuse ``_capture.enforce_write_path`` for parity with
        handler-side enforcement (Codex finding #6 + the divergent-
        enforcement vulnerability the round-1 reviewer flagged).

        Returns a reason string when a violation is found, else None.
        Iterates every PATH_ARG_KEYS field present in tool_arguments.
        """
        if not self._allowed_write_paths:
            # No write paths configured → the judge can't enforce them.
            # Defer to handler-side checks. (Operators with judges.md
            # in PR 3 will get a more strict default.)
            return None

        for key in PATH_ARG_KEYS:
            raw_path = proposal.tool_arguments.get(key)
            if not raw_path or not isinstance(raw_path, str):
                continue
            # Heuristic gate: only check values that LOOK like a path.
            # Filters out ambiguous uses of PATH_ARG_KEYS for non-path
            # values (e.g., ``send_email(to="user@host")`` —
            # ``to`` is in PATH_ARG_KEYS for tools that write to a
            # filesystem path, but "user@host" is not a path). Path-
            # like values contain ``/`` (POSIX), ``\\`` (Windows), or
            # start with ``~`` (home expansion); bare filenames (no
            # separator) skip the judge check and defer to handler-
            # side enforcement. Operators authoring write-tools whose
            # path argument is a bare filename should use
            # ``target_path``/``destination_path`` (more explicit) or
            # register a handler that performs ``enforce_write_path``
            # itself.
            if not ("/" in raw_path or "\\" in raw_path or raw_path.startswith("~")):
                continue
            try:
                # ``expanduser()`` parity with ``_platform.expand`` used
                # in ``_tools.parse_tools_md`` — the allowed paths are
                # already expanded; the proposal's path arg may carry a
                # tilde from the LLM. Without this, ``~/docs/x.md``
                # falsely BLOCKs against expanded write paths.
                enforce_write_path(
                    Path(raw_path).expanduser(),
                    self._allowed_write_paths,
                    self._read_only_paths,
                )
            except WritePathViolation as exc:
                return (
                    f"write-path violation on tool_arguments[{key!r}]: "
                    f"{exc}"
                )
        return None

    def _allow(self, proposal: ActionProposal, *, reason: str) -> Judgment:
        return Judgment(
            outcome=JudgmentOutcome.ALLOW,
            reason=reason,
            judge_id=self._judge_id,
            policy_version=self._policy_version,
            latency_ms=0,
            cost_usd=0.0,
        )

    def _block(self, proposal: ActionProposal, *, reason: str) -> Judgment:
        return Judgment(
            outcome=JudgmentOutcome.BLOCK,
            reason=reason,
            judge_id=self._judge_id,
            policy_version=self._policy_version,
            latency_ms=0,
            cost_usd=0.0,
        )


def make_default_policy_judge(
    *,
    tools_md_text: str = "",
    allowed_write_paths: list[Path] | None = None,
    read_only_paths: list[Path] | None = None,
) -> PolicyJudge:
    """Construct the default ``PolicyJudge`` instance for an agent.

    Convenience wrapper used by ``agent.py``'s judge dispatch wiring —
    encapsulates the typical construction shape so the wiring code
    doesn't need to keep the keyword names in sync with the class.
    """
    return PolicyJudge(
        tools_md_text=tools_md_text,
        allowed_write_paths=list(allowed_write_paths or []),
        read_only_paths=list(read_only_paths or []),
    )
