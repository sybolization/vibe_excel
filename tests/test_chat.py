"""对话后端：消息要留在同一段对话里，模型不可用时要明说。

模型用一个本机的假网关来顶：这里验的是「落盘 + 续写 + 上下文 + 失败路径」，
不是某个真模型说了什么。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from vibe_excel.book.chat import (
    ChatConfig,
    append_message,
    pick_conversation,
    probe,
    read_messages,
    send,
    system_prompt,
)
from vibe_excel.util import VibeError

SESSION = "session-1"
PROGRAM = "demo-sales"


class _Stub(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容后端：回一句固定的话，并记下收到的 messages。"""

    received: list[list[dict[str, str]]] = []
    reply = "好的，看到了。"

    def do_POST(self) -> None:  # noqa: N802 - 覆盖父类
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append(list(payload.get("messages") or []))
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": self.reply}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 覆盖父类
        return


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Stub]]:
    _Stub.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    monkeypatch.setenv("VIBE_EXCEL_CHAT_BASE_URL", f"http://{host}:{port}/v1")
    monkeypatch.setenv("VIBE_EXCEL_CHAT_MODEL", "stub-model")
    try:
        yield _Stub
    finally:
        server.shutdown()
        server.server_close()


def _store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    (store / "sessions" / SESSION / "sessions").mkdir(parents=True)
    return store


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    path = _store(tmp_path) / "sessions" / SESSION / "sessions" / "a.jsonl"

    append_message(path, "user", "第一句")
    append_message(path, "assistant", "第一句的回复")

    assert read_messages(path) == [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "第一句的回复"},
    ]


def test_pick_conversation_resumes_the_latest_one(tmp_path: Path) -> None:
    directory = _store(tmp_path) / "sessions" / SESSION / "sessions"
    older = directory / "older.jsonl"
    newer = directory / "newer.jsonl"
    append_message(older, "user", "旧")
    append_message(newer, "user", "新")
    older.touch()
    newer.touch()

    # 不给 id：续最近那段，而不是另起一段
    assert pick_conversation(directory, None).name == newer.name
    assert pick_conversation(directory, "older").name == older.name
    with pytest.raises(VibeError):
        pick_conversation(directory, "nonexistent")


def test_pick_conversation_creates_one_when_directory_is_empty(tmp_path: Path) -> None:
    directory = _store(tmp_path) / "sessions" / SESSION / "sessions"
    created = pick_conversation(directory, None)

    assert created.name.endswith(".jsonl")
    assert created.parent == directory


def test_send_lands_in_one_conversation_and_carries_history(
    tmp_path: Path, stub: type[_Stub]
) -> None:
    store = _store(tmp_path)

    first = send(store, SESSION, PROGRAM, "第一句")
    assert first["ok"] is True
    assert first["reply"] == "好的，看到了。"

    # 第二句必须接在同一段对话里：传回 conversationId，不能再开一段
    second = send(store, SESSION, PROGRAM, "第二句", conversation_id=first["conversationId"])
    assert second["conversationId"] == first["conversationId"]
    files = list((store / "sessions" / SESSION / "sessions").glob("*.jsonl"))
    assert len(files) == 1
    assert read_messages(files[0]) == [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "好的，看到了。"},
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "好的，看到了。"},
    ]
    # 第二句请求里带上了前一轮，模型才不会每次都从零开始
    assert [m["content"] for m in stub.received[-1] if m["role"] == "user"] == ["第一句", "第二句"]


def test_send_keeps_the_question_when_backend_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("VIBE_EXCEL_CHAT_BASE_URL", "http://127.0.0.1:9/v1")

    result = send(store, SESSION, PROGRAM, "后端没起的时候")

    assert result["ok"] is False
    assert "连不上模型后端" in result["error"]
    # 问题本身仍然留在对话里：不然用户打完字就凭空消失了
    files = list((store / "sessions" / SESSION / "sessions").glob("*.jsonl"))
    assert read_messages(files[0]) == [{"role": "user", "content": "后端没起的时候"}]


def test_probe_asks_for_the_key_only_when_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    remote = ChatConfig(base_url="https://example.com/v1", model="m", key_env="NOPE_KEY", timeout_ms=1000)
    monkeypatch.delenv("NOPE_KEY", raising=False)
    ready, reason = probe(remote)
    assert ready is False
    assert "NOPE_KEY" in reason

    local = ChatConfig(base_url="http://127.0.0.1:23100/v1", model="m", key_env="NOPE_KEY", timeout_ms=1000)
    assert probe(local) == (True, "")


def test_system_prompt_lists_recent_input_changes() -> None:
    prompt = system_prompt(
        PROGRAM,
        [
            {"kind": "remove", "at": "2026-11-11T04:00:00.000Z", "file": "orders.xlsx"},
            {"kind": "undo", "at": "2026-11-11T04:01:00.000Z", "file": "orders.xlsx"},
        ],
    )

    assert PROGRAM in prompt
    assert "移出 orders.xlsx" in prompt
    assert "撤回 orders.xlsx" in prompt
