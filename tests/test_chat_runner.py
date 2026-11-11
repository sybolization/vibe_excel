"""对话执行器：排队、停止、唤醒。

这些测的是「一轮怎么跑」的约定，不是模型的回答质量：用假的 stream_events
替掉真 pi，把节奏捏在手里。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vibe_excel.book import chat_runner
from vibe_excel.book.chat_runner import Runner
from vibe_excel.util import VibeError


def _make_conversation(tmp_path: Path) -> tuple[Path, str, str]:
    """造一个最小的会话目录：store/sessions/<sid>/sessions/<file>.jsonl。"""
    store = tmp_path / "store"
    session = "sess-1"
    directory = store / "sessions" / session / "sessions"
    directory.mkdir(parents=True)
    program = "demo"
    (directory / "2026-01-01T00-00-00-000Z_chat.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "message": {"role": "user", "content": [{"type": "text", "text": "老问题"}]},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return store, session, program


def _runner_with(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, str]]) -> Runner:
    def fake_stream(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, str]]:
        yield from events

    monkeypatch.setattr(chat_runner, "stream_events", fake_stream)
    monkeypatch.setattr(chat_runner, "snapshot", lambda _workspace: "")
    monkeypatch.setattr(chat_runner, "ensure_helper", lambda _workspace: None)
    return Runner()


def test_send_returns_immediately_and_records_the_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, session, program = _make_conversation(tmp_path)
    gate = threading.Event()

    def slow_stream(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, str]]:
        gate.wait(timeout=5)
        yield {"type": "text", "delta": "好"}
        yield {"type": "reply", "text": "好", "thinking": "", "calls": "[]"}

    monkeypatch.setattr(chat_runner, "stream_events", slow_stream)
    monkeypatch.setattr(chat_runner, "snapshot", lambda _workspace: "")
    monkeypatch.setattr(chat_runner, "ensure_helper", lambda _workspace: None)
    runner = Runner()

    started = time.time()
    result = runner.send(store, session, program, "新问题")
    elapsed = time.time() - started

    assert result["ok"] is True
    assert elapsed < 1.0, "send 不该等模型"
    assert runner.state(result["conversationId"])["busy"] is True
    gate.set()

    deadline = time.time() + 5
    while time.time() < deadline and runner.state(result["conversationId"])["busy"]:
        time.sleep(0.05)
    assert runner.state(result["conversationId"])["busy"] is False


def test_two_messages_run_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, session, program = _make_conversation(tmp_path)
    seen: list[str] = []
    release = threading.Event()

    def fake_stream(
        _config: Any, messages: list[dict[str, str]], **_kwargs: Any
    ) -> Iterator[dict[str, str]]:
        seen.append(messages[-1]["content"])
        release.wait(timeout=5)
        yield {"type": "text", "delta": "答"}
        yield {"type": "reply", "text": "答", "thinking": "", "calls": "[]"}

    monkeypatch.setattr(chat_runner, "stream_events", fake_stream)
    monkeypatch.setattr(chat_runner, "snapshot", lambda _workspace: "")
    monkeypatch.setattr(chat_runner, "ensure_helper", lambda _workspace: None)
    runner = Runner()

    first = runner.send(store, session, program, "第一句")
    conversation = first["conversationId"]
    second = runner.send(store, session, program, "第二句", conversation_id=conversation)
    assert second["queued"] is True, "跑着的时候再说话应当排队"

    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and len(seen) < 2:
        time.sleep(0.05)
    assert seen[:2] == ["第一句", "第二句"]


def test_stop_marks_interrupted_and_does_not_fake_an_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, session, program = _make_conversation(tmp_path)
    started = threading.Event()

    def hanging_stream(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, str]]:
        started.set()
        time.sleep(1.5)
        yield {"type": "text", "delta": "不该出现的回答"}
        yield {"type": "reply", "text": "不该出现的回答", "thinking": "", "calls": "[]"}

    monkeypatch.setattr(chat_runner, "stream_events", hanging_stream)
    monkeypatch.setattr(chat_runner, "snapshot", lambda _workspace: "")
    monkeypatch.setattr(chat_runner, "ensure_helper", lambda _workspace: None)
    runner = Runner()

    result = runner.send(store, session, program, "跑个长命令")
    conversation = result["conversationId"]
    events = runner.subscribe(conversation)
    assert events is not None
    assert started.wait(timeout=3)

    stopped = runner.stop(conversation)
    assert stopped["stopped"] is True

    path = store / "sessions" / session / "sessions" / f"{conversation}.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    notes = [item for item in records if item["message"]["role"] == "note"]
    assert notes, "打断必须留一条记录"
    payload = json.loads(notes[-1]["message"]["content"][0]["text"])
    assert payload["kind"] == "interrupted"
    # 被打断的那一轮没有结论，不许落一条假回答
    assistant = [
        item for item in records[1:] if item["message"]["role"] == "assistant"
    ]
    assert assistant == []


def test_stop_when_idle_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, session, program = _make_conversation(tmp_path)
    runner = _runner_with(
        monkeypatch,
        [{"type": "text", "delta": "答"}, {"type": "reply", "text": "答", "thinking": "", "calls": "[]"}],
    )
    result = runner.send(store, session, program, "问一句")
    conversation = result["conversationId"]

    deadline = time.time() + 5
    while time.time() < deadline and runner.state(conversation)["busy"]:
        time.sleep(0.02)

    assert runner.stop(conversation)["stopped"] is False


def test_wake_runs_another_turn_without_a_fake_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台 job 跑完要唤得醒 agent：不追加用户发言，只让它接着说。"""
    store, session, program = _make_conversation(tmp_path)
    seen: list[str] = []

    def fake_stream(
        _config: Any, messages: list[dict[str, str]], **_kwargs: Any
    ) -> Iterator[dict[str, str]]:
        seen.append(messages[-1]["content"])
        yield {"type": "text", "delta": "收到"}
        yield {"type": "reply", "text": "收到", "thinking": "", "calls": "[]"}

    monkeypatch.setattr(chat_runner, "stream_events", fake_stream)
    monkeypatch.setattr(chat_runner, "snapshot", lambda _workspace: "")
    monkeypatch.setattr(chat_runner, "ensure_helper", lambda _workspace: None)
    runner = Runner()

    first = runner.send(store, session, program, "先问一句")
    conversation_id = first["conversationId"]
    deadline = time.time() + 5
    while time.time() < deadline and runner.state(conversation_id)["busy"]:
        time.sleep(0.02)

    runner._wake(runner._conversations[conversation_id], "后台任务结束：j1")  # noqa: SLF001
    deadline = time.time() + 5
    while time.time() < deadline and len(seen) < 2:
        time.sleep(0.02)

    assert len(seen) == 2, "唤醒没有把下一轮跑起来"
    assert "后台任务结束" in seen[1]

    path = store / "sessions" / session / "sessions" / f"{conversation_id}.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    user_texts = [
        item["message"]["content"][0]["text"]
        for item in records
        if item["message"]["role"] == "user"
    ]
    assert user_texts == ["老问题", "先问一句"], "唤醒不该伪造一条用户发言"


