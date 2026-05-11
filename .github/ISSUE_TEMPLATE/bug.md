---
name: Bug report
about: Something behaves differently than documented
title: "[bug] "
labels: bug
assignees: ''
---

## What you expected

<!-- What did you expect to happen, and where is that behavior documented? -->

## What you got

<!-- What actually happened. Include the relevant output verbatim. -->

## Reproduction

<!-- The shortest command sequence or code snippet that reproduces the issue. -->

```
# commands here
```

## Environment

- atomic-agents-stack version: <!-- output of `pip show atomic-agents-stack | grep Version` or commit SHA -->
- Python: <!-- 3.11.x / 3.12.x -->
- OS: <!-- macOS 14 / Ubuntu 22.04 / etc. -->
- Runtime: <!-- cron / Claude Code skill / Codex CLI / ChatGPT / OpenAI API / programmatic -->

## Relevant log line

<!-- If the failure produced a JSONL log line in `<vault>/<agent>/log/`, paste it here. -->

```json
{}
```

## What you tried

<!-- Steps you took before filing. Did `atomic-agents doctor` flag anything? Did you check disaster-recovery.md? -->

## Anything else

<!-- Optional context: when this started, whether it's consistent or intermittent, etc. -->
