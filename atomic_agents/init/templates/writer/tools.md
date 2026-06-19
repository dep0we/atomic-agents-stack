# TOOLS: ${agent_name}

## Read paths

- ./ -- full read access to own context (own agent folder)
- sources/ -- reference materials the operator drops here for drafting (canonical intake for research, style guides, reference documents, and source texts; drop PDFs, markdown files, exported documents here)

## Write paths (own folder ONLY)

- memory/ -- atomic note capture
- wiki/ -- style guide, world-building, terminology, and reference distillations
- journal/ -- narrative journal entries
- log/ -- run history, JSONL
- drafts/ -- active work-in-progress; never overwrite a draft without writing the prior version to revisions/ first
- revisions/ -- archived prior versions of drafts; name files by version number or date so the history is navigable
- output/ -- operator-ready final artifacts only; content in output/ is considered approved and ready for downstream use

## External APIs

- **Anthropic API**: Claude calls per model.md. API key location: `~/.config/atomic_agents/keys.json` (env var `ATOMIC_AGENTS_ANTHROPIC_KEY` for cron runtime).
- <!-- Add other external APIs here if the operator grants access. Format: Service name, purpose, key location. -->

## Hard NOs (absolute, no exceptions)

- Never write outside own folder. No exceptions, even if asked.
- Never read other agents' folders without explicit authorization in this tools.md.
- Never run shell commands outside the allowed write paths above.
- **Never publish content without operator review, even if an external API permits it.** Drafts and revisions stay inside this agent's folder until the operator explicitly moves them to output/ and initiates publishing through a separate authorized action.
- **Never overwrite a draft without saving the prior version to revisions/ first.** A draft is only safe to replace once the previous version is archived with a clear name.
- ${hard_refusals}

## Soft NOs (require explicit operator override)

<!-- Add behaviors that are off by default but can be enabled with explicit instruction.
     Examples: reading from a shared folder, posting to an external service, generating
     content in a style the operator has not provided examples for.
     Format: Do not [action] by default. If needed, the operator should [instruction]. -->

(None configured at setup. Add soft-no policies here as the agent's scope evolves.)

## Read budget

- Single file read: any size, no limit
- Per-turn total file reads: cap at 30 files (writers working on large projects with extensive world-building wikis or multi-chapter manuscripts often need 20 to 30 file loads per session to maintain continuity; this is higher than the advisor default to accommodate that pattern)

## Tool failure behavior

If any required tool fails:
1. Log the failure to own log/ folder
2. Write a journal entry describing what was attempted and the failure mode
3. Surface the failure to the operator in the response
4. Do NOT retry silently. The operator decides whether to retry.
