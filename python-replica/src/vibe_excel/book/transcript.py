"""读取 pi 兼容的 JSONL 对话记录，包括未完成的最后一行。

数据来源是会话目录下的 .jsonl 对话文件；本模块只解析，不写回。
损坏或不完整的行会跳过并写入 warnings，保证单行失败不会让整页不可用。
"""

import json
import logging
import re
from pathlib import Path

from ._values import as_list, as_object, basename
from .types import EpisodeView

_WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "patch", "apply_patch"})
_LOG = logging.getLogger(__name__)


def one_line(text: str, limit: int = 220) -> str:
    """把多行文本压成单行，并按字符数截断。

    超出上限时补省略号，避免长消息撑破对话摘要的布局。
    """
    cleaned = re.sub(r"\s+", " ", text.removeprefix("\ufeff")).strip()
    return f"{cleaned[:limit]}…" if len(cleaned) > limit else cleaned


def _text_parts(content: object) -> str:
    """抽取 content 数组里 type=text 的片段并用换行合并。"""
    parts: list[str] = []
    for raw in as_list(content):
        part = as_object(raw)
        text = part.get("text")
        if part.get("type") == "text" and isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _diagnostic(message: str, warnings: list[str] | None) -> None:
    """有诊断列表就追加，否则退到日志，供无 UI 的调用方观察。"""
    if warnings is None:
        _LOG.warning(message)
    else:
        warnings.append(message)


def parse_transcript(
    file: str | Path, text: str, *, warnings: list[str] | None = None
) -> EpisodeView | None:
    """汇总首批人类消息与所有助手工具调用。

    用户名与助手名都只取第一条非空文本；没有任何带时间戳记录时返回 None，
    让调用方跳过该文件而不是生成空对话。
    """
    first: str | None = None
    last: str | None = None
    user: str | None = None
    assistant: str | None = None
    tool_calls = writes = records = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        # 逐行去掉可能残留的 BOM，兼容首行带 BOM 的 JSONL。
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
                # 工具名大小写不敏感地匹配写操作集合，不同 provider 的命名习惯不一致。
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
        # runId 由 collect 在关联作者记录后回填，解析阶段还无从得知。
        "runId": None,
    }


oneLine = one_line
parseTranscript = parse_transcript
