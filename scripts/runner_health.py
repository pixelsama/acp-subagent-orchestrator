#!/usr/bin/env python3
"""Quick runner health check for ACP subagent orchestration.

Checks:
1) Whether each configured runner can connect via ACP.
2) Whether the configured default model is still available.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from acp_orchestrator import ACPConnection, ACPError, _expand_env, _extract_current_model, _extract_model_choices


def _load_setup(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise ValueError(f"setup file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"setup file is not valid JSON: {path}") from exc

    if not isinstance(data, dict):
        raise ValueError("setup root must be a JSON object")
    return data


def _parse_command(value: Any, field_name: str) -> List[str]:
    if isinstance(value, str):
        command = shlex.split(value)
        if command:
            return command
        raise ValueError(f"{field_name} must not be empty")

    if isinstance(value, list) and all(isinstance(x, str) for x in value) and value:
        return value

    raise ValueError(f"{field_name} must be a string command or an array of strings")


def _parse_env_map(value: Any, field_name: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")

    parsed: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"{field_name} keys/values must be strings")
        parsed[k] = _expand_env(v)
    return parsed


def _select_agents(all_agents: Dict[str, Any], raw_agents: Optional[str]) -> Dict[str, Any]:
    if not raw_agents:
        return all_agents

    wanted = [x.strip() for x in raw_agents.split(",") if x.strip()]
    selected: Dict[str, Any] = {}
    for name in wanted:
        if name not in all_agents:
            raise ValueError(f"agent not found in setup: {name}")
        selected[name] = all_agents[name]
    return selected


def _probe_agent(
    *,
    agent_name: str,
    cfg: Dict[str, Any],
    global_cwd: str,
    timeout_sec: int,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "agent": agent_name,
        "status": "fail",
        "connected": False,
        "default_model": None,
        "current_model": None,
        "available_models": [],
        "message": "",
    }

    default_cfg = cfg.get("default_config_options")
    if isinstance(default_cfg, dict):
        default_model = default_cfg.get("model")
        if isinstance(default_model, str) and default_model.strip():
            result["default_model"] = default_model.strip()

    command = _parse_command(cfg.get("command"), f"agents.{agent_name}.command")
    env = os.environ.copy()
    env.update(_parse_env_map(cfg.get("env"), f"agents.{agent_name}.env"))

    raw_cwd = cfg.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd.strip():
        run_cwd = str(Path(_expand_env(raw_cwd)).expanduser().resolve())
    else:
        run_cwd = global_cwd

    session_id: Optional[str] = None
    conn = ACPConnection(
        command=command,
        env=env,
        cwd=run_cwd,
        auto_approve_permissions=True,
        heartbeat_enabled=False,
        heartbeat_interval_sec=60,
        status_tick_callback=None,
        status_tick_interval_sec=15,
        verbose=False,
    )

    try:
        conn.start()
        conn.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": "acp-runner-health-check",
                    "title": "ACP Runner Health Check",
                    "version": "0.1.0",
                },
            },
            timeout_sec=timeout_sec,
        )
        new_result = conn.request(
            "session/new",
            {"cwd": run_cwd, "mcpServers": []},
            timeout_sec=timeout_sec,
        )

        raw_session_id = new_result.get("sessionId")
        if isinstance(raw_session_id, str) and raw_session_id:
            session_id = raw_session_id
        else:
            raise ACPError("session/new did not return sessionId")

        models = _extract_model_choices(new_result)
        current_model = _extract_current_model(new_result)
        result["connected"] = True
        result["available_models"] = models
        result["current_model"] = current_model

        default_model = result["default_model"]
        if default_model and models and default_model not in models:
            result["status"] = "warn"
            result["message"] = (
                f"default model '{default_model}' is not in the available model list"
            )
        elif default_model and not models:
            result["status"] = "warn"
            result["message"] = (
                "default model is configured but the runner did not expose model choices"
            )
        else:
            result["status"] = "pass"
            result["message"] = "runner connected"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "fail"
        result["message"] = str(exc)
    finally:
        if session_id:
            try:
                conn.request("session/close", {"sessionId": session_id}, timeout_sec=min(5, timeout_sec))
            except Exception:  # noqa: BLE001
                pass
        conn.close()

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Quick health check for configured ACP runners")
    parser.add_argument("--setup", default="setup.json", help="Path to setup JSON file")
    parser.add_argument("--agents", help="Comma-separated subset of agents to check (default: all)")
    parser.add_argument("--timeout-sec", type=int, default=15, help="Timeout per ACP call (seconds)")
    parser.add_argument("--output", help="Write full JSON report to this path")
    args = parser.parse_args()

    if args.timeout_sec <= 0:
        raise SystemExit("Error: --timeout-sec must be > 0")

    setup_path = Path(args.setup).expanduser().resolve()
    setup = _load_setup(setup_path)

    agents_any = setup.get("agents", {})
    if not isinstance(agents_any, dict) or not agents_any:
        raise SystemExit("Error: setup.agents must be a non-empty object")

    selected_agents = _select_agents(agents_any, args.agents)
    global_cwd_raw = setup.get("cwd") or os.getcwd()
    global_cwd = str(Path(str(global_cwd_raw)).expanduser().resolve())

    checks: List[Dict[str, Any]] = []
    for name, cfg_any in selected_agents.items():
        if not isinstance(cfg_any, dict):
            checks.append(
                {
                    "agent": name,
                    "status": "fail",
                    "connected": False,
                    "default_model": None,
                    "current_model": None,
                    "available_models": [],
                    "message": "agent config must be an object",
                }
            )
            continue

        checks.append(
            _probe_agent(
                agent_name=name,
                cfg=cfg_any,
                global_cwd=global_cwd,
                timeout_sec=args.timeout_sec,
            )
        )

    summary = {
        "total": len(checks),
        "pass": sum(1 for c in checks if c["status"] == "pass"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "fail": sum(1 for c in checks if c["status"] == "fail"),
    }
    report = {
        "setup": str(setup_path),
        "summary": summary,
        "checks": checks,
    }

    for check in checks:
        status = str(check["status"]).upper()
        print(f"[{status}] {check['agent']}: {check['message']}")
        if check["default_model"]:
            print(f"  default_model: {check['default_model']}")
        if check["current_model"]:
            print(f"  current_model: {check['current_model']}")
        if check["available_models"]:
            print(f"  available_models: {', '.join(check['available_models'])}")

    print(
        f"\nSummary: total={summary['total']} pass={summary['pass']} "
        f"warn={summary['warn']} fail={summary['fail']}"
    )

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"Report written to: {output_path}")

    if summary["fail"] > 0:
        return 2
    if summary["warn"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
