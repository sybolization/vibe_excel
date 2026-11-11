"""后台 job 与快照：把长命令交出去、盯哨兵、唤醒对话。

这几条是「停止不杀命令」「异步输入」能不能成立的地基，所以测的是约定本身：
日志末尾的哨兵、认领标记、上限、以及交给 agent 的那段话。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vibe_excel.book.jobs import (
    DONE_MARK,
    MAX_JOBS,
    Finished,
    Watcher,
    _decode_log,
    enforce_limits,
    ensure_helper,
    harvest,
    helper_path,
    snapshot,
    wake_text,
)


def _job(workspace: Path, name: str, *, code: int | None = 0, text: str = "hello") -> Path:
    job = workspace / "_chat" / "jobs" / name
    job.mkdir(parents=True)
    body = text + ("\n" if not text.endswith("\n") else "")
    if code is not None:
        body += f"{DONE_MARK} {code}\n"
    (job / "out.log").write_text(body, encoding="utf-8")
    return job


def test_helper_is_written_once_and_runnable(tmp_path: Path) -> None:
    target = ensure_helper(tmp_path)
    assert target == helper_path(tmp_path)
    assert target.is_file()
    first = target.read_text(encoding="utf-8")
    ensure_helper(tmp_path)
    assert target.read_text(encoding="utf-8") == first
    # 用法不对时它得说清楚，而不是静默退出
    # 子进程要 UTF-8 输出：中文 Windows 默认 cp936，测试按 UTF-8 读就会错
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert result.returncode == 2
    assert "用法" in result.stdout


def test_harvest_only_takes_finished_jobs_once(tmp_path: Path) -> None:
    _job(tmp_path, "20260101-000000", code=0, text="done")
    _job(tmp_path, "20260101-000001", code=None, text="still running")

    done = harvest(tmp_path)
    assert [item.job for item in done] == ["20260101-000000"]
    assert done[0].code == 0
    # 正文里不该混进哨兵那一行
    assert "done" in done[0].text
    assert DONE_MARK not in done[0].text

    # 已经认领过的不再交第二遍
    assert harvest(tmp_path) == []


def test_harvest_keeps_non_zero_code(tmp_path: Path) -> None:
    _job(tmp_path, "20260101-000002", code=3, text="boom")
    done = harvest(tmp_path)
    assert done[0].code == 3


def test_enforce_limits_drops_expired_and_extra_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expired = _job(tmp_path, "old", code=None)
    stamp = time.time() - 7200
    expired.touch()
    import os

    os.utime(expired, (stamp, stamp))

    for index in range(MAX_JOBS + 2):
        _job(tmp_path, f"fresh-{index:02d}", code=None)

    dropped = enforce_limits(tmp_path)
    assert "old" in dropped
    # 超量的也要清掉，别让脱离进程无限堆着
    assert len(dropped) >= 3


def test_decode_log_reads_utf8_and_gbk() -> None:
    """命令可能吐 UTF-8，也可能吐 GBK（中文 Windows 的默认），两种都要读得懂。"""
    assert _decode_log("中文输出 ok".encode()) == "中文输出 ok"
    assert _decode_log("中文输出 ok".encode("gbk")) == "中文输出 ok"
    # 两种都不是的字节也不能炸：给一串能看的替换符，比抛异常强
    assert _decode_log(b"\xff\xfe\xff") != ""


def test_wake_text_says_what_happened() -> None:
    ok = wake_text(Finished(job="j1", code=0, text="42"))
    assert "后台任务结束" in ok and "j1" in ok and "42" in ok

    bad = wake_text(Finished(job="j2", code=9, text=""))
    assert "退出码 9" in bad
    assert "没有输出" in bad


def test_watcher_calls_back_for_finished_jobs(tmp_path: Path) -> None:
    calls: list[tuple[str, Finished | None, str | None]] = []
    watcher = Watcher(lambda ws, done, dropped: calls.append((str(ws), done, dropped)), interval=0.05)
    watcher.watch(tmp_path)
    watcher.start()
    try:
        _job(tmp_path, "20260101-000003", code=0, text="ok")
        deadline = time.time() + 5
        while time.time() < deadline and not calls:
            time.sleep(0.05)
    finally:
        watcher.stop()

    assert calls, "watcher 没有把跑完的 job 交出来"
    assert calls[0][1] is not None
    assert calls[0][1].job == "20260101-000003"


def test_snapshot_skips_when_there_is_no_repo(tmp_path: Path) -> None:
    assert snapshot(tmp_path) == ""


def test_snapshot_commits_changes(tmp_path: Path) -> None:
    repo = tmp_path / "program-repos"
    repo.mkdir()
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("没有 git")

    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    (repo / "pipeline.py").write_text("print(1)\n", encoding="utf-8")
    first = snapshot(tmp_path)
    assert "对话前快照" in first
    # 没有变化就不该再提交一次
    assert snapshot(tmp_path) == ""
