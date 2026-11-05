from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from vibe_excel.util import VibeError, require_list, require_object, require_text

from ._json import (
    Number,
    as_number,
    as_record,
    json_number,
    json_string,
    loads_json,
    optional_number,
    read_utf8,
    require_integer,
    require_number,
    text_list,
    trim_text,
    utf16_key,
)

PREVIEW_CHARS = 220
_WHITESPACE = re.compile(
    r"[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)


@dataclass(slots=True)
class StepSummary:
    seq: int
    tool: str
    ok: bool
    args_sha256: str
    args_preview: str
    output_preview: str
    files: list[str] = field(default_factory=list)
    exit_code: Number | None = None
    duration_ms: Number | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "seq": self.seq,
            "tool": self.tool,
            "ok": self.ok,
            "argsSha256": self.args_sha256,
            "argsPreview": self.args_preview,
            "files": list(self.files),
        }
        if self.exit_code is not None:
            result["exitCode"] = self.exit_code
        if self.duration_ms is not None:
            result["durationMs"] = self.duration_ms
        result["outputPreview"] = self.output_preview
        return result

    @classmethod
    def from_dict(cls, value: object, where: str = "step") -> StepSummary:
        record = require_object(value, where)
        ok = record.get("ok")
        if not isinstance(ok, bool):
            raise VibeError(f"{where}.ok 必须是布尔值")
        return cls(
            seq=require_integer(record.get("seq"), f"{where}.seq"),
            tool=require_text(record.get("tool"), f"{where}.tool", allow_empty=True),
            ok=ok,
            args_sha256=require_text(
                record.get("argsSha256"), f"{where}.argsSha256", allow_empty=True
            ),
            args_preview=require_text(
                record.get("argsPreview"), f"{where}.argsPreview", allow_empty=True
            ),
            output_preview=require_text(
                record.get("outputPreview"), f"{where}.outputPreview", allow_empty=True
            ),
            files=text_list(record.get("files"), f"{where}.files"),
            exit_code=optional_number(record, "exitCode", where),
            duration_ms=optional_number(record, "durationMs", where),
        )


@dataclass(slots=True)
class UsageTotals:
    input: Number = 0
    output: Number = 0
    cache_read: Number = 0
    cache_write: Number = 0
    reasoning: Number = 0
    total_tokens: Number = 0
    cost_usd: Number = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheWrite": self.cache_write,
            "reasoning": self.reasoning,
            "totalTokens": self.total_tokens,
            "costUsd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, value: object, where: str = "usage") -> UsageTotals:
        record = require_object(value, where)
        return cls(
            input=require_number(record.get("input"), f"{where}.input"),
            output=require_number(record.get("output"), f"{where}.output"),
            cache_read=require_number(record.get("cacheRead"), f"{where}.cacheRead"),
            cache_write=require_number(record.get("cacheWrite"), f"{where}.cacheWrite"),
            reasoning=require_number(record.get("reasoning"), f"{where}.reasoning"),
            total_tokens=require_number(record.get("totalTokens"), f"{where}.totalTokens"),
            cost_usd=require_number(record.get("costUsd"), f"{where}.costUsd"),
        )


@dataclass(slots=True)
class MessageCounts:
    assistant: int = 0
    user: int = 0
    tool_results: int = 0

    def to_dict(self) -> dict[str, object]:
        return {"assistant": self.assistant, "user": self.user, "toolResults": self.tool_results}

    @classmethod
    def from_dict(cls, value: object, where: str = "messages") -> MessageCounts:
        record = require_object(value, where)
        return cls(
            assistant=require_integer(record.get("assistant"), f"{where}.assistant"),
            user=require_integer(record.get("user"), f"{where}.user"),
            tool_results=require_integer(record.get("toolResults"), f"{where}.toolResults"),
        )


@dataclass(slots=True)
class ExtractedRun:
    steps: list[StepSummary] = field(default_factory=list)
    turns: int = 0
    messages: MessageCounts = field(default_factory=MessageCounts)
    usage: UsageTotals = field(default_factory=UsageTotals)
    tool_names: list[str] = field(default_factory=list)
    bad_lines: int = 0
    event_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "steps": [step.to_dict() for step in self.steps],
            "turns": self.turns,
            "messages": self.messages.to_dict(),
            "usage": self.usage.to_dict(),
            "toolNames": list(self.tool_names),
            "badLines": self.bad_lines,
            "eventCount": self.event_count,
        }


