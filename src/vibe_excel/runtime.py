"""会话的执行基础设施：锁、快照、干净重跑。

三件事各管一摊：锁（acquire_lock / SessionLock）保证同一会话同刻只有一个写者；
快照（snapshot_database / restore_database）把数据版本按内容寻址落盘，可反复还原；
干净重跑（clean_rerun）在独立 git worktree 里用干净代码跑入口，产出带清单的制品。
这里不判断业务成功与否，只负责“跑得干净、可复核”。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sqlite3
import time
from collections.abc import Callable, Mapping
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
    """会话工作区各子目录的绝对路径集合。"""

    root: Path
    work: Path
    code: Path
    pi_sessions: Path
    agent: Path
    runs: Path
    data_revisions: Path


def workspace_paths(store_root: str | Path, session_id: SessionId) -> WorkspacePaths:
    """由 store 根与会话 id 拼出工作区路径；只算路径，不建目录。"""
    store = Path(store_root).resolve()
    root = store / "sessions" / session_id
    return WorkspacePaths(
        root, root / "work", root / "code", root / "sessions", root / "agent", root / "runs",
        store / "data-revisions",
    )


def ensure_workspace(store_root: str | Path, session_id: SessionId) -> WorkspacePaths:
    """创建工作区骨架目录（幂等）。"""
    paths = workspace_paths(store_root, session_id)
    for path in (
        paths.root,
        paths.work,
        # 导入接口把文件写进 work/inputs：骨架里少了它，新建的项目一打开就报
        # 「读不到输入批次目录」，而那本该是最正常的空状态。
        paths.work / "inputs",
        paths.pi_sessions,
        paths.agent,
        paths.runs,
        paths.data_revisions,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def session_lock_path(store_root: str | Path, session_id: SessionId) -> Path:
    """会话锁文件的位置；锁建在会话根，覆盖整个会话的写操作。"""
    return workspace_paths(store_root, session_id).root / ".session.lock"



@dataclass(frozen=True)
class AcquireOptions:
    """拿锁的等待与陈旧判定参数：stale_ms 判定僵死锁，timeout_ms=0 表示不等待直接报占用。"""

    stale_ms: int = 30 * 60 * 1000
    timeout_ms: int = 0
    poll_ms: int = 200

    def __post_init__(self) -> None:
        if self.stale_ms <= 0 or self.timeout_ms < 0 or self.poll_ms <= 0:
            raise ValueError("锁限制要求 stale/poll 为正数且 timeout 非负")


def _read_lock(path: Path) -> dict[str, object] | None:
    """读锁文件；文件缺失、半截或损坏都返回 None，让上层按“无主”处理。"""
    try:
        return require_object(json.loads(path.read_text(encoding="utf-8")), "锁文件")
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, VibeError) as error:
        logger.warning("锁文件不完整 %s: %s", path, error)
        return None


def _is_stale(path: Path, info: dict[str, object] | None, stale_ms: int) -> bool:
    """判断锁是否陈旧：持有者已死、pid 被复用，或按 at 时间戳超过 stale_ms。

    依次看：文件在但读不出内容且超过 5 秒（写锁进程多半在半途退出）；pid 非法或进程已不在；
    pid 被复用（创建时间与锁里记录不一致）；锁里有合法的 at 时间戳时，再按年龄判定。
    注意：at 缺失或非法时不会按年龄回收，只靠 pid 与复用检测，避免误抢活锁。
    """
    if info is None:
        try:
            # 文件存在却读不出 JSON：多半是抢锁进程在写盘途中退出，超过 5 秒就当作陈旧。
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
            # pid 会被复用；创建时间不同说明这把锁属于已经退出的旧进程。
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
        logger.warning("锁文件 %s 中的时间戳无效: %s", path, error)
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - timestamp).total_seconds() * 1000 > stale_ms


@dataclass
class SessionLock:
    """持锁句柄；release 只在文件里的 token 仍属于自己时才删除，避免误删别人重拿的锁。"""

    path: Path
    pid: int
    token: str
    released: bool = False

    def release(self) -> None:
        """释放锁：token 不匹配说明锁已被别人抢占，此时不删文件。"""
        if self.released:
            return
        info = _read_lock(self.path)
        if info is not None and info.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.released = True

    def __enter__(self) -> SessionLock:
        """返回自身，支持 with 语句。"""
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出 with 时释放锁。"""
        self.release()


def acquire_lock(path: str | Path, options: AcquireOptions | None = None) -> SessionLock:
    """以原子创建（open "x"）拿锁，必要时等待或抢占陈旧锁。

    写入后 fsync 落盘，避免进程刚拿到锁就崩溃留下空文件。发现陈旧锁时，先重读确认内容没变
    再删除，然后重新竞争；等待超过 timeout_ms 才报 VibeError。返回的锁一定是最新创建的。
    """
    settings = AcquireOptions() if options is None else options
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + settings.timeout_ms / 1000
    while True:
        token = str(uuid4())
        try:
            # 原子创建：open "x" 只有一个进程能成功；写满并 fsync 后才算真正持有。
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
            if _is_stale(target, info, settings.stale_ms):
                current = _read_lock(target)
                if current == info:
                    target.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise VibeError(f"会话已被占用: {target}（持有者={info}）") from None
            time.sleep(settings.poll_ms / 1000)


