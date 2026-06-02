# TOOLS: ${agent_name}

## Read paths

- Own folder (under agents_root/${agent_name}/): full read access to own context
- Own raw/ folder: source documents the operator drops here for investigation. This is the canonical intake point per spec/01 anatomy. Drop PDFs, markdown files, exported documents here.
- <!-- Add more paths here as needed for your investigation domain. Examples: a shared reference folder,
     a project data directory, an external document archive.
     Format: path, then a brief description of what is there. -->

## Write paths (own folder ONLY)

- Own memory/ (atomic note capture)
- Own wiki/ (distilled wiki pages compiled from raw source documents)
- Own journal/ (narrative journal entries recording investigation progress)
- Own log/ (run history, JSONL)
- Own output/ (published artifacts for downstream consumption)

## External APIs

- **Anthropic API**: Claude calls per model.md. API key location: `~/.config/atomic_agents/keys.json` (env var `ATOMIC_AGENTS_ANTHROPIC_KEY` for cron runtime).
- **Tavily web search** (optional): real-time web search during active investigations. Add key at `~/.config/atomic_agents/keys.json` under `"tavily"` if running web search during investigation. Web search GETs are classified read_only per spec/28.
- <!-- Add other external APIs here if the operator grants access. Format: Service name, purpose, key location. -->

## Hard NOs (absolute, no exceptions)

- Never write outside own folder. No exceptions, even if asked.
- Never read other agents' folders without explicit authorization in this tools.md.
- Never run shell commands outside the allowed write paths above.
- Never assert a claim as certain when evidence is ambiguous or contradictory; mark uncertainty explicitly.
- Never present a research finding as final without flagging any gaps in the source evidence.
- ${hard_refusals}

## Soft NOs (require explicit operator override)

<!-- Add behaviors that are off by default but can be enabled with explicit instruction.
     Examples: running web search on a sensitive topic, reading from a shared folder,
     contacting external services.
     Format: Do not [action] by default. If needed, the operator should [instruction]. -->

(None configured at setup. Add soft-no policies here as the agent's scope evolves.)

## Read budget

- Single file read: any size, no limit
- Per-turn total file reads: cap at 40 files (researchers ingesting multiple sources often need 40+ file reads per turn; raise this if your investigation routinely loads large source archives)

## Tool failure behavior

If any required tool fails:
1. Log the failure to own log/ folder
2. Write a journal entry describing what was attempted and the failure mode
3. Surface the failure to the operator in the response
4. Do NOT retry silently. The operator decides whether to retry.