def stable_stringify(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json_string(value)
    if isinstance(value, (int, float)):
        return json_number(value)
    if isinstance(value, list):
        return "[" + ",".join(stable_stringify(item) for item in require_list(value, "JSON")) + "]"
    if isinstance(value, dict):
        record = require_object(value, "JSON")
        return (
            "{"
            + ",".join(
                f"{json_string(key)}:{stable_stringify(record[key])}"
                for key in sorted(record, key=utf16_key)
            )
            + "}"
        )
    raise VibeError(f"无法将 {type(value).__name__} 稳定序列化为 JSON")


def sha256_text(text: str) -> str:
    # Node hashes UTF-8 strings, replacing any unpaired UTF-16 surrogates.
    normalized = text.encode("utf-16-le", errors="surrogatepass").decode(
        "utf-16-le", errors="replace"
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _one_line(text: str, limit: int = PREVIEW_CHARS) -> str:
    collapsed = _WHITESPACE.sub(" ", text).strip(" ")
    encoded = collapsed.encode("utf-16-le", errors="surrogatepass")
    return (
        collapsed
        if len(encoded) <= limit * 2
        else encoded[: limit * 2].decode("utf-16-le", errors="surrogatepass") + "…"
    )


def _text_of(result: object) -> str:
    record = as_record(result)
    content = record.get("content") if record is not None else None
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in require_list(content, "result.content"):
        piece = as_record(item)
        text = piece.get("text") if piece is not None else None
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _exit_code_of(result: object) -> Number | None:
    record = as_record(result)
    structured = as_record(record.get("structuredContent")) if record is not None else None
    return as_number(structured.get("exit_code")) if structured is not None else None


def _args_preview_of(tool: str, args: object, args_sha256: str) -> str:
    record = as_record(args)
    if record is not None:
        command, path, pattern = record.get("command"), record.get("path"), record.get("pattern")
        if tool == "bash" and isinstance(command, str):
            return _one_line(command)
        if isinstance(path, str):
            return _one_line(path)
        if isinstance(pattern, str):
            return _one_line(f"pattern={pattern}")
    return f"sha256:{args_sha256[:12]}"


@dataclass(slots=True)
class _PendingStart:
    tool: str
    args_sha256: str
    args_preview: str
    files: list[str]


def extract_run(events_path: str | Path) -> ExtractedRun:
    run = ExtractedRun()
    pending: dict[str, _PendingStart] = {}
    tool_names: set[str] = set()
    text = read_utf8(events_path)
    for raw_line in text.split("\n"):
        line = trim_text(raw_line)
        if not line:
            continue
        try:
            event = as_record(loads_json(line))
        except json.JSONDecodeError:
            run.bad_lines += 1
            continue
        if event is None:
            run.bad_lines += 1
            continue
        run.event_count += 1
        event_type = event.get("type")
        if event_type == "turn_start":
            run.turns += 1
        elif event_type == "tool_execution_start":
            raw_id, raw_tool = event.get("toolCallId"), event.get("toolName")
            call_id = raw_id if isinstance(raw_id, str) else ""
            tool = raw_tool if isinstance(raw_tool, str) else "unknown"
            args = event.get("args")
            args_sha256 = sha256_text(stable_stringify(args))
            args_record = as_record(args)
            path = args_record.get("path") if args_record is not None else None
            pending_start = _PendingStart(
                tool=tool,
                args_sha256=args_sha256,
                args_preview=_args_preview_of(tool, args, args_sha256),
                files=[path] if isinstance(path, str) else [],
            )
            if call_id:
                pending[call_id] = pending_start
            tool_names.add(tool)
        elif event_type == "tool_execution_end":
            raw_id, raw_tool = event.get("toolCallId"), event.get("toolName")
            call_id = raw_id if isinstance(raw_id, str) else ""
            start = pending.pop(call_id, None) if call_id else None
            tool = (
                start.tool
                if start is not None
                else raw_tool
                if isinstance(raw_tool, str)
                else "unknown"
            )
            tool_names.add(tool)
            run.steps.append(
                StepSummary(
                    seq=len(run.steps),
                    tool=tool,
                    ok=event.get("isError") is not True,
                    args_sha256=start.args_sha256 if start is not None else "",
                    args_preview=start.args_preview
                    if start is not None
                    else "(配对失败：没有对应的 start 事件)",
                    files=start.files if start is not None else [],
                    exit_code=_exit_code_of(event.get("result")),
                    duration_ms=as_number(event.get("durationMs")),
                    output_preview=_one_line(_text_of(event.get("result"))),
                )
            )
        elif event_type == "message_end":
            message = as_record(event.get("message"))
            role = message.get("role") if message is not None else None
            if role == "assistant":
                run.messages.assistant += 1
                usage = as_record(message.get("usage")) if message is not None else None
                if usage is not None:
                    run.usage.input += as_number(usage.get("input")) or 0
                    run.usage.output += as_number(usage.get("output")) or 0
                    run.usage.cache_read += as_number(usage.get("cacheRead")) or 0
                    run.usage.cache_write += as_number(usage.get("cacheWrite")) or 0
                    run.usage.reasoning += as_number(usage.get("reasoning")) or 0
                    run.usage.total_tokens += as_number(usage.get("totalTokens")) or 0
                    cost = as_record(usage.get("cost"))
                    run.usage.cost_usd += (
                        (as_number(cost.get("total")) or 0) if cost is not None else 0
                    )
            elif role == "user":
                run.messages.user += 1
            elif role == "toolResult":
                run.messages.tool_results += 1
    for call_id, start in pending.items():
        run.steps.append(
            StepSummary(
                seq=len(run.steps),
                tool=start.tool,
                ok=False,
                args_sha256=start.args_sha256,
                args_preview=start.args_preview,
                output_preview=f"(未完成：toolCallId={call_id} 没有 end 事件，可能是取消或崩溃)",
                files=start.files,
            )
        )
    scaled_cost = run.usage.cost_usd * 1e6
    if math.isfinite(scaled_cost):
        run.usage.cost_usd = math.floor(scaled_cost + 0.5) / 1e6
    run.tool_names = sorted(tool_names, key=utf16_key)
    return run
