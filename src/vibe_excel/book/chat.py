"""对话后端：把页面上说的话变成一条会留在会话文件里、并且真的有人回答的消息。

两件事必须一起做，否则用户看到的就是「发出去的话变成了一段新对话」：

- 落盘：消息追加进该项目的会话 JSONL，和 pi 写的是同一种记录格式，
  所以刷新之后它是**原来那段对话的续**，不是新开一段
- 回答：走 OpenAI 兼容的 ``/chat/completions``；没有可用后端时如实报错，
  绝不伪造一条「已记录」的回复

后端由环境变量配置，默认指向本机网关（``VIBE_EXCEL_CHAT_BASE_URL``）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..util import VibeError

DEFAULT_BASE_URL = "http://127.0.0.1:23100/v1"
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_KEY_ENV = "OPENCODE_API_KEY"
DEFAULT_TIMEOUT_MS = 120_000


@dataclass(frozen=True)
class ChatConfig:
    """对话后端的位置与凭据来源。"""

    base_url: str
    model: str
    key_env: str
    timeout_ms: int

    @classmethod
    def from_env(cls) -> ChatConfig:
        def read(name: str, fallback: str) -> str:
            value = os.environ.get(name, "").strip()
            return value or fallback

        raw_timeout = os.environ.get("VIBE_EXCEL_CHAT_TIMEOUT_MS", "").strip()
        try:
            timeout = int(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_MS
        except ValueError as error:
            raise VibeError(f"VIBE_EXCEL_CHAT_TIMEOUT_MS 必须是整数：{raw_timeout}") from error
        return cls(
            base_url=read("VIBE_EXCEL_CHAT_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            model=read("VIBE_EXCEL_CHAT_MODEL", DEFAULT_MODEL),
            key_env=read("VIBE_EXCEL_CHAT_KEY_ENV", DEFAULT_KEY_ENV),
            timeout_ms=timeout,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def is_local(self) -> bool:
        return "127.0.0.1" in self.base_url or "localhost" in self.base_url


def probe(config: ChatConfig) -> tuple[bool, str]:
    """能不能真的问一句；不能就说清楚缺什么。"""
    if not config.is_local() and not os.environ.get(config.key_env, "").strip():
        return (
            False,
            f"没有可用的模型后端：{config.base_url} 需要环境变量 {config.key_env}。"
            f"本机网关（127.0.0.1）不需要 key，起起来就能用",
        )
    return True, ""


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def session_directory(store: Path, session_id: str) -> Path:
    """会话 JSONL 所在目录：与 collect_book 读的是同一个位置。"""
    return store / "sessions" / session_id / "sessions"


def pick_conversation(directory: Path, conversation_id: str | None) -> Path:
    """要续写哪一段对话：给了 id 就用它，否则用最近的一段。"""
    if not directory.is_dir():
        directory.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        (item for item in directory.glob("*.jsonl") if item.is_file()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if conversation_id:
        for item in existing:
            if item.stem == conversation_id:
                return item
        raise VibeError(f"找不到这段对话：{conversation_id}")
    if existing:
        return existing[0]
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"
    return directory / f"{stamp}_chat.jsonl"


def append_message(path: Path, role: str, text: str) -> None:
    """按 pi 的记录格式追加一条消息；一份文件就是一段对话。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "message",
        "timestamp": _timestamp(),
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_messages(path: Path, *, limit: int = 12) -> list[dict[str, str]]:
    """取最近几条对话作为上下文，工具块不进上下文。"""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise VibeError(f"读不了对话文件 {path}：{error}") from error
    messages: list[dict[str, str]] = []
    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            record: object = json.loads(trimmed)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        parts = content if isinstance(content, list) else []
        text_parts = [
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        joined = "\n".join(part for part in text_parts if part).strip()
        if joined:
            messages.append({"role": str(role), "content": joined})
    return messages[-limit:]


def system_prompt(program: str, events: Sequence[dict[str, Any]]) -> str:
    """把页面知道的事实先交代清楚，省得每句话都要人重复。"""
    lines = [
        f"你在 vibe_excel 的构建页面里回答问题，当前项目是「{program}」。",
        "这个项目把 Excel 表物化成表，再经过若干步骤产出结果表；页面能看运行、输入和对话。",
        "回答用中文，直接说结论，不要复述用户的提问，也不要编造没看到的数据。",
    ]
    if events:
        lines.append("该项目最近的输入变更：")
        for item in list(events)[-8:]:
            verb = "移出" if item.get("kind") == "remove" else "撤回" if item.get("kind") == "undo" else str(item.get("kind"))
            lines.append(f"- {item.get('at', '')} {verb} {item.get('file', '')}")
    return "\n".join(lines)


def ask(config: ChatConfig, messages: list[dict[str, str]]) -> str:
    """问一次模型；失败时把能看的原因抛出去，不要静默。"""
    ready, reason = probe(config)
    if not ready:
        raise VibeError(reason)
    body = json.dumps({"model": config.model, "messages": messages, "stream": False}).encode("utf-8")
    request = urllib.request.Request(config.endpoint, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    key = os.environ.get(config.key_env, "").strip()
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    elif config.is_local():
        request.add_header("Authorization", "Bearer local")
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_ms / 1000) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        raise VibeError(f"模型返回 {error.code}：{detail}") from error
    except urllib.error.URLError as error:
        raise VibeError(f"连不上模型后端 {config.base_url}：{error.reason}") from error
    except TimeoutError as error:
        raise VibeError(f"模型超过 {config.timeout_ms} ms 没有回应") from error
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        raise VibeError(f"模型没有返回 choices：{str(payload)[:200]}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise VibeError("模型返回了空内容")
    return text.strip()


def send(
    store: Path,
    session_id: str,
    program: str,
    text: str,
    *,
    conversation_id: str | None = None,
    new: bool = False,
    events: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """落用户消息 → 问模型 → 落回答，返回这段对话的最新信息。

    两边都落盘：刷新之后这段话还在原来那段对话里，不会另起一段。
    """
    clean = text.strip()
    if not clean:
        raise VibeError("说点什么再发")
    directory = session_directory(store, session_id)
    # 只有「新建对话」才另起一段；默认是续写最近那段
    path = pick_conversation(directory, None if new else conversation_id)
    append_message(path, "user", clean)
    history = read_messages(path)
    messages = [{"role": "system", "content": system_prompt(program, events)}, *history]
    try:
        reply = ask(ChatConfig.from_env(), messages)
    except VibeError as error:
        # 问题已经写进文件了：留下痕迹比悄悄丢掉诚实，页面照样显示用户这句话
        return {"ok": False, "error": str(error), "conversationId": path.stem, "reply": ""}
    append_message(path, "assistant", reply)
    return {"ok": True, "conversationId": path.stem, "reply": reply, "error": ""}
