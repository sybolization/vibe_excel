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
    return status in TERMINAL_RUN_STATUSES


@dataclass(frozen=True)
class ExecLimits:
    timeout_ms: int = 15 * 60 * 1000
    max_output_bytes: int = 8 * 1024 * 1024
    kill_grace_ms: int = 5000

    def __post_init__(self) -> None:
        if self.timeout_ms <= 0 or self.max_output_bytes <= 0 or self.kill_grace_ms < 0:
            raise ValueError("Execution limits require positive timeout/output and nonnegative grace")


DEFAULT_EXEC_LIMITS = ExecLimits()


@dataclass(frozen=True)
class ExecEvent:
    seq: int
    at: str
    kind: Literal["spawn", "stdout", "stderr", "exit", "cancel-requested"]
    run_id: RunId
    text: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class RunRecord:
    run_id: RunId
    session_id: SessionId
    program_id: ProgramId
    input_batch_id: str
    data_revision_id: DataRevisionId
    status: RunStatus
    started_at: str
    code_commit: CodeCommit | None = None
    finished_at: str | None = None
