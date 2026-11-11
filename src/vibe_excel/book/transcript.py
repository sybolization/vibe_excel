"""读取 pi 兼容的 JSONL 对话记录，包括未完成的最后一行。

数据来源是会话目录下的 .jsonl 对话文件；本模块只解析，不写回。
损坏或不完整的行会跳过并写入 warnings，保证单行失败不会让整页不可用。
"""

import json
import logging
import re
from pathlib import Path

from ._values import as_list, as_object, basename
from .types import ChatBlock, ConversationView, EpisodeView

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




_CLIP_TEXT = 4000
_CLIP_ASSISTANT = 6000
_CLIP_RESULT = 1400
_CLIP_BRIEF = 160


def clip(text: str, limit: int) -> str:
    """正文截断：超长时补一句说明，不静默丢内容。"""
    return f"{text[:limit]}\n\n…（已截断）" if len(text) > limit else text


def conversation_title(text: str) -> str:
    """标题取首条用户消息的第一行，去掉 Markdown 的 #。"""
    first = text.removeprefix("\ufeff").split("\n", 1)[0]
    cleaned = re.sub(r"^#+\s*", "", first).strip()
    return "未命名对话" if not cleaned else cleaned[:40]


def _brief_of(args: object) -> str:
    """工具参数里挑一个最能说明它在干什么的字段。"""
    record = as_object(args)
    for key in ("command", "path", "pattern", "query"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return clip(value, _CLIP_BRIEF)
    return ""


def parse_conversation(
    file: str | Path, text: str, *, warnings: list[str] | None = None
) -> ConversationView | None:
    """把一段 JSONL 展开成逐条消息与工具执行。

    和 parse_transcript 读同一份文件，但那份只留片段摘要，这份留逐条消息。
    工具结果按 toolCallId 回填到对应工具块上；没有结果的按成功算。
    """
    blocks: list[ChatBlock] = []
    pending: dict[str, ChatBlock] = {}
    first: str | None = None
    last: str | None = None
    title = ""

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
            continue
        record = as_object(parsed)
        stamp = record.get("timestamp")
        if isinstance(stamp, str):
            first = first if first is not None else stamp
            last = stamp
        if record.get("type") != "message":
            continue
        message = as_object(record.get("message"))
        role = message.get("role")
        content = message.get("content")
        at = stamp if isinstance(stamp, str) else ""

        if role == "note":
            # 系统事件的记录（比如「用户按了停止」）：不当成人说的话
            raw_note = _text_parts(content)
            try:
                payload = json.loads(raw_note) if raw_note.strip() else {}
            except json.JSONDecodeError:
                payload = {}
            kind = str((payload or {}).get("kind") or "")
            label = "你按了停止" if kind == "interrupted" else raw_note.strip()
            if label:
                blocks.append(
                    {
                        "kind": "event", "at": at, "text": clip(label, _CLIP_TEXT),
                        "name": "", "brief": "", "ok": True, "ms": None, "result": "",
                    }
                )
            continue

        if role == "user":
            joined = _text_parts(content).removeprefix("\ufeff")
            if not joined.strip():
                continue
            if not title:
                title = conversation_title(joined)
            blocks.append(
                {
                    "kind": "user", "at": at, "text": clip(joined, _CLIP_TEXT),
                    "name": "", "brief": "", "ok": True, "ms": None, "result": "",
                }
            )
            continue

        if role == "assistant":
            joined = _text_parts(content)
            if joined.strip():
                blocks.append(
                    {
                        "kind": "assistant", "at": at, "text": clip(joined, _CLIP_ASSISTANT),
                        "name": "", "brief": "", "ok": True, "ms": None, "result": "",
                    }
                )
            for raw in as_list(content):
                part = as_object(raw)
                # 思考单独成块：页面默认折叠，点开才看
                if part.get("type") == "thinking":
                    thought = part.get("thinking")
                    if isinstance(thought, str) and thought.strip():
                        blocks.append(
                            {
                                "kind": "thinking", "at": at, "text": clip(thought, _CLIP_ASSISTANT),
                                "name": "", "brief": "", "ok": True, "ms": None, "result": "",
                            }
                        )
                    continue
                if part.get("type") != "toolCall":
                    continue
                block: ChatBlock = {
                    "kind": "tool", "at": at, "text": "",
                    "name": str(part.get("name") or "?"),
                    "brief": _brief_of(part.get("arguments")),
                    "ok": True, "ms": None, "result": "",
                }
                blocks.append(block)
                call_id = part.get("id")
                if isinstance(call_id, str):
                    pending[call_id] = block
            continue

        if role == "toolResult":
            call_id = message.get("toolCallId")
            # 用独立名字：上面那个 block 已经定型成 ChatBlock，这里可能是 None。
            target = pending.get(call_id) if isinstance(call_id, str) else None
            if target is None:
                continue
            target["ok"] = message.get("isError") is not True
            duration = message.get("durationMs")
            target["ms"] = duration if isinstance(duration, (int, float)) else None
            result = _text_parts(content)
            if result:
                target["result"] = clip(result, _CLIP_RESULT)
            tool_name = message.get("toolName")
            if isinstance(tool_name, str) and tool_name:
                target["name"] = tool_name

    if not blocks:
        _diagnostic(f"对话 {file} 没有可展示的消息", warnings)
        return None
    name = basename(file)
    return {
        "id": re.sub(r"\.jsonl$", "", name, flags=re.IGNORECASE),
        "title": title or "未命名对话",
        "start": first or "",
        "end": last or "",
        "blocks": blocks,
    }