def test_wake_loop_stops_after_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """唤醒次数到上限就不再唤醒：不能自己转成无休止的循环。"""
    store, session, program = _make_conversation(tmp_path)
    prompts: list[str] = []

    def fake_stream(_config: Any, messages: list[dict[str, str]], **_kwargs: Any) -> Iterator[dict[str, str]]:
        prompts.append(messages[-1]["content"])
        yield {"type": "text", "delta": "好"}
        yield {"type": "reply", "text": "好", "thinking": "", "calls": "[]"}

    monkeypatch.setattr(chat_runner, "stream_events", fake_stream)
    monkeypatch.setattr(chat_runner, "snapshot", lambda _workspace: "")
    monkeypatch.setattr(chat_runner, "ensure_helper", lambda _workspace: None)
    runner = Runner()

    result = runner.send(store, session, program, "第一句")
    conversation_id = result["conversationId"]
    conversation = runner._conversations[conversation_id]  # noqa: SLF001

    deadline = time.time() + 5
    while time.time() < deadline and runner.state(conversation_id)["busy"]:
        time.sleep(0.02)

    from vibe_excel.book.jobs import Finished

    for index in range(5):
        runner._on_job(  # noqa: SLF001
            conversation.workspace, Finished(job=f"j{index}", code=0, text="x"), None
        )
        deadline = time.time() + 5
        while time.time() < deadline and runner.state(conversation_id)["busy"]:
            time.sleep(0.02)

    assert conversation.wakes == 3, "唤醒次数要卡在上限"
    assert len(prompts) <= 4, f"唤醒只该跑有限次，实际 {len(prompts)}"


def test_stop_terminates_the_running_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """停止要真的把子进程杀掉，不能只改个标记。

    stream_events 会把 pi 进程 append 进传给它的盒子里；这里用一个假的进程对象
    验证那个盒子确实被 runner 拿到、并且 terminate 被调用。
    """
    store, session, program = _make_conversation(tmp_path)
    started = threading.Event()
    killed = threading.Event()

    class FakeProcess:
        returncode = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            self.returncode = -15
            killed.set()

    def fake_stream(_config: Any, _messages: Any, **kwargs: Any):
        box = kwargs.get("proc_box")
        if box is not None:
            box.append(FakeProcess())
        started.set()
        time.sleep(1.5)
        yield {"type": "text", "delta": "不该出现的回答"}
        yield {"type": "reply", "text": "不该出现的回答", "thinking": "", "calls": "[]"}

    monkeypatch.setattr(chat_runner, "stream_events", fake_stream)
    monkeypatch.setattr(chat_runner, "snapshot", lambda _workspace: "")
    monkeypatch.setattr(chat_runner, "ensure_helper", lambda _workspace: None)
    runner = Runner()

    result = runner.send(store, session, program, "跑个长命令")
    assert started.wait(timeout=3)

    assert runner.stop(result["conversationId"])["stopped"] is True
    assert killed.wait(timeout=2), "停止没有把子进程 terminate 掉"


