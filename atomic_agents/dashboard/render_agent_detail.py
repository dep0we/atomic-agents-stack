"""Per-Agent Detail Cockpit renderer (spec/57, #637 + #684).

render_agent_detail() replaces the content of <agent>/dashboard.html with the
Fable "Briefing" B7 cockpit layout:
  - Banner (name/id/model/status/last-run/health-0-100)
  - Governance block (spec/51 — all five states)
  - Telemetry tabs via compose_agent_detail() (agent-tab slot)
  - Dreaming tab restored (#684) — real manifest/report fields, observe-only

URL + back-compat (MUST 1 + 2): the output path is still <agent>/dashboard.html.
render_all()["per_agent"], /agents/<name>, and existing consumers keep working.

Also generates _dashboard/agent-detail.html — the static resolver that the Fleet
Monitor links to via ?agent=<id> (works file:// AND served).

Pure-compute: zero new LLM spend (MUST 10). No LLMBackend is constructed on
any code path through this module.
"""

from __future__ import annotations

import html as _html
import json as _json
import logging
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

from ..core_api import atomic_write
from ._shared import (
    CSS as _SHARED_CSS,
    _CSP as _SHARED_CSP,
    eval_score_display as _eval_score_display,
    eval_score_fmt as _eval_score_fmt,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# Detail-specific CSS (B7 cockpit palette — matches variant-B7-agent-detail.html)

_DETAIL_CSS = """
/* ── B7 Agent Detail Cockpit ───────────────────────────────────── */

/* Banner */
.agent-banner {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  padding: 22px 28px; margin-bottom: 20px;
}
.banner-row1 {
  display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  margin-bottom: 8px;
}
.banner-name { font-size: 24px; font-weight: 700; }
.banner-id { font-size: 13px; color: var(--muted); font-family: ui-monospace, "SF Mono", monospace; }
.banner-pills { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
.banner-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 12px 20px;
}
.banner-kv { }
.banner-kv .bk { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--muted); margin-bottom: 2px; }
.banner-kv .bv { font-size: 16px; font-weight: 650; font-variant-numeric: tabular-nums; }
.bv.status-ok { color: var(--good); }
.bv.status-warn { color: var(--warn); }
.bv.status-error { color: var(--error); }
.bv.status-stale { color: var(--muted); }
.bv.health-green { color: var(--good); }
.bv.health-amber { color: var(--warn); }
.bv.health-red { color: var(--error); }
.bv.health-unknown { color: var(--muted); }

/* Status pill (larger than normal) */
.status-pill {
  display: inline-flex; align-items: center; gap: 5px; padding: 3px 12px;
  border-radius: 18px; font-size: 12px; font-weight: 700;
}
.status-pill .sdot {
  width: 7px; height: 7px; border-radius: 50%; flex: none;
}
.status-pill.ok { background: rgba(152, 195, 121, 0.12); color: var(--good);
  border: 1px solid rgba(152, 195, 121, 0.3); }
.status-pill.ok .sdot { background: var(--good); }
.status-pill.warn { background: rgba(209, 154, 102, 0.12); color: var(--warn);
  border: 1px solid rgba(209, 154, 102, 0.3); }
.status-pill.warn .sdot { background: var(--warn); }
.status-pill.error { background: rgba(224, 108, 117, 0.12); color: var(--error);
  border: 1px solid rgba(224, 108, 117, 0.3); }
.status-pill.error .sdot { background: var(--error); }
.status-pill.stale { background: rgba(138, 150, 163, 0.1); color: var(--muted);
  border: 1px solid var(--border); }
.status-pill.stale .sdot { background: var(--muted); }

/* Governance block */
.gov-block {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 24px; margin-bottom: 20px;
}
.gov-block-title {
  font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.09em;
  color: var(--muted); margin-bottom: 12px;
}
.gov-state-absent { color: var(--warn); font-size: 13px; font-style: italic; }
.gov-state-invalid { color: var(--error); font-size: 13px; }
.gov-state-unreadable { color: var(--error); font-size: 13px; }
.gov-state-no-block { color: var(--muted); font-size: 13px; font-style: italic; }
.gov-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px 20px; margin-bottom: 12px;
}
.gov-kv .gk { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--muted); margin-bottom: 2px; }
.gov-kv .gv { font-size: 13px; font-weight: 500; }
.gov-kv .gv.missing { color: var(--muted); font-style: italic; }
.gov-actions { margin-top: 12px; }
.gov-actions-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--muted); margin-bottom: 6px; }
.action-chip {
  display: inline-block; padding: 2px 9px; border-radius: 10px; font-size: 11px;
  margin: 2px 3px 2px 0;
}
.action-chip.forbidden { background: rgba(224, 108, 117, 0.1);
  color: var(--error); border: 1px solid rgba(224, 108, 117, 0.25); }
.parse-error-note { color: var(--error); font-size: 11px; margin-top: 6px; font-family: monospace; }

/* Standalone Recommendations zone (B7 — between governance block and tabs) */
.zone-label {
  font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.09em;
  color: var(--accent); margin-bottom: 10px;
}
.rec-zone { margin-bottom: 20px; }
.rec-empty-state {
  color: var(--muted); font-style: italic; font-size: 13px; padding: 10px 0;
}
.rec-card {
  background: linear-gradient(135deg, rgba(78, 201, 176, 0.06), rgba(97, 175, 239, 0.04));
  border: 1px solid rgba(78, 201, 176, 0.2); border-radius: 10px; padding: 14px 18px;
  margin-bottom: 10px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}
.rec-body { flex: 1; min-width: 200px; }
.rec-title { font-size: 13px; font-weight: 600; margin-bottom: 3px; }
.rec-rationale { font-size: 12px; color: var(--muted); }
.rec-tags { display: flex; gap: 5px; flex-wrap: wrap; }
.rec-tag {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 8px; font-size: 11px; font-weight: 600;
}
.rec-tag.cost { background: rgba(152, 195, 121, 0.12); color: var(--good);
  border: 1px solid rgba(152, 195, 121, 0.28); }
.rec-tag.advisory { background: rgba(209, 154, 102, 0.1); color: var(--warn);
  border: 1px solid rgba(209, 154, 102, 0.26); }
/* apply-rec rec-id badge (spec/55 #727) — muted monospace, savings_cost cards only */
.rec-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10px;
  color: var(--muted); opacity: .8; margin-left: 6px; }

/* Telemetry tabs */
.detail-tabs {
  display: flex; gap: 0; flex-wrap: wrap;
  border-bottom: 1px solid var(--border); margin-bottom: 24px;
}
.detail-tabs .dtab {
  display: inline-block; padding: 8px 18px; font-size: 13px; font-weight: 500;
  color: var(--muted); text-decoration: none; cursor: pointer; border: none;
  background: transparent; border-bottom: 2px solid transparent;
  margin-bottom: -1px; outline: none;
}
.detail-tabs .dtab:hover { color: var(--text); border-bottom-color: var(--border); }
.detail-tabs .dtab.active { color: var(--accent); border-bottom-color: var(--accent); }
.detail-tabs .dtab .diamond { color: var(--accent); font-size: 10px; vertical-align: 1px; }

.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* Dreaming tab */
.dream-panel {
  border: 1px solid rgba(78, 201, 176, 0.3);
  background: linear-gradient(135deg, rgba(78, 201, 176, 0.05), rgba(42, 50, 61, 0));
  border-radius: 10px; padding: 20px 24px; margin-bottom: 16px;
}
.dream-title { font-size: 14px; font-weight: 650; color: var(--accent); margin-bottom: 14px; }
.dream-stats {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 10px 20px; margin-bottom: 16px;
}
.dream-stat .dk { font-size: 10px; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--muted); margin-bottom: 2px; }
.dream-stat .dv { font-size: 18px; font-weight: 700; }
.dream-stat .dv.applied { color: var(--good); }
.dream-stat .dv.failed { color: var(--error); }
.dream-stat .dv.pending { color: var(--warn); }
.dream-status-pill {
  display: inline-block; padding: 1px 7px; border-radius: 8px; font-size: 11px;
  font-weight: 600;
}
.dream-status-pill.completed { background: rgba(152, 195, 121, 0.12); color: var(--good); }
.dream-status-pill.failed { background: rgba(224, 108, 117, 0.12); color: var(--error); }
.dream-status-pill.running { background: rgba(78, 201, 176, 0.12); color: var(--accent); }
.dream-status-pill.pending { background: rgba(209, 154, 102, 0.1); color: var(--warn); }
.dream-status-pill.canceled { background: rgba(138, 150, 163, 0.1); color: var(--muted); }

/* Tab content panels */
.tab-section { margin-bottom: 24px; }
.tab-section h3 {
  font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin-bottom: 14px; padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}
.metrics-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 12px; margin-bottom: 16px;
}
.metric-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 16px;
}
.metric-card .mk { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); margin-bottom: 4px; }
.metric-card .mv { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.metric-card .mv.green { color: var(--good); }
.metric-card .mv.amber { color: var(--warn); }
.metric-card .mv.red { color: var(--error); }
.metric-card .mv.muted { color: var(--muted); }

/* Axis score cards */
.axis-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
@media (max-width: 700px) { .axis-cards { grid-template-columns: 1fr; } }
.axis-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 18px;
}
.axis-card .ax-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin-bottom: 6px; }
.axis-card .ax-score { font-size: 32px; font-weight: 800; font-variant-numeric: tabular-nums; }
.axis-card .ax-score.green { color: var(--good); }
.axis-card .ax-score.amber { color: var(--warn); }
.axis-card .ax-score.red { color: var(--error); }
.axis-card .ax-score.unknown { color: var(--muted); }
.axis-card .ax-band { font-size: 11px; color: var(--muted); margin-top: 4px; }

.degraded-banner {
  margin-bottom: 16px; padding: 10px 16px; border-radius: 8px;
  background: rgba(209, 154, 102, 0.08); border: 1px solid rgba(209, 154, 102, 0.3);
  font-size: 13px; color: var(--warn);
}
.tab-degraded { color: var(--muted); font-style: italic; font-size: 13px; padding: 24px 0; }
.empty-tab { color: var(--muted); font-style: italic; font-size: 13px; padding: 24px 0; }

/* Report summary block */
.report-summary {
  background: rgba(78, 201, 176, 0.04); border: 1px solid rgba(78, 201, 176, 0.15);
  border-radius: 8px; padding: 14px 18px; margin-bottom: 16px;
  font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
}

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.mono { font-family: ui-monospace, "SF Mono", monospace; }
"""


# ──────────────────────────────────────────────────────────────────
# Governance-state rendering


def _governance_state(has_gov: bool, gov) -> str:
    """Detect the governance state (one of five, spec/51) and return the state string.

    The five spec/51 states:
      ABSENT           — governance.md not found (has_governance=False)
      PRESENT_NO_BLOCK — governance.md exists but no parseable YAML block
                         (has_governance=True, governance=None)
      PRESENT_INVALID  — YAML block exists but has parse errors
                         (has_governance=True, governance is not None,
                          parse_errors non-empty)
      PRESENT_INCOMPLETE — parsed OK but owner is None
                           (has_governance=True, governance is not None,
                            parse_errors empty, owner=None)
      PRESENT_VALID    — fully parsed with owner present
    """
    if not has_gov:
        return "ABSENT"
    if gov is None:
        # governance.md exists but has no parseable governance: YAML block
        return "PRESENT_NO_BLOCK"
    if gov.parse_errors:
        return "PRESENT_INVALID"
    owner = getattr(gov, "owner", None)
    if owner is None:
        return "PRESENT_INCOMPLETE"
    return "PRESENT_VALID"


def _render_governance_block(agent_ref) -> str:
    """Render the spec/51 governance block, surfacing all five states honestly."""
    if agent_ref is None:
        return (
            '<div class="gov-block">'
            '<div class="gov-block-title">Governance</div>'
            '<div class="gov-state-absent">governance.md absent — '
            "no governance record for this agent</div>"
            "</div>"
        )

    has_gov = getattr(agent_ref, "has_governance", False)
    gov = getattr(agent_ref, "governance", None)

    state = _governance_state(has_gov, gov)

    inner = ""
    if state == "ABSENT":
        inner = (
            '<div class="gov-state-absent">ABSENT — '
            "governance.md not found. Add a governance.md to register this agent.</div>"
        )
    elif state == "PRESENT_INVALID":
        errors_html = "".join(
            f'<div class="parse-error-note">{_html.escape(str(e))}</div>'
            for e in (gov.parse_errors if gov else [])
        )
        inner = (
            '<div class="gov-state-invalid">PRESENT_INVALID — '
            "governance.md exists but has parse errors:</div>" + errors_html
        )
    elif state == "PRESENT_NO_BLOCK":
        inner = (
            '<div class="gov-state-no-block">PRESENT_NO_BLOCK — '
            "governance.md exists but has no parseable governance: YAML block.</div>"
        )
    elif state == "PRESENT_INCOMPLETE":
        inner = (
            '<div class="gov-state-incomplete">PRESENT_INCOMPLETE — '
            "governance.md parsed but owner is missing. "
            "Add an owner field to complete governance.</div>"
        )
    else:
        # PRESENT_VALID
        def _v(val, fallback="—") -> str:
            if val is None:
                return f'<span class="missing">{fallback}</span>'
            return _html.escape(str(val))

        review = gov.review if gov else None
        risk = gov.risk if gov else None
        actions = gov.actions if gov else None

        # FIX 5: Include all parsed governance fields — next review, reviewed by, sources.
        gov_grid = (
            '<div class="gov-grid">'
            f'<div class="gov-kv"><div class="gk">Owner</div>'
            f'<div class="gv">{_v(gov.owner if gov else None)}</div></div>'
            f'<div class="gov-kv"><div class="gk">Permission</div>'
            f'<div class="gv">{_v(gov.permission_tier if gov else None)}</div></div>'
            f'<div class="gov-kv"><div class="gk">Customer data</div>'
            f'<div class="gv">{_v(gov.customer_data if gov else None)}</div></div>'
            f'<div class="gov-kv"><div class="gk">Writes SoR</div>'
            f'<div class="gv">{_v(gov.writes_sor if gov else None)}</div></div>'
            f'<div class="gov-kv"><div class="gk">Lifecycle</div>'
            f'<div class="gv">{_v(gov.lifecycle_status if gov else None)}</div></div>'
        )
        if risk:
            gov_grid += (
                f'<div class="gov-kv"><div class="gk">Risk</div>'
                f'<div class="gv">{_v(risk.level)}</div></div>'
            )
        if review:
            reviewed_at = getattr(review, "reviewed_at", None)
            reviewer = getattr(review, "reviewer", None)
            approved_by = getattr(review, "approved_by", None)
            gov_grid += (
                f'<div class="gov-kv"><div class="gk">Last review</div>'
                f'<div class="gv">{_v(reviewed_at)}</div></div>'
            )
            # FIX 5: Reviewed by (reviewer or approved_by)
            reviewed_by = reviewer or approved_by
            gov_grid += (
                f'<div class="gov-kv"><div class="gk">Reviewed by</div>'
                f'<div class="gv">{_v(reviewed_by)}</div></div>'
            )
        # FIX 5: Sources — use getattr so stub objects in tests (no sources attr) still work
        sources = getattr(gov, "sources", None) if gov else None
        if sources is not None:
            primary = getattr(sources, "primary", []) or []
            secondary = getattr(sources, "secondary", []) or []
            all_srcs = list(primary) + list(secondary)
            sources_str = ", ".join(str(s) for s in all_srcs[:5]) if all_srcs else None
            gov_grid += (
                f'<div class="gov-kv"><div class="gk">Sources</div>'
                f'<div class="gv">{_v(sources_str)}</div></div>'
            )
        gov_grid += "</div>"

        # Forbidden actions chips
        forbidden_chips = ""
        if actions and actions.forbidden:
            chips = "".join(
                f'<span class="action-chip forbidden">{_html.escape(a)}</span>'
                for a in actions.forbidden
            )
            forbidden_chips = (
                '<div class="gov-actions">'
                '<div class="gov-actions-label">Forbidden actions</div>'
                + chips
                + "</div>"
            )
        inner = gov_grid + forbidden_chips

    return (
        '<div class="gov-block">'
        '<div class="gov-block-title">Governance'
        f' <span style="color: var(--muted); font-weight: normal;">({state})</span>'
        "</div>" + inner + "</div>"
    )


# ──────────────────────────────────────────────────────────────────
# Helper: model pill


def _model_pill_class(model: str) -> str:
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    if m.startswith("gpt"):
        return "gpt"
    if "kimi" in m or m.startswith("moonshot"):
        return "kimi"
    if m.startswith("local") or "qwen" in m or "llama" in m:
        return "local"
    return "opus"


def _short_model_name(model: str) -> str:
    if "opus" in model.lower():
        return "Opus"
    if "sonnet" in model.lower():
        return "Sonnet"
    if "haiku" in model.lower():
        return "Haiku"
    if model.startswith("gpt-5-mini"):
        return "GPT-5 mini"
    if model.startswith("gpt-5"):
        return "GPT-5"
    return model.split("/")[-1][:24]


# ──────────────────────────────────────────────────────────────────
# Helper: recommendations rendering (spec/52 §17.3 layered tags — MUST 7)


def _render_detail_recommendations(recs: list | None, agent_id: str) -> str:
    """Render the per-agent standalone recommendations zone (spec/52 §17.3, MUST 7).

    Always renders a zone — empty-state placeholder when the engine produces no
    recommendations for this agent (B7 design contract: always visible, not absent).

    Zone label matches the B7 dark-teal `zone-label` style.
    """
    agent_recs: list = []
    if recs:
        agent_recs = [r for r in recs if getattr(r, "agent", None) == agent_id]

    zone_label = '<div class="zone-label">&#9670; Recommendations</div>'

    if not agent_recs:
        empty_state = '<div class="rec-empty-state">No recommendations right now.</div>'
        return '<div class="rec-zone">' + zone_label + empty_state + "</div>"

    cards = []
    for rec in agent_recs:
        kind = getattr(rec, "kind", "")
        rationale = getattr(rec, "rationale", "") or ""
        pts_delta = getattr(rec, "projected_points_delta", None)
        usd_delta = getattr(rec, "projected_usd_delta", None)

        # Build title
        rec_id_html = ""
        if kind == "savings_cost":
            curr_model = getattr(rec, "current_model", None) or ""
            cand_model = getattr(rec, "candidate_model", None) or ""
            title = f"Swap {_html.escape(_short_model_name(curr_model))} → {_html.escape(_short_model_name(cand_model))}"
            if usd_delta is not None and usd_delta < 0:
                title += f" (${-usd_delta:.2f}/mo saved)"
            # rec-id badge (spec/55 #727) — savings_cost only, the only kind
            # apply-rec can act on. Lazy import for surface parity with the
            # console's own _render_recommendations badge.
            from .. import advisor as _advisor  # noqa: PLC0415

            rec_id = _advisor.canonical_rec_id(
                getattr(rec, "agent", ""), kind, getattr(rec, "candidate_model", None)
            )
            rec_id_html = (
                f'<span class="rec-id" title="apply with: manage apply-rec '
                f'{rec_id}">{rec_id}</span>'
            )
        elif kind == "governance":
            title = "Governance gap"
        elif kind == "quality_report":
            title = "Quality advisory"
        else:
            title = _html.escape(kind)

        # Layered rec tags: savings_cost → "→ Cost" (or "→ Cost · +N pts" if non-zero);
        # governance/quality_report → "advisory · not scored".
        # MUST 7: governance recs must NOT get an axis tag — only advisory tag.
        # FIX 4: suppress "+N pts" when pts_delta is 0 or None (post-#687 savings recs
        # have ~0 point impact; showing "+0 pts" is misleading).
        tags = ""
        if kind == "savings_cost":
            if pts_delta is not None:
                display_pts = round(pts_delta, 1)
                pts_suffix = (
                    f" &middot; +{display_pts:.1f} pts" if display_pts != 0.0 else ""
                )
            else:
                pts_suffix = ""
            tags = f'<span class="rec-tag cost">&#8594; Cost{pts_suffix}</span>'
        elif kind in ("governance", "quality_report"):
            tags = '<span class="rec-tag advisory">advisory &middot; not scored</span>'

        cards.append(
            '<div class="rec-card">'
            '<div class="rec-body">'
            f'<div class="rec-title">{title}{rec_id_html}</div>'
            f'<div class="rec-rationale">{_html.escape(rationale[:200])}</div>'
            "</div>"
            f'<div class="rec-tags">{tags}</div>'
            "</div>"
        )

    return '<div class="rec-zone">' + zone_label + "".join(cards) + "</div>"


# ──────────────────────────────────────────────────────────────────
# Dream manifest reading (spec/57 §4 — real fields, no invented ones)


def _read_dream_manifests(agent_root: Path) -> list[dict]:
    """Read dream manifests for an agent. Returns list of raw dicts, newest first.

    MUST 6: reads only real fields from manifest.json. No invented cadence/next-run.
    Returns [] if no dreams/ dir or no manifests (used as Dreaming tab gate).
    """
    dreams_dir = agent_root / "dreams"
    if not dreams_dir.exists():
        return []
    manifests = []
    for drm_dir in sorted(dreams_dir.iterdir(), reverse=True):
        if not drm_dir.is_dir():
            continue
        if not drm_dir.name.startswith("drm_"):
            continue
        mf_path = drm_dir / "manifest.json"
        if not mf_path.exists():
            continue
        try:
            data = _json.loads(mf_path.read_text(encoding="utf-8"))
            data["_drm_id"] = drm_dir.name
            data["_report_md"] = None
            report_path = drm_dir / "report.md"
            if report_path.exists():
                try:
                    data["_report_md"] = report_path.read_text(encoding="utf-8")
                except OSError:
                    pass
            manifests.append(data)
        except (OSError, _json.JSONDecodeError) as exc:
            logger.warning("dream manifest read failed for %s: %s", drm_dir, exc)
            continue
    return manifests


def _render_dream_tab(agent_root: Path) -> str:
    """Render the Dreaming tab content from real manifest/report fields (MUST 6)."""
    manifests = _read_dream_manifests(agent_root)
    if not manifests:
        return '<div class="empty-tab">No dream runs found for this agent.</div>'

    latest = manifests[0]

    # Summary stats from real manifest fields only (no invented cadence/next-run)
    total_consolidated = sum(len(m.get("consolidated", [])) for m in manifests)
    total_promoted = sum(len(m.get("promoted", [])) for m in manifests)
    total_stale = sum(len(m.get("marked_stale", [])) for m in manifests)
    total_cost = sum(float(m.get("total_cost_usd", 0)) for m in manifests)
    applied_count = sum(1 for m in manifests if m.get("applied_at"))

    stats_html = (
        '<div class="dream-stats">'
        f'<div class="dream-stat"><div class="dk">Dream runs</div>'
        f'<div class="dv">{len(manifests)}</div></div>'
        f'<div class="dream-stat"><div class="dk">Notes consolidated</div>'
        f'<div class="dv">{total_consolidated}</div></div>'
        f'<div class="dream-stat"><div class="dk">Notes promoted</div>'
        f'<div class="dv">{total_promoted}</div></div>'
        f'<div class="dream-stat"><div class="dk">Marked stale</div>'
        f'<div class="dv">{total_stale}</div></div>'
        f'<div class="dream-stat"><div class="dk">Applied</div>'
        f'<div class="dv{"" if applied_count else ""}">{applied_count}</div></div>'
        f'<div class="dream-stat"><div class="dk">Total cost</div>'
        f'<div class="dv">${total_cost:.4f}</div></div>'
        "</div>"
    )

    # Last run summary (report.md if available)
    report_html = ""
    latest_report = latest.get("_report_md")
    if latest_report:
        summary_text = latest_report[:1000]
        report_html = (
            '<div class="tab-section">'
            "<h3>Last run summary</h3>"
            f'<div class="report-summary">{_html.escape(summary_text)}</div>'
            f'<div style="font-size:11px; color: var(--muted); margin-top: 4px;">'
            f"Status: <strong>{_html.escape(str(latest.get('status', '')))}</strong>"
            f" &middot; Model: <strong>{_html.escape(str(latest.get('model', '')))}</strong>"
        )
        applied_at = latest.get("applied_at")
        if applied_at:
            report_html += (
                f" &middot; Applied: <strong>{_html.escape(str(applied_at))}</strong>"
            )
        archived = latest.get("archived_path")
        if archived:
            report_html += (
                f" &middot; Archived: <code>{_html.escape(str(archived))}</code>"
            )
        report_html += "</div></div>"

    # Recent runs table
    def _dream_status_pill(s: str) -> str:
        cls = (
            s.lower()
            if s.lower() in ("completed", "failed", "running", "pending", "canceled")
            else "pending"
        )
        return f'<span class="dream-status-pill {cls}">{_html.escape(s)}</span>'

    def _fmt_ts(ts_str) -> str:
        if not ts_str:
            return "—"
        # Truncate to the readable portion
        s = str(ts_str)
        return _html.escape(s[:19].replace("T", " "))

    rows = []
    for m in manifests[:10]:
        status = m.get("status", "")
        model = m.get("model", "")
        started = _fmt_ts(m.get("started_at"))
        ended = _fmt_ts(m.get("ended_at"))
        n_cons = len(m.get("consolidated", []))
        n_prom = len(m.get("promoted", []))
        n_stale = len(m.get("marked_stale", []))
        tokens_in = m.get("total_input_tokens", 0)
        tokens_out = m.get("total_output_tokens", 0)
        cost = float(m.get("total_cost_usd", 0))
        err = m.get("error") or ""
        applied = "✓" if m.get("applied_at") else ""
        error_cell = (
            f' <span style="color:var(--error); font-size:11px;">{_html.escape(err[:60])}</span>'
            if err
            else ""
        )
        rows.append(
            f"<tr>"
            f"<td>{_dream_status_pill(status)}{error_cell}</td>"
            f"<td class='mono' style='font-size:11px;'>{started}</td>"
            f"<td class='mono' style='font-size:11px;'>{ended}</td>"
            f"<td class='mono'>{_html.escape(_short_model_name(model) if model else '')}</td>"
            f"<td class='right num'>{n_cons}</td>"
            f"<td class='right num'>{n_prom}</td>"
            f"<td class='right num'>{n_stale}</td>"
            f"<td class='right num'>{tokens_in:,}&nbsp;/&nbsp;{tokens_out:,}</td>"
            f"<td class='right num'>${cost:.4f}</td>"
            f"<td class='right'>{applied}</td>"
            f"</tr>"
        )
    table_html = (
        '<div class="tab-section"><h3>Recent dream runs</h3>'
        "<table><thead><tr>"
        "<th>Status</th><th>Started</th><th>Ended</th><th>Model</th>"
        '<th class="right">Consol.</th><th class="right">Promoted</th>'
        '<th class="right">Stale</th><th class="right">Tokens (in/out)</th>'
        '<th class="right">Cost</th><th class="right">Applied</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        "</div>"
    )

    return (
        '<div class="dream-panel">'
        '<div class="dream-title">&#9670; Dream / Consolidation Runs</div>'
        + stats_html
        + "</div>"
        + report_html
        + table_html
    )


# ──────────────────────────────────────────────────────────────────
# Tab rendering helpers


def _render_overview_tab(agent_health, cost_data, recs, agent_id) -> str:
    """Overview tab: health composite + axis breakdown (recs moved to standalone zone)."""
    parts = []

    if agent_health is None:
        parts.append(
            '<div class="empty-tab">No health data computed for this agent yet. '
            "Run the agent or check the log backend.</div>"
        )
    else:
        composite = getattr(agent_health, "composite_display", None)
        band = getattr(agent_health, "band", "unknown")
        cost_score = getattr(agent_health, "cost_score", None)
        quality_score = getattr(agent_health, "quality_score", None)
        reliability_score = getattr(agent_health, "reliability_score", None)

        def _score_str(v) -> str:
            return str(int(round(v))) if v is not None else "—"

        def _band_class(b) -> str:
            return b if b in ("green", "amber", "red") else "unknown"

        comp_str = str(composite) if composite is not None else "—"
        parts.append(
            '<div class="axis-cards">'
            f'<div class="axis-card">'
            f'<div class="ax-label">Cost</div>'
            f'<div class="ax-score {_band_class("green" if (cost_score or 0) >= 60 else "amber" if (cost_score or 0) >= 40 else "red" if cost_score is not None else "unknown")}">'
            f"{_score_str(cost_score)}</div>"
            f'<div class="ax-band">/ 100</div>'
            f"</div>"
            f'<div class="axis-card">'
            f'<div class="ax-label">Quality</div>'
            f'<div class="ax-score {_band_class("green" if (quality_score or 0) >= 60 else "amber" if (quality_score or 0) >= 40 else "red" if quality_score is not None else "unknown")}">'
            f"{_score_str(quality_score)}</div>"
            f'<div class="ax-band">/ 100</div>'
            f"</div>"
            f'<div class="axis-card">'
            f'<div class="ax-label">Reliability</div>'
            f'<div class="ax-score {_band_class("green" if (reliability_score or 0) >= 60 else "amber" if (reliability_score or 0) >= 40 else "red" if reliability_score is not None else "unknown")}">'
            f"{_score_str(reliability_score)}</div>"
            f'<div class="ax-band">/ 100</div>'
            f"</div>"
            f"</div>"
        )

    return (
        "\n".join(parts) if parts else '<div class="empty-tab">No overview data.</div>'
    )


def _render_cost_tab(data) -> str:
    """Cost tab: spend summary + top runs."""
    if data is None:
        return '<div class="empty-tab">No cost data available for this agent.</div>'

    s = getattr(data, "summary_this_month", None)
    if s is None:
        return '<div class="empty-tab">No cost data available for this agent.</div>'

    # Real field reads (MUST 9): direct attribute access, not getattr-with-default-0.
    # Phantom getattr-with-0-default silently masks missing attributes as zero.
    # These fields exist on AgentSummary (cost_usd, runs, errors, cache_hit_pct).
    cost_usd = s.cost_usd
    runs = s.runs
    errors = s.errors
    cache_hit_pct = s.cache_hit_pct
    metrics_html = (
        '<div class="metrics-grid">'
        f'<div class="metric-card"><div class="mk">Spend (month)</div>'
        f'<div class="mv">${cost_usd:.4f}</div></div>'
        f'<div class="metric-card"><div class="mk">Runs (month)</div>'
        f'<div class="mv">{runs}</div></div>'
        f'<div class="metric-card"><div class="mk">Errors (month)</div>'
        f'<div class="mv {"red" if errors > 0 else ""}">{errors}</div></div>'
        f'<div class="metric-card"><div class="mk">Cache hit</div>'
        f'<div class="mv">{cache_hit_pct}%</div></div>'
        "</div>"
    )

    top_runs = getattr(data, "top_runs", [])
    if top_runs:
        rows = []
        for r in top_runs[:10]:
            ts_str = (
                r.ts.strftime("%b %d · %H:%M")
                if hasattr(r.ts, "strftime")
                else str(r.ts)
            )
            trigger = getattr(r, "trigger", "")
            model = getattr(r, "model", "")
            in_tok = r.input_tokens
            out_tok = r.output_tokens
            cost = r.cost_usd
            summary = getattr(r, "summary", "")
            rows.append(
                f"<tr>"
                f"<td class='num'>{_html.escape(ts_str)}</td>"
                f"<td>{_html.escape(trigger)}</td>"
                f"<td><span class='pill {_model_pill_class(model)}'>"
                f"{_html.escape(_short_model_name(model))}</span></td>"
                f"<td class='right num'>{in_tok:,} / {out_tok:,}</td>"
                f"<td class='right num'>${cost:.4f}</td>"
                f"<td>{_html.escape(str(summary)[:60])}</td>"
                f"</tr>"
            )
        table_html = (
            "<table><thead><tr><th>Time</th><th>Trigger</th><th>Model</th>"
            '<th class="right">Tokens</th><th class="right">Cost</th>'
            "<th>Summary</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        table_html = '<div class="empty-tab">No runs this month.</div>'

    return (
        '<div class="tab-section"><h3>This month</h3>'
        + metrics_html
        + "</div>"
        + '<div class="tab-section"><h3>Top runs</h3>'
        + table_html
        + "</div>"
    )


def _render_activity_tab(data) -> str:
    """Activity tab: recent run history."""
    if data is None:
        return '<div class="empty-tab">No activity data available for this agent.</div>'
    top_runs = getattr(data, "top_runs", [])
    if not top_runs:
        return '<div class="empty-tab">No runs recorded this period.</div>'
    rows = []
    for r in top_runs[:20]:
        ts_str = (
            r.ts.strftime("%b %d · %H:%M") if hasattr(r.ts, "strftime") else str(r.ts)
        )
        trigger = getattr(r, "trigger", "")
        model = getattr(r, "model", "")
        in_tok = r.input_tokens
        out_tok = r.output_tokens
        cost = r.cost_usd
        fallback = getattr(r, "fallback", False)
        fallback_tag = (
            '<span class="pill fallback" style="margin-left:3px;">fallback</span>'
            if fallback
            else ""
        )
        rows.append(
            f"<tr>"
            f"<td class='num'>{_html.escape(ts_str)}</td>"
            f"<td>{_html.escape(trigger)}</td>"
            f"<td><span class='pill {_model_pill_class(model)}'>"
            f"{_html.escape(_short_model_name(model))}</span>{fallback_tag}</td>"
            f"<td class='right num'>{in_tok:,} / {out_tok:,}</td>"
            f"<td class='right num'>${cost:.4f}</td>"
            f"</tr>"
        )
    table_html = (
        "<table><thead><tr><th>Time</th><th>Trigger</th><th>Model</th>"
        '<th class="right">Tokens</th><th class="right">Cost</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )
    return '<div class="tab-section"><h3>Run history</h3>' + table_html + "</div>"


def _render_quality_tab(agent_id: str, agents_root: Path) -> str:
    """Quality tab: eval results. Empty state when no evals."""
    evals_dir = agents_root / agent_id / "evals"
    if not evals_dir.exists():
        return '<div class="empty-tab">No evals configured for this agent.</div>'
    # Read eval run results
    runs_dir = evals_dir / "runs"
    if not runs_dir.exists():
        return '<div class="empty-tab">No eval runs yet. Run evals to see scores here.</div>'
    records = []
    for f in sorted(runs_dir.glob("*.jsonl"), reverse=True)[:3]:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(_json.loads(line))
        except (OSError, _json.JSONDecodeError):
            continue
    if not records:
        return '<div class="empty-tab">No eval runs yet. Run evals to see scores here.</div>'
    rows = []
    for rec in records[:20]:
        ts = rec.get("ts", rec.get("timestamp", ""))
        ts_str = str(ts)[:19].replace("T", " ") if ts else "—"
        verdict = rec.get("verdict", rec.get("status", ""))
        # Reads weighted_score (1-5 rubric scale) with fallback to score (0-1 legacy).
        score_str = _eval_score_display(rec)
        run_id = rec.get("run_id", "")[:12]
        rows.append(
            f"<tr><td class='mono' style='font-size:11px;'>{_html.escape(ts_str)}</td>"
            f"<td>{_html.escape(str(verdict))}</td>"
            f"<td class='right num'>{score_str}</td>"
            f"<td class='mono' style='font-size:11px;'>{_html.escape(run_id)}</td></tr>"
        )
    table_html = (
        "<table><thead><tr><th>Time</th><th>Verdict</th>"
        '<th class="right">Score</th><th>Run ID</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return '<div class="tab-section"><h3>Eval results</h3>' + table_html + "</div>"


def _render_memory_tab(agent_id: str, agents_root: Path) -> str:
    """Memory tab: notes stats."""
    memory_dir = agents_root / agent_id / "memory"
    if not memory_dir.exists():
        return '<div class="empty-tab">No memory directory for this agent.</div>'
    notes = list(memory_dir.glob("*.md"))
    if not notes:
        return '<div class="empty-tab">Memory directory exists but is empty.</div>'
    atomic_notes = [
        n for n in notes if "wiki" not in n.name.lower() and "INDEX" not in n.name
    ]
    wiki_notes = [n for n in notes if "wiki" in n.name.lower()]
    return (
        '<div class="tab-section"><h3>Note inventory</h3>'
        '<div class="metrics-grid">'
        f'<div class="metric-card"><div class="mk">Total notes</div>'
        f'<div class="mv">{len(notes)}</div></div>'
        f'<div class="metric-card"><div class="mk">Atomic notes</div>'
        f'<div class="mv">{len(atomic_notes)}</div></div>'
        f'<div class="metric-card"><div class="mk">Wiki notes</div>'
        f'<div class="mv">{len(wiki_notes)}</div></div>'
        "</div></div>"
    )


def _render_goals_tab(agent_id: str, agents_root: Path) -> str:
    """Goals tab: goal progress. Only shown when goal file exists."""
    goal_path = agents_root / agent_id / "goal.md"
    if not goal_path.exists():
        return '<div class="empty-tab">No goal.md for this agent.</div>'
    try:
        content = goal_path.read_text(encoding="utf-8")
    except OSError:
        return '<div class="tab-degraded">Could not read goal.md.</div>'
    return (
        '<div class="tab-section"><h3>Goal</h3>'
        f'<pre style="background:var(--card);border:1px solid var(--border);'
        f"border-radius:8px;padding:16px;font-size:12px;line-height:1.6;"
        f'white-space:pre-wrap;word-break:break-word;">'
        f"{_html.escape(content[:3000])}</pre>"
        "</div>"
    )


def _render_efficiency_tab(agent_health, data) -> str:
    """Efficiency tab: derived efficiency metrics."""
    parts = ['<div class="tab-section"><h3>Efficiency metrics</h3>']
    if agent_health is None and data is None:
        return '<div class="empty-tab">No efficiency data available.</div>'
    if data is not None:
        s = getattr(data, "summary_this_month", None)
        if s is not None:
            # Real field reads (MUST 9): no getattr-with-default-0 (phantom-field masking).
            cost_usd = s.cost_usd
            runs = s.runs
            if runs > 0:
                cost_per_run = cost_usd / runs
                parts.append(
                    '<div class="metrics-grid">'
                    f'<div class="metric-card"><div class="mk">Cost / run</div>'
                    f'<div class="mv">${cost_per_run:.4f}</div></div>'
                    f'<div class="metric-card"><div class="mk">Total runs</div>'
                    f'<div class="mv">{runs}</div></div>'
                    "</div>"
                )
    helper_savings = getattr(data, "helper_savings", None) if data else None
    if helper_savings and getattr(helper_savings, "helper_calls", 0) > 0:
        hs = helper_savings
        parts.append(
            f'<div class="metric-card" style="margin-top:12px;">'
            f'<div class="mk">Helper savings</div>'
            f'<div class="mv green">${getattr(hs, "saved", 0):.4f}</div>'
            f'<div style="font-size:11px;color:var(--muted);margin-top:4px;">'
            f"{getattr(hs, 'helper_calls', 0)} helper calls · "
            f"{getattr(hs, 'cost_ratio', 1.0):.1f}× cost ratio</div>"
            f"</div>"
        )
    parts.append("</div>")
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────
# Banner rendering


def _read_model_from_model_md(agent_root: "Path") -> str:
    """Read the primary model id from <agent>/model.md (FIX 2).

    Returns the first recognised model token found in the file, or "" if absent.
    Reads only the first 2 KB to stay fast — model.md is always small.
    """
    import re as _re2

    model_md = agent_root / "model.md"
    if not model_md.exists():
        return ""
    try:
        content = model_md.read_text(encoding="utf-8")[:2048]
    except OSError:
        return ""
    # Match known model id prefixes — longest-prefix-first.
    patterns = [
        r"claude-[a-z0-9][a-z0-9\-\.]+",
        r"gpt-[a-z0-9][a-z0-9\-\.]+",
        r"moonshot/[a-z0-9][a-z0-9\-\.]+",
        r"local/[a-z0-9][a-z0-9\-\.]+",
        r"qwen[0-9a-z\-\.]+",
        r"llama[0-9a-z\-\.]+",
    ]
    for pat in patterns:
        m = _re2.search(pat, content, _re2.IGNORECASE)
        if m:
            return m.group(0)
    return ""


def _read_permission_tier(agent_ref) -> str:
    """Extract permission_tier from agent_ref (FIX 2 — tier pill independent of health)."""
    if agent_ref is None:
        return ""
    gov = getattr(agent_ref, "governance", None)
    if gov is None:
        return ""
    if getattr(gov, "parse_errors", None):
        return ""
    tier = getattr(gov, "permission_tier", None)
    return str(tier) if tier is not None else ""


def _compute_banner_stats(
    cost_data,
    now: "datetime",
    today: "date",
    agents_root: "Path",
    agent_id: str,
) -> dict:
    """Compute the banner grid metrics (FIX 3 — 8 fields matching the B7 mockup).

    Returns: spend_7d, spend_30d, runs_7d, failures_7d, eval_score.
    None = no data (renders as "—").
    """
    from datetime import timedelta as _td

    spend_7d: "float | None" = None
    spend_30d: "float | None" = None
    runs_7d: "int | None" = None
    failures_7d: "int | None" = None

    if cost_data is not None:
        # 7d spend from daily_costs dict (per-day totals this month)
        daily = getattr(cost_data, "daily_costs", {}) or {}
        if daily:
            cutoff_7d_str = (today - _td(days=7)).isoformat()
            spend_7d = sum(v for k, v in daily.items() if k >= cutoff_7d_str)

        # 30d spend: monthly summary is a close proxy
        s = getattr(cost_data, "summary_this_month", None)
        if s is not None:
            spend_30d = getattr(s, "cost_usd", None)

        # 7d runs + failures from top_runs (time-filtered)
        top_runs = getattr(cost_data, "top_runs", []) or []
        if top_runs:
            try:
                now_tz = now
                if now_tz.tzinfo is None:
                    from datetime import timezone as _tz

                    now_tz = now_tz.replace(tzinfo=_tz.utc)
                cutoff_7d_dt = now_tz - _td(days=7)
                runs_7d = 0
                failures_7d = 0
                for r in top_runs:
                    ts = getattr(r, "ts", None)
                    if ts is None:
                        continue
                    if ts.tzinfo is None:
                        from datetime import timezone as _tz

                        ts = ts.replace(tzinfo=_tz.utc)
                    if ts >= cutoff_7d_dt:
                        runs_7d += 1
                        if getattr(r, "status", "ok") in ("error", "failed", "blocked"):
                            failures_7d += 1
            except Exception:
                pass

    # Eval score: most recent score from evals/runs/*.jsonl.
    # Track which field was read so the formatter uses the correct scale.
    eval_score: "float | None" = None
    eval_score_scale: str = "rubric"  # "rubric" (1-5) or "legacy" (0-1)
    evals_dir = agents_root / agent_id / "evals" / "runs"
    if evals_dir.exists():
        import json as _j2

        try:
            for f in sorted(evals_dir.glob("*.jsonl"), reverse=True)[:1]:
                lines = f.read_text(encoding="utf-8").splitlines()
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _j2.loads(line)
                        # weighted_score is the canonical field (1-5 rubric scale).
                        # Fall back to score (0-1) for legacy fixtures.
                        # Branch on WHICH field is present, not on the value.
                        ws = rec.get("weighted_score")
                        if ws is not None:
                            eval_score = float(ws)
                            eval_score_scale = "rubric"
                            break
                        legacy = rec.get("score")
                        if legacy is not None:
                            eval_score = float(legacy)
                            eval_score_scale = "legacy"
                            break
                    except Exception:
                        continue
                if eval_score is not None:
                    break
        except Exception:
            pass

    return {
        "spend_7d": spend_7d,
        "spend_30d": spend_30d,
        "runs_7d": runs_7d,
        "failures_7d": failures_7d,
        "eval_score": eval_score,
        "eval_score_scale": eval_score_scale,
    }


def _render_banner(
    agent_id: str,
    agent_ref,
    agent_health,
    status: str,
    last_run_at: "datetime | None",
    now: "datetime",
    cost_data=None,
    today: "date | None" = None,
    agents_root: "Path | None" = None,
    model_from_md: str = "",
) -> str:
    """Render the Fable banner (MUST 3 + 5) — full 8-field grid (FIX 2 + 3)."""
    display_name = agent_id.replace("-", " ").replace("_", " ").title()

    # Status pill
    status_lower = status.lower()
    status_pill = (
        f'<span class="status-pill {status_lower}">'
        f'<span class="sdot"></span>{status.upper()}</span>'
    )

    # FIX 2: Model pill — prefer agent_health.primary_model; fall back to model.md.
    # Independent of agent_health (renders whenever a model id is known).
    model_id = ""
    if agent_health is not None:
        model_id = getattr(agent_health, "primary_model", None) or ""
    if not model_id:
        model_id = model_from_md
    model_pill_html = ""
    if model_id:
        cls = _model_pill_class(model_id)
        model_pill_html = f'<span class="pill {cls}">{_html.escape(_short_model_name(model_id))}</span>'

    # FIX 2: Permission-tier pill — from governance, independent of health.
    tier_str = _read_permission_tier(agent_ref)
    tier_pill_html = (
        f'<span class="pill" style="color:var(--muted);border-color:var(--border);">'
        + _html.escape(tier_str)
        + "</span>"
        if tier_str
        else ""
    )

    # Health score — 0-100 integer (MUST 5: never ×100 of raw float)
    # composite_display is already int(round(composite)) per spec/53 §3.3
    health_val: str = "—"
    health_class = "health-unknown"
    if agent_health is not None:
        cd = getattr(agent_health, "composite_display", None)
        if cd is not None:
            # MUST 5: composite_display is already 0-100 (not a 0-1 float)
            # Guard: if caller accidentally passes 0-1 float, it would render
            # as 0 or 1. composite_display is int, so this is safe.
            health_val = str(int(cd))
            band = getattr(agent_health, "band", "unknown")
            health_class = f"health-{band}"

    # Last run
    last_run_str = "never"
    if last_run_at is not None:
        lpr = last_run_at
        if lpr.tzinfo is None:
            lpr = lpr.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        diff = now - lpr
        secs = int(diff.total_seconds())
        if secs < 0:
            last_run_str = "just now"
        elif secs < 60:
            last_run_str = f"{secs}s ago"
        elif secs < 3600:
            last_run_str = f"{secs // 60}m ago"
        elif secs < 86400:
            last_run_str = f"{secs // 3600}h ago"
        else:
            last_run_str = f"{secs // 86400}d ago"

    # FIX 3: 8-field banner grid — matches variant-B7-agent-detail.html mockup.
    _today_val = today or date.today()
    banner_stats: dict = {}
    if agents_root is not None:
        banner_stats = _compute_banner_stats(
            cost_data, now, _today_val, agents_root, agent_id
        )

    def _fmt_spend(v) -> str:
        return f"${v:.2f}" if v is not None else "—"

    def _fmt_int(v) -> str:
        return str(v) if v is not None else "—"

    def _fmt_score(v, scale: str) -> str:
        # Delegates to shared helper using the scale tracked by _compute_banner_stats.
        # scale="rubric" → (v-1)/4*100 normalisation; scale="legacy" → v*100.
        return _eval_score_fmt(v, scale=scale)

    spend_7d_str = _fmt_spend(banner_stats.get("spend_7d"))
    spend_30d_str = _fmt_spend(banner_stats.get("spend_30d"))
    runs_7d_str = _fmt_int(banner_stats.get("runs_7d"))
    failures_7d_str = _fmt_int(banner_stats.get("failures_7d"))
    eval_score_str = _fmt_score(
        banner_stats.get("eval_score"),
        banner_stats.get("eval_score_scale", "rubric"),
    )

    pills_html = status_pill
    if model_pill_html:
        pills_html += " " + model_pill_html
    if tier_pill_html:
        pills_html += " " + tier_pill_html

    return (
        '<div class="agent-banner">'
        '<div class="banner-row1">'
        f'<span class="banner-name">{_html.escape(display_name)}</span>'
        f'<span class="banner-id">{_html.escape(agent_id)}</span>'
        "</div>"
        f'<div class="banner-pills">{pills_html}</div>'
        '<div class="banner-grid">'
        # Field 1: Status
        '<div class="banner-kv">'
        '<div class="bk">Status</div>'
        f'<div class="bv status-{status_lower}">{status.upper()}</div>'
        "</div>"
        # Field 2: Last run
        '<div class="banner-kv">'
        '<div class="bk">Last run</div>'
        f'<div class="bv">{_html.escape(last_run_str)}</div>'
        "</div>"
        # Field 3: 7d spend
        '<div class="banner-kv">'
        '<div class="bk">7d spend</div>'
        f'<div class="bv">{_html.escape(spend_7d_str)}</div>'
        "</div>"
        # Field 4: 30d spend
        '<div class="banner-kv">'
        '<div class="bk">30d spend</div>'
        f'<div class="bv">{_html.escape(spend_30d_str)}</div>'
        "</div>"
        # Field 5: Failures (7d)
        '<div class="banner-kv">'
        '<div class="bk">Failures (7d)</div>'
        f'<div class="bv">{_html.escape(failures_7d_str)}</div>'
        "</div>"
        # Field 6: Runs (7d)
        '<div class="banner-kv">'
        '<div class="bk">Runs (7d)</div>'
        f'<div class="bv">{_html.escape(runs_7d_str)}</div>'
        "</div>"
        # Field 7: Fleet health
        '<div class="banner-kv">'
        '<div class="bk">Fleet health</div>'
        f'<div class="bv {health_class}">{health_val}</div>'
        "</div>"
        # Field 8: Eval score
        '<div class="banner-kv">'
        '<div class="bk">Eval score</div>'
        f'<div class="bv">{_html.escape(eval_score_str)}</div>'
        "</div>"
        "</div>"  # banner-grid
        "</div>"  # agent-banner
    )


# ──────────────────────────────────────────────────────────────────
# Per-tab capability gating (MUST 4)


def _has_dreaming(agent_root: Path) -> bool:
    """True when at least one dreams/drm_*/manifest.json exists (Dreaming gate)."""
    dreams_dir = agent_root / "dreams"
    if not dreams_dir.exists():
        return False
    for d in dreams_dir.iterdir():
        if d.is_dir() and d.name.startswith("drm_") and (d / "manifest.json").exists():
            return True
    return False


def _has_goals_agent(agent_root: Path) -> bool:
    """True when this agent has a goal.md."""
    return (agent_root / "goal.md").exists()


def _has_memory(agent_root: Path) -> bool:
    """True when the memory/ surface exists (even if empty).

    Per spec/57 §3: the Memory tab renders an empty state when the surface is
    present but has no notes.  Only omit the tab when the surface is absent.
    """
    return (agent_root / "memory").exists()


def _has_evals(agent_root: Path) -> bool:
    """True when evals/ exists — quality surface available (may be empty)."""
    return (agent_root / "evals").exists()


# ──────────────────────────────────────────────────────────────────
# Agent detail context (passed to agent-tab panels via ctx.agent_detail)


class _AgentDetailData:
    """Agent-specific data container set on PanelContext before compose_agent_detail().

    Panel render() methods read from ctx.agent_detail (duck-typed attribute set
    after PanelContext construction — not a dataclass field — to avoid changing
    the PanelContext signature or breaking existing tests).

    All data is pre-loaded before the panel engine loop (MUST 13: no I/O in render).
    """

    __slots__ = (
        "agent_id",
        "agents_root",
        "agent_root",
        "agent_ref",
        "agent_health",
        "cost_data",
        "recs",
        "has_dreaming",
        "has_goals",
        "has_memory",
        "has_evals",
    )

    def __init__(
        self,
        agent_id: str,
        agents_root,
        agent_root,
        agent_ref,
        agent_health,
        cost_data,
        recs,
        has_dreaming: bool,
        has_goals: bool,
        has_memory: bool,
        has_evals: bool,
    ) -> None:
        self.agent_id = agent_id
        self.agents_root = agents_root
        self.agent_root = agent_root
        self.agent_ref = agent_ref
        self.agent_health = agent_health
        self.cost_data = cost_data
        self.recs = recs
        self.has_dreaming = has_dreaming
        self.has_goals = has_goals
        self.has_memory = has_memory
        self.has_evals = has_evals


# ──────────────────────────────────────────────────────────────────
# Main detail page template


def _render_detail_template(
    agent_id: str,
    agents_root: Path,
    agent_ref,
    agent_health,
    status: str,
    last_run_at: datetime | None,
    cost_data,
    recs: list | None,
    now: datetime,
    today: date,
    has_goals_nav: bool = False,
) -> str:
    """Compose the full B7 detail page HTML."""
    from datetime import datetime as _dt

    agent_root = agents_root / agent_id
    agent_name_safe = _html.escape(agent_id)

    # Breadcrumb
    breadcrumb = (
        '<div class="breadcrumb">'
        '<a href="../_dashboard/monitor.html">&#8592; Fleet Monitor</a>'
        f" / {agent_name_safe}"
        "</div>"
    )

    # Banner (MUST 3 + 5) — pass model_from_md and cost_data for FIX 2+3+8-grid
    _model_from_md = _read_model_from_model_md(agent_root)
    banner = _render_banner(
        agent_id,
        agent_ref,
        agent_health,
        status,
        last_run_at,
        now,
        cost_data=cost_data,
        today=today,
        agents_root=agents_root,
        model_from_md=_model_from_md,
    )

    # Governance block (MUST 3)
    gov_block = _render_governance_block(agent_ref)

    # Degraded banner (spec/09)
    degraded_html = ""
    if cost_data is not None and getattr(cost_data, "cost_data_degraded", False):
        degraded_html = (
            '<div class="degraded-banner">'
            '<span class="pill warn">&#9888; data may be incomplete</span>'
            " One or more log reads failed. Cost figures below may be understated."
            "</div>"
        )

    # Capability gates (MUST 4) — pre-loaded before the panel engine loop (MUST 13).
    show_dreaming = _has_dreaming(agent_root)
    show_goals = _has_goals_agent(agent_root)
    show_memory = _has_memory(agent_root)
    show_evals = _has_evals(agent_root)

    # Build agent-detail context for the panel engine (MUST 4 — compose_agent_detail
    # drives tabs; panels read from ctx.agent_detail set here).
    detail_data = _AgentDetailData(
        agent_id=agent_id,
        agents_root=agents_root,
        agent_root=agent_root,
        agent_ref=agent_ref,
        agent_health=agent_health,
        cost_data=cost_data,
        recs=recs,
        has_dreaming=show_dreaming,
        has_goals=show_goals,
        has_memory=show_memory,
        has_evals=show_evals,
    )

    # Ensure agent-tab panels are registered (import side effect).
    from .panels import _agent_tabs as _at_reg  # noqa: F401
    from .panels._registry import ConsoleCapabilities, PanelContext, get_registry

    # Build a minimal PanelContext for the agent-tab panels.  These panels read
    # from ctx.agent_detail, NOT from ctx.console_data, so we stub console_data
    # with a lightweight sentinel (no fleet-wide aggregation needed here).
    class _StubConsoleData:
        attention_queue = []
        cost_trends = []
        quality_signals = []
        reliability_metrics = []
        fleet_health = None
        recommendations = None
        degraded = False
        rendered_alert_keys = frozenset()
        last_primary_runs = {}
        agent_count = 0

    _ctx = PanelContext(
        console_data=_StubConsoleData(),
        capabilities=ConsoleCapabilities(),
        today=today,
        now=now,
    )
    _ctx.agent_detail = detail_data  # duck-typed attribute (not a dataclass field)

    registry = get_registry()

    # compose_agent_detail() is the SINGLE engine entry point (MUST 4).
    # Its return value — an ordered list of (panel, html) for every available
    # panel — drives BOTH the tab-nav buttons and the tab-content panes.
    # There is no second panels_by_slot() render pass.
    composed_tabs = registry.compose_agent_detail(_ctx)

    # Tab nav + tab panel contents — both built from the composed list (MUST 4).
    tab_nav_items = []
    panels_html_parts = []
    for i, (_p, _panel_content) in enumerate(composed_tabs):
        active = " active" if i == 0 else ""
        tab_nav_items.append(
            f'<button class="dtab{active}" onclick="showTab(\'{_p.tab_id}\')" '
            f'id="dtab-{_p.tab_id}">{_p.tab_label}</button>'
        )
        panels_html_parts.append(
            f'<div class="tab-panel{active}" id="tabpanel-{_p.tab_id}">'
            + _panel_content
            + "</div>"
        )
    tab_nav = '<div class="detail-tabs">' + "".join(tab_nav_items) + "</div>"
    panels_html = "\n".join(panels_html_parts)

    # Standalone recommendations zone (B7 design contract — always visible, between
    # governance block and telemetry tabs; empty-state shown when engine fires nothing).
    recs_standalone = _render_detail_recommendations(recs, agent_id)

    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{_SHARED_CSP}">
<title>{agent_name_safe} — Agent Detail — Atomic Agents</title>
<style>{_SHARED_CSS}{_DETAIL_CSS}</style>
</head>
<body>

<header>
  <div>
    {breadcrumb}
    <h1>Fleet Console</h1>
    <div class="period">Agent detail &middot; {now_str}</div>
  </div>
  <div>
    <button class="refresh-btn" onclick="refresh()">&#8635; Refresh</button>
  </div>
</header>

<nav class="tab-nav" style="margin-bottom:20px;">
  <a href="../_dashboard/index.html">Console</a>
  <a href="../_dashboard/monitor.html">Monitor</a>
  <a href="../_dashboard/cost.html">Cost</a>
  <a href="../_dashboard/activity.html">Activity</a>
  <a href="../_dashboard/quality.html">Quality</a>
  <a href="../_dashboard/memory.html">Memory</a>
  {"<a href='../_dashboard/goals.html'>Goals</a>" if has_goals_nav else ""}
</nav>

{degraded_html}
{banner}
{gov_block}
{recs_standalone}
{tab_nav}
{panels_html}

<footer>
  <div>Generated {today.isoformat()} by atomic_agents.dashboard</div>
  <div>Per-agent detail cockpit (spec/57)</div>
</footer>

<script>
function showTab(id) {{
  document.querySelectorAll('.tab-panel').forEach(function(p) {{
    p.classList.remove('active');
  }});
  document.querySelectorAll('.dtab').forEach(function(t) {{
    t.classList.remove('active');
  }});
  var panel = document.getElementById('tabpanel-' + id);
  if (panel) panel.classList.add('active');
  var tab = document.getElementById('dtab-' + id);
  if (tab) tab.classList.add('active');
}}
function refresh() {{
  fetch('/regenerate', {{method: 'POST'}})
    .then(function(r) {{ if (r.ok) location.reload(); else location.reload(); }})
    .catch(function() {{ location.reload(); }});
}}
</script>

</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────
# Static resolver: _dashboard/agent-detail.html (MUST 1)

_RESOLVER_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Agent Detail — Redirecting</title>
<style>
body { font-family: system-ui, sans-serif; background: #0f1419; color: #e6e6e6;
  padding: 32px 48px; }
.msg { color: #8a96a3; }
a { color: #4ec9b0; }
</style>
</head>
<body>
<p class="msg" id="msg">Redirecting to agent detail page&hellip;</p>
<script>
(function() {
  var params = new URLSearchParams(window.location.search);
  var agent = params.get('agent');
  var known = AGENT_LIST_PLACEHOLDER;
  if (!agent || known.indexOf(agent) === -1) {
    document.getElementById('msg').innerHTML =
      '<strong style="color:#e06c75;">Agent not found</strong>' +
      (agent ? ' &mdash; <code>' + agent.replace(/[<>&"]/g,'') + '</code> is not in the fleet.' : '');
    return;
  }
  var encoded = encodeURIComponent(agent);
  window.location.replace('../' + encoded + '/dashboard.html');
})();
</script>
<noscript>
  <p>JavaScript is required for the agent resolver. Navigate directly to
  <code>../{agent}/dashboard.html</code>.</p>
</noscript>
</body>
</html>
"""


def _render_resolver(agent_ids: list[str]) -> str:
    """Generate the _dashboard/agent-detail.html resolver page."""
    # JSON-encode the agent list for injection; escape for HTML context
    known_json = _json.dumps(sorted(agent_ids))
    safe_json = (
        known_json.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return _RESOLVER_HTML.replace("AGENT_LIST_PLACEHOLDER", safe_json)


# ──────────────────────────────────────────────────────────────────
# Public API


def render_agent_detail(
    agents_root: Path,
    agent_id: str,
    console_data=None,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> Path:
    """Render <agents_root>/<agent_id>/dashboard.html as the B7 detail cockpit.

    Called from render_agent() and from render_all() (MUST 2 — same path, back-compat).
    Returns the path written.

    Status + health come from the shared status_for_agent() + FleetHealth
    (MUST 5 — same derivation as the Monitor). When console_data is supplied
    (from render_all()) the SAME snapshot is used; standalone calls build a
    fresh consistent snapshot.

    Pure-compute: zero LLM spend (MUST 10).
    """
    from datetime import datetime as _dt

    from ._status import status_for_agent
    from .costs import aggregate_agent, discover_agents

    today = today or date.today()
    now = now if now is not None else _dt.now(tz=timezone.utc)

    agent_root = agents_root / agent_id

    # ── Load agent health from console_data fleet_health if available ──────────
    # FIX 1 + 6: When console_data has fleet_health, use it (MUST 5 parity).
    # When absent (standalone call), compute a fresh self-consistent snapshot
    # so the banner/Overview tab are FULL (not skeleton) in both modes.
    agent_health = None
    if console_data is not None:
        fh = getattr(console_data, "fleet_health", None)
        if fh is not None:
            for ah in getattr(fh, "agents", []):
                if getattr(ah, "agent", None) == agent_id:
                    agent_health = ah
                    break

    # FIX 1: Standalone self-sufficient snapshot — compute health if still None.
    if agent_health is None:
        try:
            from ..advisor.score import compute_fleet_health as _cfh

            _fh = _cfh(agents_root, today=today)
            for ah in getattr(_fh, "agents", []):
                if getattr(ah, "agent", None) == agent_id:
                    agent_health = ah
                    break
        except Exception as _exc:
            logger.warning(
                "standalone compute_fleet_health failed for %s (%s); banner will show '—'",
                agent_id,
                type(_exc).__name__,
            )

    # ── Load agent_ref from AgentRegistryBackend (fail-soft) ──────────────────
    # get_agent() returns a ref without governance (include_governance is not a
    # param on that method). Use list_agents(include_governance=True) and filter
    # by id so we get the full governance record (spec/51).
    agent_ref = None
    try:
        from ..agent_registry import FilesystemAgentRegistryBackend

        registry = FilesystemAgentRegistryBackend(agents_root)
        # get_agent() does not accept include_governance; use list_agents to
        # get the governance-populated ref and filter by agent_id.
        agent_refs = registry.list_agents(include_governance=True)
        for ref in agent_refs:
            if ref.id == agent_id:
                agent_ref = ref
                break
        # Fall back to get_agent() (no governance) if list_agents didn't find it.
        if agent_ref is None:
            agent_ref = registry.get_agent(agent_id)
    except Exception as exc:
        logger.warning("agent_registry load failed for %s: %s", agent_id, exc)

    # ── Derive status (MUST 5 — shared status_for_agent()) ────────────────────
    last_run_at: datetime | None = None
    open_items = []
    cost_spike = False
    if console_data is not None:
        last_run_at = getattr(console_data, "last_primary_runs", {}).get(agent_id)
        queue = getattr(console_data, "attention_queue", [])
        open_items = [
            item
            for item in queue
            if getattr(item, "agent", None) == agent_id
            and getattr(item, "ack_snooze_status", "open") == "open"
        ]
        cost_trends = getattr(console_data, "cost_trends", [])
        for ct in cost_trends:
            if getattr(ct, "agent", None) == agent_id and getattr(
                ct, "spike_detected", False
            ):
                cost_spike = True

    # FIX 1: When console_data absent, derive last_run_at from cost_data.top_runs
    # so status_for_agent() sees a real last-run timestamp (not None → STALE).
    # cost_data is loaded after this block, so we do a lightweight load here.
    if last_run_at is None and console_data is None:
        try:
            _data = aggregate_agent(agents_root, agent_id, today=today)
            _top = getattr(_data, "top_runs", [])
            if _top:
                from ..dashboard._reliability import _is_primary_run as _ipr

                _primary = [r for r in _top if _ipr(r)]
                if not _primary:
                    _primary = _top  # fall back to any run
                if _primary:
                    _ts = _primary[0].ts
                    if _ts.tzinfo is None:
                        _ts = _ts.replace(tzinfo=timezone.utc)
                    last_run_at = _ts
        except Exception:
            pass

    status = status_for_agent(
        agent_health=agent_health,
        attention_items=open_items,
        last_primary_run_at=last_run_at,
        now=now,
        cost_spike=cost_spike,
    )

    # ── Load per-agent cost data (fail-soft) ───────────────────────────────────
    cost_data = None
    try:
        cost_data = aggregate_agent(agents_root, agent_id, today=today)
    except Exception as exc:
        logger.warning("aggregate_agent failed for %s: %s", agent_id, exc)

    # ── Recommendations (FIX 4: from console_data OR standalone-computed) ───────
    recs = None
    if console_data is not None:
        recs = getattr(console_data, "recommendations", None)
    if recs is None:
        # FIX 4: standalone — compute recs for this fleet so the detail page is full.
        try:
            from ..advisor.recommend import recommend_fleet as _rf

            _fh_for_rec = None
            if agent_health is not None:
                # Reuse the already-computed FleetHealth if we have it (avoid second pass).
                # Wrap in a minimal FleetHealth-shaped object the recommender accepts.
                try:
                    from ..advisor.score import compute_fleet_health as _cfh2

                    _fh_for_rec = _cfh2(agents_root, today=today)
                except Exception:
                    pass
            recs = _rf(agents_root, today=today, fleet_health=_fh_for_rec)
        except Exception as _exc:
            logger.warning(
                "standalone recommend_fleet failed (%s); recs panel will be empty",
                type(_exc).__name__,
            )

    # ── has_goals_nav (for the top nav Goals link) ─────────────────────────────
    has_goals_nav = any(
        (agents_root / a / "goal.md").exists() for a in discover_agents(agents_root)
    )

    # ── Render ─────────────────────────────────────────────────────────────────
    html_content = _render_detail_template(
        agent_id=agent_id,
        agents_root=agents_root,
        agent_ref=agent_ref,
        agent_health=agent_health,
        status=status,
        last_run_at=last_run_at,
        cost_data=cost_data,
        recs=recs,
        now=now,
        today=today,
        has_goals_nav=has_goals_nav,
    )

    out_path = agent_root / "dashboard.html"
    agent_root.mkdir(parents=True, exist_ok=True)
    atomic_write(out_path, html_content)
    return out_path


def render_agent_detail_resolver(agents_root: Path) -> Path:
    """Generate _dashboard/agent-detail.html resolver (MUST 1).

    Called from render_all() after rendering all per-agent pages. Also called
    standalone when only the resolver needs updating.
    """
    from .costs import discover_agents

    agent_ids = list(discover_agents(agents_root))
    resolver_html = _render_resolver(agent_ids)
    out_dir = agents_root / "_dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "agent-detail.html"
    atomic_write(out_path, resolver_html)
    return out_path
