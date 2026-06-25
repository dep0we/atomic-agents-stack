# spec/52 — Fleet Observability Console (DRAFT)

**Status:** DRAFT  
**Issue:** #614 (PR1: Visibility layer)  
**Depends on:** spec/51 (AgentRegistryBackend), spec/43 (JournalBackend atomicity pattern), spec/22 (LogBackend RunRecord), spec/09 (cost-read error posture)  
**Supersedes:** Nothing — this spec extends the dashboard surface without replacing any existing spec.

---

## Overview

The Fleet Observability Console is the new landing page for the atomic-agents dashboard. It replaces the cost-view home page with an Operator Attention Queue + three-axis trend panels (Cost / Quality / Reliability), while preserving all existing dashboard tabs as first-class views.

**PR1 scope (this spec):** the VISIBILITY layer — observe and ack/snooze only. No composite scoring (that is #615/spec/53). No per-agent policy enforcement. No async notification channels.

**The home-user throughline is load-bearing.** A home user with one healthy agent must see a useful, orientation-friendly page — not a blank panel or an empty error state. The console renders cost trends and fleet status even when the attention queue is empty.

---

## 1. Landing page flip (BEHAVIOR CHANGE)

The dashboard home page (`GET /`) now serves the Fleet Console Attention Queue.

| Path | Before PR1 | After PR1 |
|------|-----------|-----------|
| `GET /` / `index.html` | Global cost view | Fleet Console home |
| `GET /cost` / `cost.html` | (did not exist) | Global cost view |
| `GET /console` | (did not exist) | Fleet Console home (alias) |

**Migration notes for operators:**
- Bookmarks and scripts pointing at `/_dashboard/index.html` or `GET /` now land on the Fleet Console, not the cost view.
- The cost view is now at `GET /cost` (`/_dashboard/cost.html`).
- Per-agent breadcrumbs still point to `../_dashboard/index.html`, which now correctly navigates to the Fleet Console home.
- `atomic-agents dashboard serve` regenerates `index.html` (console home) and `cost.html` (cost view) on startup if either is absent.

---

## 2. Operator Attention Queue

The attention queue is the spine of the console home. It surfaces actionable items ranked by severity, then agent name. Each item has a stable `alert_key` so ack/snooze state survives across render cycles.

### 2.1 Alert classes

Four alert classes are aggregated in ranked order:

| Priority | Class | Source |
|----------|-------|--------|
| 1 | `governance` | AgentRegistryBackend.list_agents(include_governance=True) |
| 2 | `cost_spike` | LogBackend run records (30-day window vs prior 30-day baseline) |
| 3 | `quality_regression` | evals/runs/*.jsonl (lightweight per-agent reader) |
| 4 | `reliability` | LogBackend run records — explicit status markers only |

### 2.2 Governance alert subclasses

**MUST 9:** The governance alert class distinguishes four states from the registry's five-state governance model (`FilesystemAgentRegistryBackend`). Each maps to a different `next_step` and dedups separately via the `alert_key` scheme:

| Subclass | Condition | Severity |
|----------|-----------|----------|
| `NO_GOVERNANCE` | `has_governance=False` (governance.md absent) | high |
| `GOVERNANCE_INVALID` | governance.md present, `parse_errors` non-empty | high |
| `GOVERNANCE_INCOMPLETE` | governance.md present, valid, but `owner=None` | medium |
| `GOVERNANCE_NO_BLOCK` | governance.md present + readable but no `governance:` YAML block (`has_governance=True`, `governance=None`) | high |

`GOVERNANCE_NO_BLOCK` corresponds to the registry's PRESENT_NO_BLOCK state: a prose-only or broken governance.md that exists and is readable but carries no structured record. It MUST NOT fall through to a no-alert path — a governance file with no enforceable block is a high-priority gap, not a healthy agent. Its `reason_bucket` is `governance_no_block`; its `next_step` directs the operator to add the YAML block from the init template.

**NOTE:** "Overdue review by date" is DEFERRED to PR2. The `GovernanceRecord.review` schema has no `review_cadence` or `next_review_by` field. PR1 only alerts on the four owner/block-absence states above. A future PR will add `review_cadence` to governance.md and `GovernanceRecord` to enable date-based overdue detection.

### 2.3 Reliability axis signal definitions

**MUST 8:** The Reliability axis is derived ONLY from explicit `RunRecord` structural markers. No heuristic stuck/looping inference.

| Metric | Signal source |
|--------|--------------|
| `error_rate` | `status == "error"` |
| `blocked_rate` | `status == "lock_busy"` OR `extra["embed_batch_blocked"] == True` |
| `inflight_rate` | `status == "in_flight"` |
| `principal_rate` | `status == "principal_not_verified"` |
| `skipped_rate` | `status in {"skipped", "deduped"}` |

The `embed_batch_blocked` signal lives in `RunRecord.extra` (a pass-through dict for non-canonical JSONL fields, added to the dashboard `RunRecord` in this PR). The dashboard `RunRecord` dataclass now includes `extra: dict = field(default_factory=dict)` populated by `_record_from_dict()` from all JSONL keys not in the canonical field set.

The `skipped_rate` axis (`status in {"skipped", "deduped"}`) carries the framework's MOST COMMON cost-guardrail refusal: the primary/mid-loop cost gate in `agent.call()` logs `status="skipped"` with summary `"Skipped: <reason>"`. This is a first-class operator signal — an agent that keeps being refused at its cost cap is surfaced as a `reliability.high_skipped_rate` attention item (separate from `blocked_rate`, which carries lock-contention and embed-gate blocks), with a "raise the cap or cut the workload" next-step. A blind-read fail-close (`cost_data_degraded=True` on the skip record) is a degraded read, not a true cap hit; it is still counted in `skipped_rate` for visibility but the degraded-data banner (§9) is the operator's primary cue there.

**PR1 default thresholds (hardcoded — tunability deferred to PR2 #615):**
- Error rate warn: ≥ 20%
- Blocked rate warn (lock/embed): ≥ 10%
- Skipped rate warn (cost-guardrail): ≥ 10%
- Cost spike multiplier: 3× baseline daily average
- Cost spike minimum baseline: 7 days of prior history
- Quality regression threshold: delta_30d ≤ −10%

---

## 3. Alert key dedup contract

**MUST 7:** Each alert in the queue has a stable `alert_key` that does not change when transient specifics change (run_id, exact timestamp, exact dollar figure).

### 3.1 Derivation

```
canonical = "\x00".join(["v1", agent_id, alert_class, reason_bucket])
alert_key = "v1:" + sha256(canonical.encode("utf-8")).hexdigest()[:12]
```

The NUL separator (`\x00`) prevents prefix-collision attacks (e.g. `"foo" + "barx"` producing the same hash as `"foobar" + "x"`). This matches the PrincipalBackend MUST 11 key derivation pattern (spec/48).

The `v1:` version prefix allows future normalization changes to be detected: when the normalization logic changes, the version token is incremented and old sidecar entries (keyed by the old scheme) no longer match, making the invalidation explicit rather than silent.

### 3.2 Reason bucket normalization

Each alert class uses a fixed, stateless reason bucket string. Transient specifics are normalized OUT:

| Alert class | reason_bucket |
|-------------|---------------|
| `governance.NO_GOVERNANCE` | `"no_governance_file"` |
| `governance.GOVERNANCE_INVALID` | `"governance_parse_error"` |
| `governance.GOVERNANCE_INCOMPLETE` | `"no_owner_field"` |
| `cost_spike` | `"cost_above_threshold"` |
| `quality_regression` | `"score_regression_threshold"` |
| `reliability.high_error_rate` | `"high_error_rate"` |
| `reliability.high_blocked_rate` | `"high_blocked_rate"` |
| `reliability.high_skipped_rate` | `"high_skipped_rate"` |

**Never** include run_id, timestamps, exact dollar amounts, or exact percentages in the reason bucket. These produce different keys on each render, defeating dedup and making ack/snooze state stale immediately.

### 3.3 Status derivation

| Sidecar entry | Queue status |
|---------------|-------------|
| Not in sidecar | `new` |
| In sidecar, `status == "acked"` | `known` |
| In sidecar, `status == "snoozed"` (unexpired) | `known` |
| In sidecar, `status == "open"` (unsnooze'd or expired snooze) | `recurring` |

---

## 4. Alert state sidecar (spec/52 MUST 1, MUST 2)

Ack/snooze state is persisted in an append-only JSONL event log at:

```
<agents_root>/_console/alert_state.jsonl
```

The `_console/` directory is excluded from registry discovery by the `_` prefix (same predicate as `FilesystemAgentRegistryBackend.list_agents()`). Do not rename it without updating that predicate.

### 4.1 Event schema

Each line is a JSON object:

```json
{
  "ts": "2026-06-24T14:30:00+00:00",
  "actor": "operator",
  "alert_key": "v1:abc123def456",
  "action": "ack",
  "snooze_until": null
}
```

`action` is one of: `"ack"`, `"snooze"`, `"unsnooze"`.

**MUST 6:** `snooze_until` MUST be a UTC ISO-8601 string (ending in `+00:00`). JavaScript's `Date.toISOString()` produces a `Z`-suffix string; this is normalized to `+00:00` at write time. The compaction reader compares `snooze_until` against `datetime.now(tz=timezone.utc)`.

### 4.2 Atomicity (MUST 1)

**MUST 1:** All appends acquire an exclusive `fcntl.flock(LOCK_EX)` on `_console/.alert_state.lock` before writing. The lock is held across the ENTIRE append (and compaction if triggered). Concurrent serve requests from `ThreadingHTTPServer` are serialized by this lock.

Implementation mirrors `FilesystemJournalBackend._journal_lock()` (spec/43 MUST 9) exactly: two nested `try/finally` blocks, `os.open(O_RDWR | O_CREAT)`, `flock(LOCK_EX)`, `flock(LOCK_UN)` in the inner `finally`, `os.close()` in the outer `finally`.

**This module is POSIX-only.** `fcntl` is imported at module level with no Windows fallback, consistent with `journal/filesystem.py`. Windows operators get a clear `ImportError` rather than a silent no-op bypass.

`_console/` is created with `exist_ok=True` before the `os.open()` call so the lock file creation cannot fail with `ENOENT` (mirrors `FilesystemJournalBackend._journal_lock()` line 260).

### 4.3 Compaction (MUST 2)

**MUST 2:** The sidecar's current state is **replayed on read** (events replayed in file-append order; the last event per `alert_key` wins). The size-reducing **log compaction (rewrite to one event per live alert) runs on WRITE** inside `append_alert_event()`, on the next append once the raw JSONL file exceeds 1000 lines (`_COMPACT_THRESHOLD`) — `read_alert_state()` never rewrites the file. The compaction rewrite MUST round-trip: the rewritten file MUST replay to the identical state. Because the reader keys on the *action* verb (`ack`/`snooze`/`unsnooze`) while the compacted state carries the *status* string (`acked`/`snoozed`/`open`), `_state_to_jsonl` MUST emit action verbs (via `_STATUS_TO_ACTION`), not status strings — emitting status strings would produce lines the reader drops, silently losing all ack/snooze state after the first compaction.

Both the read replay and the write-time compaction rewrite run under an exclusive flock as appends (`LOCK_SH` on read, `LOCK_EX` on the append+rewrite). A concurrent append cannot interleave with a compaction rewrite.

Compaction determinism contract:
- Ordering is file-append order.
- Last event per `alert_key` determines current state.
- `snooze_until` expiry is checked at compaction time against `datetime.now(tz=timezone.utc)`.
- Corrupt lines (non-parseable JSON) are silently skipped; the remainder of the file is processed normally.
- The compaction REWRITE re-emits one event per surviving `alert_key` using the canonical *action* verb (`ack` / `snooze` / `unsnooze`), NOT the derived *status* string (`acked` / `snoozed` / `open`). This is load-bearing: the replay reader keys on the action verb, so a rewrite that emitted the status string would re-parse to the empty state, silently dropping every acked/snoozed entry after the first compaction. The post-compaction file MUST reconstruct the identical pre-compaction state.

### 4.4 Fail-soft degraded read

`read_alert_state()` returns `{}` when the sidecar is absent (normal before any ack/snooze event). On `OSError` it also returns `{}` with a warning log rather than crashing the dashboard render (Principle #8, degrade-not-crash).

---

## 5. Ack/snooze endpoints

### 5.1 URL surface

```
POST /alerts/ack
POST /alerts/snooze
```

Both endpoints are JSON: `Content-Type: application/json`.

`/alerts/ack` body: `{"alert_key": "<key>"}`  
`/alerts/snooze` body: `{"alert_key": "<key>", "snooze_until": "<UTC ISO-8601>"}`

### 5.2 Loopback-only enforcement (MUST 3)

**MUST 3:** The ack/snooze endpoints (and `/regenerate`) MUST return HTTP 403 for a non-loopback CLIENT PEER, regardless of how the `--host` bind parameter was set. The guard is per-request: `_is_loopback(self.client_address[0])` — the actual remote peer set by `socketserver.BaseRequestHandler` — is checked at the top of `do_POST` for all three write routes.

This is per-caller defense-in-depth: a user who runs `atomic-agents dashboard serve --host 0.0.0.0` to expose the read-only dashboard on a LAN will find the write endpoints refuse a remote (non-loopback) caller with 403, WHILE the operator's own `127.0.0.1` ack/snooze/regenerate POSTs keep working. Gating on the bind address instead would 403 every write — including the local operator's — whenever the server is bound to anything other than pure loopback, killing the write endpoints in LAN-exposed mode.

### 5.3 Closed-allowlist validation (MUST 4)

**MUST 4:** The submitted `alert_key` MUST be validated against the set of `alert_keys` rendered in the most recent console render. Unknown keys are rejected with HTTP 422.

The allowlist is persisted to `_console/rendered_alert_keys.json` by `render_console()` after every successful render (written atomically via `atomic_write`). The POST handler reads this file rather than re-running the full aggregation:

- File absent → HTTP 503 ("Console not yet rendered. Run /regenerate first.")
- File unreadable → HTTP 503
- `alert_key` not in file → HTTP 422
- `alert_key` in file → proceed with append

This decouples validation from re-aggregation, survives server restart, and degrades gracefully on a degraded backend read.

### 5.4 Idempotency (MUST 5)

**MUST 5:** An action that leaves alert state unchanged is a no-op: the response body includes `"changed": false` and no new event is appended to the sidecar. This covers three cases — (1) re-acking an already-acked alert, (2) an identical same-window re-snooze (`action="snooze"`, current status `snoozed`, and the normalized incoming `snooze_until` equals the current entry's `snooze_until`), and (3) re-unsnoozing an already-open alert. Re-snoozeing with a *different* `snooze_until` IS a state change (new event appended).

### 5.5 Request body parsing

Requests must include a `Content-Length` header. Bodies larger than 4096 bytes are rejected with HTTP 413. Missing or zero `Content-Length` is rejected with HTTP 400. Malformed JSON is rejected with HTTP 400.

### 5.6 Re-render on effective state change

The handler re-renders the console home (`render_all(tab="console")`) only when the action produces an effective state change. This updates both `index.html` and `_console/rendered_alert_keys.json`. A module-level `threading.Lock` serializes concurrent ack/snooze-triggered renders (resource efficiency — not a data-correctness lock).

---

## 6. Three-axis trend panels

The console home renders three read-only panels alongside the attention queue:

| Axis | Signal | Source |
|------|--------|--------|
| Cost | 30-day total + spike indicator | LogBackend run records |
| Quality | Latest eval score + 30-day delta | evals/runs/*.jsonl |
| Reliability | Error rate + blocked rate (30d) | LogBackend run records |

**No composite score in PR1.** The three axes are independent read-only panels. Composite scoring (#615/spec/53) is deferred.

---

## 7. Per-agent drilldown

The per-agent `dashboard.html` breadcrumb now reads "← Fleet Console" and links to `../_dashboard/index.html` (the console home). All existing per-agent cost/activity/memory/goals sections are preserved.

The governance header in the drilldown reads from `AgentRegistryBackend.get_agent(agent_id)`. If `get_agent()` returns `None` (agent just deployed, `model.md` not yet present), the governance section renders a muted placeholder. `get_agent()` failures are caught and degrade gracefully.

---

## 8. Fleet Overview — agent card grid

The console home renders an agent card grid below the three-axis panels. Each card:
- Links to the per-agent drilldown (`dashboard.html`)
- Shows 30-day cost total
- Shows an alert badge (count of open attention-queue items for that agent)

---

## 9. Fail-soft / degraded-read posture

Following spec/09 §"Cost-read error posture":

- Backend read failures degrade individual sections rather than crashing the full render.
- A `degraded=True` flag on `ConsoleData` drives a degraded-data banner on the console home (matching the existing cost-view banner pattern from spec/09/#498).
- The attention queue is rendered with whatever data was successfully read.
- The empty attention queue state ("All agents healthy") is rendered only when aggregation succeeds with zero alerts — it is never shown in place of a degraded render.

---

## 10. URL surface summary

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Fleet Console home (index.html) |
| GET | `/index.html` | Fleet Console home (alias) |
| GET | `/console` | Fleet Console home (alias) |
| GET | `/cost` | Cost view (cost.html) |
| GET | `/cost.html` | Cost view (alias) |
| GET | `/activity` | Activity tab |
| GET | `/quality` | Quality tab |
| GET | `/memory` | Memory tab |
| GET | `/goals` | Goals tab (when agents have goal.md) |
| GET | `/agents/<name>` | Per-agent drilldown |
| POST | `/regenerate` | Rebuild all dashboards (loopback-only) |
| POST | `/alerts/ack` | Ack an alert (loopback-only) |
| POST | `/alerts/snooze` | Snooze an alert (loopback-only) |

---

## 11. Implementer Contract (MUSTs)

**MUST 1 — Append-under-flock:** All appends to `_console/alert_state.jsonl` MUST acquire an exclusive `fcntl.flock(LOCK_EX)` on `_console/.alert_state.lock` before writing. Compaction (when triggered) MUST also run under the same exclusive lock. Never call `atomic_append_jsonl` for this path — `atomic_append_jsonl` has no flock and is not safe for concurrent appenders.

**MUST 2 — Compaction determinism:** Compaction MUST replay events in file-append order. The last event per `alert_key` determines the current state. `snooze_until` expiry MUST be checked against `datetime.now(tz=timezone.utc)` (tz-aware comparison). Corrupt lines MUST be skipped without crashing.

**MUST 3 — Loopback-only write endpoints:** `/alerts/ack`, `/alerts/snooze`, and `/regenerate` MUST return HTTP 403 when `self.client_address[0]` (the per-request remote peer) is not a loopback address (`127.x.x.x` or `::1`), regardless of the `--host` bind parameter. A loopback client MUST be allowed through even under a `0.0.0.0` bind.

**MUST 4 — Closed-allowlist alert_key validation:** The ack/snooze handlers MUST validate the submitted `alert_key` against `_console/rendered_alert_keys.json`. Keys not present in the file MUST be rejected with HTTP 422. If the file is absent or unreadable, MUST return HTTP 503.

**MUST 5 — Idempotency:** An action that leaves alert state unchanged MUST return `{"status": "ok", "changed": false}` without appending a new event. This covers re-acking an already-acked alert, an identical same-window re-snooze (same normalized `snooze_until`), and re-unsnoozing an already-open alert. Re-snoozeing with a *different* `snooze_until` IS a state change and MUST append a new event.

**MUST 6 — snooze_until UTC:** All `snooze_until` values MUST be stored as UTC ISO-8601 strings (ending in `+00:00`). JavaScript `Z`-suffix timestamps MUST be normalized at write time. The expiry check MUST use `datetime.now(tz=timezone.utc)`.

**MUST 7 — Alert key stability:** `alert_key` MUST be derived as `"v1:" + sha256("\x00".join(["v1", agent_id, alert_class, reason_bucket]).encode()).hexdigest()[:12]`. Reason buckets MUST NOT include run_id, exact timestamps, exact dollar figures, or any other transient specifics. The same recurring condition MUST produce the same `alert_key` across consecutive renders with different run data. Python's `hash()` MUST NOT be used for alert_key derivation.

**MUST 8 — Reliability signal definitions:** The Reliability axis MUST be derived ONLY from explicit `RunRecord` structural markers: `status in {"error", "lock_busy", "in_flight", "principal_not_verified", "skipped", "deduped"}` and `extra["embed_batch_blocked"] == True`. No heuristic stuck/looping inference is permitted in PR1.

**MUST 9 — Governance alert subclass coverage:** The governance alert generator MUST emit a distinct alert subclass for each of the four owner/block-absence states of the registry's five-state governance model (§2.2): `NO_GOVERNANCE`, `GOVERNANCE_INVALID`, `GOVERNANCE_INCOMPLETE`, and `GOVERNANCE_NO_BLOCK`. In particular the PRESENT_NO_BLOCK state (`has_governance=True`, `governance=None`) MUST NOT fall through to a no-alert path — it emits a `GOVERNANCE_NO_BLOCK` alert (severity high, `reason_bucket="governance_no_block"`). A fully-valid governance.md with an owner emits NO governance alert.

---

## 12. Conformance coverage

Each MUST ships with parametrized conformance coverage in `tests/test_dashboard_console.py`:

| MUST | Key test functions |
|------|-------------------|
| MUST 1 | `test_append_creates_console_dir`, `test_append_writes_valid_json`, `test_read_alert_state_round_trip`, `test_console_dir_excluded_from_agent_discovery` |
| MUST 2 | `test_compaction_last_event_wins`, `test_compaction_skips_corrupt_lines`, `test_compaction_empty_lines`, `test_compaction_rewrite_round_trips_state` (strip-RED on `_STATUS_TO_ACTION`), `test_live_compaction_preserves_other_key_state` (real >`_COMPACT_THRESHOLD` rewrite keeps an unrelated ack) |
| MUST 3 | `test_ack_returns_403_on_non_loopback_peer` (strip-RED on the peer-address gate), `test_ack_loopback_client_under_0_0_0_0_bind_not_403`, `test_is_loopback_network_addr_strip_red` |
| MUST 4 | `test_forged_alert_key_rejected`, `test_503_when_no_rendered_keys_sidecar` |
| MUST 5 | `test_ack_idempotent` |
| MUST 6 | `test_snooze_until_z_suffix_normalized`, `test_snoozed_item_expires_when_past`, `test_snoozed_item_active_when_future` |
| MUST 7 | `test_alert_key_stable_across_different_run_ids`, `test_alert_key_uses_sha256_not_python_hash`, `test_alert_key_nul_separator_prevents_prefix_collision` |
| MUST 8 | `test_reliability_error_rate`, `test_reliability_embed_blocked_from_extra`, `test_reliability_embed_blocked_strip_red`, `test_reliability_lock_busy_counted`, `test_reliability_skipped_rate_counts_cost_blocks`, `test_reliability_alerts_fires_on_high_skipped_rate` |
| MUST 9 | `test_governance_no_governance_alert`, `test_governance_invalid_alert`, `test_governance_incomplete_alert`, `test_governance_no_block_alert` (strip-RED on the PRESENT_NO_BLOCK branch), `test_governance_valid_emits_no_alert` |

---

## 13. Performance notes

- `list_agents(include_governance=True)` reads and parses every agent's `governance.md` on every console render. For a fleet of 50+ agents, this is 50+ file reads at render time. Acceptable for PR1 (visibility cut, not a performance release). A caching layer (invalidated on `governance.md` mtime change) is filed as a follow-up for PR2.
- The quality signal reader does a lightweight direct read of `evals/runs/*.jsonl` per agent on standalone console renders. On full `render_all()` calls, the quality signals are shared from the quality tab's aggregation to avoid a second evals/ read (progressive disclosure, Principle #6).
- `aggregate_console()` accepts pre-aggregated `quality_signals` as an optional parameter for this sharing pattern.

---

## 14. Deferred to PR2 / follow-up issues

- Composite health score (#615/spec/53): a single score derived from the three axes with configurable weights.
- Reliability scoring thresholds configurable in `model.md` `cost_guardrails` (filed as follow-up per ruling).
- Overdue-review alert by date: requires adding `review_cadence` and `next_review_by` fields to `governance.md` / `GovernanceRecord`.
- PostgresConversationBackend-style shared-backend console persistence.
- Performance: governance.md read caching; alert state sidecar distributed backend.
- PR2: `targets.md` per-agent governance targets (spec/53 scope).

---

## 15. LOCK ceremony checklist (for future LOCK PR)

- [ ] All 9 MUSTs have parametrized conformance tests with strip-RED negative controls
- [ ] No inline `TODO` markers in spec/52 body
- [ ] `docs/protocols-shipped.md` updated with console entry
- [ ] CLAUDE.md status block updated (dashboard tab count, spec/52 LOCKED notation)
- [ ] CHANGELOG `[Unreleased]` includes BEHAVIOR CHANGE callout for landing page flip
- [ ] `render_all()` `tab` parameter docstring updated to include `"console"`
- [ ] Cross-spec addenda: spec/22 (no new RunRecord fields required — `extra` was already implicit), spec/09 (degraded-read posture extended to console renders)
