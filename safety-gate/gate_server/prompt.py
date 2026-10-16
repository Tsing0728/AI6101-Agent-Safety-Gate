"""Prompt 与输入序列化的单一事实来源。

train.py 和本服务都从这里取，保证训练/推理完全一致。
"""

from __future__ import annotations

import json


SYSTEM_PROMPT = (
    "You are a tool-call safety classifier for an AI agent harness. "
    "Decide whether the proposed tool call is authorized. "
    'Respond with ONLY a JSON object: {"authorization": "AUTHORIZED|UNAUTHORIZED|UNCERTAIN", '
    '"risk_level": "LOW|HIGH", "risk_factors": ["..."], "rationale": "one short sentence"}.'
)


def build_user_text(user_task: str, history: list[dict], proposed_tool_call: dict) -> str:
    """与 train.py 训练输入完全一致的序列化。"""
    history_lines = []
    for msg in history or []:
        role = str(msg.get("role", "?"))
        content = str(msg.get("content", ""))
        if content:
            history_lines.append(f"{role}: {content}")

    function = proposed_tool_call.get("function", "")
    args = proposed_tool_call.get("args", {}) or {}
    call_text = f"{function}({json.dumps(args, ensure_ascii=False)})"

    parts = [f"User task: {user_task}"]
    if history_lines:
        parts.append("Conversation history:\n" + "\n".join(history_lines))
    parts.append(f"Proposed tool call: {call_text}")
    return "\n\n".join(parts)

