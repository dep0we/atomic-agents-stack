"""Fleet Health Score targets.md parser (spec/53).

Parses the operator-editable targets.md file at <agents_root>/targets.md.
Applies per-key fail-soft: a missing or malformed key falls back to the
baked-in default for THAT key only, never the whole file (spec/53 MUST 6).

Schema (model.md-style — ONE embedded fenced YAML block, single root key):

```markdown
## Fleet Health Targets

```yaml
scoring:
  weights:
    cost: 0.333
    quality: 0.333
    reliability: 0.334
  axes:
    cost:
      metrics:
        cheaper_model_share:
          target: 0.50
          direction: higher
          band: 0.10
          floor: 0.0
        tokens_per_output:
          target: 500
          direction: lower
          band: 100
          floor: 5000
        spend_vs_trend:
          target: 0.05
          direction: lower
          band: 0.02
          floor: 0.50
    quality:
      metrics:
        pass_rate:
          target: 0.80
          direction: higher
          band: 0.10
          floor: 0.0
        hard_fail_rate:
          target: 0.0
          direction: lower
          band: 0.05
          floor: 0.50
    reliability:
      metrics:
        error_rate:
          target: 0.0
          direction: lower
          band: 0.05
          floor: 0.40
        blocked_rate:
          target: 0.0
          direction: lower
          band: 0.05
          floor: 0.30
        skipped_rate:
          target: 0.0
          direction: lower
          band: 0.05
          floor: 0.40
```
```

Design notes:
- yaml.safe_load() ONLY — never yaml.load() (alias-bomb risk; spec/25 PR1).
- Per-key fail-soft: walk each expected key with .get(); TypeError/missing →
  fall back to baked-in default for that key, log a warning.
- YAML parse error (syntax) → ALL defaults, log warning.
- targets.override.md: deep-merge key-by-key onto fleet targets, skip None values.
- targets.md absent → all defaults (home-user throughline — valid state, not error).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # yaml.safe_load only — spec/25 PR1 / mandates_md.py:330

logger = logging.getLogger(__name__)


def _is_real_number(v: object) -> bool:
    """True iff v is a finite, non-bool int/float.

    bool is an int subclass in Python (``isinstance(True, int)`` is True), so a
    YAML ``true`` would otherwise pass an ``isinstance(v, (int, float))`` gate and
    coerce to 1.0. NaN/inf are also rejected — they corrupt the scoring curve's
    span arithmetic silently. Both are DOMAIN failures, not type failures, so the
    callers fail soft to the baked-in default for that key (spec/53 MUST 6).
    """
    if isinstance(v, bool):
        return False
    if not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


# ──────────────────────────────────────────────────────────────────
# Baked-in defaults (normative constants — spec/53 §4)
# These are the values used when targets.md is absent, a key is missing,
# or a value is malformed. They are NOT derived from PRICING at runtime.

_DEFAULT_WEIGHTS = {
    "cost": 1.0 / 3.0,
    "quality": 1.0 / 3.0,
    "reliability": 1.0 / 3.0,
}

# Cheap-model output-rate cutoff: see spec/53 §5.1.
# cheap iff PRICING[model]['output'] < this value (strict less-than).
# Ties are classified NOT cheap (fail-pessimistic, spec/53 MUST 9).
CHEAP_OUTPUT_RATE_THRESHOLD_USD_PER_1M = 5.0  # $5.00/1M output tokens

# Critical sub-score threshold and composite ceiling (spec/53 MUST 4).
# Any sub-score below this value → composite capped at CRITICAL_COMPOSITE_CAP
# AND band forced to 'red', regardless of other axes.
CRITICAL_SUBSCORE_THRESHOLD = 30  # sub-score below 30 is critical
CRITICAL_COMPOSITE_CAP = 60  # composite cannot exceed 60 when any axis is critical

# Band boundaries (spec/53 §3.3)
BAND_GREEN_MIN = 80
BAND_AMBER_MIN = 60  # 60..79 = amber
# red: 0..59

_DEFAULT_AXES: dict[str, dict] = {
    "cost": {
        "metrics": {
            "cheaper_model_share": {
                "target": 0.50,
                "direction": "higher",
                "band": 0.10,
                "floor": 0.0,
            },
            "tokens_per_output": {
                "target": 500.0,
                "direction": "lower",
                "band": 100.0,
                "floor": 5000.0,
            },
            "spend_vs_trend": {
                "target": 0.05,
                "direction": "lower",
                "band": 0.02,
                "floor": 0.50,
            },
        }
    },
    "quality": {
        "metrics": {
            "pass_rate": {
                "target": 0.80,
                "direction": "higher",
                "band": 0.10,
                "floor": 0.0,
            },
            "hard_fail_rate": {
                "target": 0.0,
                "direction": "lower",
                "band": 0.05,
                "floor": 0.50,
            },
        }
    },
    "reliability": {
        "metrics": {
            "error_rate": {
                "target": 0.0,
                "direction": "lower",
                "band": 0.05,
                "floor": 0.40,
            },
            "blocked_rate": {
                "target": 0.0,
                "direction": "lower",
                "band": 0.05,
                "floor": 0.30,
            },
            "skipped_rate": {
                "target": 0.0,
                "direction": "lower",
                "band": 0.05,
                "floor": 0.40,
            },
        }
    },
}


@dataclass
class MetricTarget:
    """Per-metric scoring target."""

    target: float
    direction: str  # 'higher' | 'lower'
    band: float  # tolerance around target (still scores 100)
    floor: float  # metric value at which score = 0


@dataclass
class FleetTargets:
    """Parsed + fail-soft merged fleet scoring targets."""

    weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    axes: dict[str, dict[str, MetricTarget]] = field(default_factory=dict)
    used_defaults: list[str] = field(default_factory=list)  # keys that fell back


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base; skip None override values."""
    result = dict(base)
    for k, v in override.items():
        if v is None:
            # null in YAML override = no-op (keep parent value)
            continue
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _extract_scoring_yaml(text: str) -> dict | None:
    """Extract the first fenced YAML block that contains a top-level 'scoring:' key.

    Never merges multiple blocks. Returns None if none found.
    Uses yaml.safe_load (spec/25 PR1 / mandates_md.py:330 — never yaml.load).
    """
    blocks = re.findall(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
    for block in blocks:
        try:
            parsed = yaml.safe_load(block)  # MUST be safe_load — spec/25 PR1
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        if "scoring" in parsed:
            return parsed
    return None


def _parse_metric(
    raw: object, axis: str, metric: str, used_defaults: list[str]
) -> MetricTarget:
    """Parse a single metric config dict, applying per-key fail-soft."""
    defaults = _DEFAULT_AXES.get(axis, {}).get("metrics", {}).get(metric, {})
    if not isinstance(raw, dict):
        used_defaults.append(f"{axis}.metrics.{metric}")
        return MetricTarget(
            target=float(defaults.get("target", 0.0)),
            direction=str(defaults.get("direction", "lower")),
            band=float(defaults.get("band", 0.05)),
            floor=float(defaults.get("floor", 1.0)),
        )

    def _get_float(key: str) -> float:
        v = raw.get(key)
        if v is None or not _is_real_number(v):
            used_defaults.append(f"{axis}.metrics.{metric}.{key}")
            return float(defaults.get(key, 0.0))
        return float(v)

    def _get_direction() -> str:
        """Parse the optimization direction with per-key enum fail-soft (MUST 6).

        `direction` is a closed enum {'higher','lower'}. A string that is not one of
        those (an operator typo like 'highr' or 'up') would otherwise be accepted
        verbatim and silently invert the score curve in _map_metric_to_score (which
        treats anything != 'higher' as 'lower'). Fail soft to the baked-in default
        for THIS key and record it, exactly as a non-string value would.
        """
        v = raw.get("direction")
        if v in ("higher", "lower"):
            return v
        used_defaults.append(f"{axis}.metrics.{metric}.direction")
        default_dir = str(defaults.get("direction", "lower"))
        if v is not None:
            logger.warning(
                "advisor targets: %s.metrics.%s.direction=%r is not 'higher'|'lower'; "
                "falling back to default %r",
                axis,
                metric,
                v,
                default_dir,
            )
        return default_dir

    target = _get_float("target")
    direction = _get_direction()
    band = _get_float("band")
    floor_val = _get_float("floor")

    # Domain guard: band must be a non-negative tolerance.
    if band < 0:
        used_defaults.append(f"{axis}.metrics.{metric}.band(negative)")
        band = float(defaults.get("band", 0.05))

    # Domain guard: the decay span must be POSITIVE, else the curve has no linear
    # region — it becomes a cliff straight to 0 (or a ZeroDivisionError). The
    # denominator in _map_metric_to_score is:
    #   higher: span = (target - band) - floor   → require floor < target - band
    #   lower:  span = floor - (target + band)   → require floor > target + band
    # The previous code only rejected floor == target, so an operator could set a
    # span <= 0 (e.g. higher-is-better with floor >= target-band) and silently turn
    # the metric into a binary cliff. On a bad span, default the whole metric config
    # for THIS metric and mark it (spec/53 MUST 6).
    if direction == "higher":
        span = (target - band) - floor_val
    else:
        span = floor_val - (target + band)
    if span <= 1e-9:
        used_defaults.append(f"{axis}.metrics.{metric}(degenerate_span)")
        return MetricTarget(
            target=float(defaults.get("target", 0.0)),
            direction=str(defaults.get("direction", "lower")),
            band=float(defaults.get("band", 0.05)),
            floor=float(defaults.get("floor", 1.0)),
        )

    return MetricTarget(target=target, direction=direction, band=band, floor=floor_val)


def _parse_weights(raw_weights: object, used_defaults: list[str]) -> dict[str, float]:
    """Parse weights dict with per-key fail-soft + domain + sum validation."""
    if not isinstance(raw_weights, dict):
        used_defaults.append("weights")
        return dict(_DEFAULT_WEIGHTS)

    result: dict[str, float] = {}
    for axis in ("cost", "quality", "reliability"):
        v = raw_weights.get(axis)
        # Reject bool/NaN/inf/None HERE: bool subclasses int, so the bare
        # isinstance(v, (int, float)) gate accepted `cost: true` and coerced it
        # to 1.0 before the [0,1] domain guard below ever ran. _is_real_number
        # rejects bool/NaN/inf (and None) up front.
        if not _is_real_number(v):
            used_defaults.append(f"weights.{axis}")
            result[axis] = _DEFAULT_WEIGHTS[axis]
        else:
            result[axis] = float(v)

    # Domain validation BEFORE the sum check: a weight must be a finite, non-bool
    # number in [0, 1]. A weighted MEAN is undefined otherwise — a negative weight
    # subtracts an axis (`cost=.55, quality=.55, reliability=-.10` sums to 1.0 but
    # is NOT a weighted mean), a weight > 1 over-counts an axis past the whole, and
    # NaN/inf/bool corrupt the arithmetic. The sum check alone misses all of these.
    # Any out-of-domain weight invalidates the whole vector → equal weights.
    for axis in ("cost", "quality", "reliability"):
        w = result[axis]
        if not _is_real_number(w) or w < 0.0 or w > 1.0:
            logger.warning(
                "targets.md weights.%s=%r out of [0,1]; using equal weights",
                axis,
                w,
            )
            used_defaults.append("weights")
            return dict(_DEFAULT_WEIGHTS)

    # Validate sum ≈ 1.0 (tolerance 0.01); if not, fall back to equal weights.
    total = sum(result.values())
    if abs(total - 1.0) >= 0.01:
        logger.warning(
            "targets.md weights sum to %.4f (expected ~1.0); using equal weights",
            total,
        )
        used_defaults.append("weights(invalid_sum)")
        return dict(_DEFAULT_WEIGHTS)

    return result


def _parse_axes(
    raw_axes: object, used_defaults: list[str]
) -> dict[str, dict[str, MetricTarget]]:
    """Parse axes config with per-key fail-soft."""
    result: dict[str, dict[str, MetricTarget]] = {}

    for axis_name, default_axis_cfg in _DEFAULT_AXES.items():
        if not isinstance(raw_axes, dict) or axis_name not in raw_axes:
            # Whole axis missing — use all defaults for that axis
            used_defaults.append(f"axes.{axis_name}")
            result[axis_name] = {
                metric: MetricTarget(
                    target=float(cfg["target"]),
                    direction=str(cfg["direction"]),
                    band=float(cfg["band"]),
                    floor=float(cfg["floor"]),
                )
                for metric, cfg in default_axis_cfg["metrics"].items()
            }
            continue

        raw_axis = raw_axes[axis_name]
        raw_metrics = raw_axis.get("metrics", {}) if isinstance(raw_axis, dict) else {}
        axis_metrics: dict[str, MetricTarget] = {}

        for metric_name in default_axis_cfg["metrics"]:
            raw_metric = (
                raw_metrics.get(metric_name) if isinstance(raw_metrics, dict) else None
            )
            axis_metrics[metric_name] = _parse_metric(
                raw_metric, axis_name, metric_name, used_defaults
            )

        result[axis_name] = axis_metrics

    return result


def parse_targets(agents_root: Path) -> FleetTargets:
    """Parse <agents_root>/targets.md (+ optional targets.override.md) into FleetTargets.

    Fail-soft posture (spec/53 MUST 6):
    - File absent → all defaults (valid home-user state, not an error).
    - YAML block missing/unparseable → all defaults.
    - Individual key missing/malformed → default for THAT key only.
    - targets.override.md: parsed separately, schema-validated, then deep-merged.
      Override entries that are None (YAML null) are skipped.
    """
    used_defaults: list[str] = []
    raw_scoring: dict = {}

    base_path = agents_root / "targets.md"
    if base_path.exists():
        try:
            text = base_path.read_text(encoding="utf-8")
            parsed = _extract_scoring_yaml(text)
            if parsed is None:
                logger.warning(
                    "targets.md: no 'scoring:' YAML block found; using defaults"
                )
                used_defaults.append("(no_scoring_block)")
            else:
                raw_scoring = parsed.get("scoring", {}) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "targets.md: parse error (%s); using all defaults", type(exc).__name__
            )
            used_defaults.append("(parse_error)")
    else:
        # Absent is a valid home-user state — not an error, just a note.
        used_defaults.append("(targets.md absent)")

    # Apply targets.override.md deep-merge (if present)
    override_path = agents_root / "targets.override.md"
    if override_path.exists():
        try:
            override_text = override_path.read_text(encoding="utf-8")
            override_parsed = _extract_scoring_yaml(override_text)
            if override_parsed is not None:
                override_scoring = override_parsed.get("scoring", {}) or {}
                # Validate override structure before merging (fail-soft: skip on error)
                if isinstance(override_scoring, dict):
                    raw_scoring = _deep_merge(raw_scoring, override_scoring)
                else:
                    logger.warning(
                        "targets.override.md: 'scoring' is not a dict; skipping override"
                    )
        except (OSError, yaml.YAMLError) as exc:
            logger.warning(
                "targets.override.md: parse error (%s); using base targets only",
                type(exc).__name__,
            )

    # Parse weights with per-key fail-soft + sum validation
    raw_weights = raw_scoring.get("weights") if isinstance(raw_scoring, dict) else None
    weights = _parse_weights(raw_weights, used_defaults)

    # Parse axes
    raw_axes = raw_scoring.get("axes") if isinstance(raw_scoring, dict) else None
    axes = _parse_axes(raw_axes, used_defaults)

    return FleetTargets(weights=weights, axes=axes, used_defaults=used_defaults)
