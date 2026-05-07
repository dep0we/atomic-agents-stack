# Caldwell — Run Log

Autonomous run history. JSONL files, one record per cron tick / skill invocation.

Format: `YYYY-MM/YYYY-MM-DD.jsonl`

Each record includes:
- `ts` — ISO timestamp
- `trigger` — `cron | skill | api | manual`
- `model` — model ID used
- `input_tokens`, `output_tokens` — counts
- `status` — `ok | error | skipped`
- `summary` — one-line description

Optional fields: `error`, `cost_usd`, `cache_hit`, `tools_called`, `skill_invocation_id`.

## Why JSONL not Markdown

Logs are for **observability**: cost analysis, error rates, latency tracking. They're queryable with `jq`, parseable by any tool, append-only.

The `journal/` folder handles narrative and content. This folder handles *operations*.

## Sample query: today's cost

```bash
cd ~/agents/caldwell/log/2026-05
jq -s 'map(.cost_usd // 0) | add' 2026-05-06.jsonl
```

## Sample query: error rate this month

```bash
jq -s 'group_by(.status) | map({status: .[0].status, count: length})' \
  ~/agents/caldwell/log/2026-05/*.jsonl
```

## Sample record

```json
{"ts":"2026-05-06T11:32:00-05:00","trigger":"skill","model":"claude-opus-4-7-20260101","input_tokens":4102,"output_tokens":892,"status":"ok","summary":"Bonus allocation question — applied locked debt priority","cost_usd":0.13,"cache_hit":true,"skill_invocation_id":"caldwell-2026-05-06-002"}
```

(This README would not exist in a real agent's log/ — it's here as documentation. In a real agent, this folder is just JSONL files.)
