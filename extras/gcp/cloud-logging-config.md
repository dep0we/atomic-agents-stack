# cloud-logging-config.md - Cloud Logging for atomic-agents-stack
#
# NOT YET DOGFOODED against the live GCP platform.
# Claims verified against GCP documentation as of 2026-06-09.
# See extras/gcp/README.md §External-claim verification status.

# Cloud Logging - what this deployment sends and what it doesn't

## What Cloud Logging actually receives

Cloud Run forwards only stdout and stderr to Cloud Logging. This deployment
sends two signal streams:

1. **uvicorn HTTP access logs** - one line per request: client address, request
   line (method + path), and status code. (`atomic-agents serve` runs uvicorn
   with its default access formatter, which emits no latency or response-byte
   fields; those require a custom uvicorn access formatter or the Cloud Run
   `request_count` / `request_latencies` infra metrics - see
   `cloud-monitoring-policies.yaml`.) These appear in Cloud Logging under
   `resource.type="cloud_run_revision"`.
2. **Python `_logger` output** - `_logger.warning` and `_logger.error` calls
   from the framework's error paths (MCP subprocess failures, backend
   connection errors, unhandled exceptions in the serve layer).

## What Cloud Logging does NOT receive

**The JSONL audit trail** - including `run_id`, `parent_run_id`, `http_caller`,
`cost_usd`, `status`, and every other field in the `RunRecord` shape (spec/22)
- lives in the **LogBackend**, not in Cloud Logging.

- **FilesystemLogBackend** (v0 default): JSONL files on the ephemeral container
  layer at `<agent_root>/log/` (Cloud Run stateless v0 - logs are NOT durable
  across container restarts in this topology; activate PostgresLogBackend for
  durability). For the Compute Engine VM path (extras/gcp/compute-vm/),
  FilesystemLogBackend writes to the persistent disk and logs survive reboots.
  Query by reading/grepping the JSONL or via the dashboard (see §Querying the
  JSONL audit trail).
- **PostgresLogBackend** (issue #258 PR 1, shipped - activate with the
  `[postgres]` extra + `ATOMIC_AGENTS_LOG_BACKEND=postgres`): Cloud SQL. Query
  via psql against `run_records`, or the dashboard pointing at
  `ATOMIC_AGENTS_LOG_BACKEND=postgres`. Recommended for Cloud Run v0 if you
  need durable run logs.

Neither backend writes to stdout. Cloud Logging receives the uvicorn access
log line for each HTTP call, **not** the structured JSONL run record.

**Practical consequence for compliance audits:** queries against Cloud Logging
will show HTTP 402 responses (cost-cap skips) in the access log but will NOT
show the accompanying `cost_usd`, `run_id`, or the skip reason. Those live in
the persisted RunRecord. The persisted RunRecord carries `status="skipped"`
and the human-readable reason inside its `summary` field - `"Skipped:
<reason>"` for a pre-loop skip, `"cost cap hit at iteration N: <reason>"` for
a mid-loop skip. There is no dedicated `skip_reason` field/column; the reason
is embedded in `summary`. The HTTP 402 response body additionally exposes the
reason under the JSON key `reason` (e.g.
`{"status": "skipped", "reason": "...", "run_id": "..."}`), sourced from the
`Response.skip_reason` attribute in the serve layer. To query the full audit
trail, connect to Cloud SQL directly or read the JSONL files (see below).

---

## Log-based alerts

Because the JSONL audit trail is not in Cloud Logging, alert policies based on
JSONL fields (e.g., `jsonPayload.status="skipped"`) will never fire against
Cloud Logging. Use HTTP response-code metrics instead:

- **Cost guardrail breach (HTTP 402):** see `cloud-monitoring-policies.yaml`
  for the Cloud Monitoring alert on `request_count` filtered to
  `response_code=402`.
- **Agent error (HTTP 500):** filter `request_count` to `response_code=500`.
- **Lock busy / double-trigger (HTTP 503):** filter to `response_code=503`.

