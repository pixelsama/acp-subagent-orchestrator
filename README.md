# acp-subagent-orchestrator

ACP (Agent Client Protocol) subagent orchestration skill for Codex-style workflows.

This project helps you:

- run explicit delegated tasks to ACP-compatible agents (Claude / Codex / Copilot),
- enforce task ownership boundaries,
- model task dependencies and critical-path scheduling,
- produce a structured JSON report for integration and review.

## License

MIT

## Requirements

- Python 3.10+
- One or more ACP runners available locally (or installed via setup flow)

## Quick Start

1. Generate/setup runner config:

```bash
python3 scripts/setup.py --help
```

Show supported runners (25 ACP tools in current catalog):

```bash
python3 scripts/setup.py --list-catalog
```

Example:

```bash
# Configure only Claude (recommended to match explicit user choice)
python3 scripts/setup.py --agents claude

# Configure by alias (e.g. github-copilot / qwen-code / factory-droid)
python3 scripts/setup.py --agents github-copilot,qwen-code,factory-droid

# Configure all runners explicitly
python3 scripts/setup.py --agents all
```

2. Prepare a plan JSON (see [`references/plan.example.json`](references/plan.example.json)).

3. Run the orchestrator:

```bash
python3 scripts/acp_orchestrator.py \
  --plan /path/to/plan.json \
  --setup ./setup.json \
  --output /tmp/acp-report.json
```

## Heartbeat (Long Tasks)

By default, waiting on a long `session/prompt` emits a low-noise heartbeat line every 60s:

```text
[task-id] running 120s, updates=5, idle=42s
```

This heartbeat is intentionally non-semantic and only signals liveness.

- override interval: `--heartbeat-interval-sec 30`
- disable heartbeat: `--no-heartbeat`
- config via `plan/setup`: `heartbeat_enabled`, `heartbeat_interval_sec`

## Repository Layout

- `SKILL.md`: skill instructions and guardrails
- `scripts/setup.py`: setup/discovery and config generation
- `references/agent_catalog.json`: ACP runner catalog (aliases + install metadata)
- `scripts/acp_orchestrator.py`: ACP JSON-RPC orchestration runtime
- `references/`: examples and rule references
