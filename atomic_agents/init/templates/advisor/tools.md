# TOOLS: ${agent_name}

## Read paths

- Own folder (under agents_root/${agent_name}/): full read access to own context
- <!-- Add additional read paths here. Examples: a shared reference folder, a data directory,
     a project root the agent needs to consult. Format: path, then a brief description of what is there. -->

## Write paths (own folder ONLY)

- Own memory/ (atomic note capture)
- Own wiki/ (wiki page authoring)
- Own journal/ (narrative journal entries)
- Own log/ (run history, JSONL)
- Own output/ (published artifacts for downstream consumption)

## External APIs

- **Anthropic API**: Claude calls per model.md. API key location: `~/.config/atomic_agents/keys.json` (env var `ATOMIC_AGENTS_ANTHROPIC_KEY` for cron runtime).
- <!-- Add other external APIs here if the operator grants access. Format: Service name, purpose, key location. -->

## Hard NOs (absolute, no exceptions)

- Never write outside own folder. No exceptions, even if asked.
- Never read other agents' folders without explicit authorization in this tools.md.
- Never run shell commands outside the allowed write paths above.
- ${hard_refusals}

## Soft NOs (require explicit operator override)

<!-- Add behaviors that are off by default but can be enabled with explicit instruction.
     Examples: web search, contacting external services, reading from shared folders.
     Format: Do not [action] by default. If needed, the operator should [instruction]. -->

(None configured at setup. Add soft-no policies here as the agent's scope evolves.)

## Read budget

- Single file read: any size, no limit
- Per-turn total file reads: cap at 20 files (avoid runaway "let me read everything")

## Tool failure behavior

If any required tool fails:
1. Log the failure to own log/ folder
2. Write a journal entry describing what was attempted and the failure mode
3. Surface the failure to the operator in the response
4. Do NOT retry silently. The operator decides whether to retry.
