#!/usr/bin/env python3
"""生成 ACP 子代理编排器的 setup 配置。

setup 阶段会先写入 runner 基础配置。默认不做动态发现，保持 runner 默认
mode；仅在显式启用 discover 时，读取 `configOptions / modes` 并支持
setup 阶段的默认会话配置。
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
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNNERS_DIR = str((SKILL_ROOT / ".runtime" / "runners").resolve())
CATEGORY_FILE = SKILL_ROOT / "references" / "agent_catalog.json"
ALL_PERMISSIONS_PRESET = {
    "codex": "full-access",
    "claude": "bypassPermissions",
}


def _load_agent_catalog() -> Dict[str, Dict[str, Any]]:
    if not CATEGORY_FILE.exists():
        raise RuntimeError(f"找不到 agent catalog 文件: {CATEGORY_FILE}")
    try:
        payload = json.loads(CATEGORY_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"读取 agent catalog 失败: {CATEGORY_FILE}") from exc

    order = payload.get("order")
    agents = payload.get("agents")
    if not isinstance(order, list) or not isinstance(agents, dict):
        raise RuntimeError(f"agent catalog 格式错误: {CATEGORY_FILE}")

    catalog: Dict[str, Dict[str, Any]] = {}
    for key in order:
        if not isinstance(key, str):
            continue
        item = agents.get(key)
        if not isinstance(item, dict):
            continue
        default_command = item.get("default_command")
        if not isinstance(default_command, str) or not default_command.strip():
            continue
        copied = dict(item)
        copied["default_command"] = default_command.strip()
        copied.setdefault("label", key)
        copied.setdefault("description", "")
        copied.setdefault("aliases", [])
        catalog[key] = copied

    if not catalog:
        raise RuntimeError(f"agent catalog 为空: {CATEGORY_FILE}")
    return catalog


def _normalize_alias(token: str) -> str:
    normalized = token.strip().lower().replace("_", "-")
    normalized = " ".join(normalized.split())
    return normalized


def _build_alias_map(catalog: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for key, meta in catalog.items():
        candidates: set[str] = {
            key,
            key.replace("_", "-"),
            key.replace("_", " "),
        }
        raw_aliases = meta.get("aliases")
        if isinstance(raw_aliases, list):
            for raw in raw_aliases:
                if isinstance(raw, str) and raw.strip():
                    candidates.add(raw)
        label = meta.get("label")
        if isinstance(label, str) and label.strip():
            candidates.add(label)
        registry_id = meta.get("registry_id")
        if isinstance(registry_id, str) and registry_id.strip():
            candidates.add(registry_id)

        for candidate in candidates:
            norm = _normalize_alias(candidate)
            if norm:
                alias_map[norm] = key
    return alias_map


def _agent_option_name(agent: str) -> str:
    return agent.replace("_", "-")


AGENT_CATALOG: Dict[str, Dict[str, Any]] = _load_agent_catalog()
AGENTS: Tuple[str, ...] = tuple(AGENT_CATALOG.keys())
AGENT_ALIAS_MAP: Dict[str, str] = _build_alias_map(AGENT_CATALOG)


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


def _print_catalog() -> None:
    print("可配置的 subagent 清单（预设）：")
    for agent in AGENTS:
        item = AGENT_CATALOG[agent]
        print(f"- {agent}: {item['label']}")
        print(f"  描述: {item['description']}")
        print(f"  默认命令: {item['default_command']}")
        install_strategy = item.get("isolated_install")
        if isinstance(install_strategy, str) and install_strategy:
            print(f"  隔离安装策略: {install_strategy}")
        pkg = item.get("isolated_npm_package")
        if isinstance(pkg, str) and pkg:
            print(f"  隔离安装包: {pkg}")
        npx_pkg = item.get("npx_package")
        if isinstance(npx_pkg, str) and npx_pkg:
            print(f"  npx 包: {npx_pkg}")


def _parse_selected_agents(raw: str) -> List[str]:
    if not raw.strip():
        raise ValueError("--agents 不能为空，请按用户选择显式传入（例如: --agents claude）")

    selected: List[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        raw_agent = part.strip()
        if not raw_agent:
            continue
        token = _normalize_alias(raw_agent)
        if not token:
            continue
        if token == "all":
            return list(AGENTS)

        agent = AGENT_ALIAS_MAP.get(token)
        if agent is None:
            agent = AGENT_ALIAS_MAP.get(token.replace(" ", "-"))
        if agent is None:
            agent = AGENT_ALIAS_MAP.get(token.replace("-", " "))
        if agent == "all":
            return list(AGENTS)
        if agent is None or agent not in AGENT_CATALOG:
            supported = ", ".join(AGENTS)
            raise ValueError(f"未知 agent: {raw_agent}（支持: {supported}）")
        if agent not in seen:
            selected.append(agent)
            seen.add(agent)

    if not selected:
        raise ValueError("--agents 不能为空")
    return selected


def _interactive_select_agents() -> List[str]:
    if not sys.stdin.isatty():
        raise ValueError(
            "--agents 未指定，且当前非交互环境。请先向用户确认 runner 选择后传入 --agents，"
            "或用 --agents all 显式全量配置。"
        )

    print("\n请选择要配置的 runner（可多选）：")
    for idx, agent in enumerate(AGENTS, start=1):
        item = AGENT_CATALOG[agent]
        print(f"  {idx}. {agent}: {item['label']} - {item['description']}")
    print("  all. 全部配置")

    while True:
        raw = input("请输入编号/名称，多个值用逗号分隔: ").strip().lower()
        if not raw:
            print("输入为空，请至少选择一个 runner。")
            continue

        tokens = [part.strip() for part in raw.split(",") if part.strip()]
        if not tokens:
            print("输入无效，请重试。")
            continue
        if any(token == "all" for token in tokens):
            return list(AGENTS)

        selected: List[str] = []
        seen: set[str] = set()
        invalid: List[str] = []
        for token in tokens:
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(AGENTS):
                    agent = AGENTS[idx - 1]
                else:
                    invalid.append(token)
                    continue
            else:
                norm = _normalize_alias(token)
                agent = AGENT_ALIAS_MAP.get(norm)
                if agent is None:
                    agent = AGENT_ALIAS_MAP.get(norm.replace(" ", "-"))
                if agent is None:
                    agent = AGENT_ALIAS_MAP.get(norm.replace("-", " "))
            if agent is None or agent not in AGENT_CATALOG:
                invalid.append(token)
                continue
            if agent not in seen:
                selected.append(agent)
                seen.add(agent)

        if invalid:
            print(f"存在无效选择: {', '.join(invalid)}，请重试。")
            continue
        if not selected:
            print("请至少选择一个 runner。")
            continue
        return selected


def _npm_bin_name(base: str) -> str:
    if os.name == "nt":
        return f"{base}.cmd"
    return base


def _default_isolated_base_dir(path_like: str) -> Path:
    return Path(_expand_env(path_like)).expanduser().resolve()


def _current_platform_key() -> Optional[str]:
    if sys.platform.startswith("darwin"):
        os_name = "darwin"
    elif sys.platform.startswith("linux"):
        os_name = "linux"
    elif os.name == "nt":
        os_name = "windows"
    else:
        return None

    machine = ""
    if hasattr(os, "uname"):
        machine = os.uname().machine.lower()
    else:
        machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()

    if machine in ("arm64", "aarch64"):
        arch = "aarch64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x86_64"
    else:
        return None

    return f"{os_name}-{arch}"


def _resolve_binary_target(agent: str) -> Optional[Dict[str, Any]]:
    meta = AGENT_CATALOG[agent]
    binary_targets = meta.get("binary_targets")
    if not isinstance(binary_targets, dict):
        return None
    platform_key = _current_platform_key()
    if platform_key is None:
        return None
    target = binary_targets.get(platform_key)
    if not isinstance(target, dict):
        return None
    return target


def _normalize_cmd_path_token(raw_token: str) -> Path:
    token = raw_token.strip().replace("\\", "/")
    if token.startswith("./"):
        token = token[2:]
    pure = PurePosixPath(token)
    return Path(*pure.parts)


def _build_isolated_binary_runner_command(agent: str, runners_dir: Path) -> List[str]:
    target = _resolve_binary_target(agent)
    if target is None:
        raise ValueError(f"[{agent}] 当前平台无可用 binary 发行包，无法构建隔离命令")
    cmd_raw = target.get("cmd")
    if not isinstance(cmd_raw, str) or not cmd_raw.strip():
        raise ValueError(f"[{agent}] binary target 缺少 cmd")

    parts = shlex.split(cmd_raw)
    if not parts:
        raise ValueError(f"[{agent}] binary target cmd 非法: {cmd_raw}")

    cmd_path = (runners_dir / agent / _normalize_cmd_path_token(parts[0])).resolve()
    command: List[str] = [str(cmd_path)]

    target_args = target.get("args")
    if isinstance(target_args, list):
        command.extend(str(x) for x in target_args)
    if len(parts) > 1:
        command.extend(parts[1:])
    return command


def _build_isolated_runner_command(agent: str, runners_dir: Path) -> List[str]:
    meta = AGENT_CATALOG[agent]
    install_strategy = meta.get("isolated_install")

    if install_strategy == "binary":
        return _build_isolated_binary_runner_command(agent, runners_dir)

    if install_strategy == "npm":
        base = runners_dir / agent
        bin_name = _npm_bin_name(str(meta["isolated_bin"]))
        runner_bin = base / "node_modules" / ".bin" / bin_name
        command = [str(runner_bin)]
        command.extend(meta.get("isolated_extra_args", []))
        return command

    raise ValueError(f"[{agent}] 未配置隔离安装策略，无法构建隔离命令")


def _command_uses_default_runner(agent: str, command_raw: str) -> bool:
    default_cmd = AGENT_CATALOG[agent]["default_command"]
    return command_raw.strip() == default_cmd


def _install_missing_runner_isolated_npm(
    agent: str,
    *,
    runners_dir: Path,
    timeout_sec: int,
) -> Tuple[bool, str, List[str]]:
    package_name = AGENT_CATALOG[agent].get("isolated_npm_package")
    if not isinstance(package_name, str) or not package_name:
        return False, "未配置 isolated npm 包", []

    npm_bin = shutil.which("npm")
    if npm_bin is None:
        return False, "npm 不可用，无法进行隔离安装", []

    install_dir = (runners_dir / agent).resolve()
    install_dir.mkdir(parents=True, exist_ok=True)

    command = [
        npm_bin,
        "install",
        "--prefix",
        str(install_dir),
        package_name,
    ]
    print(f"[{agent}] 尝试隔离安装 runner: {' '.join(shlex.quote(x) for x in command)}")
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"执行失败: {exc}", []

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        return False, (stderr or stdout or "npm install 失败"), []

    isolated_command = _build_isolated_runner_command(agent, runners_dir)
    ok, msg = _check_command_available(isolated_command)
    if not ok:
        return False, f"隔离安装完成但 runner 不可执行: {msg}", []

    return True, "隔离安装成功", isolated_command


def _install_missing_runner_isolated_binary(
    agent: str,
    *,
    runners_dir: Path,
    timeout_sec: int,
) -> Tuple[bool, str, List[str]]:
    target = _resolve_binary_target(agent)
    if target is None:
        return False, "当前平台无可用 binary 发行包", []

    archive_url = target.get("archive")
    if not isinstance(archive_url, str) or not archive_url:
        return False, "binary target 缺少 archive URL", []

    install_dir = (runners_dir / agent).resolve()
    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"acp-{agent}-") as td:
        archive_path = Path(td) / "runner-archive"
        curl_bin = shutil.which("curl")
        if curl_bin:
            command = [
                curl_bin,
                "-fL",
                "--max-time",
                str(timeout_sec),
                "-o",
                str(archive_path),
                archive_url,
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec + 5,
                )
            except Exception as exc:  # noqa: BLE001
                return False, f"下载 binary 失败: {exc}", []
            if completed.returncode != 0:
                stderr = completed.stderr.strip()
                stdout = completed.stdout.strip()
                return False, (stderr or stdout or "curl 下载失败"), []
        else:
            try:
                with urllib.request.urlopen(archive_url, timeout=timeout_sec) as response:
                    archive_path.write_bytes(response.read())
            except Exception as exc:  # noqa: BLE001
                return False, f"下载 binary 失败: {exc}", []

        try:
            if zipfile.is_zipfile(archive_path):
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(install_dir)
            else:
                with tarfile.open(archive_path, mode="r:*") as tf:
                    tf.extractall(install_dir)
        except Exception as exc:  # noqa: BLE001
            return False, f"解压 binary 失败: {exc}", []

    try:
        isolated_command = _build_isolated_runner_command(agent, runners_dir)
    except Exception as exc:  # noqa: BLE001
        return False, f"构建隔离命令失败: {exc}", []

    if os.name != "nt":
        try:
            bin_path = Path(isolated_command[0])
            current_mode = bin_path.stat().st_mode
            bin_path.chmod(current_mode | 0o111)
        except Exception:  # noqa: BLE001
            pass

    ok, msg = _check_command_available(isolated_command)
    if not ok:
        return False, f"binary 安装完成但 runner 不可执行: {msg}", []

    return True, "binary 隔离安装成功", isolated_command


def _install_missing_runner_isolated(
    agent: str,
    *,
    runners_dir: Path,
    timeout_sec: int,
) -> Tuple[bool, str, List[str]]:
    install_strategy = AGENT_CATALOG[agent].get("isolated_install")
    if install_strategy == "npm":
        return _install_missing_runner_isolated_npm(
            agent,
            runners_dir=runners_dir,
            timeout_sec=timeout_sec,
        )
    if install_strategy == "binary":
        return _install_missing_runner_isolated_binary(
            agent,
            runners_dir=runners_dir,
            timeout_sec=timeout_sec,
        )
    return False, "未配置隔离安装策略", []


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


def _parse_key_int_items(items: List[str], flag_name: str) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for raw in items:
        if "=" not in raw:
            raise ValueError(f"{flag_name} 必须是 KEY=INDEX 形式: {raw}")
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"{flag_name} 的 KEY 不能为空: {raw}")
        try:
            idx = int(value)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{flag_name} 的 INDEX 必须是整数: {raw}") from exc
        if idx <= 0:
            raise ValueError(f"{flag_name} 的 INDEX 必须大于 0: {raw}")
        result[key] = idx
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
        # Newer ACP runners require mcpServers explicitly.
        new_result = conn.request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            timeout_sec=timeout_sec,
        )

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
            print("    choices:")
            for idx, (value, name) in enumerate(option.choices.items(), start=1):
                print(f"      {idx}. {name} ({value})")


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


def _resolve_index_overrides(
    agent: str,
    index_overrides: Dict[str, int],
    discovered_options: List[DiscoveredOption],
) -> Dict[str, str]:
    if not index_overrides:
        return {}
    by_id = _option_map(discovered_options)
    resolved: Dict[str, str] = {}

    for config_id, idx in index_overrides.items():
        option = by_id.get(config_id)
        if option is None or not option.choices:
            raise ValueError(f"[{agent}] {config_id} 不存在或无可选项，无法按编号选择")
        values = list(option.choices.keys())
        if idx < 1 or idx > len(values):
            raise ValueError(f"[{agent}] {config_id} 的编号越界: {idx}（范围 1-{len(values)}）")
        resolved[config_id] = values[idx - 1]

    return resolved


def _discovery_snapshot(discovered: Dict[str, DiscoveryResult]) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {}
    for agent, result in discovered.items():
        option_items: List[Dict[str, Any]] = []
        for option in result.options:
            choices: List[Dict[str, Any]] = []
            for idx, (value, name) in enumerate(option.choices.items(), start=1):
                choices.append({"index": idx, "value": value, "name": name})
            option_items.append(
                {
                    "id": option.id,
                    "name": option.name,
                    "category": option.category,
                    "current_value": option.current_value,
                    "choices": choices,
                }
            )
        snapshot[agent] = {
            "warning": result.warning,
            "options": option_items,
        }
    return snapshot


def _build_parser() -> argparse.ArgumentParser:
    default_output = Path("~/.acp-subagent-orchestrator/setup.json").expanduser()
    parser = argparse.ArgumentParser(description="生成 ACP 子代理编排器 setup 配置")
    parser.add_argument(
        "--list-catalog",
        action="store_true",
        help="仅打印预设 subagent 清单并退出",
    )
    parser.add_argument(
        "--agents",
        default="",
        help="要配置的 agent，逗号分隔。留空时进入交互选择；可用 all 显式全量配置",
    )
    parser.add_argument("--output", default=str(default_output), help="输出 setup.json 路径")
    parser.add_argument("--cwd", default=".", help="默认工作目录")
    parser.add_argument("--max-parallel", type=int, default=2, help="默认最大并行任务数")
    parser.add_argument(
        "--install-scope",
        choices=("isolated",),
        default="isolated",
        help="runner 安装范围：isolated（仅隔离安装，禁止全局安装）",
    )
    parser.add_argument(
        "--runners-dir",
        default=DEFAULT_RUNNERS_DIR,
        help="隔离 runner 根目录（默认: skill/.runtime/runners）",
    )

    for agent in AGENTS:
        option_name = _agent_option_name(agent)
        label = AGENT_CATALOG[agent].get("label", agent)
        parser.add_argument(
            f"--{option_name}-command",
            dest=f"{agent}_command",
            default=AGENT_CATALOG[agent]["default_command"],
            help=f"{label} runner 命令",
        )
        parser.add_argument(
            f"--{option_name}-mode",
            dest=f"{agent}_mode",
            default="",
            help=f"{label} 默认 mode（可选）",
        )

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

    discover_group = parser.add_mutually_exclusive_group()
    discover_group.add_argument(
        "--discover",
        action="store_true",
        help="启用 ACP 动态发现（读取 runner 返回的 configOptions/modes）",
    )
    discover_group.add_argument(
        "--no-discover",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--require-discovery",
        action="store_true",
        help="要求所有 agent 都必须动态发现成功，否则报错（自动启用 discover）",
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
        help="动态发现后，交互式选择默认会话配置（自动启用 discover）",
    )
    parser.add_argument(
        "--interactive-all-options",
        action="store_true",
        help="交互式模式下同时配置非 mode 配置项（自动启用 discover）",
    )
    parser.add_argument(
        "--strict-command-check",
        action="store_true",
        help="命令不可执行时直接报错（默认仅警告）",
    )
    parser.add_argument(
        "--no-install-missing",
        action="store_true",
        help="runner 缺失时不自动安装（默认自动尝试安装）",
    )
    parser.add_argument(
        "--install-timeout",
        type=int,
        default=300,
        help="每条安装命令超时（秒）",
    )

    for agent in AGENTS:
        option_name = _agent_option_name(agent)
        label = AGENT_CATALOG[agent].get("label", agent)
        parser.add_argument(
            f"--{option_name}-env",
            dest=f"{agent}_env",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help=f"{label} 的环境变量，可重复传入",
        )
        parser.add_argument(
            f"--{option_name}-cwd",
            dest=f"{agent}_cwd",
            default="",
            help=f"{label} 的工作目录（可选）",
        )
        parser.add_argument(
            f"--{option_name}-config",
            dest=f"{agent}_config",
            action="append",
            default=[],
            metavar="KEY=VALUE",
            help=f"{label} 的默认 session config 选项，可重复传入",
        )
        parser.add_argument(
            f"--{option_name}-config-index",
            dest=f"{agent}_config_index",
            action="append",
            default=[],
            metavar="KEY=INDEX",
            help=f"{label} 的默认 session config 选项，可用编号选择（1-based）",
        )

    return parser


def _build_initial_config(
    args: argparse.Namespace,
    global_cwd: str,
    auto_approve: bool,
    selected_agents: List[str],
) -> Dict[str, object]:
    runners_dir = _default_isolated_base_dir(args.runners_dir)
    config: Dict[str, object] = {
        "cwd": global_cwd,
        "max_parallel": args.max_parallel,
        "_meta": {
            "setup": {
                "install_scope": args.install_scope,
                "runners_dir": str(runners_dir),
                "note": "仅 setup 阶段使用；runner 默认安装在 skill/.runtime/runners，且禁止自动全局安装。acp_orchestrator 运行期忽略该字段。",
            }
        },
        "agents": {},
    }

    install_missing = not args.no_install_missing

    for agent in selected_agents:
        command_raw = getattr(args, f"{agent}_command")
        use_isolated_default = _command_uses_default_runner(agent, command_raw)
        install_strategy = AGENT_CATALOG[agent].get("isolated_install")

        if use_isolated_default and install_strategy in ("npm", "binary"):
            command = _build_isolated_runner_command(agent, runners_dir)
        else:
            command = _split_command(command_raw)

        ok, msg = _check_command_available(command)
        if not ok:
            install_failures: List[str] = []
            if install_missing:
                if use_isolated_default and install_strategy in ("npm", "binary"):
                    installed, install_message, installed_command = _install_missing_runner_isolated(
                        agent,
                        runners_dir=runners_dir,
                        timeout_sec=args.install_timeout,
                    )
                    if installed:
                        command = installed_command
                        ok, msg = _check_command_available(command)
                    else:
                        install_failures.append(f"隔离安装失败: {install_message}")

            if install_failures:
                msg = f"{msg}；{'; '.join(install_failures)}"

            if not ok:
                text = f"[{agent}] 命令检查失败: {msg}"
                if args.strict_command_check:
                    raise ValueError(text)
                print(f"警告: {text}", file=sys.stderr)

        env_items = getattr(args, f"{agent}_env")
        option_name = _agent_option_name(agent)
        env = _parse_key_value_items(
            env_items,
            f"--{option_name}-env",
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
            agent_cfg["cwd"] = _resolve_existing_dir(agent_cwd_raw, f"--{option_name}-cwd")

        config["agents"][agent] = agent_cfg

    return config


def _discover_all_agents(
    args: argparse.Namespace,
    config: Dict[str, object],
    global_cwd: str,
    selected_agents: List[str],
    discover_enabled: bool,
) -> Dict[str, DiscoveryResult]:
    if not discover_enabled:
        return {}

    discovered: Dict[str, DiscoveryResult] = {}
    failures: List[str] = []

    agents_cfg = config["agents"]
    assert isinstance(agents_cfg, dict)

    for agent in selected_agents:
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


def _resolve_mode_overrides(
    args: argparse.Namespace,
    selected_agents: List[str],
) -> Dict[str, Optional[str]]:
    mode_overrides: Dict[str, Optional[str]] = {}
    for agent in selected_agents:
        raw = getattr(args, f"{agent}_mode")
        mode_overrides[agent] = raw.strip() or None

    if args.all_permissions:
        for agent, mode in ALL_PERMISSIONS_PRESET.items():
            if agent in selected_agents and mode_overrides.get(agent) is None:
                mode_overrides[agent] = mode

    return mode_overrides


def _apply_agent_defaults(
    *,
    agent: str,
    agent_cfg: Dict[str, Any],
    discovered: Optional[DiscoveryResult],
    mode_override: Optional[str],
    cli_config_overrides: Dict[str, str],
    cli_config_index_overrides: Dict[str, int],
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
    if cli_config_index_overrides:
        if discovered is None:
            raise ValueError(f"[{agent}] 未进行动态发现，无法使用编号选择配置项")
        selected.update(
            _resolve_index_overrides(agent, cli_config_index_overrides, discovered.options)
        )

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

    if args.list_catalog:
        _print_catalog()
        return 0

    if args.max_parallel <= 0:
        raise ValueError("--max-parallel 必须大于 0")
    if args.discovery_timeout <= 0:
        raise ValueError("--discovery-timeout 必须大于 0")
    if args.install_timeout <= 0:
        raise ValueError("--install-timeout 必须大于 0")

    if args.agents.strip():
        selected_agents = _parse_selected_agents(args.agents)
    else:
        selected_agents = _interactive_select_agents()

    global_cwd = _resolve_existing_dir(args.cwd, "--cwd")
    auto_approve = not args.manual_permissions
    if args.all_permissions:
        auto_approve = True

    has_config_index_overrides = False
    for agent in selected_agents:
        if getattr(args, f"{agent}_config_index"):
            has_config_index_overrides = True
            break

    needs_discovery = bool(
        args.require_discovery
        or args.interactive
        or args.interactive_all_options
        or has_config_index_overrides
    )
    if args.no_discover and needs_discovery:
        raise ValueError("当前参数需要动态发现，不能同时关闭 discover")
    discover_enabled = bool(args.discover or needs_discovery)

    config = _build_initial_config(args, global_cwd, auto_approve, selected_agents)
    discovered = _discover_all_agents(
        args,
        config,
        global_cwd,
        selected_agents,
        discover_enabled=discover_enabled,
    )
    if discovered:
        config["discovery"] = _discovery_snapshot(discovered)
    mode_overrides = _resolve_mode_overrides(args, selected_agents)

    agents_cfg = config["agents"]
    assert isinstance(agents_cfg, dict)

    for agent in selected_agents:
        agent_cfg_any = agents_cfg.get(agent)
        if not isinstance(agent_cfg_any, dict):
            continue
        option_name = _agent_option_name(agent)
        cli_config_overrides = _parse_key_value_items(
            getattr(args, f"{agent}_config"),
            f"--{option_name}-config",
            allow_empty_value=False,
            expand_value=True,
        )
        cli_config_index_overrides = _parse_key_int_items(
            getattr(args, f"{agent}_config_index"),
            f"--{option_name}-config-index",
        )
        _apply_agent_defaults(
            agent=agent,
            agent_cfg=agent_cfg_any,
            discovered=discovered.get(agent),
            mode_override=mode_overrides.get(agent),
            cli_config_overrides=cli_config_overrides,
            cli_config_index_overrides=cli_config_index_overrides,
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
    if discover_enabled and not discovered:
        print("提示: 本次没有成功发现任何 agent 的动态配置项，可后续重试并加 --require-discovery。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
