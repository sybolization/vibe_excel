"""控制数据库：程序、会话、运行、数据版本、制品与发布的记录层。

层次关系：Program（代码仓库与 main 分支）下开 Session（一个 draft 分支工作树）；Session 里的
每次执行登记为 Run，Run 绑定当时的代码提交与一个 DataRevision（数据版本）；Run 产出的文件是
Artifact；只有通过干净重跑的 Session 才能 Publish（把草稿合并回 main）。时间戳与哈希都写进
SQLite 控制库，业务数据本身存放在内容寻址的快照里。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal
from uuid import uuid4

from vibe_excel import git
from vibe_excel.contracts import (
    CodeCommit,
    DataRevisionId,
    ProgramId,
    RunId,
    RunStatus,
    SessionId,
    TerminalStatus,
    is_terminal,
)
from vibe_excel.util import VibeError, now

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, repo_path TEXT NOT NULL,
  main_branch TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES programs(id), name TEXT,
  draft_branch TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','archived')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  program_id TEXT NOT NULL REFERENCES programs(id), code_commit TEXT,
  input_batch_id TEXT NOT NULL, data_revision_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','cancelled')),
  started_at TEXT NOT NULL, finished_at TEXT, exit_code INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS data_revisions (
  id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES programs(id),
  session_id TEXT NOT NULL REFERENCES sessions(id), sha256 TEXT NOT NULL UNIQUE,
  path TEXT NOT NULL, bytes INTEGER NOT NULL, parent_id TEXT, created_at TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
  path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publishes (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  program_id TEXT NOT NULL REFERENCES programs(id), commit_sha TEXT NOT NULL,
  at TEXT NOT NULL, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_program ON sessions(program_id);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);
CREATE INDEX IF NOT EXISTS idx_revisions_session ON data_revisions(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
"""


def open_control_db(path: str | Path) -> sqlite3.Connection:
    """打开并初始化控制库：WAL、外键、schema 版本与向后兼容的细化迁移。"""
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        # WAL 允许读写并发，崩溃后恢复也更快。
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        existing = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        version = 0 if existing is None else int(existing["value"])
        # 只允许旧库升级；新库被旧代码打开会写坏，直接拒绝。
        if version > SCHEMA_VERSION:
            raise VibeError(
                f"控制数据库 schema 版本 {version} 高于支持的 {SCHEMA_VERSION}"
            )
        connection.executescript(SCHEMA)
        with connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
            # 老库没有 base_commit：补列保持兼容，旧会话视为无基线。
            if "base_commit" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN base_commit TEXT")
            connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return connection
    except BaseException:
        connection.close()
        raise


@dataclass(frozen=True)
class Program:
    """一个程序：一个 git 仓库加一条 main 分支，是所有会话的共同上游。"""

    id: ProgramId
    name: str
    repo_path: str
    main_branch: str
    created_at: str


@dataclass(frozen=True)
class Session:
    """一个会话：程序之下的一条 draft 分支及其实体工作树，是编辑与运行的单位。"""

    id: SessionId
    program_id: ProgramId
    name: str | None
    draft_branch: str
    worktree_path: str
    base_commit: CodeCommit | None  # 开会话时的 main HEAD，作为发布基线
    status: Literal["active", "archived"]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Run:
    """一次运行：会话里某次执行，绑定当时的代码提交、输入批次与数据版本。"""

    id: RunId
    session_id: SessionId
    program_id: ProgramId
    code_commit: CodeCommit | None
    input_batch_id: str  # 指向输入批次
    data_revision_id: DataRevisionId  # 绑定已登记的数据版本
    status: RunStatus
    started_at: str
    finished_at: str | None
    exit_code: int | None
    note: str | None


@dataclass(frozen=True)
class DataRevision:
    """一个数据版本：按内容寻址落盘的数据库快照，可被运行引用与还原。"""

    id: DataRevisionId
    program_id: ProgramId
    session_id: SessionId
    sha256: str
    path: str
    bytes: int
    parent_id: DataRevisionId | None  # 上一个数据版本，形成版本链
    created_at: str
    note: str | None


@dataclass(frozen=True)
class ArtifactInput:
    """登记制品时的入参：种类、路径、哈希与字节数。"""

    kind: str
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Artifact:
    """某次运行产出的文件记录。"""

    id: str
    run_id: RunId
    kind: str
    path: str
    sha256: str
    bytes: int
    created_at: str


@dataclass(frozen=True)
class PublishRecord:
    """一次发布记录：把哪个会话的草稿合并成了哪个提交。"""

    id: str
    session_id: SessionId
    program_id: ProgramId
    commit_sha: str
    at: str
    note: str | None


