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
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..util import VibeError

DEFAULT_BASE_URL = "http://127.0.0.1:23100/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_PROVIDER = "opencode-go"
DEFAULT_KEY_ENV = "OPENCODE_API_KEY"
DEFAULT_TIMEOUT_MS = 120_000


def find_pi_cli() -> Path | None:
    """找 pi 的 CLI 入口：显式配置优先，其次 npm 全局装法。"""
    configured = os.environ.get("VIBE_EXCEL_PI", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() else None
    found = shutil.which("pi")
    if found:
        path = Path(found)
        # npm 的 shim 是 .ps1/.cmd，真正能 exec 的是同目录 node_modules 里的 cli.js
        for name in ("node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",):
            sibling = path.parent / name
            if sibling.is_file():
                return sibling
        if path.suffix.lower() in (".js", ".mjs", ".exe"):
            return path
    return None


def _pi_command(cli: Path) -> list[str]:
    suffix = cli.suffix.lower()
    if suffix in (".js", ".mjs"):
        return ["node", str(cli)]
    if suffix == ".py":
        return [sys.executable, str(cli)]
    if suffix == ".ps1":
        return ["pwsh", "-NoProfile", "-File", str(cli)]
    if suffix in (".cmd", ".bat"):
        return ["cmd", "/c", str(cli)]
    return [str(cli)]


@dataclass(frozen=True)
class ChatConfig:
    """对话后端的位置、模型与凭据来源。"""

    backend: str  # pi | http
    provider: str
    model: str
    base_url: str
    key_env: str
    timeout_ms: int
    cli: str

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
        found = find_pi_cli()
        backend = read("VIBE_EXCEL_CHAT_BACKEND", "pi" if found is not None else "http")
        if backend not in ("pi", "http"):
            raise VibeError(f"VIBE_EXCEL_CHAT_BACKEND 只能是 pi 或 http：{backend}")
        return cls(
            backend=backend,
            provider=read("VIBE_EXCEL_CHAT_PROVIDER", read("VIBE_EXCEL_PROVIDER", DEFAULT_PROVIDER)),
            model=read("VIBE_EXCEL_CHAT_MODEL", read("VIBE_EXCEL_MODEL", DEFAULT_MODEL)),
            base_url=read("VIBE_EXCEL_CHAT_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            key_env=read("VIBE_EXCEL_CHAT_KEY_ENV", DEFAULT_KEY_ENV),
            timeout_ms=timeout,
            cli="" if found is None else str(found),
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def is_local(self) -> bool:
        return "127.0.0.1" in self.base_url or "localhost" in self.base_url


def probe(config: ChatConfig) -> tuple[bool, str]:
    """能不能真的问一句；不能就说清楚缺什么。"""
    if config.backend == "pi":
        if not config.cli:
            return False, "找不到 pi CLI：设置 VIBE_EXCEL_PI 指向 pi 的 cli.js"
        if not os.environ.get(config.key_env, "").strip():
            return False, f"pi 需要环境变量 {config.key_env}（模型网关的 API key）"
        return True, ""
    if not config.is_local() and not os.environ.get(config.key_env, "").strip():
        return (
            False,
            f"没有可用的模型后端：{config.base_url} 需要环境变量 {config.key_env}。"
            f"本机网关（127.0.0.1）不需要 key，起起来就能用",
        )
    return True, ""


# 追加是「读-改-写」之外的最小动作：加锁只为让两句话不会互相插进对方中间
_WRITE_LOCK = threading.Lock()

# 上下文给太多，模型又慢又贵，而且超长历史对「接着聊」没有帮助
_HISTORY_MESSAGES = 12
_HISTORY_CHARS = 6000
_MESSAGE_CHARS = 2000


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


def append_message(path: Path, role: str, text: str, *, thinking: str = "") -> None:
    """按 pi 的记录格式追加一条消息；一份文件就是一段对话。

    回答先写 thinking 再写 text，和 pi 自己写的一致；页面据此分块，刷新后仍然分块。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content: list[dict[str, str]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    content.append({"type": "text", "text": text})
    record = {
        "type": "message",
        "timestamp": _timestamp(),
        "message": {"role": role, "content": content},
    }
    with _WRITE_LOCK, path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_messages(path: Path, *, limit: int = _HISTORY_MESSAGES) -> list[dict[str, str]]:
    """取最近几条对话作为上下文，工具块不进上下文。

    有字数预算：一次贴进去三十万字，模型会慢到没法用，而且钱是用户出的。
    从最近的往回取，超预算就停；单条太长先截断，至少让最新的那句完整在场。
    """
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
    picked: list[dict[str, str]] = []
    budget = _HISTORY_CHARS
    for message in reversed(messages):
        content = message["content"]
        if len(content) > _MESSAGE_CHARS:
            content = content[:_MESSAGE_CHARS] + "…"
        if picked and len(content) > budget:
            break
        budget -= len(content)
        picked.append({"role": message["role"], "content": content})
        if len(picked) >= limit:
            break
    picked.reverse()
    return picked


def system_prompt(
    program: str,
    events: Sequence[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> str:
    """把页面知道的事实先交代清楚，省得每句话都要人重复。"""
    lines = [
        f"你在 vibe_excel 的构建页面里回答问题，当前项目是「{program}」。",
        "这个项目把 Excel 表物化成表，再经过若干步骤产出结果表；页面能看运行、输入和对话。",
        "回答用中文，直接说结论，不要复述用户的提问，也不要编造没看到的数据。",
    ]
    facts = context or {}
    files = [str(item) for item in facts.get("files") or []]
    steps = [item for item in facts.get("steps") or [] if isinstance(item, dict)]
    outputs = [str(item) for item in facts.get("outputs") or []]
    if files:
        lines.append("输入文件：" + "、".join(files))
    if steps:
        lines.append("处理步骤（函数：读 → 写）：")
        for step in steps[:12]:
            reads = "、".join(str(x) for x in step.get("reads") or []) or "—"
            writes = "、".join(str(x) for x in step.get("writes") or []) or "—"
            lines.append(f"- {step.get('fn')}：{reads} → {writes}")
    if outputs:
        lines.append("结果表：" + "、".join(outputs))
    if events:
        lines.append("该项目最近的输入变更：")
        for item in list(events)[-8:]:
            verb = "移出" if item.get("kind") == "remove" else "撤回" if item.get("kind") == "undo" else str(item.get("kind"))
            lines.append(f"- {item.get('at', '')} {verb} {item.get('file', '')}")
    return "\n".join(lines)


def _flatten(messages: list[dict[str, str]]) -> tuple[str, str]:
    """把对话摊成 (system, prompt)：pi 走命令行，只能给一段文本。"""
    system = ""
    turns: list[str] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            system = content
            continue
        turns.append(f"{'用户' if role == 'user' else '助手'}：{content}")
    return system, "\n\n".join(turns)


def ask_pi(config: ChatConfig, messages: list[dict[str, str]]) -> str:
    """借 pi 的 CLI 问一次：走的是和 DSH 同一个网关与鉴权，不用自己拼请求。"""
    system, prompt = _flatten(messages)
    argv = [*_pi_command(Path(config.cli)), "-p", "--no-session", "--no-tools"]
    argv += ["--provider", config.provider, "--model", config.model]
    if system:
        argv += ["--system-prompt", system]
    argv.append(prompt)
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_ms / 1000,
            env={**os.environ},
        )
    except FileNotFoundError as error:
        raise VibeError(f"起不了 pi：{error}") from error
    except subprocess.TimeoutExpired as error:
        raise VibeError(f"模型超过 {config.timeout_ms} ms 没有回应") from error
    text = (done.stdout or "").strip()
    if done.returncode != 0 or not text:
        detail = (done.stderr or "").strip()[:200] or f"退出码 {done.returncode}"
        raise VibeError(f"模型没有回答：{detail}")
    return text


def _pi_argv(config: ChatConfig, system: str, prompt: str, *, mode: str) -> list[str]:
    argv = [*_pi_command(Path(config.cli)), "-p", "--no-session", "--no-tools"]
    argv += ["--provider", config.provider, "--model", config.model, "--mode", mode]
    if system:
        argv += ["--system-prompt", system]
    argv.append(prompt)
    return argv


def stream_events(config: ChatConfig, messages: list[dict[str, str]]) -> Iterator[dict[str, str]]:
    """边跑边吐增量：thinking 一段、text 一段，最后一条是 reply。

    页面要的是「分块、可展开的思考」和「逐字出现的回答」，所以不能等整段跑完再返回。
    """
    ready, reason = probe(config)
    if not ready:
        raise VibeError(reason)
    if config.backend != "pi":
        # http 后端没有增量，退化成一次性：先给一段 text，再给 reply
        text = ask(config, messages)
        yield {"type": "text", "delta": text}
        yield {"type": "reply", "text": text, "thinking": ""}
        return

    system, prompt = _flatten(messages)
    argv = _pi_argv(config, system, prompt, mode="json")
    process = subprocess.Popen(  # noqa: S603 - 参数是我们自己拼的，没有 shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ},
    )
    think_parts: list[str] = []
    text_parts: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            trimmed = line.strip()
            if not trimmed:
                continue
            try:
                record = json.loads(trimmed)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "message_update":
                continue
            event = record.get("assistantMessageEvent")
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            delta = event.get("delta")
            if not isinstance(delta, str) or delta == "":
                continue
            if kind == "thinking_delta":
                think_parts.append(delta)
                yield {"type": "thinking", "delta": delta}
            elif kind == "text_delta":
                text_parts.append(delta)
                yield {"type": "text", "delta": delta}
    finally:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    answer = "".join(text_parts).strip()
    if process.returncode not in (0, None) and answer == "":
        detail = ""
        if process.stderr is not None:
            detail = process.stderr.read().strip()[:200]
        raise VibeError(f"模型没有回答：{detail or f'退出码 {process.returncode}'}")
    if answer == "":
        raise VibeError("模型没有说什么")
    yield {"type": "reply", "text": answer, "thinking": "".join(think_parts).strip()}


def ask(config: ChatConfig, messages: list[dict[str, str]]) -> str:
    """问一次模型；失败时把能看的原因抛出去，不要静默。"""
    ready, reason = probe(config)
    if not ready:
        raise VibeError(reason)
    if config.backend == "pi":
        return ask_pi(config, messages)
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


def send_stream(
    store: Path,
    session_id: str,
    program: str,
    text: str,
    *,
    conversation_id: str | None = None,
    new: bool = False,
    events: Sequence[dict[str, Any]] = (),
    context: dict[str, Any] | None = None,
) -> Iterator[dict[str, str]]:
    """流式版本：先落问题，再吐增量，最后落回答。

    出错时问题已经留在文件里了——用户打完字凭空消失，比看到一条失败更糟。
    """
    clean = text.strip()
    if not clean:
        raise VibeError("说点什么再发")
    directory = session_directory(store, session_id)
    path = pick_conversation(directory, None if new else conversation_id)
    append_message(path, "user", clean)
    history = read_messages(path)
    messages = [{"role": "system", "content": system_prompt(program, events, context)}, *history]
    thinking = ""
    answer = ""
    try:
        for event in stream_events(ChatConfig.from_env(), messages):
            if event.get("type") == "reply":
                answer = event.get("text", "")
                thinking = event.get("thinking", "")
                continue
            yield event
    except VibeError as error:
        yield {"type": "error", "error": str(error)}
        return
    append_message(path, "assistant", answer, thinking=thinking)
    yield {"type": "done", "conversationId": path.stem, "reply": answer}


def send(
    store: Path,
    session_id: str,
    program: str,
    text: str,
    *,
    conversation_id: str | None = None,
    new: bool = False,
    events: Sequence[dict[str, Any]] = (),
    context: dict[str, Any] | None = None,
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
    messages = [{"role": "system", "content": system_prompt(program, events, context)}, *history]
    try:
        reply = ask(ChatConfig.from_env(), messages)
    except VibeError as error:
        # 问题已经写进文件了：留下痕迹比悄悄丢掉诚实，页面照样显示用户这句话
        return {"ok": False, "error": str(error), "conversationId": path.stem, "reply": ""}
    append_message(path, "assistant", reply)
    return {"ok": True, "conversationId": path.stem, "reply": reply, "error": ""}
