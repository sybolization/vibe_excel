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

BASE_ENV_KEYS = (
    "PATH", "Path", "SystemRoot", "SYSTEMROOT", "windir", "TEMP", "TMP", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
)


def minimal_environment(
    extra: Mapping[str, str] | None = None, *, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    parent = os.environ if source is None else source
    result = {key: parent[key] for key in BASE_ENV_KEYS if key in parent}
    if extra is not None:
        result.update(extra)
    result.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return result


def terminate_process_tree(
    process: psutil.Process, grace_ms: int = 1000, known_children: list[psutil.Process] | None = None
) -> None:
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
        raise VibeError(f"Owned processes did not exit: {[item.pid for item in remaining]}")


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    elapsed_ms: int
    cancelled: bool = False
    truncated: bool = False


def run_process(
    command: list[str], *, cwd: str | Path, env: Mapping[str, str] | None = None,
    limits: ExecLimits = ExecLimits(), cancel: threading.Event | None = None,
    on_event: Callable[[ExecEvent], None] | None = None, run_id: RunId = RunId("execution"),
) -> ProcessResult:
    if not command:
        raise VibeError("Cannot execute an empty command")
    started = time.monotonic()
    child = subprocess.Popen(
        command, cwd=cwd, env=minimal_environment(env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    owner = psutil.Process(child.pid)
    chunks: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=128)
    reader_errors: list[OSError] = []

    def reader(name: str, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(4096):
                chunks.put((name, chunk))
        except OSError as error:
            reader_errors.append(error)
        finally:
            chunks.put((name, None))

    if child.stdout is None or child.stderr is None:
        terminate_process_tree(owner)
        raise VibeError("Child process pipes are unavailable")
    threads = [
        threading.Thread(target=reader, args=("stdout", child.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", child.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    seq = 0

    def emit(kind: str, text: str | None = None, exit_code: int | None = None) -> None:
        nonlocal seq
        if on_event is not None:
            match kind:
                case "spawn" | "stdout" | "stderr" | "exit" | "cancel-requested":
                    event = ExecEvent(seq, now(), kind, run_id, text, exit_code)
                case _:
                    raise VibeError(f"Unknown execution event: {kind}")
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
            raise VibeError(f"Failed to capture child output: {reader_errors[0]}")
        code = 130 if cancelled else 124 if timed_out else child.returncode
        emit("exit", exit_code=code)
        return ProcessResult(
            code, timed_out, "".join(output["stdout"]), "".join(output["stderr"]),
            round((time.monotonic() - started) * 1000), cancelled, truncated,
        )
    finally:
        terminate_process_tree(owner, limits.kill_grace_ms, list(known.values()))
        child.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        child.stdout.close()
        child.stderr.close()