def test_new_flag_starts_a_separate_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """new=true 要真的新开一段，而不是并进最近那段。"""
    store, session, program = _make_conversation(tmp_path)
    runner = _runner_with(
        monkeypatch,
        [{"type": "text", "delta": "答"}, {"type": "reply", "text": "答", "thinking": "", "calls": "[]"}],
    )

    first = runner.send(store, session, program, "第一段")
    second = runner.send(store, session, program, "第二段", new=True)
    assert first["conversationId"] != second["conversationId"]

    # 新的那段是独立文件：造的那段还在，另开了一份
    directory = store / "sessions" / session / "sessions"
    deadline = time.time() + 5
    while time.time() < deadline and len(list(directory.glob("*.jsonl"))) < 2:
        time.sleep(0.02)
    files = sorted(item.name for item in directory.glob("*.jsonl"))
    assert len(files) == 2, files
    assert files[0] == "2026-01-01T00-00-00-000Z_chat.jsonl"


def test_wake_during_a_turn_is_not_saved_as_user_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正在跑的时候 job 结束：唤醒文本只进上下文，不落成用户发言。"""
    store, session, program = _make_conversation(tmp_path)
    release = threading.Event()
    seen: list[str] = []

    def fake_stream(_config: Any, messages: list[dict[str, str]], **_kwargs: Any) -> Iterator[dict[str, str]]:
        seen.append(messages[-1]["content"])
        release.wait(timeout=5)
        yield {"type": "text", "delta": "答"}
        yield {"type": "reply", "text": "答", "thinking": "", "calls": "[]"}

    monkeypatch.setattr(chat_runner, "stream_events", fake_stream)
    monkeypatch.setattr(chat_runner, "snapshot", lambda _workspace: "")
    monkeypatch.setattr(chat_runner, "ensure_helper", lambda _workspace: None)
    runner = Runner()

    result = runner.send(store, session, program, "用户的问题")
    conversation_id = result["conversationId"]
    conversation = runner._conversations[conversation_id]  # noqa: SLF001

    # 这一轮还没跑完就往队里塞一个唤醒
    runner._wake(conversation, "后台任务结束：j9")  # noqa: SLF001
    release.set()

    deadline = time.time() + 5
    while time.time() < deadline and len(seen) < 2:
        time.sleep(0.02)
    assert len(seen) == 2, seen

    path = store / "sessions" / session / "sessions" / f"{conversation_id}.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    user_texts = [
        item["message"]["content"][0]["text"]
        for item in records
        if item["message"]["role"] == "user"
    ]
    assert "后台任务结束：j9" not in user_texts, "唤醒不该落成用户发言"
    assert user_texts == ["老问题", "用户的问题"]


def test_subscribe_replays_what_already_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """页面晚连上来时，这一轮已经发生的事件要先补给它。"""
    store, session, program = _make_conversation(tmp_path)
    runner = _runner_with(
        monkeypatch,
        [
            {"type": "thinking", "delta": "想一下"},
            {"type": "call", "id": "c1", "name": "bash", "brief": "ls"},
            {"type": "result", "id": "c1", "ok": "1", "text": "a.txt", "ms": "12"},
            {"type": "text", "delta": "答"},
            {"type": "reply", "text": "答", "thinking": "想一下", "calls": "[]"},
        ],
    )

    first = runner.send(store, session, program, "问一句")
    conversation_id = first["conversationId"]
    deadline = time.time() + 5
    while time.time() < deadline and runner.state(conversation_id)["busy"]:
        time.sleep(0.02)

    events, backlog = runner.subscribe(conversation_id)
    try:
        kinds = [item["type"] for item in backlog]
        assert kinds[0] == "start", kinds
        assert {"thinking", "call", "result", "text", "done"} <= set(kinds), kinds
        # 订阅之后的新事件要真的送到队列里
        runner._publish(runner._conversations[conversation_id], {"type": "text", "delta": "后续"})  # noqa: SLF001
        delivered = events.get(timeout=2)
        assert delivered["delta"] == "后续"
    finally:
        runner.unsubscribe(conversation_id, events)


def test_state_reports_queue_and_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """页面靠 state 判断「还能不能发」和「排了几条」。"""
    store, session, program = _make_conversation(tmp_path)
    runner = _runner_with(monkeypatch, [{"type": "text", "delta": "答"}, {"type": "reply", "text": "答", "thinking": "", "calls": "[]"}])

    result = runner.send(store, session, program, "第一句")
    conversation_id = result["conversationId"]
    state = runner.state(conversation_id)
    assert set(state) == {"busy", "queued"}, state
    assert isinstance(state["queued"], int)

    deadline = time.time() + 5
    while time.time() < deadline and runner.state(conversation_id)["busy"]:
        time.sleep(0.02)
    assert runner.state(conversation_id)["busy"] is False


def test_send_rejects_empty_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, session, program = _make_conversation(tmp_path)
    runner = _runner_with(monkeypatch, [])
    with pytest.raises(VibeError):
        runner.send(store, session, program, "   ")
