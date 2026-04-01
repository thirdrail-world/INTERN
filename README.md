# Intern

**Autonomous dev agent that executes tickets from a backlog using local LLMs.**

Intern scans a ticket directory, plans edits using a local or cloud LLM, executes them, runs verification, and commits. Failed tickets escalate to your preferred handler (Claude Code, Slack, webhook, or just a log file). The backlog refills automatically.

No cloud dependency required. Runs on your hardware. Ships code while you sleep.

---

## How It Works

```
tickets/backlog/          <- Drop ticket files here
       |
   Intern scans (every 60s)
       |
   Classify: safe / gated / skip
       |
   Plan edits (LLM generates edit plan)
       |
   Execute (apply file changes)
       |
   Verify (run the ticket's verify command)
       |
   Pass -> git commit -> tickets/done/
   Fail -> retry (up to 5x) -> escalate -> tickets/escalated/
```

## Quickstart

```bash
pip install intern

export INTERN_LLM_URL="http://localhost:8200/v1"
export INTERN_LLM_MODEL="Qwen/Qwen3-8B-AWQ"

intern init
intern run --once    # Process backlog once
intern run --live    # Daemon mode
```

## Ticket Format

```markdown
# TICKET-ID: Short description
**Priority:** P1 | P2 | P3
## Description
What needs to be done. Be specific.
**Allowed files:**
- path/to/file.py
## Verify:
\`command that returns exit code 0 on success\`
```

## Architecture

| Module | Role |
|--------|------|
| `agent.py` | Ticket parser + orchestrator (plan-execute-verify loop) |
| `planner.py` | LLM-powered edit planning (structured JSON plans) |
| `executor.py` | Applies edit plans to files (AST-aware for Python) |
| `verifier.py` | Runs verification commands with timeout |
| `queue_runner.py` | Backlog scanner + scheduling + preflight checks |
| `escalate.py` | Pluggable escalation (webhook, Claude Code, Slack, file) |

## Configuration (intern.yaml)

```yaml
llm_profiles:
  default:
    base_url: "http://localhost:8200/v1"
    model: "Qwen/Qwen3-8B-AWQ"
    max_tokens: 8192
    temperature: 0.2
  fallback:
    base_url: "https://api.openai.com/v1"
    model: "gpt-4o"
    api_key: "${OPENAI_API_KEY}"

writable_dirs: ["src/", "tests/", "docs/"]
protected_files: ["src/main.py"]

escalation:
  handler: "webhook"  # webhook | file | claude_code | custom
  webhook_url: "${ESCALATION_WEBHOOK_URL}"
  max_retries: 5
```

## LLM Compatibility

Works with any OpenAI-compatible API: vLLM (local), Ollama, OpenAI, NVIDIA NIM, Together AI. Best results with code-focused models: Devstral, Qwen3, CodeLlama, DeepSeek-Coder.

## Safety

- **Preflight checks:** Clean git repo required before any execution
- **Protected files:** Configurable list requiring manual approval
- **Writable directories:** Tickets can only modify allowed paths
- **Single-file default:** Multi-file tickets are gated
- **Verify required:** Every ticket must have a verify command
- **Retry limit:** Max 5 attempts before escalation
- **Rollback:** Failed edits are reverted before retry

## Backlog Auto-Refill

```bash
intern generate-tickets --scan-dir src/ --type tests
intern run --live --auto-refill --min-backlog 5
```

## License

MIT

---

**Built by [Third Rail](https://thirdrail.world).** Intern was born inside KAI, a sovereign personal AI system. It has been running autonomously since March 2026, executing 64+ tickets and writing 120+ tests without human intervention.
