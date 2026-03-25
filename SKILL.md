---
name: acp-subagent-orchestrator
description: 使用 Agent Client Protocol (ACP) 编排外部 Agent 的显式委派流程。仅在用户明确要求 subagent/delegation/并行 agent 时触发。适用于把任务拆成有边界的子任务（如 Claude、Codex、Copilot CLI），并要求清晰所有权、可控并行与安全集成的场景。若用户只是要求“更深入分析”但未授权委派，则不触发。
---

# ACP 子代理编排器

## 概览

用 ACP 调用兼容的外部 Agent 执行有边界的子任务，并严格执行委派纪律。
围绕显式授权、关键路径优先、写入范围隔离、克制等待来组织执行。
`references/delegation-rules.md` 是本 skill 的规则真源。

## Agent-Only Setup Flow

重要：脚本是给 Agent 调用的，不是给终端用户直接操作的。  
用户只需要用自然语言说目标（例如“配置 codex 和 claude 作为 subagent，并设置权限”）；Agent 负责执行脚本。

当用户要求“配置 subagent”时，Agent 按以下流程执行：

1. 调用 `scripts/setup.py --list-catalog` 读取预设清单。
2. 把可选 agent 清单用自然语言展示给用户，并确认要接入的一个或多个 agent。
3. 询问每个已选 agent 的权限模式偏好（例如 mode），若用户未指定则使用 runner 的当前默认值。
4. 调用 `scripts/setup.py` 执行 setup，脚本会对所选 agent 顺序执行：
   - runner 可用性检查
   - 缺失时自动安装
   - 安装后连通探测（`initialize` + `session/new`）
   - 动态读取 `configOptions/modes`
   - 写入 `setup.json`
5. 向用户回报每个 agent 的接入结果（成功/失败、失败原因、已落地配置）。

Agent 在流程中应主动使用这些参数（按需组合）：

- `--agents`：一次选择多个 agent。
- `--require-discovery`：要求探测成功才算接入完成。
- `--codex-config mode=...` / `--claude-config mode=...` / `--copilot-config mode=...`：用用户确认后的权限模式显式落盘。
- `--no-install-missing`：仅在用户明确不希望自动安装时使用。

setup 输出是默认配置，后续任务可在 plan 中覆盖。

## 运行执行

Agent 基于 `references/plan.example.json` 组织任务后，调用 `scripts/acp_orchestrator.py` 执行并汇总结果，再向用户报告。

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

- `scripts/setup.py`：setup 主流程（预设清单、按需安装 runner、连通探测、权限配置、写入 setup.json）。
- `scripts/acp_orchestrator.py`：ACP JSON-RPC 编排器（stdio 传输）。
- `references/delegation-rules.md`：33 条规则原文（行为真源）。
- `references/setup.example.json`：setup 文件示例。
- `references/plan.example.json`：可运行计划模板。
- `references/rules-map.md`：规则到执行逻辑的映射。