---

## Severity mapping

The framework's Python logger uses standard Python log levels. Cloud Run
maps these to Cloud Logging severities:

| Python level | Cloud Logging severity |
|---|---|
| `DEBUG` | `DEBUG` |
| `INFO` | `INFO` |
| `WARNING` | `WARNING` |
| `ERROR` | `ERROR` |
| `CRITICAL` | `CRITICAL` |

`_logger.warning` calls appear for backend connection failures and MCP server
startup issues. `_logger.error` calls appear for unhandled exceptions reaching
the serve layer.

Cost-cap *warnings* are NOT a stdout / `_logger.warning` stream. Threshold-breach
warnings (`trigger="cost_warning"`, `status="ok"`) are written to the **LogBackend
JSONL audit trail** via `_log()`, not to Cloud Logging - so they are queryable
only by reading the JSONL / Cloud SQL (like every other RunRecord), never via a
Cloud Logging alert. (The `alert_channel` field in `model.md` - default
`log_only` - is currently an inert config field with no consumer in the package,
so "when `log_only` is configured" has no behavioral meaning today.)

---

## Querying the JSONL audit trail

Query the agent's LogBackend directly, not Cloud Logging. There is no
`atomic-agents log` CLI subcommand today (the dashboard and direct store access
are the query surfaces; an `atomic-agents log` command is filed as #388).

**FilesystemLogBackend (v0):** read or grep the JSONL files. On Cloud Run
stateless v0, these files are on the ephemeral container layer and are lost on
container replacement - these commands are only meaningful if the container is
still running (exec into it) or if you use the Compute Engine VM reference
(extras/gcp/compute-vm/) where files persist on the disk. Each day's runs land
at `<agent_root>/log/YYYY-MM/YYYY-MM-DD.jsonl`, one JSON object per line:
```bash
# Tail recent runs for an agent
tail -n 20 /app/agents/<agent-name>/log/$(date +%Y-%m)/$(date +%Y-%m-%d).jsonl

# Filter to cost-cap skips (status "skipped" is the complete, correct filter).
# Note the summary text differs by skip site: a pre-loop cost-skip writes
# "Skipped: <reason>", while a mid-loop cost-cap skip writes
# "cost cap hit at iteration N: <reason>" (no "Skipped:" prefix). Do NOT
# filter on a summary prefix or you will drop the mid-loop record - the case
# where the agent burned real tokens THEN hit the cap.
grep '"status": "skipped"' /app/agents/<agent-name>/log/*/*.jsonl | jq '{run_id, summary, cost_usd}'
```
(The `grep '"status": "skipped"'` IS the complete filter - `status` is set on both skip paths; the `jq` here just projects the skip reason out of `summary`, it is not a second filter.)
The framework dashboard (`dashboard.html`) also renders these records.

**PostgresLogBackend (#258 PR 1, shipped - activate with the `[postgres]` extra + `ATOMIC_AGENTS_LOG_BACKEND=postgres`):** run `psql` against the Cloud SQL
instance and query the `run_records` table directly, e.g.:
```sql
SELECT run_id, status, summary, cost_usd, ts
FROM run_records
WHERE status = 'skipped'
ORDER BY ts DESC
LIMIT 20;
```
Note the skip reason is embedded in the `summary` text, not a dedicated
`skip_reason` column: `"Skipped: <reason>"` for a pre-loop skip,
`"cost cap hit at iteration N: <reason>"` for a mid-loop skip. The
prefix-independent, authoritative filter is `status = 'skipped'` (both skip
paths set it) - do not filter on the `summary` prefix or you will drop the
mid-loop case (the run that burned real tokens THEN hit the cap). The HTTP 402
response body additionally exposes the reason under the JSON key `reason`
(sourced from the `Response.skip_reason` attribute in the serve layer). Schema
authority: `docs/spec/22-log-backend.md`
§"Postgres implementation notes (non-normative)".
