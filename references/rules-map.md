# Delegation Rules Map

## Source of Truth

- 33 rules (full text): `references/delegation-rules.md`

## Grouped by Rule Number

- 1-2: Delegation gate — only delegate when the user explicitly authorizes it.
- 3-5: Plan first, then handle the critical path.
- 6: Model routing priority (small model for simple sidecars, escalate for complex tasks).
- 7-8: Subtask quality bar (specific, clearly bounded, independently completable, must meaningfully advance the main task).
- 9-11: Avoid duplicate work and repeated delegations; scope requests to the next-step artifact.
- 12-13: Prefer bounded patches for coding tasks; require the changed file list in results.
- 14-16: Parallel tasks must have disjoint write sets and clear module boundaries.
- 17: Validation delegations only when parallelism is valuable and can expose risks early.
- 18-19: Be conservative with waiting; main thread keeps advancing while waiting.
- 20-21: Don't redo delegated work; quickly review and integrate on return.
- 22-24: `explorer` for narrow-scope questions, avoid re-exploration, can parallelize independent questions.
- 25-27: `worker` for execution/implementation, must have explicit ownership and be compatible with concurrent changes.
- 28-30: Reuse existing agent via `send_input` when context continuity is needed; `interrupt=true` only when a hard interrupt is necessary; can wait on multiple targets at once.
- 31-32: Close agents promptly when no longer needed; `resume_agent` when continuing later.
- 33: Core goal — lower total elapsed time through parallelism and lower risk through specialization, without increasing coordination overhead.

## Rules Enforced by `acp_orchestrator.py`

- Rule 1: Plan must include `delegation_explicitly_requested=true`.
- Rules 3-5: Dependency graph + `critical`/`sidecar` scheduling.
- Rule 7: Every task must provide `ownership` or execution is refused.
- Rules 14-16: Controlled parallelism — tasks are dispatched only when their dependencies are ready.
- Rules 18-19: Scheduling loop uses non-blocking polling to reduce idle waiting.
- Rule 31: Subprocesses are reclaimed after tasks complete.
- Natural language routing: supports `plan.routing` + `task.role` to map “give frontend to Claude” to a concrete agent.
- Setup pre-configuration: supports pre-setting runner, default mode, default config options, and auto-approval policy via `--setup`.
- Session config delivery: supports `session/set_mode` and `session/set_config_option`, driven by setup defaults and per-task overrides.

## Rules That Must Be Enforced in Plans and Prompts

- Rules 2, 6-13, 17, 20-30, 32-33.
- In each task prompt, explicitly state: ownership boundaries, output artifact format, and prohibition on rolling back others' changes.
