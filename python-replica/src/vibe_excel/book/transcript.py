"""Read pi-compatible JSONL transcripts, including an unfinished final line."""

import json
import logging
import re
from pathlib import Path

from ._values import as_list, as_object, basename
from .types import EpisodeView

_WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "patch", "apply_patch"})
_LOG = logging.getLogger(__name__)


def one_line(text: str, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", text.removeprefix("\ufeff")).strip()
    return f"{cleaned[:limit]}…" if len(cleaned) > limit else cleaned


def _text_parts(content: object) -> str:
    parts: list[str] = []
    for raw in as_list(content):
        part = as_object(raw)
        text = part.get("text")
        if part.get("type") == "text" and isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _diagnostic(message: str, warnings: list[str] | None) -> None:
    if warnings is None:
        _LOG.warning(message)
    else:
        warnings.append(message)


def parse_transcript(
    file: str | Path, text: str, *, warnings: list[str] | None = None
) -> EpisodeView | None:
    """Summarize the first human messages and all assistant tool calls."""
    first: str | None = None
    last: str | None = None
    user: str | None = None
    assistant: str | None = None
    tool_calls = writes = records = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        trimmed = line.strip().removeprefix("\ufeff")
        if not trimmed:
            continue
        try:
            parsed: object = json.loads(trimmed)
        except json.JSONDecodeError as error:
            _diagnostic(f"对话 {file}:{line_number} 跳过不完整或无效 JSON：{error.msg}", warnings)
            continue
        if not isinstance(parsed, dict):
            _diagnostic(f"对话 {file}:{line_number} 记录必须是对象，已跳过", warnings)
            continue
        record = as_object(parsed)
        records += 1
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str):
            first = first if first is not None else timestamp
            last = timestamp
        if record.get("type") != "message":
            continue
        message = as_object(record.get("message"))
        role = message.get("role")
        content = message.get("content")
        if role == "user" and user is None:
            human_text = _text_parts(content)
            if human_text.strip():
                user = human_text
        if role != "assistant":
            continue
        if assistant is None:
            human_text = _text_parts(content)
            if human_text.strip():
                assistant = human_text
        for raw in as_list(content):
            part = as_object(raw)
            if part.get("type") == "toolCall":
                tool_calls += 1
                name = part.get("name")
                if isinstance(name, str) and name.lower() in _WRITE_TOOLS:
                    writes += 1

    if records == 0 or first is None or last is None:
        _diagnostic(f"对话 {file} 没有可展示的带时间戳记录", warnings)
        return None
    name = basename(file)
    return {
        "file": str(file),
        "id": re.sub(r"\.jsonl$", "", name, flags=re.IGNORECASE),
        "start": first,
        "end": last,
        "user": one_line(user or ""),
        "assistant": one_line(assistant or "", 160),
        "toolCalls": tool_calls,
        "writes": writes,
        "records": records,
        "runId": None,
    }


oneLine = one_line
parseTranscript = parse_transcript
