from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

import psutil

from vibe_excel import git
from vibe_excel.contracts import ExecLimits, SessionId
from vibe_excel.process import ProcessResult, run_process
from vibe_excel.util import (
    Digest,
    VibeError,
    contained_path,
    dir_digests,
    now,
    readonly_database,
    require_object,
    sha256_file,
    write_json,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    work: Path
    code: Path
    pi_sessions: Path
    agent: Path
    runs: Path
    data_revisions: Path


def workspace_paths(store_root: str | Path, session_id: SessionId) -> WorkspacePaths:
    store = Path(store_root).resolve()
    root = store / "sessions" / session_id
    return WorkspacePaths(
        root, root / "work", root / "code", root / "sessions", root / "agent", root / "runs",
        store / "data-revisions",
    )


def ensure_workspace(store_root: str | Path, session_id: SessionId) -> WorkspacePaths:
    paths = workspace_paths(store_root, session_id)
    for path in (
        paths.root, paths.work, paths.pi_sessions, paths.agent, paths.runs, paths.data_revisions
    ):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def session_lock_path(store_root: str | Path, session_id: SessionId) -> Path:
    return workspace_paths(store_root, session_id).root / ".session.lock"


def run_directory(store_root: str | Path, session_id: SessionId, run_id: str) -> Path:
    return workspace_paths(store_root, session_id).runs / run_id


@dataclass(frozen=True)
class AcquireOptions:
    stale_ms: int = 30 * 60 * 1000
    timeout_ms: int = 0
    poll_ms: int = 200

    def __post_init__(self) -> None:
        if self.stale_ms <= 0 or self.timeout_ms < 0 or self.poll_ms <= 0:
            raise ValueError("Lock limits require positive stale/poll and nonnegative timeout")


def _read_lock(path: Path) -> dict[str, object] | None:
    try:
        return require_object(json.loads(path.read_text(encoding="utf-8")), "lock file")
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, VibeError) as error:
        logger.warning("Incomplete lock file %s: %s", path, error)
        return None


def _is_stale(path: Path, info: dict[str, object] | None, stale_ms: int) -> bool:
    if info is None:
        try:
            return time.time() - path.stat().st_mtime > 5
        except FileNotFoundError:
            return True
    pid = info.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    if not psutil.pid_exists(pid):
        return True
    created = info.get("created")
    if isinstance(created, (int, float)):
        try:
            if psutil.Process(pid).create_time() != created:
                return True
        except psutil.NoSuchProcess:
            return True
    at = info.get("at")
    if not isinstance(at, str):
        return False
    try:
        timestamp = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError as error:
        logger.warning("Invalid lock timestamp in %s: %s", path, error)
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - timestamp).total_seconds() * 1000 > stale_ms


@dataclass
class SessionLock:
    path: Path
    pid: int
    token: str
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        info = _read_lock(self.path)
        if info is not None and info.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.released = True

    def __enter__(self) -> SessionLock:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def acquire_lock(path: str | Path, options: AcquireOptions = AcquireOptions()) -> SessionLock:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + options.timeout_ms / 1000
    while True:
        token = str(uuid4())
        try:
            with target.open("x", encoding="utf-8") as output:
                json.dump(
                    {
                        "pid": os.getpid(), "at": now(), "host": socket.gethostname(),
                        "token": token, "created": psutil.Process().create_time(),
                    }, output,
                )
                output.flush()
                os.fsync(output.fileno())
            return SessionLock(target, os.getpid(), token)
        except FileExistsError:
            info = _read_lock(target)
            if _is_stale(target, info, options.stale_ms):
                current = _read_lock(target)
                if current == info:
                    target.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise VibeError(f"Session is occupied: {target} (holder={info})") from None
            time.sleep(options.poll_ms / 1000)


@contextlib.contextmanager
def with_session_lock(
    store_root: str | Path, session_id: SessionId, options: AcquireOptions = AcquireOptions()
) -> Iterator[SessionLock]:
    with acquire_lock(session_lock_path(store_root, session_id), options) as lock:
        yield lock


def with_lock_sync[T](
    path: str | Path, function: Callable[[], T], options: AcquireOptions = AcquireOptions()
) -> T:
    with acquire_lock(path, options):
        return function()


@dataclass(frozen=True)
class SnapshotResult:
    sha256: str
    path: str
    bytes: int


@dataclass(frozen=True)
class RestoreResult:
    bytes: int


