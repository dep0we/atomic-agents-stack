"""PLAYBOOK.md loader and discovery (spec/50).

A playbook is a vault-native markdown artifact with YAML frontmatter +
an embedded YAML stage-block in the body. It reuses the single hardened
skill loader (atomic_agents/skills.py): the shared discovery scan
(skills._discover_entry_dirs) and the per-directory validator
(skills.validate_skill_manifest), both parameterized with
entry_point='PLAYBOOK.md', satisfying the playbook-vs-skill-layer ruling (OD4).
discover_playbooks layers its playbook-specific logic — the 'kind: playbook'
marker filter + embedded stage-block parse — ON TOP of that one shared scan;
it does NOT fork a second iterdir loop.

PLAYBOOK.md shape
-----------------
Frontmatter:
    name: my-playbook          (required, same charset as SKILL.md)
    description: "..."         (required)
    kind: playbook             (REQUIRED — distinguishes from SKILL.md)
    when_to_use: "..."         (optional)

Body (mandatory embedded fenced YAML block):

    ```yaml
    run_cap_usd: 10.00
    stages:
      - stage_id: draft-outline
        label: Draft an outline
        prompt: Write a structured outline for the given topic.
        rubric: The outline covers all major aspects with clear sections.
        is_gate: false
      - stage_id: write-report
        label: Write the report
        prompt: Write a comprehensive report based on the outline.
        rubric: The report is well-structured, accurate, and complete.
        is_gate: false
    ```

Stage field rules:
  - stage_id: REQUIRED. Must be non-empty string. Duplicate stage_ids are
    rejected at parse time (loader FAILS LOUD — no positional fallback).
  - label: REQUIRED. Human-readable stage name.
  - prompt: REQUIRED unless prompt_ref is set. The description / instructions
    for the stage.
  - prompt_ref: optional path relative to playbook dir for longer prompts.
  - rubric / rubric_ref: optional judge rubric; defaults to the prompt text.
  - model: optional per-stage model dial. For automated stages (is_gate: false),
    the declared model is passed as actor_model= to dispatch_sub_goal_as_outcome
    and from there as model_override= to agent.call() (#668). Policy enforce-mode
    get_effective_model supersedes the per-stage dial (spec/32 "fleet-config wins").
    For gate stages (is_gate: true), model: is rejected at parse time (hard validation
    error, symmetric with conflict_keys being gate-only): gate stages make no actor LLM
    call and suspend for a human decision, so model: there is semantically incoherent.
    At parse time a model absent from _costs.PRICING emits a non-blocking WARNING but
    the playbook loads successfully (the dispatch/runtime LLMBackend layer is
    authoritative for actual model resolution).
  - is_gate: false (default). True stages SUSPEND the run (PR2 #581): run()
    transitions the gate sub-goal to 'awaiting_decision' and returns a
    ConductorState carrying the pending GateDecision; resume() answers it.

KNOWN LIMITATION: GoalManager.for_goal() does NOT propagate a custom goal_backend
injected on the parent manager to the scoped child (tracked #656). A conductor
operator who pins a custom GoalBackend on the parent manager will silently lose
that override at the for_goal() boundary. Document this when constructing the
scoped backend in run().

KNOWN LIMITATION: Running a conductor session creates an addressed run-goal
under goals/. The 'atomic-agents export' command will return an error until
multi-goal export is implemented (#643).
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path

import frontmatter as _frontmatter
import yaml

from .._io import safe_resolve_under
from ..exceptions import PathTraversalError
from ..skills import _discover_entry_dirs, validate_skill_manifest
from .types import PlaybookManifest, StageSpec

_logger = logging.getLogger(__name__)

PLAYBOOK_ENTRY_POINT = "PLAYBOOK.md"
PLAYBOOK_KIND = "playbook"

# stage_id charset (H3): same allow-list + bound as the goal-id charset
# (goal/types._GOAL_ID_RE). A stage_id becomes the conductor sub-goal id AND the
# per-stage idempotency-key suffix (conductor:<run_id>:<stage_id>), so a
# separator/'..'/whitespace-bearing stage_id must be REFUSED at parse time —
# before any goal/ledger side effect — not stripped-and-accepted. Bounded to 64
# chars (well under NAME_MAX) so it is a safe path component.
_STAGE_ID_MAX_LEN = 64
_STAGE_ID_RE = re.compile(r"\A[a-z0-9_-]{1,%d}\Z" % _STAGE_ID_MAX_LEN)


def _resolve_ref(ref: str, playbook_dir: Path) -> Path:
    """Resolve a prompt_ref / rubric_ref to a contained, one-level-deep file path.

    Applies the framework's canonical-path containment invariant via
    :func:`_io.safe_resolve_under` (the same invariant
    ``skills.load_skill_referenced_file`` relies on), so playbooks — shareable
    vault-native artifacts, the same trust shape as shared skills — are protected
    rather than relying on a hand-rolled ``'..' in parts`` check. A bare ``'..' in
    parts`` check is escapable three ways (absolute refs like ``/etc/passwd``
    collapse to a path with no ``..`` in ``.parts``; a symlink inside the dir
    pointing outside is followed by ``is_file()``; multi-level refs contradict the
    "one level deep" contract), so instead this function:

      - reject ``..`` in the raw string (belt-and-suspenders with the resolve check);
      - reject any path separator — one level deep means a bare filename only
        (``reference.md``), never ``sub/dir/file.md``;
      - route the bare name through :func:`_io.safe_resolve_under`, which resolves
        symlinks then enforces containment under ``playbook_dir`` in one canonical-path
        invariant (catches the absolute-ref and symlink-escape cases the parts check missed).

    Returns the resolved absolute Path on success.
    Raises :exc:`PathTraversalError` on any containment / shape violation.
    """
    stripped = ref.strip()
    normalised = stripped.replace("\\", "/")
    if ".." in stripped or "/" in normalised:
        raise PathTraversalError(
            f"ref {ref!r} must be a bare one-level-deep filename "
            "(no '..', no path separator)",
            child=str(ref),
            root=str(playbook_dir),
        )
    # safe_resolve_under resolves symlinks + enforces containment; an absolute
    # ref (already rejected above by the separator check) would also be caught here.
    return safe_resolve_under(stripped, playbook_dir)


# ──────────────────────────────────────────────────────────────────
# Stage block validation


def _parse_stage_block(
    body: str, playbook_dir: Path, soft_warnings: list[str] | None = None
) -> tuple[list[StageSpec] | None, float | None, list[str]]:
    """Extract and validate the embedded YAML block from a PLAYBOOK.md body.

    The block is the first ```yaml ... ``` fenced code block in the body.

    Returns (stages, run_cap_usd, errors). On any hard error, stages is None.

    Non-blocking diagnostics (e.g. #668 unknown-model advisories) are appended to
    the caller-supplied ``soft_warnings`` list so they flow through the same
    structured (manifest, warnings) channel as every other parse diagnostic —
    not a side-channel logger only.
    """
    errors: list[str] = []
    if soft_warnings is None:
        soft_warnings = []

    # Extract fenced YAML block
    block_match = re.search(r"```yaml\s*\n(.*?)```", body, re.DOTALL)
    if not block_match:
        return (
            None,
            None,
            [
                "PLAYBOOK.md body must contain a fenced yaml block with 'run_cap_usd' and 'stages'"
            ],
        )

    block_text = block_match.group(1)
    try:
        block = yaml.safe_load(block_text)
    except yaml.YAMLError as exc:
        return None, None, [f"failed to parse embedded yaml block: {exc}"]

    if not isinstance(block, dict):
        return None, None, ["embedded yaml block must be a mapping (dict)"]

    # run_cap_usd
    run_cap_raw = block.get("run_cap_usd")
    if run_cap_raw is None:
        errors.append("embedded yaml block missing required 'run_cap_usd'")
        return None, None, errors
    try:
        run_cap_usd = float(run_cap_raw)
    except (TypeError, ValueError):
        return None, None, [f"run_cap_usd must be a number; got {run_cap_raw!r}"]
    # C1 — reject non-finite caps (`.inf` / `.nan` / `1e999`). Both `nan <= 0` and
    # `inf <= 0` are False, so a bare `<= 0` gate would ACCEPT a non-finite cap and
    # silently disable the run-level ceiling (a cost-cap bypass). Require finite > 0.
    if not math.isfinite(run_cap_usd) or run_cap_usd <= 0:
        return (
            None,
            None,
            [f"run_cap_usd must be a finite number > 0; got {run_cap_raw!r}"],
        )

    # stages
    stages_raw = block.get("stages")
    if not stages_raw:
        return (
            None,
            run_cap_usd,
            ["embedded yaml block must have a non-empty 'stages' list"],
        )
    if not isinstance(stages_raw, list):
        return None, run_cap_usd, ["stages must be a list"]

    seen_stage_ids: set[str] = set()
    duplicate_ids: list[str] = []
    stages: list[StageSpec] = []

    for i, raw in enumerate(stages_raw):
        if not isinstance(raw, dict):
            errors.append(f"stages[{i}] must be a dict")
            continue

        # stage_id REQUIRED — fail loud, no positional index fallback
        stage_id = raw.get("stage_id")
        if not stage_id or not isinstance(stage_id, str) or not stage_id.strip():
            errors.append(
                f"stages[{i}] missing required 'stage_id'. "
                "Every stage MUST have a non-empty stage_id for idempotency key generation "
                "(key format: conductor:<conductor_run_id>:<stage_id>). "
                "No positional fallback is used — an absent stage_id is rejected at parse time."
            )
            continue
        stage_id = stage_id.strip()

        # H3 — validate the stage_id charset BEFORE any side effect. A separator-
        # bearing ("a/b"), '..'-bearing, whitespace, or over-length stage_id would
        # otherwise pass (only stripped) and later poison the conductor sub-goal id
        # / idempotency key (conductor:<run_id>:<stage_id>) — refused here so a
        # malformed playbook never creates a goal or emits a ledger event.
        if not _STAGE_ID_RE.match(stage_id):
            errors.append(
                f"stages[{i}] stage_id={stage_id!r} is invalid. A stage_id must match "
                f"[a-z0-9_-] and be 1–{_STAGE_ID_MAX_LEN} chars (no path separators, "
                "no '..', no whitespace) — it becomes the conductor sub-goal id and "
                "the idempotency-key suffix conductor:<conductor_run_id>:<stage_id>."
            )
            continue

        if stage_id in seen_stage_ids:
            duplicate_ids.append(stage_id)
            continue
        seen_stage_ids.add(stage_id)

        # label REQUIRED
        label = raw.get("label", "")
        if not label or not isinstance(label, str):
            errors.append(
                f"stages[{i}] (stage_id={stage_id!r}) missing required 'label'"
            )
            continue

        # prompt / prompt_ref — at least one required
        prompt_raw = raw.get("prompt", "")
        prompt_ref = raw.get("prompt_ref")

        if prompt_ref and isinstance(prompt_ref, str) and prompt_ref.strip():
            # Resolve prompt_ref through the hardened canonical-path invariant
            # (one-level deep, contained — catches absolute / symlink / '..').
            try:
                ref_path = _resolve_ref(prompt_ref, playbook_dir)
            except PathTraversalError as exc:
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) prompt_ref rejected: {exc}"
                )
                continue
            if not ref_path.is_file():
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) prompt_ref not found: {prompt_ref!r}"
                )
                continue
            prompt = ref_path.read_text(encoding="utf-8")
        elif prompt_raw and isinstance(prompt_raw, str):
            prompt = prompt_raw.strip()
            prompt_ref = None
        else:
            errors.append(
                f"stages[{i}] (stage_id={stage_id!r}) requires 'prompt' or 'prompt_ref'"
            )
            continue

        if not prompt.strip():
            errors.append(f"stages[{i}] (stage_id={stage_id!r}) prompt is empty")
            continue

        # rubric / rubric_ref — optional
        rubric_raw = raw.get("rubric")
        rubric_ref = raw.get("rubric_ref")
        rubric: str | None = None

        if rubric_ref and isinstance(rubric_ref, str) and rubric_ref.strip():
            try:
                ref_path = _resolve_ref(rubric_ref, playbook_dir)
            except PathTraversalError as exc:
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) rubric_ref rejected: {exc}"
                )
                continue
            if not ref_path.is_file():
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) rubric_ref not found: {rubric_ref!r}"
                )
                continue
            rubric = ref_path.read_text(encoding="utf-8")
        elif rubric_raw and isinstance(rubric_raw, str):
            rubric = rubric_raw.strip() or None
            rubric_ref = None

        model_raw = raw.get("model")
        # Normalize empty/whitespace-only-after-strip to None at this single parse
        # site so all three downstream sites (gate-rejection `is not None`,
        # unknown-model `is not None`, fingerprint `is not None`, audit `is not None`)
        # agree on the field state. A blank `model: "   "` is genuinely absent, not a
        # zero-length model name that would trip a spurious PRICING warning and split
        # the fingerprint/audit gates.
        _model_stripped = str(model_raw).strip() if isinstance(model_raw, str) else ""
        model = _model_stripped or None
        # A model was "declared" if a non-blank string OR any non-string value was
        # provided (a blank/whitespace-only string is genuinely absent). The gate
        # rejection below keys on THIS, not on `model is not None`, so a non-string
        # model: on a gate stage (e.g. `model: 123`, coerced to None above) is still
        # rejected — otherwise it would slip past the `model is not None` gate and
        # parse, despite C10 forbidding model: on gate stages (#668 Codex review).
        _model_declared = model is not None or (
            model_raw is not None and not isinstance(model_raw, str)
        )

        is_gate_raw = raw.get("is_gate", False)
        # YAML may give us a string "false" or bool False
        if isinstance(is_gate_raw, bool):
            is_gate = is_gate_raw
        elif isinstance(is_gate_raw, str):
            is_gate = is_gate_raw.lower() in ("true", "yes", "1")
        else:
            is_gate = bool(is_gate_raw)

        # #668 — gate-model rejection (symmetric with conflict_keys being gate-only).
        # Gate stages make no actor LLM call (they suspend for a human decision), so
        # model: on a gate stage is semantically incoherent — hard parse error.
        # Placement: AFTER is_gate is resolved (uses the resolved bool, not is_gate_raw
        # — a raw string "false" would be truthy in Python, silently rejecting automated
        # stages carrying model:, which is exactly the feature we are building).
        if is_gate and _model_declared:
            errors.append(
                f"stages[{i}] (stage_id={stage_id!r}) 'model:' is only valid on "
                "automated (is_gate=false) stages — gate stages make no actor LLM call "
                "and suspend for a human decision; the model: field has no effect here. "
                "Strip it from this stage."
            )
            continue

        # #668 P2 — a non-string model: on an AUTOMATED stage is coerced to None
        # above. WARN (do not block) so the silent-drop path gives the same legible
        # parse-time feedback as the unknown-model warning below (gate stages with a
        # non-string model already hard-errored above, so this only reaches automated
        # stages). The WARN-not-block ruling's intent is legible feedback.
        if model_raw is not None and not isinstance(model_raw, str):
            soft_warnings.append(
                f"stages[{i}] (stage_id={stage_id!r}) 'model:' must be a string; "
                f"ignoring non-string value {model_raw!r} (treated as absent)."
            )

        # #668 — WARN (not block) when a declared model is absent from _costs.PRICING.
        # Parse-time legible feedback that flows through the structured
        # (manifest, warnings) channel (soft_warnings), so a programmatic caller of
        # validate_playbook_manifest sees it — not a logger-only side channel. The
        # dispatch/runtime LLMBackend layer stays authoritative for actual model
        # resolution. Lazy import inside the function (per project convention —
        # coordinator.py, agent.py) to avoid bootstrap-cycle risk from a new
        # cross-module dependency at module level.
        if model is not None:
            from .._costs import PRICING  # noqa: PLC0415

            if model not in PRICING:
                soft_warnings.append(
                    f"stages[{i}] (stage_id={stage_id!r}) declares model={model!r} which "
                    "is not in _costs.PRICING — the dispatch/runtime LLMBackend layer is "
                    "authoritative; the stage will run if the backend resolves the model."
                )

        # PR2 (#581): options field — structured choices for gate stages.
        # gate-stage-markdown-schema ruling: options is is_gate-only; silently
        # discarded for non-gate stages. Validated when present.
        options_raw = raw.get("options", [])
        options: tuple[str, ...] = ()
        if options_raw:
            if not isinstance(options_raw, list):
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) 'options' must be a list "
                    f"of strings; got {type(options_raw).__name__!r}"
                )
                continue
            bad = [o for o in options_raw if not isinstance(o, str) or not o.strip()]
            if bad:
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) 'options' must be a list "
                    f"of non-empty strings; found invalid entries: {bad!r}"
                )
                continue
            if is_gate:
                options = tuple(str(o).strip() for o in options_raw)
            # For non-gate stages, silently discard non-empty options (no error,
            # the stage is valid; the options are simply irrelevant).

        # PR3 (#582): conflict_keys — resources a gate stage holds while suspended.
        # Only valid on is_gate stages; rejected (hard error) for non-gate stages.
        # Validated: must be a list of non-empty strings, each max 128 chars, no
        # null bytes, no path separators ('/', '\\', os.sep), no '.' or '..'.
        conflict_keys_raw = raw.get("conflict_keys", [])
        conflict_keys: tuple[str, ...] = ()
        if conflict_keys_raw:
            if not isinstance(conflict_keys_raw, list):
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) 'conflict_keys' must be a "
                    f"list of strings; got {type(conflict_keys_raw).__name__!r}"
                )
                continue
            if not is_gate:
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) 'conflict_keys' is only "
                    "valid on is_gate=true stages"
                )
                continue
            bad_keys = []
            for ck in conflict_keys_raw:
                if (
                    not isinstance(ck, str)
                    or not ck.strip()
                    or len(ck) > 128
                    or "\x00" in ck
                    or "/" in ck
                    or "\\" in ck
                    or ck.strip() in (".", "..")
                ):
                    bad_keys.append(ck)
            if bad_keys:
                errors.append(
                    f"stages[{i}] (stage_id={stage_id!r}) 'conflict_keys' entries "
                    f"must be non-empty strings, max 128 chars, no null bytes, no path "
                    f"separators; invalid: {bad_keys!r}"
                )
                continue
            conflict_keys = tuple(str(ck).strip() for ck in conflict_keys_raw)

        stages.append(
            StageSpec(
                stage_id=stage_id,
                label=label,
                prompt=prompt,
                prompt_ref=prompt_ref,
                rubric=rubric,
                rubric_ref=rubric_ref,
                model=model,
                is_gate=is_gate,
                options=options,
                conflict_keys=conflict_keys,
            )
        )

    if duplicate_ids:
        errors.append(
            f"duplicate stage_id values detected: {duplicate_ids}. "
            "Each stage_id must be unique — duplicates produce identical idempotency keys "
            "(conductor:<run_id>:<stage_id>), causing later stages to short-circuit as COMPLETED."
        )
        return None, run_cap_usd, errors

    if errors:
        return None, run_cap_usd, errors

    if not stages:
        return None, run_cap_usd, ["no valid stages parsed from stages list"]

    return stages, run_cap_usd, []


# ──────────────────────────────────────────────────────────────────
# Playbook manifest validation


def validate_playbook_manifest(
    playbook_dir: Path,
) -> tuple[PlaybookManifest | None, list[str]]:
    """Parse and validate PLAYBOOK.md in ``playbook_dir``.

    Reuses the single hardened skill loader (skills.validate_skill_manifest)
    with entry_point='PLAYBOOK.md', then performs playbook-specific validation:
      - 'kind: playbook' frontmatter marker (REQUIRED)
      - embedded yaml stage block with run_cap_usd + stages
      - per-stage validation (REQUIRED stage_id, no duplicates)

    Returns (manifest, warnings). Manifest is None on hard error.
    """
    # Use the single hardened skill loader
    skill_manifest, warnings = validate_skill_manifest(
        playbook_dir, entry_point=PLAYBOOK_ENTRY_POINT
    )
    if skill_manifest is None:
        return None, warnings

    # Additional: check 'kind: playbook' frontmatter marker
    playbook_md = playbook_dir / PLAYBOOK_ENTRY_POINT
    try:
        parsed = _frontmatter.load(playbook_md)
    except Exception as exc:
        return None, warnings + [f"failed to re-parse {playbook_md}: {exc}"]

    kind = parsed.metadata.get("kind", "")
    if kind != PLAYBOOK_KIND:
        return None, warnings + [
            f"PLAYBOOK.md in {playbook_dir.name!r} missing 'kind: playbook' frontmatter marker "
            f"(got kind={kind!r}). A PLAYBOOK.md without 'kind: playbook' is not discoverable "
            "by discover_playbooks()."
        ]

    # Parse the embedded stage block. soft_warnings collects non-blocking
    # diagnostics (#668 unknown-model advisories) so they surface in the same
    # returned warnings list as skill-loader warnings — one structured channel.
    body = parsed.content or ""
    soft_warnings: list[str] = []
    stages, run_cap_usd, stage_errors = _parse_stage_block(
        body, playbook_dir, soft_warnings=soft_warnings
    )
    if stage_errors:
        return None, warnings + stage_errors
    if stages is None or run_cap_usd is None:
        return None, warnings + [
            "no stages or run_cap_usd parsed from embedded yaml block"
        ]

    return PlaybookManifest(
        name=skill_manifest.name,
        description=skill_manifest.description,
        when_to_use=skill_manifest.when_to_use,
        run_cap_usd=run_cap_usd,
        stages=stages,
        playbook_dir=playbook_dir,
        playbook_md_path=playbook_md,
    ), warnings + soft_warnings


# ──────────────────────────────────────────────────────────────────
# Discovery


def discover_playbooks(agent_root: Path) -> list[PlaybookManifest]:
    """Scan ``<agent_root>/skills/*/PLAYBOOK.md`` and return parsed manifests.

    Consumes the SINGLE shared hardened discovery scan
    (``skills._discover_entry_dirs`` with entry_point='PLAYBOOK.md') — the same
    loop ``skills.discover_skills`` uses — then layers playbook-specific logic on
    top: per-directory validation via ``validate_playbook_manifest`` (which calls
    the shared ``validate_skill_manifest``, filters by the 'kind: playbook'
    frontmatter marker, and parses the embedded stage block). There is NOT a
    second iterdir loop here (OD4: reuse the single hardened scan, don't fork it).

    A SKILL.md-only directory (no PLAYBOOK.md) is silently skipped by the shared
    scan. A directory with PLAYBOOK.md but no 'kind: playbook' marker logs a
    warning and is skipped (won't appear in the returned list).

    T15 trust note: the shared scan (``skills._discover_entry_dirs``) follows
    directory symlinks under ``<agent_root>/skills/`` — an out-of-vault symlinked
    playbook directory IS discovered and run. This is the SAME operator-trust model
    as skill discovery (TENSIONS T15: the agent folder is the trust boundary; what
    the operator places under it, including symlinks, is theirs to vouch for) and is
    deliberately left unchanged by this PR. The per-stage ``prompt_ref``/``rubric_ref``
    resolution is still canonical-path-contained (see ``_resolve_ref``); only the
    top-level dir-discovery follows symlinks.

    Returns an empty list if ``<agent_root>/skills/`` does not exist.
    """
    skills_dir = agent_root / "skills"

    playbooks: list[PlaybookManifest] = []
    for skill_subdir in _discover_entry_dirs(skills_dir, PLAYBOOK_ENTRY_POINT):
        manifest, warnings = validate_playbook_manifest(skill_subdir)
        for w in warnings:
            _logger.warning("playbooks: %s", w)
        if manifest is None:
            _logger.warning(
                "playbooks: skipping %s due to validation error: %s",
                skill_subdir.name,
                warnings[0] if warnings else "unknown",
            )
            continue
        playbooks.append(manifest)
        _logger.debug(
            "playbooks: loaded %r (%d stages, run_cap_usd=%.2f)",
            manifest.name,
            len(manifest.stages),
            manifest.run_cap_usd,
        )

    return playbooks
