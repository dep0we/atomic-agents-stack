## Summary

<!-- 1-3 bullet points naming what changes for the operator. Lead with what they can now do, not what you implemented. -->

-
-

## Why

<!-- The operator problem this solves. If this closes an issue, include `Closes #N`. -->

## Test plan

<!-- How a reviewer can verify this PR. Checked boxes should be runnable commands or paste-able output. -->

- [ ] `uv run pytest` — all tests pass
- [ ] Manual verification of the new behavior (describe what you ran)
- [ ] CHANGELOG entry added under `[Unreleased]`

## Design alignment

<!-- This project has explicit design taste in CLAUDE.md. Quick self-check before merge: -->

- [ ] Does not violate any of the 14 design rules in [`CLAUDE.md`](../CLAUDE.md), OR violation is named and justified
- [ ] Docs match reality — no aspirational claims about behavior not yet shipped
- [ ] If this touches `agent.call()`, `_capture.py`, `_costs.py`, `_locks.py`, or a protocol surface — the PR was discussed in an issue first

## Notes for review

<!-- Optional: anything reviewers should look at first, any decisions you made you'd flag for second opinion, any flags-for-review. -->

🤖 If this PR was drafted by an AI agent, mention which one + the prompt context so reviewers know the provenance.
