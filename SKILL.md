---
name: acp-subagent-orchestrator
description: 使用 Agent Client Protocol (ACP) 编排外部 Agent 的显式委派流程。仅在用户明确要求 subagent/delegation/并行 agent 时触发。适用于把任务拆成有边界的子任务（如 Claude、Codex、Copilot CLI），并要求清晰所有权、可控并行与安全集成的场景。若用户只是要求“更深入分析”但未授权委派，则不触发。
---

# ACP 子代理编排器

## 概览

用 ACP 调用兼容的外部 Agent 执行有边界的子任务，并严格执行委派纪律。
围绕显式授权、关键路径优先、写入范围隔离、克制等待来组织执行。
`references/delegation-rules.md` 是本 skill 的规则真源。

## /setup 一次配置

先做一次 setup，把 runner、自动审批策略写入本地配置；若 runner 可连通，会动态读取其 `configOptions/modes`，并在 setup 阶段直接配置默认权限项。

```bash
python3 scripts/setup.py \
  --all-permissions \
  --interactive \
  --output ~/.acp-subagent-orchestrator/setup.json
```

说明：

- `--all-permissions` 会把 `codex` 设为 `full-access`，把 `claude` 设为 `bypassPermissions`，并启用自动审批。
- setup 默认会尝试动态发现 runner 返回的 `mode/configOptions`；可用 `--no-discover` 关闭，用 `--require-discovery` 要求全部发现成功。
- 可通过 `--codex-config mode=...` / `--claude-config mode=...` 等参数直接设置，也可用 `--interactive` 交互选择。
- 支持按 agent 配置 `env/cwd`：如 `--claude-env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY`、`--codex-cwd /path/to/repo`。
- setup 只是“默认值”。单次任务仍可在 plan 中覆写。

如果你的客户端支持自定义斜杠命令，可把 `/setup` 绑定到上面的命令。

## 快速开始

1. 先执行 `/setup`（或直接运行 `scripts/setup.py`）。
2. 基于 `references/plan.example.json` 编写任务计划。
3. 执行：

```bash
python3 scripts/acp_orchestrator.py \
  --setup ~/.acp-subagent-orchestrator/setup.json \
  --plan references/plan.example.json \
  --output /tmp/acp-run-report.json
```

4. 阅读报告，审阅子任务产出，再做受控集成。

## 工作流

执行前先完整阅读 `references/delegation-rules.md`。

### 1) 委派闸门

- 仅在用户明确要求 subagent/delegation/并行 agent 时委派。
- 对“仅分析、未授权委派”的请求，保持主线程本地执行。

### 2) 先规划再派发

- 委派前先明确主线程“此刻立刻要做什么”。
- 把任务拆成关键路径任务与可并行 sidecar 任务。
- 若下一步被阻塞，优先主线程直做，不先外包后空等。

### 3) 定义有边界的任务契约

每个任务至少包含：

- `id`：稳定任务编号。
- `agent`：目标 ACP 代理键名。
- `prompt`：收窄到“下一步所需产物”的具体请求。
- `depends_on`：显式依赖。
- `priority`：`critical` 或 `sidecar`。
- `timeout_sec`：超时时间。
- `ownership`：在提示词内声明文件/模块所有权边界。

可选增强：

- `session_mode`：为本任务临时指定 mode（如 `full-access`）。
- `session_config_options`：为本任务设置 config option（键值对）。

### 4) 通过 ACP 执行

- 用 `scripts/acp_orchestrator.py` 执行计划。
- 编排器会调用 `initialize`、`session/new`、`session/prompt`。
- 若配置了 `mode/configOptions`，会在 `session/new` 后自动下发。
- 并行仅用于独立 sidecar，且写入范围尽量不重叠。

### 5) 集成与复核

- 子任务返回后，快速审阅并集成，不重复劳动。
- 某子任务失败时，仅重试该有边界子任务。
- 不再使用的会话或进程尽快关闭，避免悬挂。

## 路由建议

- 简单 sidecar 优先小而快的模型。
- 复杂实现再升级到更强模型。
- 并行编码时，优先保证所有权边界清晰，其次再考虑模型偏好。

## 护栏

- 未显式授权委派的计划直接拒绝。
- 避免重复/重叠子任务。
- 避免反复阻塞等待；等待期间主线程继续推进不重叠工作。
- 依赖失败时，自动跳过后续依赖任务。

## 资源

- `scripts/setup.py`：生成 setup 配置（runner + 动态发现的默认权限配置 + 自动审批策略）。
- `scripts/acp_orchestrator.py`：ACP JSON-RPC 编排器（stdio 传输）。
- `references/delegation-rules.md`：33 条规则原文（行为真源）。
- `references/setup.example.json`：setup 文件示例。
- `references/plan.example.json`：可运行计划模板。
- `references/rules-map.md`：规则到执行逻辑的映射。
