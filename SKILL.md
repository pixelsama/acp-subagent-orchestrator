---
name: acp-subagent-orchestrator
description: 使用 Agent Client Protocol (ACP) 编排外部 Agent 的显式委派流程。仅在用户明确要求 subagent/delegation/并行 agent 时触发。适用于把任务拆成有边界子任务（如 Claude、Codex、Copilot CLI），并要求清晰所有权、可控并行与安全集成。此 skill 必须以自然语言交互为入口：用户只表达意图，Agent 自主完成 setup、安装、探测、权限确认和任务分派；不得要求用户运行 CLI 或提供参数。
---

# ACP 子代理编排器

## 核心原则

- 只接受自然语言意图，不把 CLI 参数暴露给用户。
- 用户绝不需要自己运行脚本；Agent 负责执行所有命令。
- 仅在必要决策点向用户确认，例如权限档位、是否接入某个 agent。
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
- 不再使用的子进程及时关闭。

## 内部实现说明（给 Agent）

以下内容是 Agent 的内部调用约定，不向用户暴露为操作步骤：

- 用 `scripts/setup.py --list-catalog` 获取支持列表。
- 用 `scripts/setup.py` 生成或更新 `setup.json`。
- runner 默认安装到 skill 目录内 `.runtime/runners`，不自动回退到全局安装。
- `setup.json` 中 `_meta.setup` 仅用于 setup 审计信息（如安装策略和 runners 目录），`acp_orchestrator.py` 运行时忽略该字段。
- 用 `scripts/acp_orchestrator.py` 执行 plan 并输出报告。
- 在提示词中显式写出 ownership、输出格式、并发协作约束。

## 资源

- `scripts/setup.py`：setup 主流程（支持隔离安装、探测、权限落盘）。
- `scripts/acp_orchestrator.py`：ACP JSON-RPC 编排器（stdio 传输）。
- `references/delegation-rules.md`：33 条规则原文。
- `references/setup.example.json`：setup 示例。
- `references/plan.example.json`：plan 示例。
- `references/rules-map.md`：规则映射。