@dataclass(frozen=True)
class PublishResult:
    """发布结果：新产生的合并提交与对应记录。"""

    commit: CodeCommit
    record: PublishRecord


@dataclass(frozen=True)
class RestorableNode:
    """可复现的最小现场：代码提交 + 数据版本 + 输入批次 + 运行 id。"""

    code_commit: CodeCommit
    data_revision_id: DataRevisionId
    input_batch_id: str
    run_id: RunId


@dataclass(frozen=True)
class RecoveryView:
    """恢复视图：一次查出会话、程序、草稿 HEAD、运行历史、数据版本与可恢复节点。"""

    session: Session
    program: Program
    draft_head: CodeCommit | None
    runs: list[Run]
    data_revisions: list[DataRevision]
    latest_run: Run | None
    artifacts_of_latest_run: list[Artifact]
    restorable_node: RestorableNode | None


def _optional_text(value: object) -> str | None:
    """把可空列统一转成 str 或 None。"""
    return None if value is None else str(value)


def _program(row: sqlite3.Row) -> Program:
    """把 programs 行转成 Program。"""
    return Program(
        ProgramId(str(row["id"])), str(row["name"]), str(row["repo_path"]),
        str(row["main_branch"]), str(row["created_at"]),
    )


def _run(row: sqlite3.Row) -> Run:
    """把 runs 行转成 Run；未知状态视为库损坏并报错。"""
    status: RunStatus
    match row["status"]:
        case "pending" | "running" | "succeeded" | "failed" | "cancelled" as value:
            status = value
        case value:
            raise VibeError(f"未知的已存储运行状态: {value}")
    return Run(
        RunId(str(row["id"])), SessionId(str(row["session_id"])), ProgramId(str(row["program_id"])),
        None if row["code_commit"] is None else CodeCommit(str(row["code_commit"])),
        str(row["input_batch_id"]), DataRevisionId(str(row["data_revision_id"])), status,
        str(row["started_at"]), _optional_text(row["finished_at"]),
        None if row["exit_code"] is None else int(row["exit_code"]), _optional_text(row["note"]),
    )


