"""Parsers for Claude Code and Codex jsonl transcripts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SKIP_CONTENT_TYPES = {
    "function_call",
    "function_call_output",
    "image",
    "input_image",
    "output_image",
    "reasoning",
    "server_tool_use",
    "thinking",
    "tool_use",
}


def ts_to_epoch(value: str | None) -> int:
    if not value:
        return 0
    value = value.strip()
    if not value:
        return 0
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _join(parts: list[str]) -> str:
    cleaned = [part.strip() for part in parts if part and part.strip()]
    return "\n\n".join(cleaned).strip()


def _extract_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        return _join([_extract_text(item) for item in node])
    if not isinstance(node, dict):
        return ""

    item_type = node.get("type")
    if item_type in _SKIP_CONTENT_TYPES:
        return ""
    if item_type in {"text", "input_text", "output_text"}:
        return str(node.get("text", "")).strip()
    if item_type == "tool_result":
        return _extract_text(node.get("content"))

    if "text" in node and isinstance(node["text"], str):
        return node["text"].strip()
    if "content" in node:
        return _extract_text(node["content"])
    if "toolUseResult" in node:
        return _extract_text(node["toolUseResult"])

    streams = []
    for key in ("stdout", "stderr"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            streams.append(value.strip())
    return _join(streams)


def _default_title(cwd: str | None, turns: list[tuple[str, str, str | None]], fallback: str) -> str:
    if cwd:
        name = Path(cwd).name.strip()
        if name:
            return name
    for role, text, _ts in turns:
        if role == "user" and text.strip():
            return text.strip().splitlines()[0][:120]
    return fallback


def parse_claude_jsonl(path: str | Path, is_subagent: bool = False) -> dict[str, Any] | None:
    path = Path(path)
    session_id = path.stem
    subagent_id = ""
    cwd = ""
    turns: list[tuple[str, str, str | None]] = []
    first_ts = None
    last_ts = None
    slug = ""

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = obj.get("timestamp")
                if timestamp and first_ts is None:
                    first_ts = timestamp
                if timestamp:
                    last_ts = timestamp

                cwd = cwd or obj.get("cwd", "")
                slug = slug or obj.get("slug", "")
                session_id = obj.get("sessionId") or session_id
                subagent_id = subagent_id or obj.get("agentId", "")

                role = obj.get("type")
                if role not in {"user", "assistant"}:
                    continue

                text = _extract_text(obj.get("message", {}).get("content"))
                if not text:
                    continue
                turns.append((role, text, timestamp))
    except OSError:
        return None

    if not turns:
        return None

    if is_subagent:
        unique_suffix = subagent_id or path.stem
        session_id = f"{session_id}:{unique_suffix}"
        fallback_title = unique_suffix
        platform = "claude-subagent"
    else:
        fallback_title = slug or path.stem
        platform = "claude"

    return {
        "csid": session_id,
        "cwd": cwd,
        "title": _default_title(cwd, turns, fallback_title),
        "platform": platform,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "turns": turns,
    }


def _is_noise_turn(role: str, text: str) -> bool:
    text = text.strip()
    if not text:
        return True
    if role == "user" and text.startswith("<environment_context>"):
        return True
    return False


def parse_codex_jsonl(path: str | Path) -> dict[str, Any] | None:
    path = Path(path)
    session_id = path.stem
    cwd = ""
    first_ts = None
    last_ts = None
    turns: list[tuple[str, str, str | None]] = []

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = obj.get("timestamp")
                if timestamp and first_ts is None:
                    first_ts = timestamp
                if timestamp:
                    last_ts = timestamp

                if obj.get("type") == "session_meta":
                    payload = obj.get("payload", {})
                    session_id = payload.get("id") or session_id
                    cwd = cwd or payload.get("cwd", "")
                    first_ts = payload.get("timestamp") or first_ts
                    continue

                if obj.get("type") != "response_item":
                    continue

                payload = obj.get("payload", {})
                if payload.get("type") != "message":
                    continue

                role = payload.get("role")
                if role not in {"user", "assistant"}:
                    continue
                text = _extract_text(payload.get("content"))
                if _is_noise_turn(role, text):
                    continue
                turns.append((role, text, timestamp))
    except OSError:
        return None

    if not turns:
        return None

    return {
        "csid": session_id,
        "cwd": cwd,
        "title": _default_title(cwd, turns, path.stem),
        "platform": "codex",
        "first_ts": first_ts,
        "last_ts": last_ts,
        "turns": turns,
    }
