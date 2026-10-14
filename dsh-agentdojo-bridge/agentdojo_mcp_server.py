#!/usr/bin/env python3
"""Minimal stdio MCP server exposing one AgentDojo suite's tools to dsh.

dsh's ``@deepseek-ai/dsh-mcp-client`` speaks Model Context Protocol over stdio
(newline-delimited JSON-RPC 2.0). This server:

  * loads one suite and one initial (optionally injected) environment,
  * advertises the suite's tools via ``tools/list``,
  * executes ``tools/call`` against a live ``FunctionsRuntime`` + ``Env``,
  * checkpoints the mutated environment and the tool-call trace to disk after
    every call, so the Python driver can score utility/security afterwards.

Only the MCP ``tools`` capability is implemented (resources/prompts are not
needed by dsh's MCP client).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentdojo.agent_pipeline.tool_execution import tool_result_to_str
from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "agentdojo-mcp"
SERVER_VERSION = "0.1.0"


def log(message: str) -> None:
    """Debug output on stderr; stdout is reserved for the JSON-RPC protocol."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def reply(message_id: object, result: object = None, error: object = None) -> None:
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def checkpoint(checkpoint_dir: Path, env, call: dict) -> None:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    (checkpoint_dir / "env.json").write_text(env.model_dump_json(), encoding="utf-8")
    with (checkpoint_dir / "trace.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(call, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True, help="workspace|travel|banking|slack")
    parser.add_argument("--benchmark-version", default="v1.2.2")
    parser.add_argument("--env-file", required=True, help="JSON file with the initial environment")
    parser.add_argument("--checkpoint-dir", required=True, help="Directory for env.json + trace.jsonl")
    args = parser.parse_args()

    suite = get_suite(args.benchmark_version, args.suite)
    initial_env_json = Path(args.env_file).read_text(encoding="utf-8")
    env = suite.environment_type.model_validate(json.loads(initial_env_json))
    runtime = FunctionsRuntime(suite.tools)
    checkpoint_dir = Path(args.checkpoint_dir)

    log(f"loaded suite={suite.name} tools={len(suite.tools)}")

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        if not isinstance(method, str):
            continue
        message_id = message.get("id")

        if method == "initialize":
            reply(message_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method == "ping":
            reply(message_id, {})
        elif method == "tools/list":
            tools = [
                {
                    "name": fn.name,
                    "description": fn.description,
                    "inputSchema": fn.parameters.model_json_schema(),
                }
                for fn in suite.tools
            ]
            reply(message_id, {"tools": tools})
        elif method == "tools/call":
            params = message.get("params") or {}
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                result, error = runtime.run_function(env, tool_name, arguments)
            except Exception as exc:  # pragma: no cover - defensive boundary
                result, error = "", f"{type(exc).__name__}: {exc}"
            checkpoint(checkpoint_dir, env, {"function": tool_name, "args": arguments})
            if error:
                reply(message_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
            else:
                text = tool_result_to_str(result)
                reply(message_id, {"content": [{"type": "text", "text": text}], "isError": False})
        else:
            # Notifications (e.g. ``notifications/initialized``) carry no id and
            # get no response; unknown requests get a JSON-RPC method-not-found.
            if message_id is not None:
                reply(message_id, error={"code": -32601, "message": f"Method not found: {method}"})


if __name__ == "__main__":
    main()

