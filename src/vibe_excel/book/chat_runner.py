"""对话的一轮怎么跑：排队、停止、以及被后台 job 唤醒。

每条消息现起一个 pi 进程够简单，却撑不住三件事：命令跑着的时候用户又说了话、
用户按了停止、后台命令结束后要把结果交回来。所以这里放一个很小的执行器：
同一段对话里的消息排队，上一轮不是被打断就接着跑下一轮，页面通过订阅队列实时收事件。
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..util import VibeError
from .chat import (
    ChatConfig,
    append_message,
    append_tool_call,
    pick_conversation,
    read_messages,
    stream_events,
    system_prompt,
)
from .jobs import Finished, Watcher, ensure_helper, snapshot, wake_text

# 页面跟不上时宁可丢事件，也不要把跑着的 agent 卡住
_MAX_QUEUED_EVENTS = 400


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class _Turn:
    """一轮正在跑的东西：进程句柄、收集到的内容、是否被用户打断。"""

    proc: subprocess.Popen[str] | None = None
    stopped: bool = False
    thinking: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Conversation:
    """一段对话的执行状态。"""

    path: Path
    workspace: Path
    program: str
    pending: list[str] = field(default_factory=list)
    busy: bool = False
    turn: _Turn = field(default_factory=_Turn)
    subscribers: list[queue.Queue[dict[str, Any]]] = field(default_factory=list)
    recent: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Runner:
    """按对话串行执行；服务进程里只有一个。"""

    def __init__(self) -> None:
        self._conversations: dict[str, _Conversation] = {}
        self._lock = threading.Lock()
        self._watcher: Watcher | None = None

    # ---- 订阅 ----

    def subscribe(self, conversation_id: str) -> tuple[queue.Queue[dict[str, Any]], list[dict[str, Any]]] | None:
        """订阅者：队列 + 刚发生过的那些事件（页面晚一步连上来也能补齐）。"""
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return None
        events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_MAX_QUEUED_EVENTS)
        with conversation.lock:
            conversation.subscribers.append(events)
            backlog = list(conversation.recent)
        return events, backlog

    def unsubscribe(self, conversation_id: str, events: queue.Queue[dict[str, Any]]) -> None:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return
        with conversation.lock:
            if events in conversation.subscribers:
                conversation.subscribers.remove(events)

    def _publish(self, conversation: _Conversation, event: dict[str, Any]) -> None:
        with conversation.lock:
            conversation.recent.append(event)
            if len(conversation.recent) > 200:
                conversation.recent = conversation.recent[-200:]
            targets = list(conversation.subscribers)
        for target in targets:
            try:
                target.put_nowait(event)
            except queue.Full:
                continue

    def state(self, conversation_id: str) -> dict[str, Any]:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return {"busy": False, "queued": 0}
        return {"busy": conversation.busy, "queued": len(conversation.pending)}

    # ---- 说话 ----

    def _ensure(
        self, store: Path, session_id: str, program: str, conversation_id: str | None, new: bool
    ) -> _Conversation:
        from .chat import session_directory

        directory = session_directory(store, session_id)
        path = pick_conversation(directory, None if new else conversation_id)
        with self._lock:
            found = self._conversations.get(path.stem)
            if found is None:
                found = _Conversation(path=path, workspace=store, program=program)
                self._conversations[path.stem] = found
            return found

    def send(
        self,
        store: Path,
        session_id: str,
        program: str,
        text: str,
        *,
        conversation_id: str | None = None,
        new: bool = False,
        context: dict[str, Any] | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        """把一句话排进队，必要时把这一轮拉起来。立刻返回，不等模型。"""
        clean = text.strip()
        if not clean:
            raise VibeError("说点什么再发")
        conversation = self._ensure(store, session_id, program, conversation_id, new)
        conversation.program = program
        with conversation.lock:
            conversation.pending.append(clean)
            start = not conversation.busy
            if start:
                conversation.busy = True
        if self._watcher is not None:
            self._watcher.watch(store)
        if start:
            threading.Thread(
                target=self._drain,
                args=(conversation, context, resume),
                name=f"chat-{conversation.path.stem}",
            ).start()
        return {
            "ok": True,
            "conversationId": conversation.path.stem,
            "queued": not start or len(conversation.pending) > 1,
        }

    def stop(self, conversation_id: str) -> dict[str, Any]:
        """停止：杀掉这一轮，并把「用户打断」写进上下文。

        没在跑就什么也不做——不去唤醒一个闲着的 agent。
        """
        conversation = self._conversations.get(conversation_id)
        if conversation is None or not conversation.busy:
            return {"ok": True, "stopped": False}
        conversation.turn.stopped = True
        proc = conversation.turn.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        append_message(
            conversation.path,
            "note",
            json.dumps({"kind": "interrupted", "at": _now()}, ensure_ascii=False),
        )
        self._publish(conversation, {"type": "interrupted", "error": "你按了停止"})
        return {"ok": True, "stopped": True}

    # ---- 干活 ----

    def _drain(self, conversation: _Conversation, context: dict[str, Any] | None, resume: bool) -> None:
        while True:
            with conversation.lock:
                if not conversation.pending:
                    conversation.busy = False
                    return
                text = conversation.pending.pop(0)
                conversation.turn = _Turn()
            try:
                self._turn(conversation, text, context, resume=resume)
            except Exception as error:  # 一轮崩了不能把整段对话带死
                self._publish(conversation, {"type": "error", "error": str(error)})
            _ = resume
            with conversation.lock:
                if not conversation.pending:
                    conversation.busy = False
                    return

    # ---- 后台 job ----

    def start_watcher(self) -> None:
        """盯工作区里的 job：跑完了把结果交回对话。"""
        if self._watcher is not None:
            return
        self._watcher = Watcher(self._on_job)
        for conversation in self._conversations.values():
            self._watcher.watch(conversation.workspace)
        self._watcher.start()

    def _on_job(self, workspace: Path, finished: Finished | None, dropped: str | None) -> None:
        conversation = next(
            (item for item in self._conversations.values() if item.workspace == workspace), None
        )
        if conversation is None:
            return
        if finished is not None:
            append_message(conversation.path, "note", "后台任务结束，把结果交给 agent")
            self._wake(conversation, wake_text(finished))
        elif dropped is not None:
            append_message(conversation.path, "note", f"后台任务 {dropped} 超时或超出上限，已放弃")

    def _wake(self, conversation: _Conversation, text: str) -> None:
        """把一段内容排进对话，让 agent 接着说（不伪装成用户发言）。"""
        with conversation.lock:
            conversation.pending.append(text)
            start = not conversation.busy
            if start:
                conversation.busy = True
        if start:
            threading.Thread(
                target=self._drain, args=(conversation, None, True), name="chat-wake"
            ).start()

    def _turn(
        self,
        conversation: _Conversation,
        text: str,
        context: dict[str, Any] | None,
        *,
        resume: bool = False,
    ) -> None:
        turn = conversation.turn
        # 轮到它了才落盘：排队的两句话不会挤在同一个时间点上
        if not resume:
            append_message(conversation.path, "user", text)
        # 回合开始先留一条后路：它改坏了东西能回退
        snapshot(conversation.workspace)
        ensure_helper(conversation.workspace)
        history = read_messages(conversation.path)
        prompt = system_prompt(conversation.program, (), context, workspace=conversation.workspace)
        messages = [{"role": "system", "content": prompt}, *history]
        if resume and text:
            # 唤醒的那段话只进这一次的上下文，不落盘——它不是用户说的
            messages.append({"role": "user", "content": text})
        # 新的一轮从零开始：backlog 只回放这一轮的事件，别把上一轮的回答再播一遍
        with conversation.lock:
            conversation.recent = []
        self._publish(conversation, {"type": "start", "text": text})
        final_thinking = ""
        final_calls: list[dict[str, Any]] = []
        try:
            for event in stream_events(
                ChatConfig.from_env(),
                messages,
                workspace=conversation.workspace,
                proc_box=[turn],
            ):
                kind = event.get("type")
                if kind == "reply":
                    final_thinking = event.get("thinking", "")
                    raw_calls = event.get("calls")
                    if isinstance(raw_calls, str) and raw_calls:
                        parsed = json.loads(raw_calls)
                        if isinstance(parsed, list):
                            final_calls = [item for item in parsed if isinstance(item, dict)]
                    continue
                if kind == "thinking":
                    turn.thinking.append(event.get("delta", ""))
                elif kind == "text":
                    turn.text.append(event.get("delta", ""))
                self._publish(conversation, event)
        finally:
            # 被打断就不落回答：那一轮本来也没有结论，落一条空回答只会误导后来的人
            if not turn.stopped and turn.text:
                if final_thinking:
                    append_message(conversation.path, "assistant", "", thinking=final_thinking)
                for call in final_calls:
                    append_tool_call(conversation.path, call)
                append_message(conversation.path, "assistant", "".join(turn.text).strip())
        if not turn.stopped:
            self._publish(conversation, {"type": "done", "conversationId": conversation.path.stem})


__all__ = ["Runner"]
