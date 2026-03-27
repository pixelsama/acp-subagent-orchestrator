# Delegation Rules (33 Rules)

The following 33 rules are the authoritative behavioral constraints for this skill.

1. Only call `spawn_agent` when the user **explicitly requests** the use of a subagent / delegation / parallel agent.
2. “Please analyze more deeply / research in more detail” does not constitute automatic delegation authorization.
3. Before delegating, make a high-level plan and distinguish critical-path tasks from parallelizable sidecar tasks.
4. Before delegating, decide what the main thread will do immediately right now.
5. If the next step is blocked on a result, the main thread should typically handle it locally rather than outsourcing it and idling.
6. Prefer smaller models for simple sidecar subtasks; escalate to stronger models for complex tasks.
7. Subtasks must be specific, clearly bounded, and independently completable.
8. Subtasks must meaningfully advance the main task — do not delegate for its own sake.
9. The main thread and subtasks must not duplicate work.
10. Do not open multiple similar delegations on the same unresolved thread unless they are genuinely distinct and necessary new tasks.
11. Scope delegation requests to “the artifact needed for the next step” — avoid over-generalizing.
12. In coding contexts, prefer delegating a bounded patch that directly modifies code over pure exploration, when feasible.
13. For coding delegations, explicitly require the subagent to modify files directly and list the changed file paths in the result.
14. When multiple coding subtasks run in parallel, their write scopes must not overlap.
15. Independent information-gathering questions can be parallelized.
16. Implementation tasks can be split in parallel, provided module boundaries are clear and write sets are disjoint.
17. Validation delegations should only be used when parallelism is beneficial and they can expose concrete risks early.
18. Use `wait_agent` sparingly — only when the critical path is blocked and the result is immediately required.
19. Do not “habitually wait” repeatedly; the main thread should continue advancing non-overlapping work while waiting.
20. Do not redo work that has already been delegated.
21. After a subagent returns, quickly review its output before integrating or correcting it.
22. `explorer` is for “well-defined, narrow-scope” questions within the codebase.
23. `explorer` should avoid re-exploring already-covered questions; reuse existing explorers for related questions.
24. Multiple independent exploration questions can be run in parallel with multiple explorers.
25. `worker` is for execution and implementation work.
26. A `worker` must be given explicit file/module ownership and responsibility boundaries.
27. Remind `worker` that it does not have exclusive access to the codebase — it must not roll back others' changes and must be compatible with concurrent modifications.
28. If subsequent tasks strongly depend on the same context, prefer reusing an existing agent via `send_input` rather than creating a new one.
29. `send_input` with `interrupt=true` is only for situations where the current task must be immediately interrupted and redirected.
30. `wait_agent` can wait on multiple targets at once — commonly used to proceed with whoever finishes first.
31. Call `close_agent` when an agent is no longer needed to avoid long-running dangling processes.
32. After closing, use `resume_agent` to continue if needed.
33. Core goal: reduce total elapsed time through parallelism and reduce risk through specialization, without increasing coordination overhead.
