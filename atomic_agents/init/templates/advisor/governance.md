# Governance : ${agent_name}

This file is the machine-readable governance record for this agent.
Edit the structured YAML block below to fill in your agent's governance details.
The markdown sections below the YAML block are free-prose documentation.

```yaml
governance:
  # Required: who is responsible for this agent day-to-day
  owner: null               # e.g. "your-name@example.com"
  backup_owner: null        # e.g. "backup-owner@example.com"

  # Permission tier : what this agent is allowed to do:
  #   read-only     : reads data only, no writes
  #   draft-only    : writes to draft/staging areas only
  #   writes        : writes to production data stores
  #   sends-or-acts : sends emails / triggers external actions
  permission_tier: null

  # Does this agent process customer data?  yes / no / partial
  customer_data: null

  # Does this agent write to a system of record?  yes / no / partial
  writes_sor: null

  # Lifecycle status: active / paused / deprecated / retired
  lifecycle_status: active

  created_at: null          # ISO-8601 date, e.g. "2026-06-24"
  updated_at: null

  review:
    reviewer: null
    reviewed_at: null
    approved_by: null

  risk:
    level: null             # e.g. "low" / "medium" / "high"
    notes: null

  sources:
    primary: []             # primary data sources this agent reads
    secondary: []           # secondary / derived sources

  actions:
    permitted: []           # explicit list of permitted action types
    forbidden: []           # explicit list of forbidden action types
```

## Forbidden actions

<!-- List actions this agent must never take, regardless of instructions. -->

## Failure modes

<!-- Document known failure modes and how to recover from them. -->

## Pause / retire criteria

<!-- When should this agent be paused or retired? -->
