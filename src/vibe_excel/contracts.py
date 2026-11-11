"""跨层共享的类型契约：id 类型、运行状态、执行限制与事件。

这里只有类型和新类型别名，不放业务逻辑；NewType 让 program/session/run 等 id 在 mypy
层面不可互换，避免把会话 id 当运行 id 传。终态判定集中在这里，供控制库与调用方共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NewType

ProgramId = NewType("ProgramId", str)
SessionId = NewType("SessionId", str)
RunId = NewType("RunId", str)
DataRevisionId = NewType("DataRevisionId", str)
CodeCommit = NewType("CodeCommit", str)

RunStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]
TerminalStatus = Literal["succeeded", "failed", "cancelled"]
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def is_terminal(status: str) -> bool:
    """判断运行状态是否为终态（成功/失败/取消）。"""
    return status in TERMINAL_RUN_STATUSES


@dataclass(frozen=True)
class ExecLimits:
    """一次子进程执行的限制；构造即校验，避免零超时或负宽限期。"""

    timeout_ms: int = 15 * 60 * 1000
    max_output_bytes: int = 8 * 1024 * 1024
    kill_grace_ms: int = 5000

    def __post_init__(self) -> None:
        if self.timeout_ms <= 0 or self.max_output_bytes <= 0 or self.kill_grace_ms < 0:
            raise ValueError("执行限制要求 timeout/output 为正数且 grace 非负")


@dataclass(frozen=True)
class ExecEvent:
    """执行过程事件，按 seq 有序；kind 限定为 spawn/stdout/stderr/exit/cancel-requested。"""

    seq: int
    at: str
    kind: Literal["spawn", "stdout", "stderr", "exit", "cancel-requested"]
    run_id: RunId
    text: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class RunRecord:
    """运行的只读快照记录，用于对外传递（与控制库里的 Run 解耦）。"""

    run_id: RunId
    session_id: SessionId
    program_id: ProgramId
    input_batch_id: str
    data_revision_id: DataRevisionId
    status: RunStatus
    started_at: str
    code_commit: CodeCommit | None = None
    finished_at: str | None = None
