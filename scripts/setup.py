#!/usr/bin/env python3
"""生成 ACP 子代理编排器的 setup 配置。

setup 阶段会先写入 runner 基础配置；若能成功连上 agent，会动态发现
`configOptions / modes` 并支持在 setup 阶段完成默认权限配置。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


AGENTS = ("codex", "claude", "copilot")
ALL_PERMISSIONS_PRESET = {
    "codex": "full-access",
    "claude": "bypassPermissions",
}


class ACPProbeError(RuntimeError):
    """用于表示探测阶段的 ACP 错误。"""


@dataclass
class DiscoveredOption:
    id: str
    name: str
    category: Optional[str]
    current_value: Optional[str]
    choices: Dict[str, str]


@dataclass
class DiscoveryResult:
    options: List[DiscoveredOption]
    warning: Optional[str] = None


class ACPProbeConnection:
    """最小 ACP 探测连接（stdio + JSON-RPC）。"""

    def __init__(self, command: List[str], env: Dict[str, str], cwd: str) -> None:
        self.command = command
        self.env = env
        self.cwd = cwd
        self._next_id = 1
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._stderr_lines: List[str] = []
        self._process: Optional[subprocess.Popen[str]] = None

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    def start(self) -> None:
        if self._process is not None:
            raise ACPProbeError("探测连接已启动")
        self._process = subprocess.Popen(
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
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        self._process = None

    def request(self, method: str, params: Dict[str, Any], timeout_sec: int) -> Dict[str, Any]:
        if self._process is None or self._process.stdin is None:
            raise ACPProbeError("探测连接尚未启动")

        request_id = self._next_id
        self._next_id += 1

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        self._send(payload)

        deadline = time.time() + timeout_sec
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ACPProbeError(f"{method} 超时")

            msg = self._next_message(remaining)
            if msg is None:
                raise ACPProbeError("runner 已退出，未收到完整响应")

            # Agent -> Client 请求（探测阶段只做最小应答）
            if "method" in msg and "id" in msg and "result" not in msg and "error" not in msg:
                self._handle_agent_request(msg)
                continue

            # Agent -> Client 通知（探测阶段忽略）
            if "method" in msg and "id" not in msg:
                method_name = msg.get("method")
                if method_name == "_internal/error":
                    message = msg.get("params", {}).get("message", "内部传输错误")
                    raise ACPProbeError(str(message))
                continue

            if msg.get("id") == request_id:
                if "error" in msg:
                    raise ACPProbeError(f"{method} 失败: {msg['error']}")
                result = msg.get("result")
                if isinstance(result, dict):
                    return result
                return {"value": result}

    def _send(self, payload: Dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise ACPProbeError("runner stdin 不可用")
        wire = json.dumps(payload, ensure_ascii=False)
        self._process.stdin.write(wire + "\n")
        self._process.stdin.flush()

    def _next_message(self, timeout: float) -> Optional[Dict[str, Any]]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for raw in self._process.stdout:
            line = raw.strip()
            if not line:
                continue
            if line.lower().startswith("content-length:"):
                self._queue.put(
                    {
                        "method": "_internal/error",
                        "params": {
                            "message": "检测到 Content-Length 分帧，当前 setup 探测仅支持按行 JSON。"
                        },
                    }
                )
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self._queue.put(msg)
        self._queue.put(None)

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for raw in self._process.stderr:
            self._stderr_lines.append(raw.rstrip("\n"))

    def _handle_agent_request(self, msg: Dict[str, Any]) -> None:
        request_id = msg.get("id")
        method = msg.get("method")

        if method == "session/request_permission":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            )
            return

        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"setup probe 不支持客户端方法: {method}",
                },
            }
        )


def _split_command(command: str) -> List[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError("命令不能为空")
    return parts


def _expand_env(value: str) -> str:
    return os.path.expandvars(value)


def _resolve_existing_dir(path_like: str, field_name: str) -> str:
    resolved = Path(_expand_env(path_like)).expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"{field_name} 不存在: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"{field_name} 不是目录: {resolved}")
    return str(resolved)


def _check_command_available(command: List[str]) -> Tuple[bool, str]:
    if not command:
        return False, "空命令"
    executable = command[0]
    has_path_part = (os.sep in executable) or (os.altsep is not None and os.altsep in executable)

    if has_path_part:
        candidate = Path(executable).expanduser()
        if candidate.exists():
            return True, ""
        return False, f"可执行文件不存在: {candidate}"

    if shutil.which(executable) is not None:
        return True, ""
    return False, f"在 PATH 中找不到命令: {executable}"


def _parse_key_value_items(
    items: List[str],
    flag_name: str,
    *,
    allow_empty_value: bool = False,
    expand_value: bool = True,
) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise ValueError(f"{flag_name} 必须是 KEY=VALUE 形式: {raw}")
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value if allow_empty_value else value.strip()
        if not key:
            raise ValueError(f"{flag_name} 的 KEY 不能为空: {raw}")
        if not allow_empty_value and not value:
            raise ValueError(f"{flag_name} 的 VALUE 不能为空: {raw}")
        result[key] = _expand_env(value) if expand_value else value
    return result


def _value_to_string(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        inner = value.get("value")
        if isinstance(inner, str) and inner:
            return inner
        inner = value.get("id")
        if isinstance(inner, str) and inner:
            return inner
    return None


def _extract_select_payload(option: Dict[str, Any]) -> Dict[str, Any]:
    kind = option.get("kind")
    if isinstance(kind, dict):
        if any(k in kind for k in ("currentValue", "current_value", "options", "selectOptions")):
            return kind
        for key in ("select", "Select"):
            nested = kind.get(key)
            if isinstance(nested, dict):
                return nested
    return option


def _walk_collect_choices(node: Any, out: Dict[str, str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_collect_choices(item, out)
        return

    if not isinstance(node, dict):
        return

    maybe_value = node.get("value")
    if isinstance(maybe_value, str) and maybe_value:
        maybe_name = node.get("name")
        out[maybe_value] = maybe_name if isinstance(maybe_name, str) and maybe_name else maybe_value
        return

    for value in node.values():
        _walk_collect_choices(value, out)


def _parse_config_option(raw: Dict[str, Any]) -> Optional[DiscoveredOption]:
    option_id = raw.get("id")
    if not isinstance(option_id, str) or not option_id:
        return None

    name_raw = raw.get("name")
    category_raw = raw.get("category")

    name = name_raw if isinstance(name_raw, str) and name_raw else option_id
    category = category_raw if isinstance(category_raw, str) and category_raw else None

    select_payload = _extract_select_payload(raw)

    current_value: Optional[str] = None
    for key in ("currentValue", "current_value"):
        current_value = _value_to_string(select_payload.get(key))
        if current_value:
            break
    if not current_value:
        current_value = _value_to_string(raw.get("currentValue")) or _value_to_string(raw.get("current_value"))

    choices: Dict[str, str] = {}
    for key in ("options", "selectOptions", "choices"):
        if key in select_payload:
            _walk_collect_choices(select_payload[key], choices)

    return DiscoveredOption(
        id=option_id,
        name=name,
        category=category,
        current_value=current_value,
        choices=choices,
    )


def _parse_modes_fallback(raw_modes: Any) -> Optional[DiscoveredOption]:
    if not isinstance(raw_modes, dict):
        return None

    current = _value_to_string(raw_modes.get("currentModeId")) or _value_to_string(
        raw_modes.get("current_mode_id")
    )
    available = raw_modes.get("availableModes")
    if available is None:
        available = raw_modes.get("available_modes")

    if not isinstance(available, list):
        return None

    choices: Dict[str, str] = {}
    for item in available:
        if not isinstance(item, dict):
            continue
        mode_id = _value_to_string(item.get("id"))
        if not mode_id:
            continue
        mode_name = item.get("name")
        choices[mode_id] = mode_name if isinstance(mode_name, str) and mode_name else mode_id

    if not choices:
        return None

    return DiscoveredOption(
        id="mode",
        name="Mode",
        category="mode",
        current_value=current,
        choices=choices,
    )


def _normalize_discovered_options(response: Dict[str, Any]) -> List[DiscoveredOption]:
    raw_options = response.get("configOptions")
    if raw_options is None:
        raw_options = response.get("config_options")

    options: List[DiscoveredOption] = []
    if isinstance(raw_options, list):
        for raw in raw_options:
            if not isinstance(raw, dict):
                continue
            parsed = _parse_config_option(raw)
            if parsed is not None:
                options.append(parsed)

    if options:
        return options

    fallback = _parse_modes_fallback(response.get("modes"))
    return [fallback] if fallback is not None else []


def _is_mode_option(option: DiscoveredOption) -> bool:
    if option.id == "mode":
        return True
    return option.category == "mode"


def _discover_agent(
    *,
    agent: str,
    command: List[str],
    env: Dict[str, str],
    cwd: str,
    timeout_sec: int,
) -> DiscoveryResult:
    conn = ACPProbeConnection(command=command, env=env, cwd=cwd)
    session_id: Optional[str] = None

    try:
        conn.start()
        conn.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": "acp-subagent-orchestrator-setup",
                    "title": "ACP setup probe",
                    "version": "0.3.0",
                },
            },
            timeout_sec=timeout_sec,
        )
        new_result = conn.request("session/new", {"cwd": cwd}, timeout_sec=timeout_sec)

        maybe_session = new_result.get("sessionId")
        if isinstance(maybe_session, str) and maybe_session:
            session_id = maybe_session

        options = _normalize_discovered_options(new_result)
        warning = None
        if not options:
            warning = "未返回 configOptions/modes，无法动态列出权限选项。"
        return DiscoveryResult(options=options, warning=warning)
    except Exception as exc:  # noqa: BLE001
        stderr_tail = conn.stderr_text.strip()
        suffix = f"\nrunner stderr:\n{stderr_tail}" if stderr_tail else ""
        raise ACPProbeError(f"{agent} 探测失败: {exc}{suffix}") from exc
    finally:
        if session_id:
            try:
                conn.request("session/close", {"sessionId": session_id}, timeout_sec=3)
            except Exception:  # noqa: BLE001
                pass
        conn.close()


def _print_discovery(agent: str, result: DiscoveryResult) -> None:
    print(f"\n[{agent}] 动态发现结果")
    if result.warning:
        print(f"  - 警告: {result.warning}")
    if not result.options:
        print("  - 无可用配置项")
        return
    for option in result.options:
        category = option.category or "uncategorized"
        current = option.current_value or "(none)"
        print(f"  - {option.id} [{category}] current={current}")
        if option.choices:
            values = ", ".join(option.choices.keys())
            print(f"    choices: {values}")


def _interactive_select(
    agent: str,
    options: List[DiscoveredOption],
    selected: Dict[str, str],
    *,
    all_options: bool,
) -> None:
    if not sys.stdin.isatty():
        print(f"[{agent}] stdin 不是 TTY，跳过交互选择。")
        return

    ordered = sorted(options, key=lambda o: (0 if _is_mode_option(o) else 1, o.id))
    for option in ordered:
        if not option.choices:
            continue
        if not all_options and not _is_mode_option(option):
            continue

        current = selected.get(option.id) or option.current_value
        print(f"\n[{agent}] 选择 {option.id}（当前: {current or '(none)'}）")
        items = list(option.choices.items())
        for idx, (value, name) in enumerate(items, start=1):
            print(f"  {idx}. {name} ({value})")
        print("  回车：保持当前值")

        while True:
            raw = input("请输入编号或 value: ").strip()
            if not raw:
                break
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(items):
                    selected[option.id] = items[idx - 1][0]
                    break
            elif raw in option.choices:
                selected[option.id] = raw
                break
            print("输入无效，请重试。")


def _option_map(options: List[DiscoveredOption]) -> Dict[str, DiscoveredOption]:
    return {opt.id: opt for opt in options}


def _validate_selected_values(
    agent: str,
    selected: Dict[str, str],
    discovered_options: List[DiscoveredOption],
) -> None:
    by_id = _option_map(discovered_options)
    for config_id, value in selected.items():
        option = by_id.get(config_id)
        if option is None or not option.choices:
            continue
        if value not in option.choices:
            supported = ", ".join(option.choices.keys())
            raise ValueError(
                f"[{agent}] {config_id}={value} 无效。可选值: {supported}"
            )


def _build_parser() -> argparse.ArgumentParser:
    default_output = Path("~/.acp-subagent-orchestrator/setup.json").expanduser()
    parser = argparse.ArgumentParser(description="生成 ACP 子代理编排器 setup 配置")
    parser.add_argument("--output", default=str(default_output), help="输出 setup.json 路径")
    parser.add_argument("--cwd", default=".", help="默认工作目录")
    parser.add_argument("--max-parallel", type=int, default=2, help="默认最大并行任务数")

    parser.add_argument("--codex-command", default="codex-acp", help="Codex ACP runner 命令")
    parser.add_argument("--claude-command", default="claude-agent-acp", help="Claude ACP runner 命令")
    parser.add_argument(
        "--copilot-command",
        default="copilot --acp --stdio",
        help="Copilot ACP runner 命令",
    )

    parser.add_argument("--codex-mode", default="", help="Codex 默认 mode（可选）")
    parser.add_argument("--claude-mode", default="", help="Claude 默认 mode（可选）")
    parser.add_argument("--copilot-mode", default="", help="Copilot 默认 mode（可选）")

    perms = parser.add_mutually_exclusive_group()
    perms.add_argument("--manual-permissions", action="store_true", help="关闭自动审批，改为手动审批")
    perms.add_argument(
        "--all-permissions",
        action="store_true",
        help="一键放开：Codex=full-access，Claude=bypassPermissions，并启用自动审批",
    )

    parser.add_argument(
        "--strict-session-config",
        action="store_true",
        help="会话 mode/configOptions 下发失败时直接报错（默认仅警告）",
    )
    parser.add_argument(
        "--emit-default-mode",
        action="store_true",
        help="兼容旧字段：同时写入 default_mode（默认仅写 default_config_options）",
    )

    parser.add_argument(
        "--no-discover",
        action="store_true",
        help="跳过 ACP 动态发现（不读取 runner 返回的 configOptions/modes）",
    )
    parser.add_argument(
        "--require-discovery",
        action="store_true",
        help="要求所有 agent 都必须动态发现成功，否则报错",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=int,
        default=15,
        help="每个 agent 的动态发现超时（秒）",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="动态发现后，交互式选择默认权限配置",
    )
    parser.add_argument(
        "--interactive-all-options",
        action="store_true",
        help="交互式模式下同时配置非 mode 配置项",
    )
    parser.add_argument(
        "--strict-command-check",
        action="store_true",
        help="命令不可执行时直接报错（默认仅警告）",
    )

    for agent in AGENTS:
        parser.add_argument(
            f"--{agent}-env",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help=f"{agent} 的环境变量，可重复传入",
        )
        parser.add_argument(
            f"--{agent}-cwd",
            default="",
            help=f"{agent} 的工作目录（可选）",
        )
        parser.add_argument(
            f"--{agent}-config",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help=f"{agent} 的默认 session config 选项，可重复传入",
        )

    return parser


def _build_initial_config(args: argparse.Namespace, global_cwd: str, auto_approve: bool) -> Dict[str, object]:
    config: Dict[str, object] = {
        "cwd": global_cwd,
        "max_parallel": args.max_parallel,
        "agents": {},
    }

    for agent in AGENTS:
        command_raw = getattr(args, f"{agent}_command")
        command = _split_command(command_raw)
        ok, msg = _check_command_available(command)
        if not ok:
            text = f"[{agent}] 命令检查失败: {msg}"
            if args.strict_command_check:
                raise ValueError(text)
            print(f"警告: {text}", file=sys.stderr)

        env_items = getattr(args, f"{agent}_env")
        env = _parse_key_value_items(
            env_items,
            f"--{agent}-env",
            allow_empty_value=True,
            expand_value=True,
        )

        agent_cwd_raw = getattr(args, f"{agent}_cwd")
        agent_cfg: Dict[str, object] = {
            "command": command,
            "auto_approve_permissions": auto_approve,
            "strict_session_config": args.strict_session_config,
        }
        if env:
            agent_cfg["env"] = env
        if agent_cwd_raw:
            agent_cfg["cwd"] = _resolve_existing_dir(agent_cwd_raw, f"--{agent}-cwd")

        config["agents"][agent] = agent_cfg

    return config


def _discover_all_agents(
    args: argparse.Namespace,
    config: Dict[str, object],
    global_cwd: str,
) -> Dict[str, DiscoveryResult]:
    if args.no_discover:
        return {}

    discovered: Dict[str, DiscoveryResult] = {}
    failures: List[str] = []

    agents_cfg = config["agents"]
    assert isinstance(agents_cfg, dict)

    for agent in AGENTS:
        cfg_any = agents_cfg.get(agent)
        if not isinstance(cfg_any, dict):
            continue

        command = cfg_any.get("command")
        if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
            failures.append(f"[{agent}] command 配置非法")
            continue

        env = os.environ.copy()
        extra_env = cfg_any.get("env", {})
        if isinstance(extra_env, dict):
            env.update({k: str(v) for k, v in extra_env.items()})

        cwd = cfg_any.get("cwd")
        run_cwd = cwd if isinstance(cwd, str) and cwd else global_cwd

        try:
            result = _discover_agent(
                agent=agent,
                command=command,
                env=env,
                cwd=run_cwd,
                timeout_sec=args.discovery_timeout,
            )
            discovered[agent] = result
            _print_discovery(agent, result)
        except ACPProbeError as exc:
            failures.append(str(exc))
            print(f"\n[{agent}] 动态发现失败: {exc}", file=sys.stderr)

    if failures and args.require_discovery:
        joined = "\n".join(failures)
        raise ValueError(f"--require-discovery 已启用，但存在发现失败:\n{joined}")

    return discovered


def _resolve_mode_overrides(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    mode_overrides: Dict[str, Optional[str]] = {
        "codex": args.codex_mode.strip() or None,
        "claude": args.claude_mode.strip() or None,
        "copilot": args.copilot_mode.strip() or None,
    }

    if args.all_permissions:
        for agent, mode in ALL_PERMISSIONS_PRESET.items():
            if mode_overrides.get(agent) is None:
                mode_overrides[agent] = mode

    return mode_overrides


def _apply_agent_defaults(
    *,
    agent: str,
    agent_cfg: Dict[str, Any],
    discovered: Optional[DiscoveryResult],
    mode_override: Optional[str],
    cli_config_overrides: Dict[str, str],
    interactive: bool,
    interactive_all_options: bool,
    emit_default_mode: bool,
) -> None:
    selected: Dict[str, str] = {}

    if discovered is not None:
        for option in discovered.options:
            if _is_mode_option(option) and option.current_value:
                selected[option.id] = option.current_value

    if mode_override:
        selected["mode"] = mode_override

    selected.update(cli_config_overrides)

    if interactive and discovered is not None:
        _interactive_select(
            agent,
            discovered.options,
            selected,
            all_options=interactive_all_options,
        )

    if discovered is not None:
        _validate_selected_values(agent, selected, discovered.options)

    if selected:
        agent_cfg["default_config_options"] = selected
        if emit_default_mode and "mode" in selected:
            agent_cfg["default_mode"] = selected["mode"]


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.max_parallel <= 0:
        raise ValueError("--max-parallel 必须大于 0")
    if args.discovery_timeout <= 0:
        raise ValueError("--discovery-timeout 必须大于 0")

    global_cwd = _resolve_existing_dir(args.cwd, "--cwd")
    auto_approve = not args.manual_permissions
    if args.all_permissions:
        auto_approve = True

    config = _build_initial_config(args, global_cwd, auto_approve)
    discovered = _discover_all_agents(args, config, global_cwd)
    mode_overrides = _resolve_mode_overrides(args)

    agents_cfg = config["agents"]
    assert isinstance(agents_cfg, dict)

    for agent in AGENTS:
        agent_cfg_any = agents_cfg.get(agent)
        if not isinstance(agent_cfg_any, dict):
            continue
        cli_config_overrides = _parse_key_value_items(
            getattr(args, f"{agent}_config"),
            f"--{agent}-config",
            allow_empty_value=False,
            expand_value=True,
        )
        _apply_agent_defaults(
            agent=agent,
            agent_cfg=agent_cfg_any,
            discovered=discovered.get(agent),
            mode_override=mode_overrides.get(agent),
            cli_config_overrides=cli_config_overrides,
            interactive=args.interactive,
            interactive_all_options=args.interactive_all_options,
            emit_default_mode=args.emit_default_mode,
        )

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"\nsetup 配置已写入: {output_path}")
    print("后续运行可加: --setup <该文件路径>")
    if not args.no_discover and not discovered:
        print("提示: 本次没有成功发现任何 agent 的动态配置项，可后续重试并加 --require-discovery。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
