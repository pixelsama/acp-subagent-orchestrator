---
name: acp-subagent-orchestrator
description: Explicit delegation workflow for orchestrating external agents via Agent Client Protocol (ACP). Only triggered when the user explicitly requests subagent/delegation/parallel agent. Designed to split tasks into bounded subtasks (e.g. Claude, Codex, Copilot CLI) with clear ownership, controlled parallelism, and safe integration. This skill must be entered via natural language: the user expresses intent only; the Agent autonomously handles setup, installation, probing, permission configuration, and task dispatch — the user must never be asked to run CLI commands or supply parameters.
---

# ACP Subagent Orchestrator

## Core Principles

- Accept natural language intent only; do not expose CLI parameters to the user.
- The user never runs scripts themselves; the Agent is responsible for executing all commands.
- Orchestration parameters are decided autonomously by the Agent using fixed, stable defaults — do not ask the user for script parameters.
- If the user asks for a model preference (or asks to set a default model), the Agent must handle model discovery and selection end-to-end, while keeping script details hidden from the user.
- Confirm with the user only at necessary decision points (e.g. which agent to connect); advanced permission/mode configuration is only confirmed when the user explicitly requests it.
- During setup, never default to configuring all runners without first confirming the user's selection.
- `references/delegation-rules.md` is the authoritative source for delegation discipline — read it in full before executing.

## Natural Language Setup Flow

When the user says “configure subagent”, follow this conversational flow:

1. The Agent reads the supported runner list and tells the user what is available (e.g. Claude/Codex/Copilot).
2. The user selects a target in natural language (e.g. “set up Claude first”).
3. The Agent probes for the runner on the local machine; if absent, installs it to an isolated directory (default).
4. The Agent probes connection availability (`initialize` + `session/new`).
5. If model options are requested or needed, the Agent discovers supported models, shows the model list in natural language, and asks the user to choose.
6. The Agent writes results to `setup.json` (including the selected default model when applicable) and explicitly reports “setup complete”.

## Natural Language Delegation Flow

When the user says “give the frontend to Claude, you handle the backend”:

1. The main thread makes a high-level plan first and identifies the critical-path tasks it will drive immediately.
2. Map parallelizable subtasks to external agents (e.g. frontend -> claude).
3. Define clear ownership boundaries for each subtask to prevent write overlap.
4. Invoke `scripts/acp_orchestrator.py` to execute subtasks and collect results.
5. The main thread quickly reviews subtask output and integrates it without duplicating work.

## Task Contract

Each delegated task must include at minimum:

- `id`: stable task identifier.
- `agent` or `role`: target agent or role to route to.
- `prompt`: scoped to the artifact needed for the next step.
- `depends_on`: explicit dependencies.
- `priority`: `critical` or `sidecar`.
- `timeout_sec`: timeout in seconds. Default is 900. For coding tasks that write files, use at least 1800; for complex multi-file implementations, 2400–3600. Underestimating timeout is the primary cause of dependency chain failures.
- `ownership`: file/module ownership boundary (required).

Optional fields:

- `session_mode`: temporary mode override.
- `session_config_options`: session config overrides.

## Guardrails

- Do not run external subagents unless delegation has been explicitly authorized.
- Do not misinterpret “analyze in more depth” as delegation authorization.
- Avoid duplicate or overlapping subtasks.
- Be conservative with waiting; the main thread continues advancing non-overlapping work while waiting.
- Long tasks (`timeout_sec > 300`) are treated as “silent is normal” by default — no intermediate semantic output does not mean the task is stuck.
- During long tasks, rely on orchestrator heartbeats to assess liveness; abort only on timeout, process exit, or explicit error.
- Heartbeats must be low-noise (report only elapsed time / update count / idle duration) — do not forward subagent semantic content.
- Close subprocesses promptly when no longer needed.

## Internal Implementation Notes (for Agent)

The following are internal calling conventions for the Agent and are not exposed to the user as steps:

- Use `scripts/setup.py --list-catalog` to get the supported runner list.
- Confirm the runner(s) to connect with the user first, then use `scripts/setup.py --agents <user-selected>` to generate or update `setup.json` (use `--agents all` only when explicitly needed).
- Default to quick setup (no discovery, no mode/configOptions display). Exception: when the user asks for default model selection (or gives an explicit model preference), run discovery and model selection flow automatically without exposing CLI details.
- The runner catalog is maintained in `references/agent_catalog.json` (currently covers 25 common ACP runners).
- Runners are installed by default to `.runtime/runners` inside the skill directory; do not fall back to global installation automatically.
- `_meta.setup` in `setup.json` is for setup audit info only (e.g. install scope and runners directory); it is ignored at `acp_orchestrator.py` runtime.
- Model selection policy during setup:
  - Run discovery for the selected runner(s) and look for `configOptions` entry `id=model`.
  - Present discovered model choices to the user in natural language (name + value), then ask the user to pick one.
  - Persist the selection as that agent's default model in `setup.json` via `default_config_options.model`.
  - If discovery does not return model choices, report that limitation clearly and keep the runner's current default model.
  - For execution, task-level `session_config_options.model` overrides setup-level default model.
- Plan JSON can include a `"setup"` field (path relative to the plan file) pointing to the setup JSON. The `--setup` CLI flag takes precedence if both are provided.
- Use `scripts/acp_orchestrator.py` to execute a plan and produce a report.
- Long tasks always use fixed call presets (do not ask the user for parameters):
  - `--heartbeat-interval-sec 60`
  - `--status-interval-sec 15`
  - `--status-file /tmp/acp-status-<task-or-plan>-<timestamp>.json`
  - `--output /tmp/acp-report-<task-or-plan>-<timestamp>.json`
- If the orchestrator runs in the background, the main thread polls the status file every 60 seconds (`cat`/`jq`) to verify health.
- Abort decisions are based on the status file — do not abort solely because there is no stdout output:
  - Abort is allowed only when: the process has exited, the status file has not been refreshed for 180 consecutive seconds, or `idle_sec` is persistently approaching the timeout threshold.
- Explicitly include ownership, output format, and concurrent collaboration constraints in prompts.
- To resume after partial failure: use `--resume <previous-report.json>` to skip already-succeeded tasks, and `--skip-tasks <id1,id2>` to force-mark specific timed-out tasks as succeeded (after verifying their output artifacts).

## Resources

- `scripts/setup.py`: setup main flow (supports isolated install, discovery, permission persistence).
- `references/agent_catalog.json`: runner catalog with install metadata (25 agents, including aliases and distribution info).
- `scripts/acp_orchestrator.py`: ACP JSON-RPC orchestration runtime (stdio transport).
- `references/delegation-rules.md`: 33 delegation rules (source of truth).
- `references/setup.example.json`: setup example.
- `references/plan.example.json`: plan example.
- `references/rules-map.md`: rules mapping.
