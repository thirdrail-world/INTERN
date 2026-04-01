<p align="center">
  <h1 align="center">Intern</h1>
  <p align="center"><strong>Autonomous dev agent that runs on your hardware. No cloud. No API keys. Ships code while you sleep.</strong></p>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> · <a href="#how-it-works">How It Works</a> · <a href="#why-intern">Why Intern</a> · <a href="#configuration">Configuration</a>
</p>

---

Intern scans a ticket backlog, plans edits using a local LLM, executes them, runs verification, and commits. Failed tickets escalate to your preferred handler. The backlog refills automatically by scanning your codebase.

Born inside [KAI](https://thirdrail.world), a sovereign personal AI system. Intern has been running autonomously since March 2026, executing 64+ tickets and writing 120+ tests without human intervention.

## Why Intern

The entire AI coding agent industry is converging on the same architecture: ticket queue → LLM planner → file executor → verification → commit. Claude Code's [recently leaked source](https://www.theregister.com/2026/03/31/anthropic_claude_code_source_code/) confirmed they built the exact same pattern (KAIROS daemon, autoDream memory, coordinator mode).

The difference: **theirs requires their cloud.** Intern runs on your metal.

| | Claude Code | Intern |
|---|---|---|
| Runs locally | No (API required) | Yes |
| Uses any LLM | No (Claude only) | Yes (vLLM, Ollama, OpenAI, etc.) |
| Autonomous daemon | Feature-flagged | Production since March 2026 |
| Escalation pipeline | No | Yes (webhook, Slack, Claude Code, custom) |
| Auto-refill backlog | No | Yes (codebase scanning) |
| Open source | Accidentally (npm leak) | Intentionally (MIT) |
| Your code leaves your machine | Yes | Never |

## Quickstart

```bash
git clone https://github.com/thirdrail-world/INTERN.git
cd INTERN
pip install -e .

# Point at your LLM
export INTERN_LLM_URL="http://localhost:11434/v1"  # Ollama
export INTERN_LLM_MODEL="devstral"

# Initialize
intern init

# Drop a ticket
cat > tickets/backlog/add-tests-001.md << 'EOF'
# add-tests-001: Add unit tests for utils.py

**Priority:** P2

## Description
Add pytest tests for `src/utils.py`. Cover the main functions.

**Allowed files:**
- `tests/test_utils.py`

## Verify:
`pytest tests/test_utils.py -v`
EOF

# Run
intern run --once    # Process backlog once
intern run --live    # Daemon mode (scan every 60s)
```

> **PyPI package coming soon.** Star the repo to get notified.

#### What just happened?

```bash
git log --oneline -5        # See Intern's commits
cat tickets/done/*.md       # Completed tickets
cat tickets/escalated/*.md  # Tickets that needed help
```

## How It Works

```
tickets/backlog/        ← Drop markdown tickets here
       ↓
  Intern scans (every 60s)
       ↓
  Classify: safe / gated / skip
       ↓
  Plan edits (your LLM generates edit plan)
       ↓
  Execute (apply changes to files)
       ↓
  Verify (run ticket's verify command)
       ↓
  ✅ Pass → git commit → tickets/done/
  ❌ Fail → retry (up to 5x) → escalate
```

## Architecture

```
intern/
├── queue_runner.py   # Backlog scanner, scheduling, preflight checks
├── agent.py          # Ticket parser, plan→execute→verify orchestrator
├── planner.py        # LLM-powered edit planning (structured JSON)
├── executor.py       # Applies edits to files (AST-aware for Python)
├── verifier.py       # Runs verify commands with timeout
├── escalate.py       # Pluggable escalation (webhook, file, custom)
└── cli.py            # CLI entry point
```

## Configuration

### intern.yaml

```yaml
llm_profiles:
  default:
    base_url: "http://localhost:11434/v1"  # Ollama, vLLM, etc.
    model: "devstral"
    max_tokens: 8192
    temperature: 0.2

  fallback:
    base_url: "https://api.openai.com/v1"
    model: "gpt-4o"
    api_key: "${OPENAI_API_KEY}"

writable_dirs:
  - "src/"
  - "tests/"
  - "docs/"

protected_files:
  - "src/main.py"

escalation:
  handler: "webhook"  # webhook | file | custom
  webhook_url: "${ESCALATION_WEBHOOK_URL}"
  max_retries: 5
```

### Ticket Format

```markdown
# TICKET-ID: Short description

**Priority:** P1 | P2 | P3

## Description
What needs to be done.

**Allowed files:**
- `path/to/file.py`

## Verify:
`command that returns exit 0 on success`
```

## LLM Compatibility

Works with any OpenAI-compatible API:

| Provider | Status | Notes |
|----------|--------|-------|
| vLLM | ✅ | Recommended for sovereign setups |
| Ollama | ✅ | Easiest local setup |
| OpenAI | ✅ | Set API key in profile |
| NVIDIA NIM | ✅ | Cloud or on-prem |
| Together AI | ✅ | OpenAI-compatible |

Best results with: **Devstral**, Qwen3, DeepSeek-Coder, CodeLlama.

## Auto-Refill

Intern can scan your codebase and generate tickets automatically:

```bash
intern generate-tickets --scan-dir src/ --type tests
intern run --live --auto-refill --min-backlog 5
```

## Escalation Pipeline

Not every ticket can be solved by a local LLM. Intern handles this with a two-tier system:

```
Ticket claimed → Plan → Execute → Verify
                                     ↓
                              ✅ Pass → commit
                              ❌ Fail → retry (up to 5x)
                                          ↓
                                   Still failing → escalate
                                          ↓
                              webhook / Slack / Claude Code / custom handler
```

Escalation is pluggable. Point it at a Discord webhook, a Slack channel, or a more capable agent. In production, Intern escalates to Claude Code via a Discord bridge — simple tickets stay local, complex ones get routed to a stronger model automatically.

## Safety

- **Clean git required** — won't run on dirty repos
- **Protected files** — configurable files that need manual approval
- **Writable dirs** — tickets can only touch allowed paths
- **Single-file default** — multi-file = gated
- **Verify required** — every ticket needs a verify command
- **Retry limit** — max 5 attempts before escalation
- **Rollback** — failed edits reverted before retry

## Origin Story

Intern was extracted from [KAI](https://thirdrail.world), a sovereign personal AI system running on an NVIDIA DGX Spark. It started as an internal tool called NemoClaw, built to automate development tasks on KAI's codebase. After executing 64+ tickets autonomously and writing 120+ tests with zero human intervention, we extracted and rebranded it as Intern.

The Claude Code source leak on March 31, 2026 revealed that Anthropic built the same architecture (KAIROS, autoDream, coordinator mode) — but locked behind their cloud. Intern is the sovereign alternative.

## Contributing

Open an issue or PR. We're building in public.

If you run Intern on your own hardware and want to share results, open a discussion — we want to see what models and configurations work best across different setups.

## License

MIT — [Third Rail](https://thirdrail.world)
