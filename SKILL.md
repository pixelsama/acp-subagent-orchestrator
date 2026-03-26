---
name: acp-subagent-orchestrator
description: 使用 Agent Client Protocol (ACP) 编排外部 Agent 的显式委派流程。仅在用户明确要求 subagent/delegation/并行 agent 时触发。适用于把任务拆成有边界子任务（如 Claude、Codex、Copilot CLI），并要求清晰所有权、可控并行与安全集成。此 skill 必须以自然语言交互为入口：用户只表达意图，Agent 自主完成 setup、安装、探测、权限确认和任务分派；不得要求用户运行 CLI 或提供参数。
---

# ACP 子代理编排器

## 核心原则

- 只接受自然语言意图，不把 CLI 参数暴露给用户。
- 用户绝不需要自己运行脚本；Agent 负责执行所有命令。
- 编排参数由 Agent 自主决定，默认使用固定稳健档位，不向用户询问脚本参数。
- 仅在必要决策点向用户确认，例如权限档位、是否接入某个 agent。
- setup 时禁止在未确认用户选择的情况下默认全量配置所有 runner。
- `references/delegation-rules.md` 是委派纪律真源，执行前先完整阅读。

## 自然语言 Setup 流程

当用户说“配置 subagent”时，按以下对话流程执行：

1. Agent 自行读取支持列表并告诉用户可接入项（例如 Claude/Codex/Copilot）。
2. 用户用自然语言选择目标（例如“先配 Claude”）。
3. Agent 自行探测本机 runner；不存在时自动安装到隔离目录（默认）。
4. Agent 探测连接可用性（`initialize` + `session/new`）。
5. Agent 把可选权限按编号展示给用户（例如 `1/2/3`）。
6. 用户说“选第二个”等自然语言。
7. Agent 把选择写入 `setup.json`，然后明确告知“配置完成”。

## 自然语言委派流程

当用户说“前端交给 Claude，后端你自己做”时：

1. 主线程先做高层计划，明确自己立刻要推进的关键路径任务。
2. 将可并行子任务映射到外部 agent（例如 frontend -> claude）。
3. 为每个子任务定义明确所有权边界，避免写入重叠。
4. 调用 `scripts/acp_orchestrator.py` 执行子任务并汇总结果。
5. 主线程快速审阅子任务产物并集成，不重复劳动。

## 任务契约

每个委派任务至少包含：

- `id`：稳定任务编号。
- `agent` 或 `role`：目标 agent 或待路由角色。
- `prompt`：收窄到下一步所需产物。
- `depends_on`：显式依赖。
- `priority`：`critical` 或 `sidecar`。
- `timeout_sec`：超时时间。
- `ownership`：文件/模块所有权边界（必填）。

可选字段：

- `session_mode`：临时 mode。
- `session_config_options`：会话 config 覆写。

## 护栏

- 未显式授权委派时，不运行外部子代理。
- 不把“深入分析”误判为委派授权。
- 避免重复/重叠子任务。
- 等待要克制；主线程在等待期间继续推进不重叠工作。
- 长任务（建议指 `timeout_sec > 300`）默认按“静默正常”处理：未出现中间语义输出不等于卡死。
- 长任务期间优先依赖编排器心跳判断存活状态；仅在超时、进程退出或明确错误时中止。
- 心跳应保持低噪音（只报告运行时长/更新计数/空闲时长），避免转发子代理语义内容。
- 不再使用的子进程及时关闭。

## 内部实现说明（给 Agent）

以下内容是 Agent 的内部调用约定，不向用户暴露为操作步骤：

- 用 `scripts/setup.py --list-catalog` 获取支持列表。
- 先与用户确认要接入的 runner，再用 `scripts/setup.py --agents <user-selected>` 生成或更新 `setup.json`（需要全量时显式使用 `--agents all`）。
- runner catalog 由 `references/agent_catalog.json` 维护（当前覆盖 25 个常用 ACP runners）。
- runner 默认安装到 skill 目录内 `.runtime/runners`，不自动回退到全局安装。
- `setup.json` 中 `_meta.setup` 仅用于 setup 审计信息（如安装策略和 runners 目录），`acp_orchestrator.py` 运行时忽略该字段。
- 用 `scripts/acp_orchestrator.py` 执行 plan 并输出报告。
- 长任务统一使用固定调用档位（不向用户询问参数）：
  - `--heartbeat-interval-sec 60`
  - `--status-interval-sec 15`
  - `--status-file /tmp/acp-status-<task-or-plan>-<timestamp>.json`
  - `--output /tmp/acp-report-<task-or-plan>-<timestamp>.json`
- 若编排器后台运行，主线程每 60 秒轮询一次 status file（`cat`/`jq`）确认健康状态。
- 中止判定按 status file 进行，不得仅凭“无 stdout 输出”中止：
  - 仅当进程消失、status file 连续 180 秒未刷新，或 `idle_sec` 持续接近超时阈值时，才允许中止。
- 在提示词中显式写出 ownership、输出格式、并发协作约束。

## 资源

- `scripts/setup.py`：setup 主流程（支持隔离安装、探测、权限落盘）。
- `references/agent_catalog.json`：runner 清单与安装元信息（25 agents，含别名和分发信息）。
- `scripts/acp_orchestrator.py`：ACP JSON-RPC 编排器（stdio 传输）。
- `references/delegation-rules.md`：33 条规则原文。
- `references/setup.example.json`：setup 示例。
- `references/plan.example.json`：plan 示例。
- `references/rules-map.md`：规则映射。
