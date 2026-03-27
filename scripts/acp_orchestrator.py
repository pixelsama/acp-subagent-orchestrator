#!/usr/bin/env python3
"""ACP Subagent Orchestrator.

Invokes ACP-compatible agents via stdio JSON-RPC to execute bounded subtasks.
Supports configuring preset runners, default permission modes, and session options via setup.
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
from typing import Any, Callable, Dict, List, Optional


DEFAULT_AGENT_COMMANDS: Dict[str, List[str]] = {
    "claude": ["claude-agent-acp"],
    "codex": ["codex-acp"],
    "copilot": ["copilot", "--acp", "--stdio"],
}


class ACPError(RuntimeError):
    """Represents an ACP protocol or runtime error."""


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


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class StatusTracker:
    """Periodically writes orchestration run status to a JSON file for background task polling."""

    def __init__(
        self,
        path: Path,
        interval_sec: int,
        plan_path: Path,
        default_cwd: str,
        max_parallel: int,
    ) -> None:
        self.path = path
        self.interval_sec = max(1, int(interval_sec))
        self._plan_started_monotonic = time.monotonic()
        self._last_write_monotonic = 0.0
        self._task_started_monotonic: Dict[str, float] = {}
        self._lock = threading.Lock()

        self._state: Dict[str, Any] = {
            "generated_at": _utc_now_iso(),
            "phase": "running",
            "pid": os.getpid(),
            "plan": str(plan_path),
            "cwd": default_cwd,
            "max_parallel": max_parallel,
            "summary": {},
            "tasks": {},
        }

    def init_tasks(self, tasks: List[TaskSpec]) -> None:
        with self._lock:
            task_map: Dict[str, Any] = {}
            for task in tasks:
                task_map[task.id] = {
                    "id": task.id,
                    "agent": task.agent,
                    "priority": task.priority,
                    "depends_on": list(task.depends_on),
                    "status": "pending",
                    "updates": 0,
                    "idle_sec": None,
                    "elapsed_sec": 0.0,
                    "last_update_at": None,
                    "started_at": None,
                    "completed_at": None,
                    "stop_reason": None,
                    "error": None,
                }
            self._state["tasks"] = task_map
            self._write_locked(force=True)

    def mark_running(self, task: TaskSpec) -> None:
        now_monotonic = time.monotonic()
        with self._lock:
            entry = self._state["tasks"].get(task.id, {})
            entry["status"] = "running"
            entry["started_at"] = _utc_now_iso()
            entry["last_update_at"] = entry["started_at"]
            entry["elapsed_sec"] = 0.0
            entry["error"] = None
            self._state["tasks"][task.id] = entry
            self._task_started_monotonic[task.id] = now_monotonic
            self._write_locked(force=True)

    def mark_update(
        self,
        task_id: str,
        updates: Optional[int] = None,
        idle_sec: Optional[float] = None,
        elapsed_sec: Optional[float] = None,
        wait_method: Optional[str] = None,
    ) -> None:
        now_monotonic = time.monotonic()
        with self._lock:
            entry = self._state["tasks"].get(task_id)
            if not isinstance(entry, dict):
                return
            if entry.get("status") != "running":
                return

            if updates is not None:
                entry["updates"] = int(updates)
            if idle_sec is not None:
                entry["idle_sec"] = round(max(0.0, float(idle_sec)), 3)
                last_update_epoch = time.time() - max(0.0, float(idle_sec))
                entry["last_update_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_update_epoch)
                )
            if elapsed_sec is not None:
                entry["elapsed_sec"] = round(max(0.0, float(elapsed_sec)), 3)
            else:
                started = self._task_started_monotonic.get(task_id)
                if started is not None:
                    entry["elapsed_sec"] = round(max(0.0, now_monotonic - started), 3)
            if wait_method is not None:
                entry["wait_method"] = wait_method
            self._write_locked(force=False)

    def mark_result(self, result: TaskResult) -> None:
        with self._lock:
            entry = self._state["tasks"].get(result.id)
            if not isinstance(entry, dict):
                return

            entry["status"] = result.status
            entry["completed_at"] = _utc_now_iso()
            entry["elapsed_sec"] = round(max(0.0, float(result.duration_sec)), 3)
            entry["stop_reason"] = result.stop_reason
            entry["error"] = result.error
            if result.status != "running":
                entry["idle_sec"] = 0.0 if result.status == "success" else entry.get("idle_sec")

            self._task_started_monotonic.pop(result.id, None)
            self._write_locked(force=True)

    def finalize(self, summary: Dict[str, Any]) -> None:
        with self._lock:
            self._state["phase"] = "completed"
            self._state["summary"] = summary
            self._write_locked(force=True)

    def _write_locked(self, force: bool) -> None:
        now_monotonic = time.monotonic()
        if not force and (now_monotonic - self._last_write_monotonic) < self.interval_sec:
            return

        tasks_any = self._state.get("tasks", {})
        tasks = tasks_any if isinstance(tasks_any, dict) else {}
        counts = {"pending": 0, "running": 0, "success": 0, "failed": 0, "skipped": 0}
        for entry in tasks.values():
            status = entry.get("status")
            if status in counts:
                counts[status] += 1

        self._state["generated_at"] = _utc_now_iso()
        self._state["summary"] = {
            **(self._state.get("summary", {}) if isinstance(self._state.get("summary"), dict) else {}),
            "total": len(tasks),
            "pending": counts["pending"],
            "running": counts["running"],
            "success": counts["success"],
            "failed": counts["failed"],
            "skipped": counts["skipped"],
            "elapsed_sec": round(max(0.0, now_monotonic - self._plan_started_monotonic), 3),
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)
        self._last_write_monotonic = now_monotonic


class ACPConnection:
    def __init__(
        self,
        command: List[str],
        env: Dict[str, str],
        cwd: str,
        auto_approve_permissions: bool,
        heartbeat_enabled: bool = True,
        heartbeat_interval_sec: int = 60,
        status_tick_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        status_tick_interval_sec: int = 15,
        verbose: bool = False,
    ) -> None:
        self.command = command
        self.env = env
        self.cwd = cwd
        self.auto_approve_permissions = auto_approve_permissions
        self.heartbeat_enabled = heartbeat_enabled
        self.heartbeat_interval_sec = max(5, int(heartbeat_interval_sec))
        self.status_tick_callback = status_tick_callback
        self.status_tick_interval_sec = max(2, int(status_tick_interval_sec))
        self.verbose = verbose

        self.process: Optional[subprocess.Popen[str]] = None
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._next_id = 1

        self.updates: List[Dict[str, Any]] = []
        self.text_chunks: List[str] = []
        self.stderr_lines: List[str] = []
        self.notification_count = 0
        self.last_notification_monotonic = time.monotonic()

    def start(self) -> None:
        if self.process is not None:
            raise ACPError("Process already started")

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

    def request(
        self,
        method: str,
        params: Dict[str, Any],
        timeout_sec: int,
        heartbeat_label: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.process is None or self.process.stdin is None:
            raise ACPError("Process not started")

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
        start_monotonic = time.monotonic()
        next_heartbeat_monotonic = start_monotonic + self.heartbeat_interval_sec
        next_status_tick_monotonic = start_monotonic + self.status_tick_interval_sec
        while True:
            now_monotonic = time.monotonic()
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ACPError(f"Timed out waiting for {method} response")

            wait_timeout = remaining
            if heartbeat_label and self.heartbeat_enabled and not self.verbose:
                wait_timeout = min(wait_timeout, max(0.0, next_heartbeat_monotonic - now_monotonic))
            if heartbeat_label and self.status_tick_callback:
                wait_timeout = min(wait_timeout, max(0.0, next_status_tick_monotonic - now_monotonic))

            try:
                msg = self._next_message(timeout=wait_timeout)
            except queue.Empty:
                now_monotonic = time.monotonic()
                elapsed_sec = max(0.0, now_monotonic - start_monotonic)
                idle_sec = max(0.0, now_monotonic - self.last_notification_monotonic)

                if heartbeat_label and self.status_tick_callback and now_monotonic >= next_status_tick_monotonic:
                    self._emit_status_tick(
                        {
                            "wait_method": method,
                            "elapsed_sec": round(elapsed_sec, 3),
                            "updates": self.notification_count,
                            "idle_sec": round(idle_sec, 3),
                        }
                    )
                    next_status_tick_monotonic += self.status_tick_interval_sec

                if (
                    heartbeat_label
                    and self.heartbeat_enabled
                    and not self.verbose
                    and now_monotonic >= next_heartbeat_monotonic
                ):
                    print(
                        f"[{heartbeat_label}] running {int(elapsed_sec)}s, "
                        f"updates={self.notification_count}, idle={int(idle_sec)}s",
                        file=sys.stderr,
                        flush=True,
                    )
                    next_heartbeat_monotonic += self.heartbeat_interval_sec
                continue

            if msg is None:
                raise ACPError("Agent process unexpectedly closed stdout")

            # Agent -> Client request
            if "method" in msg and "id" in msg and "result" not in msg and "error" not in msg:
                self._handle_agent_request(msg)
                continue

            # Agent -> Client notification
            if "method" in msg and "id" not in msg:
                self._handle_notification(msg)
                continue

            # Response matching this request
            if msg.get("id") == request_id:
                if "error" in msg:
                    raise ACPError(f"{method} call failed: {msg['error']}")
                result = msg.get("result")
                if isinstance(result, dict):
                    return result
                return {"value": result}

    def _send(self, payload: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ACPError("Process stdin unavailable")
        wire = json.dumps(payload, ensure_ascii=False)
        if self.verbose:
            print(f">>> {wire}", file=sys.stderr)
        self.process.stdin.write(wire + "\n")
        self.process.stdin.flush()

    def _next_message(self, timeout: float) -> Optional[Dict[str, Any]]:
        return self._queue.get(timeout=timeout)

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in self.process.stdout:
            line = raw.strip()
            if not line:
                continue
            # Currently only line-delimited JSON-RPC payloads are supported.
            if line.lower().startswith("content-length:"):
                self._queue.put(
                    {
                        "method": "_internal/error",
                        "params": {
                            "message": "Content-Length framing detected. Please use a line-delimited JSON ACP adapter."
                        },
                    }
                )
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                if self.verbose:
                    print(f"[acp] non-JSON stdout: {line}", file=sys.stderr)
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
        self.notification_count += 1
        self.last_notification_monotonic = time.monotonic()
        self._emit_status_tick(
            {
                "updates": self.notification_count,
                "idle_sec": 0.0,
            }
        )

        method = msg.get("method")
        if method == "_internal/error":
            raise ACPError(msg.get("params", {}).get("message", "Internal transport error"))

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

    def _emit_status_tick(self, payload: Dict[str, Any]) -> None:
        if self.status_tick_callback is None:
            return
        try:
            self.status_tick_callback(payload)
        except Exception:
            # Status output should not affect the main ACP flow.
            return

    def _handle_agent_request(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        request_id = msg.get("id")
        params = msg.get("params", {})

        if method == "session/request_permission":
            outcome = self._permission_outcome(params.get("options", []))
            self._send({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}})
            return

        # Return standard method-not-found for unsupported client methods.
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not supported by orchestrator client: {method}",
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
        raise ACPError(f"{label} file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ACPError(f"{label} file is not valid JSON: {path}") from exc

    if not isinstance(data, dict):
        raise ACPError(f"{label} file root must be a JSON object")
    return data


def _load_plan(path: Path) -> Dict[str, Any]:
    data = _load_json(path, "plan")

    if data.get("delegation_explicitly_requested") is not True:
        raise ACPError("Plan must set delegation_explicitly_requested=true to allow delegation")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ACPError("plan.tasks must be a non-empty array")

    return data


def _load_setup(path: Path) -> Dict[str, Any]:
    data = _load_json(path, "setup")
    if "agents" in data and not isinstance(data["agents"], dict):
        raise ACPError("setup.agents must be an object")
    return data


def _parse_string_map(value: Any, field_name: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ACPError(f"{field_name} must be an object")

    parsed: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str):
            raise ACPError(f"{field_name} keys must be strings")
        if not isinstance(v, str):
            raise ACPError(f"{field_name}.{k} value must be a string")
        parsed[k] = _expand_env(v)
    return parsed


def _parse_command(value: Any, fallback: Optional[List[str]], field_name: str) -> List[str]:
    if isinstance(value, str):
        cmd = shlex.split(value)
        if not cmd:
            raise ACPError(f"{field_name} must not be empty")
        return cmd

    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        if not value:
            raise ACPError(f"{field_name} must not be empty")
        return value

    if value is None and fallback:
        return fallback

    raise ACPError(f"{field_name} must be a string command or an array of strings")


def _merge_agent_overrides(
    current: Dict[str, AgentConfig],
    overrides: Dict[str, Any],
    source_name: str,
) -> Dict[str, AgentConfig]:
    for name, cfg_any in overrides.items():
        if not isinstance(name, str) or not name.strip():
            raise ACPError(f"{source_name}.agents keys must be non-empty strings")
        if not isinstance(cfg_any, dict):
            raise ACPError(f"{source_name}.agents.{name} must be an object")

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
                raise ACPError(f"{source_name}.agents.{name}.cwd must be a string or null")

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
                raise ACPError(f"{source_name}.agents.{name}.default_mode must be a string or null")

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
            raise ACPError("setup.agents must be an object")
        result = _merge_agent_overrides(result, setup_agents, "setup")

    plan_agents = plan.get("agents", {})
    if plan_agents:
        if not isinstance(plan_agents, dict):
            raise ACPError("plan.agents must be an object")
        result = _merge_agent_overrides(result, plan_agents, "plan")

    for name, cfg in result.items():
        if not cfg.command:
            raise ACPError(f"agent {name} is missing command")

    return result


def _parse_routing(plan: Dict[str, Any]) -> Dict[str, str]:
    raw = plan.get("routing", {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ACPError("plan.routing must be an object")
    routing: Dict[str, str] = {}
    for role, agent in raw.items():
        if not isinstance(role, str) or not role.strip():
            raise ACPError("plan.routing keys must be non-empty strings")
        if not isinstance(agent, str) or not agent.strip():
            raise ACPError(f"plan.routing.{role} value must be a non-empty string")
        routing[role.strip()] = agent.strip()
    return routing


def _parse_ownership(raw: Any, task_id: str) -> List[str]:
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            raise ACPError(f"task {task_id}.ownership must not be empty")
        return [value]

    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        normalized = [x.strip() for x in raw if x.strip()]
        if not normalized:
            raise ACPError(f"task {task_id}.ownership must not be empty")
        return normalized

    raise ACPError(f"task {task_id}.ownership must be a string or an array of strings")


def _parse_tasks(plan: Dict[str, Any], routing: Dict[str, str]) -> List[TaskSpec]:
    tasks: List[TaskSpec] = []
    seen: set[str] = set()

    for raw in plan["tasks"]:
        if not isinstance(raw, dict):
            raise ACPError("each task must be an object")

        task_id = str(raw.get("id", "")).strip()
        if not task_id:
            raise ACPError("task.id is required")
        if task_id in seen:
            raise ACPError(f"duplicate task.id: {task_id}")
        seen.add(task_id)

        agent = str(raw.get("agent", "")).strip()
        role_raw = raw.get("role")
        role: Optional[str] = None
        if isinstance(role_raw, str) and role_raw.strip():
            role = role_raw.strip()
        prompt = str(raw.get("prompt", "")).strip()
        if not prompt:
            raise ACPError(f"task {task_id} must provide a non-empty prompt")

        if not agent:
            if role is None:
                raise ACPError(f"task {task_id} must provide an agent or a routable role")
            mapped_agent = routing.get(role)
            if not mapped_agent:
                raise ACPError(f"task {task_id}.role={role} is not mapped in plan.routing")
            agent = mapped_agent

        ownership = _parse_ownership(raw.get("ownership"), task_id)

        depends_any = raw.get("depends_on", [])
        if not isinstance(depends_any, list) or not all(isinstance(x, str) for x in depends_any):
            raise ACPError(f"task {task_id}.depends_on must be an array of strings")

        priority = str(raw.get("priority", "sidecar")).strip().lower()
        if priority not in {"critical", "sidecar"}:
            raise ACPError(f"task {task_id}.priority must be critical or sidecar")

        timeout_any = raw.get("timeout_sec", 900)
        try:
            timeout_sec = int(timeout_any)
        except Exception as exc:  # noqa: BLE001
            raise ACPError(f"task {task_id}.timeout_sec must be an integer") from exc
        if timeout_sec <= 0:
            raise ACPError(f"task {task_id}.timeout_sec must be greater than 0")

        cwd_any = raw.get("cwd")
        cwd = None
        if isinstance(cwd_any, str) and cwd_any:
            cwd = str(Path(_expand_env(cwd_any)).expanduser())
        elif cwd_any not in (None, ""):
            raise ACPError(f"task {task_id}.cwd must be a string or null")

        session_mode_any = raw.get("session_mode")
        session_mode: Optional[str] = None
        if isinstance(session_mode_any, str) and session_mode_any:
            session_mode = session_mode_any
        elif session_mode_any not in (None, ""):
            raise ACPError(f"task {task_id}.session_mode must be a string or null")

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
            raise ACPError(f"task {t.id} depends on missing task(s): {', '.join(missing)}")

    return tasks


def _to_non_empty_str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return None


def _extract_option_id(raw: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "configId", "config_id"):
        value = _to_non_empty_str(raw.get(key))
        if value:
            return value
    return None


def _collect_choice_values(raw: Any, values: List[str], seen: set[str]) -> None:
    if isinstance(raw, list):
        for item in raw:
            _collect_choice_values(item, values, seen)
        return

    if isinstance(raw, dict):
        for key in ("value", "id", "optionId", "option_id"):
            value = _to_non_empty_str(raw.get(key))
            if value and value not in seen:
                seen.add(value)
                values.append(value)

        for key in ("options", "selectOptions", "choices", "items", "enum", "allowedValues", "values"):
            if key in raw:
                _collect_choice_values(raw.get(key), values, seen)

        for key in ("value", "select", "schema", "data", "payload"):
            nested = raw.get(key)
            if isinstance(nested, (dict, list)):
                _collect_choice_values(nested, values, seen)
        return

    value = _to_non_empty_str(raw)
    if value and value not in seen:
        seen.add(value)
        values.append(value)


def _extract_option_choices(session_new_result: Dict[str, Any], option_id: str) -> List[str]:
    raw_options = session_new_result.get("configOptions")
    if raw_options is None:
        raw_options = session_new_result.get("config_options")
    if not isinstance(raw_options, list):
        return []

    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        if _extract_option_id(raw) != option_id:
            continue

        candidates: List[Any] = []
        for key in ("options", "selectOptions", "choices", "items", "enum", "allowedValues", "values"):
            if key in raw:
                candidates.append(raw.get(key))
        for key in ("value", "select", "schema", "data", "payload"):
            nested = raw.get(key)
            if isinstance(nested, (dict, list)):
                candidates.append(nested)

        values: List[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            _collect_choice_values(candidate, values, seen)
        return values

    return []


def _extract_option_current_value(session_new_result: Dict[str, Any], option_id: str) -> Optional[str]:
    raw_options = session_new_result.get("configOptions")
    if raw_options is None:
        raw_options = session_new_result.get("config_options")
    if not isinstance(raw_options, list):
        return None

    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        if _extract_option_id(raw) != option_id:
            continue

        direct = _to_non_empty_str(raw.get("currentValue")) or _to_non_empty_str(raw.get("current_value"))
        if direct:
            return direct

        value_payload = raw.get("value")
        if isinstance(value_payload, dict):
            nested = _to_non_empty_str(value_payload.get("currentValue")) or _to_non_empty_str(
                value_payload.get("current_value")
            )
            if nested:
                return nested
        return None

    return None


def _extract_model_choices(session_new_result: Dict[str, Any]) -> List[str]:
    return _extract_option_choices(session_new_result, "model")


def _extract_current_model(session_new_result: Dict[str, Any]) -> Optional[str]:
    return _extract_option_current_value(session_new_result, "model")


def _format_model_selection_failure(
    *,
    agent_name: str,
    requested_model: str,
    available_models: Optional[List[str]] = None,
    root_cause: Optional[str] = None,
) -> str:
    parts = [
        f"Model '{requested_model}' is not usable for agent '{agent_name}'.",
    ]
    if available_models:
        parts.append(f"Available models: {', '.join(available_models)}.")
    else:
        parts.append("Available models are unknown (runner did not expose model choices).")
    parts.append(
        "Fix options: update setup.json agents.<agent>.default_config_options.model for a global default,"
        " or set task.session_config_options.model explicitly for this task."
    )
    if root_cause:
        parts.append(f"Root cause: {root_cause}")
    return " ".join(parts)


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
    available_models: Optional[List[str]] = None,
) -> None:
    strict = agent_cfg.strict_session_config
    timeout_sec = max(5, min(task.timeout_sec, 30))

    merged_options = dict(agent_cfg.default_config_options)
    merged_options.update(task.session_config_options)
    requested_model = _to_non_empty_str(merged_options.get("model"))

    if requested_model and available_models and requested_model not in available_models:
        raise ACPError(
            _format_model_selection_failure(
                agent_name=agent_cfg.name,
                requested_model=requested_model,
                available_models=available_models,
                root_cause="requested model is not in the discovered model choices",
            )
        )

    mode = task.session_mode or merged_options.get("mode") or agent_cfg.default_mode

    if mode:
        result = _request_with_fallback(
            conn,
            "session/set_mode",
            {"sessionId": session_id, "modeId": mode},
            timeout_sec,
            strict,
            f"session/set_mode({mode}) failed",
        )
        if result is None:
            _request_with_fallback(
                conn,
                "session/set_config_option",
                {"sessionId": session_id, "configId": "mode", "value": mode},
                timeout_sec,
                strict,
                f"session/set_config_option(mode={mode}) failed",
            )

    merged_options.pop("mode", None)
    for config_id, value in sorted(merged_options.items()):
        option_strict = strict or config_id == "model"
        try:
            _request_with_fallback(
                conn,
                "session/set_config_option",
                {"sessionId": session_id, "configId": config_id, "value": value},
                timeout_sec,
                option_strict,
                f"session/set_config_option({config_id}={value}) failed",
            )
        except ACPError as exc:
            if config_id == "model":
                requested = _to_non_empty_str(value) or str(value)
                raise ACPError(
                    _format_model_selection_failure(
                        agent_name=agent_cfg.name,
                        requested_model=requested,
                        available_models=available_models,
                        root_cause=str(exc),
                    )
                ) from exc
            raise


def _build_task_prompt(task: TaskSpec) -> str:
    ownership_lines = "\n".join(f"- {item}" for item in task.ownership)
    guardrail = (
        "\n\nExecution constraints:\n"
        "1. Modify only within the declared ownership scope.\n"
        "2. You are not alone in the repository; do not revert others' changes.\n"
        "3. Output the list of changed file paths and a summary of key changes.\n"
        "ownership:\n"
        f"{ownership_lines}"
    )
    return task.prompt.rstrip() + guardrail


def _run_task(
    task: TaskSpec,
    agent_cfg: AgentConfig,
    default_cwd: str,
    heartbeat_enabled: bool,
    heartbeat_interval_sec: int,
    status_tracker: Optional[StatusTracker],
    status_interval_sec: int,
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
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_interval_sec=heartbeat_interval_sec,
        status_tick_callback=(
            (lambda payload: status_tracker.mark_update(task.id, **payload)) if status_tracker else None
        ),
        status_tick_interval_sec=status_interval_sec,
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
                    "title": "ACP Subagent Orchestrator",
                    "version": "0.2.0",
                },
            },
            timeout_sec=min(task.timeout_sec, 30),
        )

        new_result = conn.request(
            "session/new",
            {"cwd": run_cwd, "mcpServers": []},
            timeout_sec=min(task.timeout_sec, 30),
        )

        session_id = new_result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ACPError("session/new did not return sessionId")

        available_models = _extract_model_choices(new_result)
        _apply_session_settings(conn, session_id, task, agent_cfg, available_models=available_models)

        prompt_result = conn.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": _build_task_prompt(task)}],
            },
            timeout_sec=task.timeout_sec,
            heartbeat_label=task.id,
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
        partial_output = "".join(conn.text_chunks).strip()
        return TaskResult(
            id=task.id,
            agent=task.agent,
            status="failed",
            stop_reason=None,
            duration_sec=duration,
            output_text=partial_output,
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
    heartbeat_enabled: bool,
    heartbeat_interval_sec: int,
    status_tracker: Optional[StatusTracker],
    status_interval_sec: int,
    verbose: bool,
    pre_completed: Optional[Dict[str, TaskResult]] = None,
) -> List[TaskResult]:
    task_by_id = {t.id: t for t in tasks}
    pending = dict(task_by_id)
    running: Dict[Any, TaskSpec] = {}
    completed: Dict[str, TaskResult] = dict(pre_completed) if pre_completed else {}
    failed_ids: set[str] = set()

    # Remove pre-completed tasks from pending so they are not re-run.
    for tid in completed:
        pending.pop(tid, None)

    if status_tracker:
        status_tracker.init_tasks(tasks)

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while pending or running:
            # Mark tasks as skipped immediately when any dependency has failed.
            for task_id, task in list(pending.items()):
                if any(dep in failed_ids for dep in task.depends_on):
                    skipped = TaskResult(
                        id=task_id,
                        agent=task.agent,
                        status="skipped",
                        stop_reason=None,
                        duration_sec=0.0,
                        output_text="",
                        updates=[],
                        error="Skipped due to failed dependency",
                    )
                    completed[task_id] = skipped
                    if status_tracker:
                        status_tracker.mark_result(skipped)
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
                    failed = TaskResult(
                        id=task.id,
                        agent=task.agent,
                        status="failed",
                        stop_reason=None,
                        duration_sec=0.0,
                        output_text="",
                        updates=[],
                        error=f"Unknown agent: {task.agent}",
                    )
                    completed[task.id] = failed
                    if status_tracker:
                        status_tracker.mark_result(failed)
                    failed_ids.add(task.id)
                    del pending[task.id]
                    continue

                if status_tracker:
                    status_tracker.mark_running(task)
                future = pool.submit(
                    _run_task,
                    task,
                    agent_cfg,
                    default_cwd,
                    heartbeat_enabled,
                    heartbeat_interval_sec,
                    status_tracker,
                    status_interval_sec,
                    verbose,
                )
                running[future] = task
                del pending[task.id]
                launched_any = True

            if running:
                done, _ = wait(list(running.keys()), timeout=0.2, return_when=FIRST_COMPLETED)
                for fut in done:
                    task = running.pop(fut)
                    result = fut.result()
                    completed[task.id] = result
                    if status_tracker:
                        status_tracker.mark_result(result)
                    if result.status != "success":
                        failed_ids.add(task.id)
                continue

            if pending and not launched_any:
                # Cyclic or otherwise unsatisfied dependencies.
                for task in pending.values():
                    skipped = TaskResult(
                        id=task.id,
                        agent=task.agent,
                        status="skipped",
                        stop_reason=None,
                        duration_sec=0.0,
                        output_text="",
                        updates=[],
                        error="Unsatisfied dependency or dependency cycle detected",
                    )
                    completed[task.id] = skipped
                    if status_tracker:
                        status_tracker.mark_result(skipped)
                pending.clear()

    return [completed[t.id] for t in tasks if t.id in completed]


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute an ACP subagent delegation plan")
    parser.add_argument("--plan", required=True, help="Path to the plan JSON file")
    parser.add_argument("--setup", help="Path to the setup JSON file (optional)")
    parser.add_argument("--output", help="Path to write the output report JSON")
    parser.add_argument(
        "--status-file",
        help="Path to write runtime status JSON (optional, useful for background polling)",
    )
    parser.add_argument("--max-parallel", type=int, help="Override maximum parallel task count")
    parser.add_argument(
        "--status-interval-sec",
        type=int,
        help="Status file refresh interval in seconds (default: 15)",
    )
    parser.add_argument(
        "--heartbeat-interval-sec",
        type=int,
        help="Heartbeat interval in seconds while waiting for session/prompt (default: 60)",
    )
    parser.add_argument(
        "--no-heartbeat",
        action="store_true",
        help="Disable heartbeat logs while waiting",
    )
    parser.add_argument("--verbose", action="store_true", help="Print ACP communication logs")
    parser.add_argument("--resume", help="Previous report JSON; tasks with status=success are skipped")
    parser.add_argument("--skip-tasks", help="Comma-separated task IDs to force-mark as succeeded and skip")
    args = parser.parse_args()

    plan_path = Path(args.plan).expanduser().resolve()
    plan = _load_plan(plan_path)

    setup: Dict[str, Any] = {}
    setup_path: Optional[Path] = None
    if args.setup:
        setup_path = Path(args.setup).expanduser().resolve()
        setup = _load_setup(setup_path)
    elif isinstance(plan.get("setup"), str):
        setup_path = (plan_path.parent / plan["setup"]).resolve()
        setup = _load_setup(setup_path)

    cwd_from_plan = plan.get("cwd")
    cwd_from_setup = setup.get("cwd")
    default_cwd = cwd_from_plan or cwd_from_setup or os.getcwd()
    default_cwd = str(Path(str(default_cwd)).expanduser().resolve())
    if not Path(default_cwd).exists():
        raise ACPError(f"cwd does not exist: {default_cwd}")

    max_parallel = int(plan.get("max_parallel", setup.get("max_parallel", 2)))
    if args.max_parallel is not None:
        max_parallel = args.max_parallel
    if max_parallel <= 0:
        raise ACPError("max_parallel must be >= 1")

    status_file_raw = args.status_file or plan.get("status_file") or setup.get("status_file")
    status_interval_sec = int(
        plan.get("status_interval_sec", setup.get("status_interval_sec", 15))
    )
    if args.status_interval_sec is not None:
        status_interval_sec = args.status_interval_sec
    if status_interval_sec <= 0:
        raise ACPError("status_interval_sec must be >= 1")

    status_tracker: Optional[StatusTracker] = None
    status_path: Optional[Path] = None
    if status_file_raw:
        status_path = Path(str(status_file_raw)).expanduser().resolve()
        status_tracker = StatusTracker(
            path=status_path,
            interval_sec=status_interval_sec,
            plan_path=plan_path,
            default_cwd=default_cwd,
            max_parallel=max_parallel,
        )

    heartbeat_enabled = bool(plan.get("heartbeat_enabled", setup.get("heartbeat_enabled", True)))
    if args.no_heartbeat:
        heartbeat_enabled = False

    heartbeat_interval_sec = int(
        plan.get("heartbeat_interval_sec", setup.get("heartbeat_interval_sec", 60))
    )
    if args.heartbeat_interval_sec is not None:
        heartbeat_interval_sec = args.heartbeat_interval_sec
    if heartbeat_interval_sec <= 0:
        raise ACPError("heartbeat_interval_sec must be >= 1")

    agents = _parse_agent_configs(plan, setup)
    routing = _parse_routing(plan)
    tasks = _parse_tasks(plan, routing)

    pre_completed: Dict[str, TaskResult] = {}
    if args.resume:
        resume_path = Path(args.resume).expanduser().resolve()
        prev_report = _load_json(resume_path, "resume report")
        for r in prev_report.get("results", []):
            if r.get("status") == "success":
                pre_completed[r["id"]] = TaskResult(
                    id=r["id"],
                    agent=r.get("agent", ""),
                    status="success",
                    stop_reason=r.get("stop_reason"),
                    duration_sec=r.get("duration_sec", 0),
                    output_text=r.get("output_text", ""),
                    updates=r.get("updates", []),
                )
    if args.skip_tasks:
        for tid in args.skip_tasks.split(","):
            tid = tid.strip()
            if tid and tid not in pre_completed:
                pre_completed[tid] = TaskResult(
                    id=tid,
                    agent="",
                    status="success",
                    stop_reason="force-skipped",
                    duration_sec=0,
                    output_text="",
                    updates=[],
                )

    started = time.time()
    results = _execute_plan(
        tasks=tasks,
        agents=agents,
        default_cwd=default_cwd,
        max_parallel=max_parallel,
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_interval_sec=heartbeat_interval_sec,
        status_tracker=status_tracker,
        status_interval_sec=status_interval_sec,
        verbose=args.verbose,
        pre_completed=pre_completed,
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
        "status_file": str(status_path) if status_path else None,
        "routing": routing,
        "setup": str(setup_path) if setup_path else None,
        "summary": summary,
        "results": [asdict(r) for r in results],
    }

    if status_tracker is not None:
        status_tracker.finalize(summary)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"Report written to: {output_path}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ACPError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
