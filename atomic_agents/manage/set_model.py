"""``manage set-model <agent>`` verb — model.md model-swap editor (spec/55 #726).

The second management verb, and the first whose M9 composition set is
non-empty by design: a ``--model`` value MUST refuse at write time (before
preview, per M4) unless it is PRICED, RESOLVES to exactly one LLM backend,
and composes with the agent's effective Policy. It is also the first
surgical ``model.md`` field-writer — a line-aware region editor mirroring
``govern.py``'s ``_edit_governance_block`` shape, regex-anchored on the same
reader pattern ``atomic_agents/_model.py:79-80`` uses so a write is
guaranteed re-readable by ``parse_model_md``.

Implements the S2 five-step safety routine for ``--model``:
  1. Validate   — model.md presence + heading shape + the M9 composition
                  chain (priced / resolves / policy-consult)
  2. Preview    — before/after of ONLY the '## Default model' value span
  3. Confirm    — --dry-run exits; --yes or TTY prompt
  4. Snapshot + Write — snapshot prior content; atomic_write new content
  5. Audit      — RunRecord appended to per-agent + fleet LogBackend (M8)

PR1 SCOPE (maintainer-ruled ``pr1-flag-scope``): ``--model`` only.
``--fallback`` (#754) and ``--provider`` (#755) are grammar-recognized by the
CLI parser but return a clean "not yet settable in PR1" refusal, fired
BEFORE the registry resolve (mirrors govern's grammar-pin-but-defer
precedent for --add/--remove/--set-json).

M9 composition chain (spec/55 "Composing with the runtime"), each check
reusing an existing pure function — never reimplemented here:
  (a) PRICED    — ``atomic_agents.core_api.get_model_rates(model_id) is not
                  None`` (the core<->extension boundary accessor onto
                  ``_costs.PRICING`` — TENSIONS T17; manage/ is an extension
                  package and never reaches into the core-private table
                  directly). Hard refuse always (maintainer ruling
                  ``unpriced-model-posture`` — no --force escape hatch).
  (b) RESOLVES  — ``atomic_agents.llm.find_backend_for_model(model_id,
                  preferred_provider=None)`` MUST resolve to exactly one
                  backend. ``UnknownModelError`` / ``AmbiguousBackendError``
                  refuse with distinct error_types (maintainer ruling
                  ``refusal-error-taxonomy``); the ambiguous-backend message
                  is DELIBERATELY NOT ``str(exc)`` — the upstream exception's
                  own text tells the operator to "pass --provider", which
                  PR1 forbids (ruling ``provider-disambiguation-posture``).
  (c) CAPS-COMPOSE — ``PolicyBackend.get_effective_caps(agent)`` is
                  CONSULTED (so the value is available/logged for a future
                  real gate) but produces NO refusal in PR1. Framework-wide
                  ``CostCaps`` carries only ``daily_usd``/``monthly_usd``
                  aggregate spend totals — there is no sanctioned
                  (model_id, CostCaps) -> forbid/allow formula anywhere in
                  the codebase, and inventing one here would be exactly the
                  unruled heuristic the build's own guardrails forbid
                  ("MUST NOT reimplement... policy evaluation"). This gap is
                  escalated to the maintainer as a Tier A fork (see the PR
                  description / newTierAForks) rather than decided silently.

POLICY-OVERRIDE = WARN-AND-WRITE (maintainer ruling
``policy-override-interaction``, verified against ``agent.py:3696`` — Policy
wins at runtime). set-model writes ``--model`` to model.md unconditionally;
if ``PolicyBackend.get_effective_model(agent)`` returns a non-None value
DIFFERING from ``--model``, a prominent source-named WARNING is emitted
(exit 0, applied) — surfaced at BOTH the preview stage (before the operator
confirms) and again from a BEST-EFFORT post-write recompute (Fix 4 wording
correction: NOT perfectly atomic-with-write — ``policy.md`` is a separate
file with no lock of its own, and this recompute runs AFTER
``run_managed_write`` returns, i.e. AFTER the manage lease has already been
released, so a Policy change landing in the narrow window between the
recompute's read and the audit append is still possible in principle. What
this recompute buys is a REDUCED, not eliminated, stale-preview window: it
narrows "Policy state as of the pre-confirm preview, which may be arbitrarily
stale behind an unbounded TTY wait" down to "Policy state as of just after
the write landed" — meaningfully tighter, not a hard atomicity guarantee).

PolicyBackend failure has TWO DISTINCT postures depending on which of the two
consults it hits — the module does not fail closed uniformly (Fix 4 wording
correction): the PRE-write consult (gate (c) + the preview-time override
check, before step 4) is FAIL-CLOSED (Tier B decision, spec/55 P1/P2 prep
findings) — mirrors ``ManageLockUnavailableError``'s posture: a
broken/misconfigured PolicyBackend refuses the write rather than silently
skip the gate. The POST-write recompute (above) is necessarily FAIL-OPEN
instead: the write has already landed by the time it runs, so a broken
PolicyBackend there degrades to the (possibly stale) preview-time value
rather than reverting an already-applied write — there is nothing left to
refuse. A caller reading only "PolicyBackend failure is fail-closed" would
misread the post-write path; both postures are named here so neither reads
as a blanket rule.

model.md preservation contract: the ``cost_guardrails`` yaml block, every
prose paragraph/HTML comment/table, and the ``provider:`` line survive
BYTE-FOR-BYTE. Only the '## Default model' heading's VALUE SPAN is
rewritten in place. Markup-style decision (spec/55 open fork 5, resolved in
this build): the operator's EXISTING value-wrapper markup
(``**`id`**``/``**id**``/`` `id` ``/bare) is PRESERVED AS FOUND, matching
M2's byte-faithful ethos — never normalized to one canonical form.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core_api import get_model_rates, parse_model_md, safe_resolve_under
from ..logs.types import PRIMITIVE_MANAGE_RESTORE, PRIMITIVE_MANAGE_SET_MODEL, RunRecord
from ._routine import (
    ManagedWriteResult,
    append_management_audit,
    get_manage_lock_backend,
    list_snapshots,
    resolve_snapshot_path,
    run_managed_write,
)
from .exceptions import (
    ManageAgentBusyError,
    ManageAmbiguousModelBackendError,
    ManageDefaultModelHeadingAbsentError,
    ManageDefaultModelValueUnparseableError,
    ManageDeferredFlagRefused,
    ManageDuplicateDefaultModelHeadingError,
    ManageLockUnavailableError,
    ManageModelMdAbsentError,
    ManagePolicyBackendUnavailableError,
    ManageSnapshotNotFoundError,
    ManageUnknownModelError,
    ManageUnpricedModelError,
    ManageUnwritableModelIdError,
)

# spec/55 #709 parameterization — set-model's own snapshot namespace, distinct
# from govern's "govern" subdir so the two verbs' snapshots never collide.
_SNAPSHOT_SUBDIR = "set-model"

# Fix 1 (cross-family review finding): shared text for the fallback
# advisory shown/emitted when the post-write Policy recompute fails —
# defined once so the human-readable print and the ``--json`` payload
# never drift apart.
_POLICY_RECHECK_FAILED_MSG = (
    "could not re-verify Policy override state after the write; the "
    "pre-confirm advisory check above is the last known signal."
)

# Deferred-flag tracking issues (maintainer ruling ``pr1-flag-scope``).
_FALLBACK_ISSUE = "#754"
_PROVIDER_ISSUE = "#755"


# ── Surgical '## Default model' value-span editor ──────────────────────────

# Heading-presence detector. Deliberately permissive (matches trailing text
# on the heading line, e.g. "## Default model (legacy)") — the SAME
# tolerance _model.py's reader has, so this module's notion of "the heading
# is present" never disagrees with parse_model_md's. Anchored to line-start
# (``^`` + re.MULTILINE) so an H3 subheading ("### Default model" — the
# trailing two ``#`` chars would otherwise satisfy ``##\s+``) or a "##
# Default model" mention inside a prose paragraph never counts as THE
# heading — only a genuine column-0 H2 does. _model.py's reader anchors the
# same way (round-trip invariant: the writer must match exactly what the
# reader matches).
_HEADING_RE = re.compile(r"^##\s+Default model[^\n]*", re.MULTILINE)

# Value-span locator. Structurally identical to _model.py:79-80's reader
# regex (same heading anchor, same markup tolerance, same value character
# class) but with the wrapper markup split into its OWN capture groups
# (2 = leading markup, 3 = the bare value, 4 = trailing markup) so a write
# can replace ONLY group 3 — the operator's existing markup survives
# byte-for-byte either side of it (spec/55 "Markup-style preservation",
# resolved PRESERVE-AS-FOUND in this build). Anchored on the FIRST match
# only (re.search, not finditer) — the same first-match semantics
# parse_model_md_text's re.search uses, so a duplicate-heading file is
# guarded separately (see _count_default_model_headings) rather than
# silently editing whichever occurrence this regex happens to find.
# Anchored to line-start (``^`` + re.MULTILINE, matching _HEADING_RE above)
# so this is guaranteed to target the SAME occurrence _HEADING_RE counted —
# an H3 subheading or a mid-prose "## Default model" mention can never be
# the match target here either.
_VALUE_SPAN_RE = re.compile(
    r"(^##\s+Default model[^\n]*\n+\s*)(\*{0,2}`?)([a-zA-Z0-9._/-]+)(`?\*{0,2})",
    re.MULTILINE,
)


def _count_default_model_headings(text: str) -> int:
    """Count '## Default model'-shaped headings in ``text`` (duplicate guard)."""
    return len(_HEADING_RE.findall(text))


# The exact value charset the reader (_model.py) and this writer's
# _VALUE_SPAN_RE group 3 both accept. Fix 2 (defense-in-depth): validated
# BEFORE any write, independent of the M9 PRICING-membership check — PRICING
# is a plain dict with no charset constraint of its own, so a future
# PRICING key containing a character outside this class (e.g. "+", a space)
# would otherwise slice into the file and silently truncate on next read.
_WRITABLE_VALUE_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")


def write_default_model(text: str, agent_id: str, new_value: str) -> str:
    """Apply a surgical in-place edit to the '## Default model' value span.

    Preserves everything else in ``text`` byte-for-byte: the
    ``cost_guardrails`` yaml block, every prose paragraph/comment/table, the
    ``provider:`` line, and — per the markup-preservation ruling — the
    operator's existing wrapper markup around the value itself (only the id
    characters are replaced, never the surrounding backticks/asterisks).

    Args:
        text: full model.md file content (read with ``newline=''`` for
            CRLF byte-fidelity — see ``_read_model_md``).
        agent_id: for error messages only.
        new_value: the new model id (already M9-composition-approved by the
            caller — PRICING membership guarantees it matches
            ``[a-zA-Z0-9._/-]+``, so the written value is always
            re-readable by ``parse_model_md_text``).

    Returns:
        Updated file content with only the value span changed.

    Raises:
        ManageDefaultModelHeadingAbsentError: no '## Default model' heading.
        ManageDuplicateDefaultModelHeadingError: more than one such heading.
        ManageDefaultModelValueUnparseableError: heading present, but no
            value token immediately follows it.
        ManageUnwritableModelIdError: ``new_value`` contains a character
            outside the writer's value charset (Fix 2 defense-in-depth) —
            checked BEFORE any slicing, so a rejected value never partially
            lands in ``text``.
    """
    if not _WRITABLE_VALUE_RE.fullmatch(new_value):
        raise ManageUnwritableModelIdError(new_value)

    heading_count = _count_default_model_headings(text)
    if heading_count == 0:
        raise ManageDefaultModelHeadingAbsentError(agent_id)
    if heading_count > 1:
        raise ManageDuplicateDefaultModelHeadingError(agent_id, heading_count)

    m = _VALUE_SPAN_RE.search(text)
    if m is None:
        raise ManageDefaultModelValueUnparseableError(agent_id)

    start, end = m.start(3), m.end(3)
    return text[:start] + new_value + text[end:]


def _extract_default_model_value(text: str) -> str | None:
    """Extract the CURRENT '## Default model' value from ``text``, or None.

    Used for the 'before' side of preview/audit — a thin wrapper that
    mirrors ``_VALUE_SPAN_RE``'s match without the duplicate/absence guards
    (those are enforced by ``write_default_model`` at the point a write is
    actually attempted; this helper is display-only).
    """
    m = _VALUE_SPAN_RE.search(text)
    return m.group(3) if m else None


# ── model.md read (CRLF byte-fidelity, mirrors govern's newline='' idiom) ──


def _read_model_md(model_path: Path) -> str:
    """Read model.md with ``newline=''`` so CRLF-authored files stay intact.

    Mirrors ``govern._read_or_create_governance``'s CRLF handling exactly
    (maintainer ruling ``crlf-byte-fidelity-read``) — ``Path.read_text()``'s
    universal-newline translation would silently normalise ``\\r\\n`` -> ``\\n``
    before the content reaches the surgical editor, breaking the
    byte-for-byte preservation contract on a CRLF file.

    Raises:
        OSError: read failure (permission, race).
    """
    with model_path.open("r", encoding="utf-8", newline="") as _f:
        return _f.read()


# ── PolicyBackend consult (M9 gate (c) + the WARN-AND-WRITE signal) ────────


def _consult_policy(agents_root: Path, agent_id: str) -> tuple[Any, str | None]:
    """Consult PolicyBackend for CAPS-COMPOSE (informational, PR1) + the
    policy-override signal (``get_effective_model``).

    FAIL-CLOSED on construction or read failure — see
    ``ManagePolicyBackendUnavailableError``'s docstring for the rationale.

    CAPS-COMPOSE (gate (c)): the returned ``CostCaps`` is fetched so it is
    available/logged, but this function performs NO refusal check against
    it — see the module docstring's Tier A escalation note.

    Returns:
        (effective_caps, effective_model) — effective_model is None when
        Policy has no opinion.

    Raises:
        ManagePolicyBackendUnavailableError: construction or read failed.
    """
    from ..policy.backend import get_default_policy_backend  # noqa: PLC0415

    try:
        backend = get_default_policy_backend(agents_root)
        effective_caps = backend.get_effective_caps(agent_id)
        effective_model = backend.get_effective_model(agent_id)
    except Exception as exc:  # noqa: BLE001 -- fail-closed on ANY consult error
        raise ManagePolicyBackendUnavailableError(str(exc)) from exc
    return effective_caps, effective_model


# ── M9 composition chain (gates (a) PRICED + (b) RESOLVES) ─────────────────


def _validate_model_composition(model_id: str) -> None:
    """Run M9 gates (a) PRICED and (b) RESOLVES against ``model_id``.

    Gate (c) CAPS-COMPOSE (PolicyBackend consult) is a SEPARATE call
    (``_consult_policy``) — it needs ``agents_root``, which this function
    intentionally does not take, keeping the PRICING/find_backend_for_model
    checks (pure, no I/O beyond the process-local LLM registry) isolated
    from the PolicyBackend I/O path.

    Raises:
        ManageUnpricedModelError: gate (a) — model_id not in PRICING.
        ManageUnknownModelError: gate (b) — zero backends claim model_id.
        ManageAmbiguousModelBackendError: gate (b) — >1 backend claims it.
    """
    from .. import llm as _llm  # noqa: PLC0415
    from ..exceptions import AmbiguousBackendError, UnknownModelError  # noqa: PLC0415

    # Gate (a) PRICED. Routed through core_api.get_model_rates() (TENSIONS
    # T17 core<->extension boundary — manage/ is an extension package and
    # MUST NOT reach into the core-private _costs.PRICING table directly).
    if get_model_rates(model_id) is None:
        raise ManageUnpricedModelError(model_id)

    # Gate (b) RESOLVES. UnknownModelError and AmbiguousBackendError are
    # caught in SEPARATE except clauses (spec/55 P1 prep finding) — a shared
    # tuple/except would collapse two DISTINCT ruled error_types into one
    # branch and risk mis-catching a third, unrelated AtomicAgentsError as
    # one of these two.
    try:
        _llm.find_backend_for_model(model_id, preferred_provider=None)
    except UnknownModelError:
        no_backends = len(list(_llm.iter_registered_backends())) == 0
        raise ManageUnknownModelError(
            model_id, no_backends_registered=no_backends
        ) from None
    except AmbiguousBackendError as exc:
        raise ManageAmbiguousModelBackendError(model_id, exc.candidates) from None


# ── --json / human output helpers (mirrors govern's shape) ────────────────


def _emit_json_error(error_type: str, reason: str) -> None:
    print(
        json.dumps({"ok": False, "error_type": error_type, "reason": reason}, indent=2)
    )


def _emit_json_success(
    agent_id: str,
    changed_fields: list[str],
    before: dict[str, Any],
    after: dict[str, Any],
    snapshot_path: str | None,
    audit_status: str,
    *,
    policy_override: str | None = None,
    policy_recheck_ok: bool | None = None,
    pricing_advisory: str | None = None,
    dry_run: bool = False,
) -> None:
    """Emit the ``--json`` success payload.

    Fix 6 (JSON key collision, spec/55 P2 finding): ``policy_override`` and
    ``pricing_advisory`` are DISTINCT keys with DISTINCT meanings, never
    conflated under one name — ``--set`` populates ONLY
    ``policy_override`` (a genuine Policy-vs-model.md conflict, the
    WARN-AND-WRITE ruling); ``--restore`` populates ONLY
    ``pricing_advisory`` (an M9-gate-(a)-staleness note — the restored
    snapshot's model id is no longer in PRICING, never a Policy signal). A
    copilot keyed on ``payload["policy_override"]`` after ``--restore``
    therefore correctly sees ``null`` rather than misreading a pricing note
    as a Policy conflict.

    ``policy_recheck_ok`` (Fix 1, cross-family review finding): True when
    the post-write Policy recompute succeeded and ``policy_override``
    reflects the AUTHORITATIVE post-write state; False when the recompute
    raised and the value fell back to the pre-confirm-wait preview
    consult. ``--restore`` does not run a post-write recompute, so it
    always passes ``None`` (not applicable, distinct from both True/False).
    """
    payload: dict = {
        "ok": True,
        "agent": agent_id,
        "changed_fields": changed_fields,
        "before": before,
        "after": after,
        "snapshot_path": snapshot_path,
        "audit_status": audit_status,
        "policy_override": policy_override,
        "policy_recheck_ok": policy_recheck_ok,
        "pricing_advisory": pricing_advisory,
    }
    if dry_run:
        payload["dry_run"] = True
    print(json.dumps(payload, indent=2))


def _principal_id() -> str:
    """Resolve the audit identity (spec/48 PrincipalBackend, home-user default).

    Identical to govern's ``_principal_id`` — duplicated rather than
    imported across verb modules to keep each verb module import-light
    (mirrors the existing govern.py pattern; both are ~6 lines).
    """
    try:
        from ..principal import LocalPrincipalBackend  # noqa: PLC0415

        principal = LocalPrincipalBackend().derive_principal(None)
        return principal.identifier
    except Exception:  # noqa: BLE001
        return "local"


def _relative_snapshot_path(snapshot_path: Path | None, agent_dir: Path) -> str | None:
    if snapshot_path is None:
        return None
    try:
        return str(snapshot_path.relative_to(agent_dir))
    except ValueError:
        return str(snapshot_path)


def _policy_override_warning(model_id: str, effective_model: str | None) -> str | None:
    """Return the WARN-AND-WRITE message, or None when there is no conflict.

    Maintainer ruling ``policy-override-interaction``: a WARN, never a
    refusal. Fires only when Policy has an opinion (``effective_model`` is
    non-None) AND it differs from the value being written.
    """
    if effective_model is None or effective_model == model_id:
        return None
    return (
        f"Policy currently overrides this agent's model with {effective_model!r}; "
        f"your model.md change to {model_id!r} will not take effect at runtime "
        "until that Policy override is removed."
    )


# ── Confirm gate (S2 step 3) — same shape as govern's ──────────────────────


def _emit_abort(use_json: bool, *, interrupted: bool) -> None:
    reason = (
        "operator interrupted (SIGINT); no changes written"
        if interrupted
        else "operator declined the confirmation; no changes written"
    )
    if use_json:
        print(
            json.dumps(
                {"ok": False, "error_type": "aborted", "reason": reason}, indent=2
            )
        )
    else:
        msg = "\nInterrupted." if interrupted else "Aborted."
        print(msg, file=sys.stderr)


def _require_confirmation(use_json: bool, yes: bool) -> int | None:
    """S2 step 3 confirm gate. Identical exit-code ladder to govern's."""
    if yes:
        return None

    if not sys.stdin.isatty():
        reason = (
            "--yes is required for non-interactive use "
            "(no TTY detected; use --yes to apply)."
        )
        if use_json:
            _emit_json_error("confirmation_required", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    try:
        print("Apply these changes? [y/N] ", end="", file=sys.stderr, flush=True)
        answer = input().strip().lower()
    except EOFError:
        _emit_abort(use_json, interrupted=False)
        return 3
    except KeyboardInterrupt:
        _emit_abort(use_json, interrupted=True)
        return 130

    if answer not in ("y", "yes"):
        _emit_abort(use_json, interrupted=False)
        return 3

    return None


# ── --show (read-only) ──────────────────────────────────────────────────────


def _show_model(
    agent_id: str, agents_root: Path, model_path: Path, use_json: bool
) -> int:
    """``--show`` — resolved model config + the policy-override signal.

    Maintainer ruling ``show-json-shape``: default_model/fallback_model/
    provider as parsed by ``parse_model_md`` (the true mirror of govern
    --show), PLUS one real signal — whether Policy currently overrides the
    WRITTEN default_model (the effective-vs-written distinction).

    Scoped to the READ path only (spec/55 P1 prep finding): --show never
    refuses on an absent model.md or absent heading — it reports the
    resolved (default-filled) state at exit 0, mirroring govern --show's
    absent-state precedent. The policy-override consult is BEST-EFFORT here
    (a broken PolicyBackend degrades to ``policy_override: null`` rather
    than refusing a pure read — the M9 gate's fail-closed posture applies to
    the WRITE decision, not to display).
    """
    model_md_present = model_path.exists()
    resolved = parse_model_md(model_path if model_md_present else None)

    effective_model: str | None = None
    policy_readable = True
    try:
        _, effective_model = _consult_policy(agents_root, agent_id)
    except ManagePolicyBackendUnavailableError:
        policy_readable = False

    policy_overrides = (
        effective_model is not None and effective_model != resolved["default_model"]
    )

    if use_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "agent": agent_id,
                    "model_md_present": model_md_present,
                    "default_model": resolved["default_model"],
                    "fallback_model": resolved["fallback_model"],
                    "provider": resolved["provider"],
                    "policy_readable": policy_readable,
                    "policy_effective_model": effective_model,
                    "policy_overrides_default_model": policy_overrides,
                },
                indent=2,
            )
        )
        return 0

    state = "present" if model_md_present else "absent"
    print(f"[{agent_id}] model.md: {state}")
    print(f"  default_model: {resolved['default_model']!r}")
    print(f"  fallback_model: {resolved['fallback_model']!r}")
    print(f"  provider: {resolved['provider']!r}")
    if not policy_readable:
        print("  policy: <unavailable — could not consult PolicyBackend>")
    elif policy_overrides:
        print(
            f"  policy: OVERRIDES default_model with {effective_model!r} "
            "(model.md's value will not take effect at runtime)"
        )
    else:
        print("  policy: no override")
    return 0