def snapshot_database(db_path: str | Path, revisions_dir: str | Path) -> SnapshotResult:
    source = Path(db_path)
    if not source.is_file():
        raise VibeError(f"Cannot snapshot missing database: {source}")
    root = Path(revisions_dir)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".tmp-{uuid4()}.db"
    try:
        with readonly_database(source) as connection:
            connection.execute("VACUUM INTO ?", (str(temporary.resolve()),))
        sha256 = sha256_file(temporary)
        final = root / f"{sha256}.db"
        if final.exists():
            if sha256_file(final) != sha256:
                raise VibeError(f"Content-addressed snapshot is corrupt: {final}")
        else:
            temporary.replace(final)
        return SnapshotResult(sha256, str(final.resolve()), final.stat().st_size)
    finally:
        temporary.unlink(missing_ok=True)


def restore_database(revision_path: str | Path, target_path: str | Path) -> RestoreResult:
    source = Path(revision_path)
    if not source.is_file():
        raise VibeError(f"Snapshot does not exist: {source}")
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{target.name}.restore-{uuid4()}")
    try:
        shutil.copyfile(source, staging)
        with readonly_database(staging) as probe:
            verdicts = [str(row[0]) for row in probe.execute("PRAGMA quick_check")]
            if verdicts != ["ok"]:
                raise VibeError(f"Snapshot integrity check failed: {verdicts}")
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        staging.replace(target)
        return RestoreResult(target.stat().st_size)
    except sqlite3.DatabaseError as error:
        raise VibeError(f"Snapshot is not a valid SQLite database: {source}: {error}") from error
    finally:
        staging.unlink(missing_ok=True)


@dataclass(frozen=True)
class CleanRunEntry:
    command: str
    args: list[str]


@dataclass(frozen=True)
class CleanRunOptions:
    repo_path: str | Path
    commit: str
    run_root: str | Path
    entry: CleanRunEntry
    outputs: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None
    timeout_ms: int = 5 * 60 * 1000
    as_of_date: str | None = None
    max_output_bytes: int = 8 * 1024 * 1024
    kill_grace_ms: int = 5000


@dataclass(frozen=True)
class CleanRunResult:
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    work_dir: str
    artifacts_dir: str
    outputs: list[Digest]
    files: list[Digest]
    manifest_path: str
    elapsed_ms: int
    cancelled: bool
    truncated: bool


def clean_rerun(options: CleanRunOptions) -> CleanRunResult:
    root = Path(options.run_root)
    root.mkdir(parents=True, exist_ok=True)
    name = f"clean-{uuid4()}"
    work = root / name
    artifacts = root / f"{name}-artifacts"
    artifacts.mkdir()
    git.git(options.repo_path, ["worktree", "add", "--detach", str(work.resolve()), options.commit])
    environment = dict(options.env or {})
    if options.as_of_date is not None:
        environment["VIBE_EXCEL_AS_OF"] = options.as_of_date
    try:
        process: ProcessResult = run_process(
            [options.entry.command, *options.entry.args], cwd=work, env=environment,
            limits=ExecLimits(options.timeout_ms, options.max_output_bytes, options.kill_grace_ms),
        )
        files = dir_digests(work, skip_git=True)
        outputs: list[Digest] = []
        missing: list[str] = []
        for declared in options.outputs:
            source = contained_path(work, declared)
            if not source.exists():
                missing.append(declared)
                continue
            if not source.is_file():
                raise VibeError(f"Declared output must be a file: {declared}")
            target = contained_path(artifacts, declared)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            outputs.append(Digest(declared, sha256_file(source), source.stat().st_size))
        manifest = artifacts / "manifest.json"
        write_json(
            manifest,
            {
                "commit": options.commit, "entry": options.entry, "asOfDate": options.as_of_date,
                "exitCode": process.exit_code, "timedOut": process.timed_out,
                "cancelled": process.cancelled, "truncated": process.truncated,
                "files": files, "outputs": outputs, "missingOutputs": missing, "executedAt": now(),
            },
        )
        return CleanRunResult(
            process.exit_code, process.timed_out, process.stdout, process.stderr,
            str(work.resolve()), str(artifacts.resolve()), outputs, files, str(manifest.resolve()),
            process.elapsed_ms, process.cancelled, process.truncated,
        )
    finally:
        try:
            git.remove_worktree(options.repo_path, work.resolve())
        except VibeError as error:
            logger.error("Worktree cleanup failed: %s", error)
            (artifacts / "worktree-cleanup-error.txt").write_text(str(error) + "\n", encoding="utf-8")


def diff_run_files(a: list[Digest], b: list[Digest]) -> dict[str, object]:
    left = {item.path: item.sha256 for item in a}
    right = {item.path: item.sha256 for item in b}
    only_a = sorted(left.keys() - right.keys())
    only_b = sorted(right.keys() - left.keys())
    changed = sorted(name for name in left.keys() & right.keys() if left[name] != right[name])
    return {
        "onlyInA": only_a, "onlyInB": only_b, "changed": changed,
        "identical": not only_a and not only_b and not changed,
    }
