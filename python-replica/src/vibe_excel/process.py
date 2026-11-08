"""子进程执行的最小封装：环境、超时、输出上限与进程树清理。

run_process 不经过 shell，按给定 argv 启动；用独立线程读 stdout/stderr 以免管道写满死锁，
用 psutil 跟踪并回收整棵进程树（Windows 上尤其重要）。这里只管“跑完并如实返回结果”，
退出码语义由调用方解释。
"""

from __future__ import annotations

import codecs
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import psutil

from vibe_excel.contracts import ExecEvent, ExecLimits, RunId
from vibe_excel.util import VibeError, now

# 只继承启动子进程所必需的系统变量，用户态变量一律不传。
BASE_ENV_KEYS = (
    "PATH", "Path", "SystemRoot", "SYSTEMROOT", "windir", "TEMP", "TMP", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
)


def minimal_environment(
    extra: Mapping[str, str] | None = None, *, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """构造最小环境：只保留系统必需变量，并强制 UTF-8 与不写 .pyc。"""
    parent = os.environ if source is None else source
    result = {key: parent[key] for key in BASE_ENV_KEYS if key in parent}
    if extra is not None:
        result.update(extra)
    result.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return result


def terminate_process_tree(
    process: psutil.Process, grace_ms: int = 1000, known_children: list[psutil.Process] | None = None
) -> None:
    """先 terminate 再在宽限期后 kill 整棵进程树；仍有残留则报错，避免僵尸进程。"""
    descendants = list(known_children or [])
    try:
        descendants.extend(process.children(recursive=True))
    except psutil.NoSuchProcess:
        pass
    owned = {item.pid: item for item in [*descendants, process]}
    for item in owned.values():
        try:
            item.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(owned.values()), timeout=grace_ms / 1000)
    for item in alive:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(alive, timeout=2)
    if remaining:
        raise VibeError(f"已接管的进程未能退出: {[item.pid for item in remaining]}")


@dataclass(frozen=True)
class ProcessResult:
    """子进程结果的不可变快照；cancelled/truncated 与 timed_out 独立记录。"""

    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    elapsed_ms: int
    cancelled: bool = False
    truncated: bool = False


def run_process(
    command: list[str], *, cwd: str | Path, env: Mapping[str, str] | None = None,
    limits: ExecLimits | None = None, cancel: threading.Event | None = None,
    on_event: Callable[[ExecEvent], None] | None = None, run_id: RunId | None = None,
) -> ProcessResult:
    """启动子进程、并发收集输出、按超时/取消终止整棵树，返回结构化结果。

    输出达到 max_output_bytes 后继续读取但不再累积，避免子进程因管道写满而卡死；超时返回
    124、取消返回 130。on_event 会实时收到 spawn/stdout/stderr/exit 事件，便于流式展示。
    """
    if not command:
        raise VibeError("无法执行空命令")
    limits = ExecLimits() if limits is None else limits
    run_id = RunId("execution") if run_id is None else run_id
    started = time.monotonic()
    child = subprocess.Popen(
        command, cwd=cwd, env=minimal_environment(env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # Windows 上建新进程组，终止时才不会误伤父进程。
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    owner = psutil.Process(child.pid)
    # 有界队列形成背压，避免子进程狂喷输出时把内存吃光。
    chunks: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=128)
    reader_errors: list[OSError] = []

    def reader(name: str, stream: BinaryIO) -> None:
        """持续读取流并把分块放入队列；结束时放入 None 作为结束标记。"""
        try:
            while chunk := stream.read(4096):
                chunks.put((name, chunk))
        except OSError as error:
            reader_errors.append(error)
        finally:
            chunks.put((name, None))

    if child.stdout is None or child.stderr is None:
        terminate_process_tree(owner)
        raise VibeError("子进程管道不可用")
    # 管道必须边读边写：子进程输出超过缓冲区时会阻塞在 write 上。
    threads = [
        threading.Thread(target=reader, args=("stdout", child.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", child.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    seq = 0

    def emit(kind: str, text: str | None = None, exit_code: int | None = None) -> None:
        """向调用方发出执行事件；未设置回调时静默跳过。"""
        nonlocal seq
        if on_event is not None:
            match kind:
                case "spawn" | "stdout" | "stderr" | "exit" | "cancel-requested":
                    event = ExecEvent(seq, now(), kind, run_id, text, exit_code)
                case _:
                    raise VibeError(f"未知的执行事件: {kind}")
            on_event(event)
            seq += 1

    output: dict[str, list[str]] = {"stdout": [], "stderr": []}
    sizes = {"stdout": 0, "stderr": 0}
    decoders = {
        name: codecs.getincrementaldecoder("utf-8")(errors="replace")
        for name in output
    }
    open_streams = 2
    timed_out = False
    cancelled = False
    truncated = False
    known: dict[int, psutil.Process] = {}
    emit("spawn", f"pid={child.pid}")
    try:
        while open_streams or child.poll() is None:
            if not timed_out and not cancelled:
                try:
                    known.update({item.pid: item for item in owner.children(recursive=True)})
                except psutil.NoSuchProcess:
                    pass
                cancelled = cancel is not None and cancel.is_set()
                timed_out = (time.monotonic() - started) * 1000 >= limits.timeout_ms
                if cancelled or timed_out:
                    emit("cancel-requested", "cancelled" if cancelled else "timeout")
                    terminate_process_tree(owner, limits.kill_grace_ms, list(known.values()))
            try:
                # 轮询间隔用于同时检查超时与取消信号。
                name, chunk = chunks.get(timeout=0.05)
            except queue.Empty:
                continue
            if chunk is None:
                open_streams -= 1
                tail = decoders[name].decode(b"", final=True)
                if tail:
                    output[name].append(tail)
                    emit(name, tail)
                continue
            # 超限后仍继续读取，只是不再累积，防止子进程被管道阻塞。
            room = max(0, limits.max_output_bytes - sizes[name])
            kept = chunk[:room]
            sizes[name] += len(kept)
            if len(kept) < len(chunk):
                truncated = True
            text = decoders[name].decode(kept)
            if text:
                output[name].append(text)
                emit(name, text)
        child.wait(timeout=2)
        if reader_errors:
            raise VibeError(f"捕获子进程输出失败: {reader_errors[0]}")
        # 124/130 是约定的超时/取消退出码，别用平台相关的负值。
        code = 130 if cancelled else 124 if timed_out else child.returncode
        emit("exit", exit_code=code)
        return ProcessResult(
            code, timed_out, "".join(output["stdout"]), "".join(output["stderr"]),
            round((time.monotonic() - started) * 1000), cancelled, truncated,
        )
    finally:
        # 无论正常还是异常都清理进程树与线程，避免泄漏。
        terminate_process_tree(owner, limits.kill_grace_ms, list(known.values()))
        child.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        child.stdout.close()
        child.stderr.close()