def _revision(row: sqlite3.Row) -> DataRevision:
    """把 data_revisions 行转成 DataRevision。"""
    return DataRevision(
        DataRevisionId(str(row["id"])), ProgramId(str(row["program_id"])),
        SessionId(str(row["session_id"])), str(row["sha256"]), str(row["path"]), int(row["bytes"]),
        None if row["parent_id"] is None else DataRevisionId(str(row["parent_id"])),
        str(row["created_at"]), _optional_text(row["note"]),
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    """把 artifacts 行转成 Artifact。"""
    return Artifact(
        str(row["id"]), RunId(str(row["run_id"])), str(row["kind"]), str(row["path"]),
        str(row["sha256"]), int(row["bytes"]), str(row["created_at"]),
    )


class SessionStore:
    """控制库的读写入口；一个实例持有一条 SQLite 连接，不是线程安全的。"""

    def __init__(self, store_root: str | Path) -> None:
        """打开（必要时创建）store 根下的 control.db。"""
        self.store_root = Path(store_root).resolve()
        self.store_root.mkdir(parents=True, exist_ok=True)
        self.db = open_control_db(self.store_root / "control.db")

    @classmethod
    def open(cls, store_root: str | Path) -> SessionStore:
        """构造 SessionStore 的类方法入口。"""
        return cls(store_root)

    def close(self) -> None:
        """关闭控制库连接。"""
        self.db.close()

    def __enter__(self) -> SessionStore:
        """支持 with 语句，进入时返回自身。"""
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出 with 时关闭连接。"""
        self.close()

    def program_repo_path(self, program_id: ProgramId) -> Path:
        """程序仓库目录：store 根下 program-repos/<id>。"""
        return self.store_root / "program-repos" / program_id

    def create_program(self, name: str) -> Program:
        """创建程序：初始化 git 仓库并登记到控制库。"""
        if not name.strip():
            raise VibeError("程序名称不能为空")
        identifier = ProgramId(str(uuid4()))
        repo = self.program_repo_path(identifier)
        git.init_repo(repo)
        record = Program(identifier, name, str(repo), "main", now())
        with self.db:
            self.db.execute(
                "INSERT INTO programs(id,name,repo_path,main_branch,created_at) VALUES(?,?,?,?,?)",
                (record.id, record.name, record.repo_path, record.main_branch, record.created_at),
            )
        return record

    def get_program(self, identifier: ProgramId) -> Program | None:
        """按 id 查程序；不存在返回 None。"""
        row = self.db.execute("SELECT * FROM programs WHERE id=?", (identifier,)).fetchone()
        return None if row is None else _program(row)

    def list_programs(self) -> list[Program]:
        """按创建时间列出全部程序。"""
        return [_program(row) for row in self.db.execute("SELECT * FROM programs ORDER BY created_at,id")]

    def _session(self, row: sqlite3.Row) -> Session:
        """把 sessions 行转成 Session，并补出由路径推导的 worktree_path。"""
        status: Literal["active", "archived"]
        match row["status"]:
            case "active" | "archived" as value:
                status = value
            case value:
                raise VibeError(f"未知的已存储会话状态: {value}")
        return Session(
            SessionId(str(row["id"])), ProgramId(str(row["program_id"])), _optional_text(row["name"]),
            str(row["draft_branch"]), str(self.store_root / "sessions" / str(row["id"]) / "code"),
            None if row["base_commit"] is None else CodeCommit(str(row["base_commit"])),
            status, str(row["created_at"]), str(row["updated_at"]),
        )

    def get_session(self, identifier: SessionId) -> Session | None:
        """按 id 查会话；不存在返回 None。"""
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
        return None if row is None else self._session(row)

    def _require_session(self, identifier: SessionId) -> Session:
        """取会话，不存在直接报错；供写操作统一做前置检查。"""
        session = self.get_session(identifier)
        if session is None:
            raise VibeError(f"会话不存在: {identifier}")
        return session

    def create_session(self, program_id: ProgramId, name: str | None = None) -> Session:
        """为程序开一个会话：从 main HEAD 建 draft 分支与独立工作树，记录发布基线。"""
        program = self.get_program(program_id)
        if program is None:
            raise VibeError(f"程序不存在: {program_id}")
        identifier = SessionId(str(uuid4()))
        branch = f"draft/{identifier[:8]}"
        root = self.store_root / "sessions" / identifier
        root.mkdir(parents=True, exist_ok=True)
        # 记下基线：发布时用它判断 main 是否仍包含这个起点。
        baseline = git.head_sha(program.repo_path, program.main_branch)
        git.add_worktree(program.repo_path, root / "code", branch, program.main_branch)
        at = now()
        with self.db:
            self.db.execute(
                "INSERT INTO sessions"
                "(id,program_id,name,draft_branch,base_commit,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'active',?,?)",
                (identifier, program_id, name, branch, baseline, at, at),
            )
        return self._require_session(identifier)

    def list_sessions(self, program_id: ProgramId | None = None) -> list[Session]:
        """按创建时间列出会话，可按程序过滤。"""
        if program_id is None:
            rows = self.db.execute("SELECT * FROM sessions ORDER BY created_at,id")
        else:
            rows = self.db.execute(
                "SELECT * FROM sessions WHERE program_id=? ORDER BY created_at,id", (program_id,)
            )
        return [self._session(row) for row in rows]

    def _touch_session(self, identifier: SessionId) -> None:
        """更新会话的 updated_at；调用方负责事务。"""
        self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), identifier))

    def archive_session(self, identifier: SessionId) -> None:
        """归档会话：先移除工作树，再把状态改为 archived。"""
        session = self._require_session(identifier)
        git.remove_worktree(self.program_repo_path(session.program_id), session.worktree_path)
        with self.db:
            self.db.execute(
                "UPDATE sessions SET status='archived',updated_at=? WHERE id=?", (now(), identifier)
            )

    def commit_draft(self, identifier: SessionId, message: str) -> CodeCommit | None:
        """把会话工作树里的改动提交到 draft 分支；无改动返回 None。"""
        session = self._require_session(identifier)
        if session.status != "active":
            raise VibeError(f"无法编辑已归档的会话: {identifier}")
        commit = git.commit_all(session.worktree_path, message)
        if commit is not None:
            with self.db:
                self._touch_session(identifier)
        return None if commit is None else CodeCommit(commit)

    def draft_head(self, identifier: SessionId) -> CodeCommit | None:
        """draft 分支当前提交；分支不存在返回 None。"""
        session = self._require_session(identifier)
        commit = git.head_sha(
            self.program_repo_path(session.program_id), session.draft_branch
        )
        return None if commit is None else CodeCommit(commit)

    def begin_run(
        self, identifier: SessionId, *, input_batch_id: str, data_revision_id: DataRevisionId
    ) -> Run:
        """登记一次运行：要求会话 active、输入批次非空、数据版本已登记。

        code_commit 取调用时的 draft HEAD，保证运行记录与当时的代码绑定。
        """
        session = self._require_session(identifier)
        if session.status != "active":
            raise VibeError("无法运行已归档的会话")
        if not input_batch_id or self.get_data_revision(data_revision_id) is None:
            raise VibeError("运行需要输入批次与已登记的数据版本")
        run_id = RunId(str(uuid4()))
        commit = self.draft_head(identifier)
        with self.db:
            self.db.execute(
                "INSERT INTO runs(id,session_id,program_id,code_commit,input_batch_id,"
                "data_revision_id,status,started_at) VALUES(?,?,?,?,?,?,'running',?)",
                (run_id, identifier, session.program_id, commit, input_batch_id, data_revision_id, now()),
            )
            self._touch_session(identifier)
        run = self.get_run(run_id)
        if run is None:
            raise VibeError("运行记录插入未持久化")
        return run

    def get_run(self, identifier: RunId) -> Run | None:
        """按 id 查运行；不存在返回 None。"""
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone()
        return None if row is None else _run(row)

    def list_runs(self, identifier: SessionId) -> list[Run]:
        """按开始时间倒序列出会话的运行。"""
        return [
            _run(row) for row in self.db.execute(
                "SELECT * FROM runs WHERE session_id=? ORDER BY started_at DESC,rowid DESC",
                (identifier,),
            )
        ]

    def finish_run(
        self, identifier: RunId, *, status: TerminalStatus, exit_code: int | None = None,
        note: str | None = None, artifacts: Sequence[ArtifactInput] = (),
    ) -> Run:
        """把运行置为终态并登记制品；已处于终态时拒绝重复收尾。

        用带状态条件的 UPDATE 做乐观并发控制：rowcount 不是 1 就说明被别的写者抢先收尾。
        """
        if not is_terminal(status):
            raise VibeError(f"不是终态: {status}")
        with self.db:
            existing = self.get_run(identifier)
            if existing is None:
                raise VibeError(f"运行不存在: {identifier}")
            if is_terminal(existing.status):
                raise VibeError(f"运行 {identifier} 已处于终态")
            # 条件更新：只有仍处于非终态的行才算这次收尾成功。
            changed = self.db.execute(
                "UPDATE runs SET status=?,finished_at=?,exit_code=?,note=? "
                "WHERE id=? AND status IN ('pending','running')",
                (status, now(), exit_code, note, identifier),
            )
            if changed.rowcount != 1:
                raise VibeError(f"运行 {identifier} 并发地变为终态")
            for item in artifacts:
                self._insert_artifact(identifier, item)
            self._touch_session(existing.session_id)
        run = self.get_run(identifier)
        if run is None:
            raise VibeError("已完成的运行记录缺失")
        return run

    def register_data_revision(
        self, *, program_id: ProgramId, session_id: SessionId, sha256: str,
        path: str | Path, bytes: int, parent_id: DataRevisionId | None = None,
        note: str | None = None,
    ) -> DataRevision:
        """登记数据版本；同 sha256 已存在时直接复用（内容寻址）。"""
        # 内容寻址：同一份数据只登记一次，后来的运行都引用它。
        existing = self.db.execute(
            "SELECT * FROM data_revisions WHERE sha256=?", (sha256,)
        ).fetchone()
        if existing is not None:
            return _revision(existing)
        identifier = DataRevisionId(str(uuid4()))
        with self.db:
            self.db.execute(
                "INSERT INTO data_revisions"
                "(id,program_id,session_id,sha256,path,bytes,parent_id,created_at,note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (identifier, program_id, session_id, sha256, str(path), bytes, parent_id, now(), note),
            )
        revision = self.get_data_revision(identifier)
        if revision is None:
            raise VibeError("数据版本插入未持久化")
        return revision

    def get_data_revision(self, identifier: DataRevisionId) -> DataRevision | None:
        """按 id 查数据版本；不存在返回 None。"""
        row = self.db.execute("SELECT * FROM data_revisions WHERE id=?", (identifier,)).fetchone()
        return None if row is None else _revision(row)

    def list_data_revisions(self, identifier: SessionId) -> list[DataRevision]:
        """按创建时间倒序列出会话的数据版本。"""
        return [
            _revision(row) for row in self.db.execute(
                "SELECT * FROM data_revisions WHERE session_id=? ORDER BY created_at DESC,rowid DESC",
                (identifier,),
            )
        ]

    def _insert_artifact(self, run_id: RunId, item: ArtifactInput) -> str:
        """插入一条制品记录，返回 id；调用方负责事务。"""
        identifier = str(uuid4())
        self.db.execute(
            "INSERT INTO artifacts(id,run_id,kind,path,sha256,bytes,created_at) VALUES(?,?,?,?,?,?,?)",
            (identifier, run_id, item.kind, item.path, item.sha256, item.bytes, now()),
        )
        return identifier

    def add_artifact(self, run_id: RunId, item: ArtifactInput) -> Artifact:
        """单独追加一条制品。"""
        with self.db:
            identifier = self._insert_artifact(run_id, item)
        row = self.db.execute("SELECT * FROM artifacts WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise VibeError("制品插入未持久化")
        return _artifact(row)

    def list_artifacts(self, identifier: RunId) -> list[Artifact]:
        """按创建时间列出某次运行的制品。"""
        return [
            _artifact(row) for row in self.db.execute(
                "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at,rowid", (identifier,)
            )
        ]

    def publish(
        self, identifier: SessionId, *, clean_run_passed: bool,
        evidence: str | None = None, note: str | None = None,
        expected_commit: str | None = None,
    ) -> PublishResult:
        """把通过干净重跑的草稿合并回 main 并登记发布记录。

        前置条件：干净重跑通过、会话 active、草稿领先 main、main 未被重写、主工作树干净；
        调用方传入 expected_commit 时，还要求当前草稿 HEAD 与证据提交一致（证据未过期）。
        任一不满足就拒绝，而不是留下半次发布。
        """
        # 发布门槛：没有干净重跑证据不允许合并。
        if not clean_run_passed:
            raise VibeError("干净重跑尚未通过：拒绝发布")
        session = self._require_session(identifier)
        program = self.get_program(session.program_id)
        if program is None:
            raise VibeError(f"程序不存在: {session.program_id}")
        if session.status != "active":
            raise VibeError("无法发布已归档的会话")
        # 证据对应某个 HEAD；HEAD 变了说明证据对不上这次内容。
        if expected_commit is not None and self.draft_head(identifier) != expected_commit:
            raise VibeError("发布证据已过期：草稿 HEAD 已变化")
        if git.commits_ahead(program.repo_path, session.draft_branch, program.main_branch) == 0:
            raise VibeError("草稿未领先 main；没有可发布的内容")
        # main 被 rebase/reset 过：基线不再可信，拒绝合并。
        if session.base_commit and not git.is_ancestor(
            program.repo_path, session.base_commit, program.main_branch
        ):
            raise VibeError("main 历史被重写：会话基线不再是其祖先")
        if not git.is_clean(program.repo_path):
            raise VibeError("主工作树有未提交变更：拒绝合并")
        commit = CodeCommit(git.merge_into(program.repo_path, session.draft_branch, program.main_branch))
        record = PublishRecord(
            str(uuid4()), identifier, session.program_id, commit, now(),
            evidence if evidence is not None else note,
        )
        with self.db:
            self.db.execute(
                "INSERT INTO publishes(id,session_id,program_id,commit_sha,at,note) VALUES(?,?,?,?,?,?)",
                (record.id, record.session_id, record.program_id, record.commit_sha, record.at, record.note),
            )
            self._touch_session(identifier)
        return PublishResult(commit, record)

    def list_publishes(self, identifier: SessionId) -> list[PublishRecord]:
        """按时间倒序列出会话的发布记录。"""
        return [
            PublishRecord(
                str(row["id"]), SessionId(str(row["session_id"])), ProgramId(str(row["program_id"])),
                str(row["commit_sha"]), str(row["at"]), _optional_text(row["note"]),
            ) for row in self.db.execute(
                "SELECT * FROM publishes WHERE session_id=? ORDER BY at DESC,rowid DESC", (identifier,)
            )
        ]

    def recover_session(self, identifier: SessionId) -> RecoveryView:
        """汇总恢复视图：会话、程序、draft HEAD、运行历史、数据版本、最近运行的制品与可复现节点。

        restorable_node 取最近一次 succeeded 且记录了代码提交的运行，用于按“代码 + 数据版本”重建现场。
        """
        session = self._require_session(identifier)
        program = self.get_program(session.program_id)
        if program is None:
            raise VibeError(f"程序不存在: {session.program_id}")
        runs = self.list_runs(identifier)
        # list_runs 已按时间倒序，第一条即最近一次运行。
        latest = runs[0] if runs else None
        # 只挑成功且有代码提交的运行：缺任一项都无法复现。
        restorable = next(
            (run for run in runs if run.status == "succeeded" and run.code_commit is not None), None
        )
        node = None
        if restorable is not None and restorable.code_commit is not None:
            node = RestorableNode(
                restorable.code_commit, restorable.data_revision_id,
                restorable.input_batch_id, restorable.id,
            )
        return RecoveryView(
            session, program, self.draft_head(identifier), runs, self.list_data_revisions(identifier),
            latest, [] if latest is None else self.list_artifacts(latest.id), node,
        )