def _list_snapshots(agent_id: str, agent_dir: Path, use_json: bool) -> int:
    """``--list-snapshots`` — read-only, symmetric with govern's."""
    snapshots = list_snapshots(agent_dir, _SNAPSHOT_SUBDIR)
    ids = [p.name for p in snapshots]

    if use_json:
        print(json.dumps({"ok": True, "agent": agent_id, "snapshots": ids}, indent=2))
        return 0

    if not ids:
        print(f"[{agent_id}] no set-model snapshots.")
        return 0

    print(f"[{agent_id}] set-model snapshots ({len(ids)}):")
    for sid in ids:
        print(f"  {sid}")
    return 0


# ── Main verb entry point ──────────────────────────────────────────────────


def run_set_model(args: Any, agents_root: Path) -> int:
    """Entry point for ``atomic-agents manage set-model <agent> ...``.

    Returns:
        Process exit-code ladder (identical to govern's, spec/55 normative
        note): 0 applied/preview/read-only, 1 refusal-or-error, 3 declined,
        130 SIGINT.
    """
    use_json = getattr(args, "json", False)
    dry_run = getattr(args, "dry_run", False)
    yes = getattr(args, "yes", False)
    show = getattr(args, "show", False)
    list_snapshots_flag = getattr(args, "list_snapshots", False)
    restore_id = getattr(args, "restore", None)
    model_id: str | None = getattr(args, "model", None)
    fallback_value = getattr(args, "fallback", None)
    provider_value = getattr(args, "provider", None)
    agent_id: str = args.agent

    # ── Deferred-flag refusal (--fallback / --provider) — grammar-pinned,
    # PR1-deferred (maintainer ruling pr1-flag-scope). Fires BEFORE the
    # registry resolve — a scope refusal independent of whether the agent
    # exists, mirroring govern's --add/--remove/--set-json ordering (spec/55
    # P2 prep finding). Checked BEFORE model_id is ever passed to
    # find_backend_for_model, so an operator cannot smuggle disambiguation
    # via --provider in PR1 (spec/55 P1 prep finding).
    if fallback_value is not None:
        exc = ManageDeferredFlagRefused("--fallback", _FALLBACK_ISSUE)
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    if provider_value is not None:
        exc = ManageDeferredFlagRefused("--provider", _PROVIDER_ISSUE)
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── Single-primary-action refusal (mirrors govern's #710 ruling) ───────
    primary_actions = [
        name
        for name, present in (
            ("--restore", bool(restore_id)),
            ("--model", bool(model_id)),
            ("--show", show),
            ("--list-snapshots", list_snapshots_flag),
        )
        if present
    ]
    if len(primary_actions) > 1:
        reason = (
            f"{' and '.join(primary_actions)} are mutually exclusive — pass "
            "exactly one primary action per invocation."
        )
        if use_json:
            _emit_json_error("multiple_primary_actions", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── S1: Resolve through AgentRegistryBackend ────────────────────────────
    try:
        from ..agent_registry import get_default_agent_registry_backend  # noqa: PLC0415

        registry = get_default_agent_registry_backend(agents_root)
        ref = registry.get_agent(agent_id)
    except Exception as exc:  # noqa: BLE001
        reason = f"Failed to load agent registry: {exc}"
        if use_json:
            _emit_json_error("registry_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    if ref is None:
        reason = f"Agent {agent_id!r} not found in the registry at {agents_root}"
        if use_json:
            _emit_json_error("agent_not_found", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    agent_dir = Path(ref.location)

    try:
        safe_resolve_under(agent_dir, agents_root)
    except Exception as exc:  # noqa: BLE001
        reason = f"Agent directory outside agents_root — refused: {exc}"
        if use_json:
            _emit_json_error("path_traversal", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    model_path = agent_dir / "model.md"

    # ── Read-only paths (--show / --list-snapshots) — NEVER touch the manage
    # lease, and NEVER refuse on an absent model.md (spec/55 P1 prep finding:
    # the absence refusal is scoped to the --model WRITE path only) ────────
    if show:
        return _show_model(agent_id, agents_root, model_path, use_json)

    if list_snapshots_flag:
        return _list_snapshots(agent_id, agent_dir, use_json)

    # ── Write paths (--model / --restore) — containment + symlink guards on
    # model.md, mirroring govern's guards on governance.md ─────────────────
    try:
        safe_resolve_under(model_path, agents_root)
    except Exception as exc:  # noqa: BLE001
        reason = f"model.md path outside agents_root — refused: {exc}"
        if use_json:
            _emit_json_error("path_traversal", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    if model_path.exists() and model_path.is_symlink():
        reason = (
            f"model.md at {model_path} is a symlink — write refused "
            "(path containment guard)."
        )
        if use_json:
            _emit_json_error("symlink_refused", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    if restore_id:
        return _run_restore(
            agent_id=agent_id,
            agent_dir=agent_dir,
            agents_root=agents_root,
            model_path=model_path,
            snapshot_id=restore_id,
            use_json=use_json,
            dry_run=dry_run,
            yes=yes,
        )

    if not model_id:
        reason = (
            "No primary action specified. Use --model <id>, --show, "
            "--list-snapshots, or --restore <snapshot-id>."
        )
        if use_json:
            _emit_json_error("no_fields", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    return _run_set_model(
        agent_id=agent_id,
        agent_dir=agent_dir,
        agents_root=agents_root,
        model_path=model_path,
        model_id=model_id,
        use_json=use_json,
        dry_run=dry_run,
        yes=yes,
    )


# ── --model write path (S2 five-step routine through the hoisted spine) ────


def _run_set_model(
    *,
    agent_id: str,
    agent_dir: Path,
    agents_root: Path,
    model_path: Path,
    model_id: str,
    use_json: bool,
    dry_run: bool,
    yes: bool,
) -> int:
    """``manage set-model <agent> --model <id>`` — the write path (#726)."""
    # ── S2 Step 1: Validate — model.md presence (surgical editor, not a
    # scaffolder; spec/55 "Behavior" step 1) ────────────────────────────────
    # Fix 3 (spec/55 reconciliation): UNREACHABLE via the real CLI/registry
    # surface — model.md-presence IS the AgentRegistryBackend discovery
    # predicate (spec/37:314), so an agent with no model.md never resolves
    # a ``ref`` in the first place (S1 refuses with 'agent_not_found' before
    # this function is ever called). Kept as a direct-API defense-in-depth
    # safety net for TWO reasons this function does not control: (1) a
    # direct caller of ``_run_set_model``/``run_set_model`` that bypasses
    # the registry resolve entirely (this module exposes both as public
    # functions, and the test suite itself calls ``_run_set_model`` directly
    # — see ``test_model_md_absent_refuses_via_direct_write_path_call``);
    # (2) a TOCTOU race — model.md deleted between the registry resolve in
    # ``run_set_model`` and this check. See ``_read_base`` below for the
    # SAME re-check done fresh INSIDE the lease, for the identical reason.
    if not model_path.exists():
        exc = ManageModelMdAbsentError(agent_id)
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── S2 Step 1: Validate — M9 composition chain, refuse BEFORE preview
    # (M4). Gates (a)/(b) first (pure, no I/O beyond the process-local LLM
    # registry); gate (c) / policy-override consult second (I/O, fail-closed
    # on a broken PolicyBackend) ─────────────────────────────────────────
    try:
        _validate_model_composition(model_id)
    except (
        ManageUnpricedModelError,
        ManageUnknownModelError,
        ManageAmbiguousModelBackendError,
    ) as exc:
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        _, effective_model_preview = _consult_policy(agents_root, agent_id)
    except ManagePolicyBackendUnavailableError as exc:
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ── Advisory pre-lock read (preview/dry-run only) ───────────────────────
    try:
        preview_content = _read_model_md(model_path)
    except OSError as exc:
        reason = f"Could not read model.md: {exc}"
        if use_json:
            _emit_json_error("read_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    before_value = _extract_default_model_value(preview_content)

    # Doomed-edit parity (mirrors govern's ordering — a heading-absent /
    # duplicate-heading / value-unparseable file fails identically on
    # --dry-run and on apply, computed BEFORE the dry-run early exit).
    try:
        preview_content = write_default_model(preview_content, agent_id, model_id)
    except (
        ManageDefaultModelHeadingAbsentError,
        ManageDuplicateDefaultModelHeadingError,
        ManageDefaultModelValueUnparseableError,
        ManageUnwritableModelIdError,
    ) as exc:
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    warning = _policy_override_warning(model_id, effective_model_preview)

    if not use_json:
        print(f"\n[{agent_id}] model.md changes:")
        print(f"  default_model: {before_value!r} -> {model_id!r}")
        if warning:
            print(f"  WARNING: {warning}")
        print()

    # ── S2 Step 3: --dry-run exits here ──────────────────────────────────
    if dry_run:
        if use_json:
            _emit_json_success(
                agent_id,
                ["default_model"],
                before={"default_model": before_value},
                after={"default_model": model_id},
                snapshot_path=None,
                audit_status="n/a",
                policy_override=warning,
                dry_run=True,
            )
        else:
            print("[dry-run] No changes written.")
        return 0

    # ── S2 Step 3: Confirm (BEFORE the manage lease is ever acquired) ──────
    confirm_exit = _require_confirmation(use_json, yes)
    if confirm_exit is not None:
        return confirm_exit

    # ── S2 Steps 4/4b: hoisted spine — lock, FRESH read, snapshot, write ───
    def _read_base() -> tuple[str, bool]:
        # Re-check absence INSIDE the lock (spec/55 P1 prep finding): the
        # file could have been deleted between the pre-lock advisory check
        # above and lock acquisition, during an unbounded confirm wait.
        if not model_path.exists():
            raise ManageModelMdAbsentError(agent_id)
        return _read_model_md(model_path), True

    def _apply_edit(fresh_content: str) -> str:
        # Re-validates heading/duplicate/value-parseable freshly against the
        # in-lock read — never relies on the pre-lock advisory check above.
        return write_default_model(fresh_content, agent_id, model_id)

    lock_backend = get_manage_lock_backend(agent_dir)

    try:
        result: ManagedWriteResult = run_managed_write(
            agent_dir=agent_dir,
            agent_id=agent_id,
            write_path=model_path,
            subdir=_SNAPSHOT_SUBDIR,
            read_base=_read_base,
            apply_edit=_apply_edit,
            lock_backend=lock_backend,
        )
    except (ManageAgentBusyError, ManageLockUnavailableError):
        raise
    except ManageModelMdAbsentError as exc:
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (
        ManageDefaultModelHeadingAbsentError,
        ManageDuplicateDefaultModelHeadingError,
        ManageDefaultModelValueUnparseableError,
        ManageUnwritableModelIdError,
    ) as exc:
        reason = f"Failed to apply --model edit against the current model.md: {exc}"
        if use_json:
            _emit_json_error(exc.error_type, reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    except OSError as exc:
        reason = f"Failed to write model.md: {exc}"
        if use_json:
            _emit_json_error("write_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- fail-closed structured refusal, mirrors govern
        reason = f"Unexpected error while writing model.md: {exc}"
        if use_json:
            _emit_json_error("write_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # ── Post-write policy-override recompute (spec/55 P1 prep finding,
    # Fix 4 wording correction): the audit record and the final WARN reflect
    # Policy state as of JUST AFTER the write landed, not the (possibly
    # stale, pre-confirm-wait) preview-time consult — a narrowed, not
    # eliminated, staleness window (this recompute runs after the manage
    # lease is already released; ``policy.md`` has no lock of its own, so
    # this is BEST-EFFORT, not a hard atomicity guarantee — see the module
    # docstring). A failure to recompute degrades to the advisory
    # preview-time value (FAIL-OPEN here, distinct from the PRE-write
    # consult's FAIL-CLOSED posture — see the module docstring) rather than
    # failing an already-applied write. ────────────────────────
    try:
        _, effective_model_authoritative = _consult_policy(agents_root, agent_id)
        policy_recheck_ok = True
    except ManagePolicyBackendUnavailableError:
        effective_model_authoritative = effective_model_preview
        policy_recheck_ok = False

    authoritative_warning = _policy_override_warning(
        model_id, effective_model_authoritative
    )

    # Fix 1 (cross-family review finding): the JSON success payload needs a
    # copilot-visible warning even when there is no genuine Policy-vs-
    # model.md conflict to report (``authoritative_warning is None``) but
    # the recompute itself failed — otherwise a copilot reading
    # ``policy_override: null`` cannot distinguish "Policy has no opinion"
    # from "we never actually re-checked". Mirrors the human-readable
    # fallback print below, sharing the same message text.
    if authoritative_warning is not None:
        json_policy_override = authoritative_warning
    elif not policy_recheck_ok:
        json_policy_override = _POLICY_RECHECK_FAILED_MSG
    else:
        json_policy_override = None

    # ── S2 Step 5: Audit (lease already released — AFTER write, non-fatal) ─
    audit_before_value = _extract_default_model_value(result.prior_content)
    rel_snapshot = _relative_snapshot_path(result.snapshot_path, agent_dir)
    principal_id = _principal_id()

    record = RunRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        run_id=str(uuid.uuid4()),
        primitive=PRIMITIVE_MANAGE_SET_MODEL,
        status="applied",
        summary=f"manage set-model {agent_id}: default_model -> {model_id}"[:200],
        model="n/a",
        input_tokens=0,
        output_tokens=0,
        # cost_usd=None (omitted — not an LLM call, even though PRICING data
        # was consulted for gate (a); a populated cost_usd here would fold a
        # phantom non-spend entry into every fleet cost aggregation that
        # sums RunRecord.cost_usd — spec/55 M8 P2 prep finding, same
        # convention as govern's --set/--restore).
        agent_name=agent_id,
        extra={
            "principal_id": principal_id,
            "changed_fields": ["default_model"],
            "before": {"default_model": audit_before_value},
            "after": {"default_model": model_id},
            "snapshot_path": rel_snapshot,
            # AUTHORITATIVE (post-write recompute) — not the pre-confirm-wait
            # advisory preview value. None when Policy has no opinion.
            "policy_override_at_write": effective_model_authoritative,
            # Fix 1 (cross-family review finding): True only when the
            # value above came from the post-write recompute; False means
            # it fell back to the pre-confirm-wait preview value on a
            # recompute exception, so a copilot auditing this record
            # cannot mistake a stale-preview fallback for an authoritative
            # post-write read.
            "policy_recheck_ok": policy_recheck_ok,
            # NOTE: no "created" key — model.md is never create-absent for
            # set-model (spec/55 "Audit primitive" — absence is a resolve-
            # time refusal, not a create-and-fill path).
        },
    )

    audit_ok, audit_warnings = append_management_audit(record, agent_dir, agents_root)
    audit_status = "ok" if audit_ok else "warn"

    if use_json:
        _emit_json_success(
            agent_id,
            ["default_model"],
            before={"default_model": audit_before_value},
            after={"default_model": model_id},
            snapshot_path=rel_snapshot,
            audit_status=audit_status,
            policy_override=json_policy_override,
            policy_recheck_ok=policy_recheck_ok,
        )
    else:
        print(f"[{agent_id}] model.md updated.")
        if rel_snapshot is not None:
            print(f"  Snapshot: {rel_snapshot}")
        if authoritative_warning:
            print(f"  WARNING: {authoritative_warning}")
        elif not policy_recheck_ok:
            print(f"  WARNING: {_POLICY_RECHECK_FAILED_MSG}")
        if not audit_ok:
            for w in audit_warnings:
                print(f"  {w}")

    return 0


# ── --restore path (M3/#710 shape — restore does NOT re-run M9) ────────────


def _run_restore(
    *,
    agent_id: str,
    agent_dir: Path,
    agents_root: Path,
    model_path: Path,
    snapshot_id: str,
    use_json: bool,
    dry_run: bool,
    yes: bool,
) -> int:
    """``manage set-model <agent> --restore <snapshot-id>`` (#726, M3 shape).

    Runs the FULL S2 five-step routine through the SAME hoisted spine
    govern --restore uses. Restore does NOT re-run the M9 composition chain
    (maintainer ruling ``fallback-m9-bar`` / spec/55 "Surface" — a snapshot
    is prior content that already passed composition once; re-validating
    against TODAY's PRICING/registered-backends would make old snapshots
    unrestorable the moment a model is deprecated).

    Non-blocking advisory (spec/55 P2 prep finding), PRICING-ONLY (Fix 5
    comment correction — the code below checks ``get_model_rates(...) is
    None`` only, never ``find_backend_for_model``): if the restored
    snapshot's default_model is no longer in ``_costs.PRICING``, a
    non-fatal advisory note is surfaced (never a refusal) — symmetric with
    the policy-override WARN, so M9 gate (a)'s bypass-by-design on restore
    is never silently invisible. Gate (b) (backend resolution) has no
    matching restore-time advisory today — a snapshot whose model id no
    longer resolves to any registered backend restores silently. Advisory
    parity for gate (b) is a documented gap, not implemented here.
    """
    try:
        snapshot_src_path = resolve_snapshot_path(
            agent_dir, _SNAPSHOT_SUBDIR, snapshot_id
        )
    except ManageSnapshotNotFoundError as exc:
        if use_json:
            _emit_json_error(exc.error_type, str(exc))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        with snapshot_src_path.open("r", encoding="utf-8", newline="") as _f:
            snapshot_content = _f.read()
    except OSError as exc:
        reason = f"Could not read snapshot {snapshot_id!r}: {exc}"
        if use_json:
            _emit_json_error("read_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    snapshot_value = _extract_default_model_value(snapshot_content)
    if snapshot_value is None:
        reason = (
            f"Snapshot {snapshot_id!r} does not contain a parseable "
            "'## Default model' value — refusing to restore."
        )
        if use_json:
            _emit_json_error("restore_snapshot_invalid", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    # Non-blocking M9-bypass advisory (P2 prep finding) — computed but never
    # refused on.
    advisory: str | None = None
    try:
        if get_model_rates(snapshot_value) is None:
            advisory = (
                f"Snapshot's default_model {snapshot_value!r} is not currently "
                "in atomic_agents._costs.PRICING (restore does not re-run M9 — "
                "see spec/55). The restored agent may bill through fallback "
                "pricing until this is corrected."
            )
    except Exception:  # noqa: BLE001 -- advisory only, never blocks restore
        pass

    current_exists = model_path.exists()
    try:
        preview_current_content = _read_model_md(model_path) if current_exists else ""
    except OSError as exc:
        reason = f"Could not read model.md: {exc}"
        if use_json:
            _emit_json_error("read_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    current_value = (
        _extract_default_model_value(preview_current_content)
        if current_exists
        else None
    )

    if not use_json:
        print(f"\n[{agent_id}] restoring model.md from snapshot {snapshot_id}:")
        print(f"  default_model: {current_value!r} -> {snapshot_value!r}")
        if advisory:
            print(f"  ADVISORY: {advisory}")
        print()

    if dry_run:
        if use_json:
            _emit_json_success(
                agent_id,
                ["default_model"] if current_value != snapshot_value else [],
                before={"default_model": current_value},
                after={"default_model": snapshot_value},
                snapshot_path=None,
                audit_status="n/a",
                pricing_advisory=advisory,
                dry_run=True,
            )
        else:
            print("[dry-run] No changes written.")
        return 0

    confirm_exit = _require_confirmation(use_json, yes)
    if confirm_exit is not None:
        return confirm_exit

    def _read_base() -> tuple[str, bool]:
        file_existed = model_path.exists()
        content = _read_model_md(model_path) if file_existed else ""
        return content, file_existed

    def _apply_edit(_fresh_content: str) -> str:
        # Restore is an absolute overwrite — the fresh base is used only for
        # the pre-restore snapshot, never as an edit target (mirrors govern).
        return snapshot_content

    lock_backend = get_manage_lock_backend(agent_dir)

    try:
        result: ManagedWriteResult = run_managed_write(
            agent_dir=agent_dir,
            agent_id=agent_id,
            write_path=model_path,
            subdir=_SNAPSHOT_SUBDIR,
            read_base=_read_base,
            apply_edit=_apply_edit,
            lock_backend=lock_backend,
        )
    except (ManageAgentBusyError, ManageLockUnavailableError):
        raise
    except OSError as exc:
        reason = f"Failed to restore model.md: {exc}"
        if use_json:
            _emit_json_error("write_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- fail-closed structured refusal, mirrors govern
        reason = f"Unexpected error while restoring model.md: {exc}"
        if use_json:
            _emit_json_error("write_error", reason)
        else:
            print(f"Error: {reason}", file=sys.stderr)
        return 1

    fresh_current_value = (
        _extract_default_model_value(result.prior_content)
        if result.file_existed
        else None
    )
    changed_fields = ["default_model"] if fresh_current_value != snapshot_value else []
    pre_restore_snapshot_path = _relative_snapshot_path(result.snapshot_path, agent_dir)
    principal_id = _principal_id()

    record = RunRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        run_id=str(uuid.uuid4()),
        primitive=PRIMITIVE_MANAGE_RESTORE,
        status="applied",
        summary=f"manage set-model {agent_id}: restore from {snapshot_id}"[:200],
        model="n/a",
        input_tokens=0,
        output_tokens=0,
        # cost_usd=None — not an LLM call (matches govern --restore's convention).
        agent_name=agent_id,
        extra={
            "principal_id": principal_id,
            "changed_fields": changed_fields,
            "before": {"default_model": fresh_current_value},
            "after": {"default_model": snapshot_value},
            "snapshot_path": pre_restore_snapshot_path,
            "restored_from": snapshot_id,
        },
    )

    audit_ok, audit_warnings = append_management_audit(record, agent_dir, agents_root)
    audit_status = "ok" if audit_ok else "warn"

    if use_json:
        _emit_json_success(
            agent_id,
            changed_fields,
            before={"default_model": fresh_current_value},
            after={"default_model": snapshot_value},
            snapshot_path=pre_restore_snapshot_path,
            audit_status=audit_status,
            pricing_advisory=advisory,
        )
    else:
        print(f"[{agent_id}] model.md restored from snapshot {snapshot_id}.")
        if pre_restore_snapshot_path is not None:
            print(f"  Pre-restore snapshot: {pre_restore_snapshot_path}")
        else:
            print("  Created model.md (no prior file; no pre-restore snapshot).")
        if not audit_ok:
            for w in audit_warnings:
                print(f"  {w}")

    return 0