def with_lock_sync[T](
    path: str | Path, function: Callable[[], T], options: AcquireOptions | None = None
) -> T:
    """拿锁后同步执行回调并返回其结果。"""
    with acquire_lock(path, options):
        return function()


@dataclass(frozen=True)
class SnapshotResult:
    """快照结果：内容哈希、落盘路径与字节数。"""

    sha256: str
    path: str
    bytes: int


@dataclass(frozen=True)
class RestoreResult:
    """还原结果：目标文件字节数。"""

    bytes: int


def snapshot_database(db_path: str | Path, revisions_dir: str | Path) -> SnapshotResult:
    """把数据库压成内容寻址的只读快照。

    用 VACUUM INTO 而不是复制文件：它生成一致的单文件副本，不带走 -wal/-shm，也不会把
    写入中的页拷成半截；先写临时名，算完哈希再 rename 到 {sha256}.db，天然去重。
    已存在同名快照时重新校验哈希，发现损坏直接报错而不是覆盖。
    """
    source = Path(db_path)
    if not source.is_file():
        raise VibeError(f"无法快照不存在的数据库: {source}")
    root = Path(revisions_dir)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".tmp-{uuid4()}.db"
    try:
        with readonly_database(source) as connection:
            # VACUUM INTO 生成一致的单文件副本，不携带 WAL/SHM。
            connection.execute("VACUUM INTO ?", (str(temporary.resolve()),))
        sha256 = sha256_file(temporary)
        final = root / f"{sha256}.db"
        if final.exists():
            if sha256_file(final) != sha256:
                raise VibeError(f"内容寻址快照已损坏: {final}")
        else:
            # 先写临时名再 rename，避免别人看到写了一半的快照。
            temporary.replace(final)
        return SnapshotResult(sha256, str(final.resolve()), final.stat().st_size)
    finally:
        temporary.unlink(missing_ok=True)


def restore_database(revision_path: str | Path, target_path: str | Path) -> RestoreResult:
    """把快照还原成可用的数据库。

    先复制到暂存文件并做 PRAGMA quick_check，再删掉目标旁的 -wal/-shm，最后原子替换；
    先删后换是为了不让旧 WAL 覆盖新快照内容。
    """
    source = Path(revision_path)
    if not source.is_file():
        raise VibeError(f"快照不存在: {source}")
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{target.name}.restore-{uuid4()}")
    try:
        shutil.copyfile(source, staging)
        with readonly_database(staging) as probe:
            verdicts = [str(row[0]) for row in probe.execute("PRAGMA quick_check")]
            if verdicts != ["ok"]:
                raise VibeError(f"快照完整性检查失败: {verdicts}")
        # 旧 WAL/SHM 若留着，打开时会把旧内容回放到新快照上。
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        staging.replace(target)
        return RestoreResult(target.stat().st_size)
    except sqlite3.DatabaseError as error:
        raise VibeError(f"快照不是有效的 SQLite 数据库: {source}: {error}") from error
    finally:
        staging.unlink(missing_ok=True)


@dataclass(frozen=True)
class CleanRunEntry:
    """干净重跑的入口命令（解释器 + 参数）。"""

    command: str
    args: list[str]


@dataclass(frozen=True)
class CleanRunOptions:
    """干净重跑的入参；outputs 是要从工作树复制出来的声明输出，env 是额外环境变量。"""

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
    """干净重跑的结果：进程结果、工作目录、制品目录、清单与摘要。"""

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
    """在独立 git worktree 里按指定提交干净重跑，并收集声明输出。

    worktree 让运行与用户编辑隔离：跑的是 commit 里的代码，不是工作区里的改动。环境只给
    最小集合（加 as_of），避免调用者的环境变量改变结果。进程结束后无论成败都移除工作树；
    移除失败不掩盖运行结果，只把错误写进制品目录。退出码原样返回（超时/取消由 run_process
    映射为 124/130）。
    """
    root = Path(options.run_root)
    root.mkdir(parents=True, exist_ok=True)
    name = f"clean-{uuid4()}"
    work = root / name
    artifacts = root / f"{name}-artifacts"
    artifacts.mkdir()
    # 每次运行一个独立 worktree：并发运行互不污染，也不碰用户工作区。
    git.git(options.repo_path, ["worktree", "add", "--detach", str(work.resolve()), options.commit])
    environment = dict(options.env or {})
    if options.as_of_date is not None:
        # 只额外注入 as_of：其余环境继承最小集合，保证可复现。
        environment["VIBE_EXCEL_AS_OF"] = options.as_of_date
    try:
        process: ProcessResult = run_process(
            [options.entry.command, *options.entry.args], cwd=work, env=environment,
            limits=ExecLimits(options.timeout_ms, options.max_output_bytes, options.kill_grace_ms),
        )
        # 跳过 .git：worktree 的 git 元数据不属于运行产物。
        files = dir_digests(work, skip_git=True)
        outputs: list[Digest] = []
        missing: list[str] = []
        for declared in options.outputs:
            source = contained_path(work, declared)
            if not source.exists():
                missing.append(declared)
                continue
            if not source.is_file():
                raise VibeError(f"声明的输出必须是文件: {declared}")
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
        # 清理工作树；失败只记录，不影响已经得到的运行结果。
        try:
            git.remove_worktree(options.repo_path, work.resolve())
        except VibeError as error:
            logger.error("工作树清理失败: %s", error)
            (artifacts / "worktree-cleanup-error.txt").write_text(str(error) + "\n", encoding="utf-8")
