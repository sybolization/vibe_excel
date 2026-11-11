"""后台 job：让长命令脱离这一轮去跑，跑完再把结果交回对话。

pi 自己的 bash 是前台等待，abort 还会杀掉整棵进程树，所以「停止但命令继续跑」
在原生工具下做不到。这里的办法是：超过 15 秒的命令不干等，交给一个脱离进程，
它把输出写进日志并在末尾留一行 ``__DONE__ <退出码>``；服务端盯这一行，跑完再唤醒 agent。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..util import VibeError

JOBS_DIR = "_chat/jobs"
HELPER = "_chat/bg.py"
_HELPER_TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "book" / "bg.py"
DONE_MARK = "__DONE__"

# 硬线：同时最多这么多个、单个最多跑这么久
MAX_JOBS = 20
MAX_LIFETIME_SECONDS = 3600
MAX_LOG_BYTES = 5 * 1024 * 1024

@dataclass
class Finished:
    """一个跑完的 job。"""

    job: str
    code: int
    text: str


def helper_path(workspace: Path) -> Path:
    return workspace / HELPER


def ensure_helper(workspace: Path) -> Path:
    """把 bg.py 放进工作区（内容一样就不动它）。

    它不是字符串拼出来的：就是包里的一个真文件，照抄过去。拼字符串那套
    在转义上栽过——生成的脚本自己就是坏的。
    """
    target = helper_path(workspace)
    source = _HELPER_TEMPLATE.read_text(encoding="utf-8")
    if target.is_file() and target.read_text(encoding="utf-8") == source:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8", newline="\n")
    return target


def _jobs_root(workspace: Path) -> Path:
    return workspace / JOBS_DIR


def _read_log(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > MAX_LOG_BYTES:
        data = data[:MAX_LOG_BYTES]
    text = data.decode("utf-8", "replace")
    # 末尾那行是哨兵，不该出现在正文里
    return text.rsplit(DONE_MARK, 1)[0].rstrip()


def _marker(path: Path) -> int | None:
    """日志末尾的 __DONE__ <code>：有它就是正常结束。"""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 200))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if line.startswith(DONE_MARK):
            code = line[len(DONE_MARK) :].strip()
            try:
                return int(code)
            except ValueError:
                return -1
    return None


def harvest(workspace: Path) -> list[Finished]:
    """收走已经跑完、还没人认领的 job。"""
    root = _jobs_root(workspace)
    if not root.is_dir():
        return []
    done: list[Finished] = []
    for job in sorted(root.iterdir()):
        if not job.is_dir():
            continue
        claimed = job / ".claimed"
        if claimed.exists():
            continue
        log = job / "out.log"
        code = _marker(log)
        if code is None:
            continue
        claimed.write_text(str(int(time.time())), encoding="utf-8")
        done.append(Finished(job=job.name, code=code, text=_read_log(log)))
    return done


def enforce_limits(workspace: Path) -> list[str]:
    """超时、超量的 job 处理掉。返回被清理的 job 名，给人一句交代。"""
    root = _jobs_root(workspace)
    if not root.is_dir():
        return []
    running = [item for item in root.iterdir() if item.is_dir() and not (item / ".claimed").exists()]
    dropped: list[str] = []
    for job in running:
        started = job.stat().st_mtime
        if time.time() - started > MAX_LIFETIME_SECONDS:
            (job / ".claimed").write_text("expired", encoding="utf-8")
            dropped.append(job.name)
    alive = [item for item in running if item.name not in dropped]
    if len(alive) > MAX_JOBS:
        for job in alive[MAX_JOBS:]:
            (job / ".claimed").write_text("too-many", encoding="utf-8")
            dropped.append(job.name)
    return dropped


def wake_text(done: Finished) -> str:
    """交给 agent 的那段话：先给结论，再给输出，省得它再问一遍。"""
    head = "后台任务结束" if done.code == 0 else f"后台任务失败（退出码 {done.code}）"
    body = done.text.strip() or "（没有输出）"
    return f"{head}：{done.job}\n输出：\n{body}\n\n请据此继续，必要时告诉用户结果。"


class Watcher:
    """盯所有工作区的 job 目录；有跑完的就唤醒对应对话。"""

    def __init__(self, callback: Any, interval: float = 2.0) -> None:
        self._callback = callback
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._roots: set[str] = set()

    def watch(self, workspace: Path) -> None:
        self._roots.add(str(workspace))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="job-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            for raw in list(self._roots):
                workspace = Path(raw)
                try:
                    dropped = enforce_limits(workspace)
                    for name in dropped:
                        self._callback(workspace, None, name)
                    for done in harvest(workspace):
                        self._callback(workspace, done, None)
                except (OSError, VibeError):
                    continue


def snapshot(workspace: Path) -> str:
    """回合开始前给工作区提交一次，改坏了能回退。没有 git 仓库就跳过。"""
    repo = workspace / "program-repos"
    if not (repo / ".git").is_dir():
        return ""
    if shutil.which("git") is None:
        return ""
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not (status.stdout or "").strip():
        return ""
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    commit = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"对话前快照 {time.strftime('%Y-%m-%d %H:%M:%S')}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return commit.stdout.strip().splitlines()[0] if commit.returncode == 0 else ""


__all__ = [
    "Finished",
    "Watcher",
    "enforce_limits",
    "ensure_helper",
    "harvest",
    "helper_path",
    "snapshot",
    "wake_text",
]


def _json_meta(workspace: Path) -> dict[str, Any]:
    """调试用：把工作区里现有的 job 状态读出来。"""
    root = _jobs_root(workspace)
    if not root.is_dir():
        return {}
    return {item.name: json.loads(json.dumps({"claimed": (item / ".claimed").exists()})) for item in root.iterdir()}
