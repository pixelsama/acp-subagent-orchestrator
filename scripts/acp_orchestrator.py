#!/usr/bin/env python3
"""ACP 子代理编排器。

通过 stdio JSON-RPC 调用 ACP 兼容 Agent，执行有边界的子任务。
支持通过 setup 配置预设 runner、默认权限模式和会话配置项。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_AGENT_COMMANDS: Dict[str, List[str]] = {
    "claude": ["claude-agent-acp"],
    "codex": ["codex-acp"],
    "copilot": ["copilot", "--acp", "--stdio"],
}


class ACPError(RuntimeError):
    """用于表示 ACP 协议或运行时错误。"""


@dataclass
class AgentConfig:
    name: str
    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    auto_approve_permissions: bool = True
    default_mode: Optional[str] = None
    default_config_options: Dict[str, str] = field(default_factory=dict)
    strict_session_config: bool = False


@dataclass
class TaskSpec:
    id: str
    agent: str
    role: Optional[str]
    prompt: str
    ownership: List[str]
    priority: str = "sidecar"
    depends_on: List[str] = field(default_factory=list)
    timeout_sec: int = 900
    cwd: Optional[str] = None
    session_mode: Optional[str] = None
    session_config_options: Dict[str, str] = field(default_factory=dict)


@dataclass
class TaskResult:
    id: str
    agent: str
    status: str
    stop_reason: Optional[str]
    duration_sec: float
    output_text: str
    updates: List[Dict[str, Any]]
    error: Optional[str] = None
    stderr: List[str] = field(default_factory=list)


class ACPConnection:
    def __init__(
        self,
        command: List[str],
        env: Dict[str, str],
        cwd: str,
        auto_approve_permissions: bool,
        verbose: bool = False,
    ) -> None:
        self.command = command
        self.env = env
        self.cwd = cwd
        self.auto_approve_permissions = auto_approve_permissions
        self.verbose = verbose

        self.process: Optional[subprocess.Popen[str]] = None
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._next_id = 1

        self.updates: List[Dict[str, Any]] = []
        self.text_chunks: List[str] = []
        self.stderr_lines: List[str] = []

    def start(self) -> None:
        if self.process is not None:
            raise ACPError("进程已启动")

        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.cwd,
            env=self.env,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None

    def request(self, method: str, params: Dict[str, Any], timeout_sec: int) -> Dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise ACPError("进程尚未启动")

        request_id = self._next_id
        self._next_id += 1

        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )

        deadline = time.time() + timeout_sec
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ACPError(f"等待 {method} 响应超时")

            msg = self._next_message(timeout=remaining)
            if msg is None:
                raise ACPError("Agent 进程意外关闭了 stdout")

            # Agent -> Client 请求
            if "method" in msg and "id" in msg and "result" not in msg and "error" not in msg:
                self._handle_agent_request(msg)
                continue

            # Agent -> Client 通知
            if "method" in msg and "id" not in msg:
                self._handle_notification(msg)
                continue

            # 本次请求对应响应
            if msg.get("id") == request_id:
                if "error" in msg:
                    raise ACPError(f"{method} 调用失败: {msg['error']}")
                result = msg.get("result")
                if isinstance(result, dict):
                    return result
                return {"value": result}

    def _send(self, payload: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ACPError("进程 stdin 不可用")
        wire = json.dumps(payload, ensure_ascii=False)
        if self.verbose:
            print(f">>> {wire}", file=sys.stderr)
        self.process.stdin.write(wire + "\n")
        self.process.stdin.flush()

    def _next_message(self, timeout: float) -> Optional[Dict[str, Any]]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in self.process.stdout:
            line = raw.strip()
            if not line:
                continue
            # 当前仅支持按行分隔的 JSON-RPC 负载。
            if line.lower().startswith("content-length:"):
                self._queue.put(
                    {
                        "method": "_internal/error",
                        "params": {
                            "message": "检测到 Content-Length 分帧。请使用按行分隔 JSON 的 ACP 适配器。"
                        },
                    }
                )
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                if self.verbose:
                    print(f"[acp] 非 JSON stdout: {line}", file=sys.stderr)
                continue
            if self.verbose:
                print(f"<<< {json.dumps(msg, ensure_ascii=False)}", file=sys.stderr)
            self._queue.put(msg)
        self._queue.put(None)

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for raw in self.process.stderr:
            line = raw.rstrip("\n")
            self.stderr_lines.append(line)
            if self.verbose:
                print(f"[acp-stderr] {line}", file=sys.stderr)

    def _handle_notification(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "_internal/error":
            raise ACPError(msg.get("params", {}).get("message", "内部传输错误"))

        if method != "session/update":
            self.updates.append({"method": method, "params": msg.get("params", {})})
            return

        params = msg.get("params", {})
        update = params.get("update", {})
        self.updates.append({"method": method, "params": params})

        update_type = update.get("sessionUpdate")
        if update_type not in {"agent_message_chunk", "assistant_message_chunk"}:
            return

        content = update.get("content", {})
        if content.get("type") == "text":
            self.text_chunks.append(content.get("text", ""))

    def _handle_agent_request(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        request_id = msg.get("id")
        params = msg.get("params", {})

        if method == "session/request_permission":
            outcome = self._permission_outcome(params.get("options", []))
            self._send({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}})
            return

        # 对暂不支持的 Client 方法，返回标准 method-not-found。
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"编排器客户端暂不支持该方法: {method}",
                },
            }
        )

    def _permission_outcome(self, options: Any) -> Dict[str, Any]:
        if not isinstance(options, list) or not options:
            return {"outcome": "cancelled"}

        def option_id(opt: Dict[str, Any]) -> Optional[str]:
            raw = opt.get("optionId")
            if isinstance(raw, str) and raw:
                return raw
            raw = opt.get("id")
            if isinstance(raw, str) and raw:
                return raw
            return None

        picked: Optional[Dict[str, Any]] = None
        if self.auto_approve_permissions:
            preferred = {"allow_once", "allow_always", "allow"}
            for opt in options:
                if isinstance(opt, dict) and str(opt.get("kind", "")).lower() in preferred:
                    picked = opt
                    break
            if picked is None:
                picked = next((opt for opt in options if isinstance(opt, dict)), None)
        else:
            preferred = {"reject_once", "reject_always", "deny", "cancel"}
            for opt in options:
                if isinstance(opt, dict) and str(opt.get("kind", "")).lower() in preferred:
                    picked = opt
                    break

        picked_id = option_id(picked or {})
        if picked_id:
            return {"outcome": "selected", "optionId": picked_id}
        return {"outcome": "cancelled"}


def _expand_env(value: str) -> str:
    return os.path.expandvars(value)


def _load_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise ACPError(f"未找到{label}文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ACPError(f"{label}文件不是合法 JSON: {path}") from exc

    if not isinstance(data, dict):
        raise ACPError(f"{label}文件根节点必须是 JSON 对象")
    return data


def _load_plan(path: Path) -> Dict[str, Any]:
    data = _load_json(path, "计划")

    if data.get("delegation_explicitly_requested") is not True:
        raise ACPError("计划必须设置 delegation_explicitly_requested=true 才允许委派")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ACPError("plan.tasks 必须是非空数组")

    return data


def _load_setup(path: Path) -> Dict[str, Any]:
    data = _load_json(path, "setup")
    if "agents" in data and not isinstance(data["agents"], dict):
        raise ACPError("setup.agents 必须是对象")
    return data


def _parse_string_map(value: Any, field_name: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ACPError(f"{field_name} 必须是对象")

    parsed: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str):
            raise ACPError(f"{field_name} 的键必须是字符串")
        if not isinstance(v, str):
            raise ACPError(f"{field_name}.{k} 的值必须是字符串")
        parsed[k] = _expand_env(v)
    return parsed


def _parse_command(value: Any, fallback: Optional[List[str]], field_name: str) -> List[str]:
    if isinstance(value, str):
        cmd = shlex.split(value)
        if not cmd:
            raise ACPError(f"{field_name} 不能为空")
        return cmd

    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        if not value:
            raise ACPError(f"{field_name} 不能为空")
        return value

    if value is None and fallback:
        return fallback

    raise ACPError(f"{field_name} 必须是字符串命令或字符串数组")


def _merge_agent_overrides(
    current: Dict[str, AgentConfig],
    overrides: Dict[str, Any],
    source_name: str,
) -> Dict[str, AgentConfig]:
    for name, cfg_any in overrides.items():
        if not isinstance(name, str) or not name.strip():
            raise ACPError(f"{source_name}.agents 的键必须是非空字符串")
        if not isinstance(cfg_any, dict):
            raise ACPError(f"{source_name}.agents.{name} 必须是对象")

        base = current.get(name)
        if base is None:
            base = AgentConfig(name=name, command=[])

        command = _parse_command(
            cfg_any.get("command"),
            base.command if base.command else None,
            f"{source_name}.agents.{name}.command",
        )

        env = dict(base.env)
        if "env" in cfg_any:
            env.update(_parse_string_map(cfg_any.get("env"), f"{source_name}.agents.{name}.env"))

        default_config_options = dict(base.default_config_options)
        if "default_config_options" in cfg_any:
            default_config_options.update(
                _parse_string_map(
                    cfg_any.get("default_config_options"),
                    f"{source_name}.agents.{name}.default_config_options",
                )
            )

        cwd = base.cwd
        if "cwd" in cfg_any:
            raw_cwd = cfg_any.get("cwd")
            if raw_cwd is None or raw_cwd == "":
                cwd = None
            elif isinstance(raw_cwd, str):
                cwd = str(Path(_expand_env(raw_cwd)).expanduser())
            else:
                raise ACPError(f"{source_name}.agents.{name}.cwd 必须是字符串或 null")

        auto_approve = base.auto_approve_permissions
        if "auto_approve_permissions" in cfg_any:
            auto_approve = bool(cfg_any.get("auto_approve_permissions"))

        strict_config = base.strict_session_config
        if "strict_session_config" in cfg_any:
            strict_config = bool(cfg_any.get("strict_session_config"))

        default_mode = base.default_mode
        if "default_mode" in cfg_any:
            raw_mode = cfg_any.get("default_mode")
            if raw_mode is None or raw_mode == "":
                default_mode = None
            elif isinstance(raw_mode, str):
                default_mode = raw_mode
            else:
                raise ACPError(f"{source_name}.agents.{name}.default_mode 必须是字符串或 null")

        current[name] = AgentConfig(
            name=name,
            command=command,
            env=env,
            cwd=cwd,
            auto_approve_permissions=auto_approve,
            default_mode=default_mode,
            default_config_options=default_config_options,
            strict_session_config=strict_config,
        )

    return current


def _parse_agent_configs(plan: Dict[str, Any], setup: Dict[str, Any]) -> Dict[str, AgentConfig]:
    result: Dict[str, AgentConfig] = {
        name: AgentConfig(name=name, command=cmd) for name, cmd in DEFAULT_AGENT_COMMANDS.items()
    }

    setup_agents = setup.get("agents", {})
    if setup_agents:
        if not isinstance(setup_agents, dict):
            raise ACPError("setup.agents 必须是对象")
        result = _merge_agent_overrides(result, setup_agents, "setup")

    plan_agents = plan.get("agents", {})
    if plan_agents:
        if not isinstance(plan_agents, dict):
            raise ACPError("plan.agents 必须是对象")
        result = _merge_agent_overrides(result, plan_agents, "plan")

    for name, cfg in result.items():
        if not cfg.command:
            raise ACPError(f"agent {name} 未配置 command")

    return result


def _parse_routing(plan: Dict[str, Any]) -> Dict[str, str]:
    raw = plan.get("routing", {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ACPError("plan.routing 必须是对象")
    routing: Dict[str, str] = {}
    for role, agent in raw.items():
        if not isinstance(role, str) or not role.strip():
            raise ACPError("plan.routing 的键必须是非空字符串")
        if not isinstance(agent, str) or not agent.strip():
            raise ACPError(f"plan.routing.{role} 的值必须是非空字符串")
        routing[role.strip()] = agent.strip()
    return routing


def _parse_ownership(raw: Any, task_id: str) -> List[str]:
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            raise ACPError(f"task {task_id}.ownership 不能为空")
        return [value]

    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        normalized = [x.strip() for x in raw if x.strip()]
        if not normalized:
            raise ACPError(f"task {task_id}.ownership 不能为空")
        return normalized

    raise ACPError(f"task {task_id}.ownership 必须是字符串或字符串数组")


def _parse_tasks(plan: Dict[str, Any], routing: Dict[str, str]) -> List[TaskSpec]:
    tasks: List[TaskSpec] = []
    seen: set[str] = set()

    for raw in plan["tasks"]:
        if not isinstance(raw, dict):
            raise ACPError("每个 task 都必须是对象")

        task_id = str(raw.get("id", "")).strip()
        if not task_id:
            raise ACPError("task.id 为必填项")
        if task_id in seen:
            raise ACPError(f"task.id 重复: {task_id}")
        seen.add(task_id)

        agent = str(raw.get("agent", "")).strip()
        role_raw = raw.get("role")
        role: Optional[str] = None
        if isinstance(role_raw, str) and role_raw.strip():
            role = role_raw.strip()
        prompt = str(raw.get("prompt", "")).strip()
        if not prompt:
            raise ACPError(f"task {task_id} 必须提供非空的 prompt")

        if not agent:
            if role is None:
                raise ACPError(f"task {task_id} 必须提供 agent，或提供可路由的 role")
            mapped_agent = routing.get(role)
            if not mapped_agent:
                raise ACPError(f"task {task_id}.role={role} 在 plan.routing 中未配置映射")
            agent = mapped_agent

        ownership = _parse_ownership(raw.get("ownership"), task_id)

        depends_any = raw.get("depends_on", [])
        if not isinstance(depends_any, list) or not all(isinstance(x, str) for x in depends_any):
            raise ACPError(f"task {task_id}.depends_on 必须是字符串数组")

        priority = str(raw.get("priority", "sidecar")).strip().lower()
        if priority not in {"critical", "sidecar"}:
            raise ACPError(f"task {task_id}.priority 必须是 critical 或 sidecar")

        timeout_any = raw.get("timeout_sec", 900)
        try:
            timeout_sec = int(timeout_any)
        except Exception as exc:  # noqa: BLE001
            raise ACPError(f"task {task_id}.timeout_sec 必须是整数") from exc
        if timeout_sec <= 0:
            raise ACPError(f"task {task_id}.timeout_sec 必须大于 0")

        cwd_any = raw.get("cwd")
        cwd = None
        if isinstance(cwd_any, str) and cwd_any:
            cwd = str(Path(_expand_env(cwd_any)).expanduser())
        elif cwd_any not in (None, ""):
            raise ACPError(f"task {task_id}.cwd 必须是字符串或 null")

        session_mode_any = raw.get("session_mode")
        session_mode: Optional[str] = None
        if isinstance(session_mode_any, str) and session_mode_any:
            session_mode = session_mode_any
        elif session_mode_any not in (None, ""):
            raise ACPError(f"task {task_id}.session_mode 必须是字符串或 null")

        session_config_options = _parse_string_map(
            raw.get("session_config_options", {}),
            f"task {task_id}.session_config_options",
        )

        tasks.append(
            TaskSpec(
                id=task_id,
                agent=agent,
                role=role,
                prompt=prompt,
                ownership=ownership,
                priority=priority,
                depends_on=depends_any,
                timeout_sec=timeout_sec,
                cwd=cwd,
                session_mode=session_mode,
                session_config_options=session_config_options,
            )
        )

    task_ids = {t.id for t in tasks}
    for t in tasks:
        missing = [dep for dep in t.depends_on if dep not in task_ids]
        if missing:
            raise ACPError(f"task {t.id} 依赖了不存在的任务: {', '.join(missing)}")

    return tasks


def _add_warning(conn: ACPConnection, message: str) -> None:
    conn.updates.append({"method": "_orchestrator/warning", "params": {"message": message}})


def _request_with_fallback(
    conn: ACPConnection,
    method: str,
    params: Dict[str, Any],
    timeout_sec: int,
    strict: bool,
    failure_hint: str,
) -> Optional[Dict[str, Any]]:
    try:
        return conn.request(method, params, timeout_sec=timeout_sec)
    except ACPError as exc:
        if strict:
            raise
        _add_warning(conn, f"{failure_hint}: {exc}")
        return None


def _apply_session_settings(
    conn: ACPConnection,
    session_id: str,
    task: TaskSpec,
    agent_cfg: AgentConfig,
) -> None:
    strict = agent_cfg.strict_session_config
    timeout_sec = max(5, min(task.timeout_sec, 30))

    merged_options = dict(agent_cfg.default_config_options)
    merged_options.update(task.session_config_options)

    mode = task.session_mode or merged_options.get("mode") or agent_cfg.default_mode

    if mode:
        result = _request_with_fallback(
            conn,
            "session/set_mode",
            {"sessionId": session_id, "modeId": mode},
            timeout_sec,
            strict,
            f"session/set_mode({mode}) 失败",
        )
        if result is None:
            _request_with_fallback(
                conn,
                "session/set_config_option",
                {"sessionId": session_id, "configId": "mode", "value": mode},
                timeout_sec,
                strict,
                f"session/set_config_option(mode={mode}) 失败",
            )

    merged_options.pop("mode", None)
    for config_id, value in sorted(merged_options.items()):
        _request_with_fallback(
            conn,
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
            timeout_sec,
            strict,
            f"session/set_config_option({config_id}={value}) 失败",
        )


def _build_task_prompt(task: TaskSpec) -> str:
    ownership_lines = "\n".join(f"- {item}" for item in task.ownership)
    guardrail = (
        "\n\n执行约束:\n"
        "1. 仅在 ownership 范围内修改。\n"
        "2. 你不是独占代码库，不得回滚他人改动。\n"
        "3. 输出改动文件路径列表和关键变更摘要。\n"
        "ownership:\n"
        f"{ownership_lines}"
    )
    return task.prompt.rstrip() + guardrail


def _run_task(
    task: TaskSpec,
    agent_cfg: AgentConfig,
    default_cwd: str,
    verbose: bool,
) -> TaskResult:
    started = time.time()

    env = os.environ.copy()
    env.update(agent_cfg.env)

    run_cwd = task.cwd or agent_cfg.cwd or default_cwd
    run_cwd = str(Path(run_cwd).expanduser().resolve())

    conn = ACPConnection(
        command=agent_cfg.command,
        env=env,
        cwd=run_cwd,
        auto_approve_permissions=agent_cfg.auto_approve_permissions,
        verbose=verbose,
    )

    try:
        conn.start()

        conn.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": "acp-subagent-orchestrator",
                    "title": "ACP 子代理编排器",
                    "version": "0.2.0",
                },
            },
            timeout_sec=min(task.timeout_sec, 30),
        )

        new_result = conn.request(
            "session/new",
            {"cwd": run_cwd},
            timeout_sec=min(task.timeout_sec, 30),
        )

        session_id = new_result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ACPError("session/new 未返回 sessionId")

        _apply_session_settings(conn, session_id, task, agent_cfg)

        prompt_result = conn.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": _build_task_prompt(task)}],
            },
            timeout_sec=task.timeout_sec,
        )

        stop_reason: Optional[str] = None
        if isinstance(prompt_result, dict):
            raw_stop_reason = prompt_result.get("stopReason")
            if isinstance(raw_stop_reason, str):
                stop_reason = raw_stop_reason

        output_text = "".join(conn.text_chunks).strip()
        duration = round(time.time() - started, 3)
        return TaskResult(
            id=task.id,
            agent=task.agent,
            status="success",
            stop_reason=stop_reason,
            duration_sec=duration,
            output_text=output_text,
            updates=conn.updates,
            stderr=conn.stderr_lines,
        )
    except Exception as exc:  # noqa: BLE001
        duration = round(time.time() - started, 3)
        return TaskResult(
            id=task.id,
            agent=task.agent,
            status="failed",
            stop_reason=None,
            duration_sec=duration,
            output_text="",
            updates=conn.updates,
            error=str(exc),
            stderr=conn.stderr_lines,
        )
    finally:
        conn.close()


def _execute_plan(
    tasks: List[TaskSpec],
    agents: Dict[str, AgentConfig],
    default_cwd: str,
    max_parallel: int,
    verbose: bool,
) -> List[TaskResult]:
    task_by_id = {t.id: t for t in tasks}
    pending = dict(task_by_id)
    running: Dict[Any, TaskSpec] = {}
    completed: Dict[str, TaskResult] = {}
    failed_ids: set[str] = set()

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while pending or running:
            # 依赖失败的任务直接标记为跳过。
            for task_id, task in list(pending.items()):
                if any(dep in failed_ids for dep in task.depends_on):
                    completed[task_id] = TaskResult(
                        id=task_id,
                        agent=task.agent,
                        status="skipped",
                        stop_reason=None,
                        duration_sec=0.0,
                        output_text="",
                        updates=[],
                        error="因依赖失败而跳过",
                    )
                    del pending[task_id]

            ready: List[TaskSpec] = []
            for task in pending.values():
                if all(dep in completed and completed[dep].status == "success" for dep in task.depends_on):
                    ready.append(task)

            ready.sort(key=lambda t: (0 if t.priority == "critical" else 1, t.id))

            launched_any = False
            for task in ready:
                if len(running) >= max_parallel:
                    break
                if task.priority == "critical" and any(
                    t.priority == "critical" for t in running.values()
                ):
                    continue

                agent_cfg = agents.get(task.agent)
                if agent_cfg is None:
                    completed[task.id] = TaskResult(
                        id=task.id,
                        agent=task.agent,
                        status="failed",
                        stop_reason=None,
                        duration_sec=0.0,
                        output_text="",
                        updates=[],
                        error=f"未知 agent: {task.agent}",
                    )
                    failed_ids.add(task.id)
                    del pending[task.id]
                    continue

                future = pool.submit(_run_task, task, agent_cfg, default_cwd, verbose)
                running[future] = task
                del pending[task.id]
                launched_any = True

            if running:
                done, _ = wait(list(running.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
                for fut in done:
                    task = running.pop(fut)
                    result = fut.result()
                    completed[task.id] = result
                    if result.status != "success":
                        failed_ids.add(task.id)
                continue

            if pending and not launched_any:
                # 依赖环或不可满足的依赖。
                for task in pending.values():
                    completed[task.id] = TaskResult(
                        id=task.id,
                        agent=task.agent,
                        status="skipped",
                        stop_reason=None,
                        duration_sec=0.0,
                        output_text="",
                        updates=[],
                        error="依赖未满足或存在循环依赖",
                    )
                pending.clear()

    return [completed[t.id] for t in tasks if t.id in completed]


def main() -> int:
    parser = argparse.ArgumentParser(description="执行 ACP 子代理委派计划")
    parser.add_argument("--plan", required=True, help="计划 JSON 文件路径")
    parser.add_argument("--setup", help="setup JSON 文件路径（可选）")
    parser.add_argument("--output", help="输出报告 JSON 文件路径")
    parser.add_argument("--max-parallel", type=int, help="覆盖最大并行任务数")
    parser.add_argument("--verbose", action="store_true", help="打印 ACP 通信日志")
    args = parser.parse_args()

    plan_path = Path(args.plan).expanduser().resolve()
    plan = _load_plan(plan_path)

    setup: Dict[str, Any] = {}
    setup_path: Optional[Path] = None
    if args.setup:
        setup_path = Path(args.setup).expanduser().resolve()
        setup = _load_setup(setup_path)

    cwd_from_plan = plan.get("cwd")
    cwd_from_setup = setup.get("cwd")
    default_cwd = cwd_from_plan or cwd_from_setup or os.getcwd()
    default_cwd = str(Path(str(default_cwd)).expanduser().resolve())
    if not Path(default_cwd).exists():
        raise ACPError(f"cwd 不存在: {default_cwd}")

    max_parallel = int(plan.get("max_parallel", setup.get("max_parallel", 2)))
    if args.max_parallel is not None:
        max_parallel = args.max_parallel
    if max_parallel <= 0:
        raise ACPError("max_parallel 必须大于等于 1")

    agents = _parse_agent_configs(plan, setup)
    routing = _parse_routing(plan)
    tasks = _parse_tasks(plan, routing)

    started = time.time()
    results = _execute_plan(
        tasks=tasks,
        agents=agents,
        default_cwd=default_cwd,
        max_parallel=max_parallel,
        verbose=args.verbose,
    )
    elapsed = round(time.time() - started, 3)

    summary = {
        "total": len(results),
        "success": sum(1 for r in results if r.status == "success"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "elapsed_sec": elapsed,
    }

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cwd": default_cwd,
        "max_parallel": max_parallel,
        "routing": routing,
        "setup": str(setup_path) if setup_path else None,
        "summary": summary,
        "results": [asdict(r) for r in results],
    }

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"报告已写入: {output_path}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ACPError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
