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
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        existing = connection.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        version = 0 if existing is None else int(existing["value"])
        if version > SCHEMA_VERSION:
            raise VibeError(
                f"Control database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        connection.executescript(SCHEMA)
        with connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
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
    id: ProgramId
    name: str
    repo_path: str
    main_branch: str
    created_at: str


@dataclass(frozen=True)
class Session:
    id: SessionId
    program_id: ProgramId
    name: str | None
    draft_branch: str
    worktree_path: str
    base_commit: CodeCommit | None
    status: Literal["active", "archived"]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Run:
    id: RunId
    session_id: SessionId
    program_id: ProgramId
    code_commit: CodeCommit | None
    input_batch_id: str
    data_revision_id: DataRevisionId
    status: RunStatus
    started_at: str
    finished_at: str | None
    exit_code: int | None
    note: str | None


@dataclass(frozen=True)
class DataRevision:
    id: DataRevisionId
    program_id: ProgramId
    session_id: SessionId
    sha256: str
    path: str
    bytes: int
    parent_id: DataRevisionId | None
    created_at: str
    note: str | None


@dataclass(frozen=True)
class ArtifactInput:
    kind: str
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Artifact:
    id: str
    run_id: RunId
    kind: str
    path: str
    sha256: str
    bytes: int
    created_at: str


@dataclass(frozen=True)
class PublishRecord:
    id: str
    session_id: SessionId
    program_id: ProgramId
    commit_sha: str
    at: str
    note: str | None


@dataclass(frozen=True)
class PublishResult:
    commit: CodeCommit
    record: PublishRecord


@dataclass(frozen=True)
class RestorableNode:
    code_commit: CodeCommit
    data_revision_id: DataRevisionId
    input_batch_id: str
    run_id: RunId


@dataclass(frozen=True)
class RecoveryView:
    session: Session
    program: Program
    draft_head: CodeCommit | None
    runs: list[Run]
    data_revisions: list[DataRevision]
    latest_run: Run | None
    artifacts_of_latest_run: list[Artifact]
    restorable_node: RestorableNode | None


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _program(row: sqlite3.Row) -> Program:
    return Program(
        ProgramId(str(row["id"])), str(row["name"]), str(row["repo_path"]),
        str(row["main_branch"]), str(row["created_at"]),
    )


def _run(row: sqlite3.Row) -> Run:
    status: RunStatus
    match row["status"]:
        case "pending" | "running" | "succeeded" | "failed" | "cancelled" as value:
            status = value
        case value:
            raise VibeError(f"Unknown stored run status: {value}")
    return Run(
        RunId(str(row["id"])), SessionId(str(row["session_id"])), ProgramId(str(row["program_id"])),
        None if row["code_commit"] is None else CodeCommit(str(row["code_commit"])),
        str(row["input_batch_id"]), DataRevisionId(str(row["data_revision_id"])), status,
        str(row["started_at"]), _optional_text(row["finished_at"]),
        None if row["exit_code"] is None else int(row["exit_code"]), _optional_text(row["note"]),
    )


def _revision(row: sqlite3.Row) -> DataRevision:
    return DataRevision(
        DataRevisionId(str(row["id"])), ProgramId(str(row["program_id"])),
        SessionId(str(row["session_id"])), str(row["sha256"]), str(row["path"]), int(row["bytes"]),
        None if row["parent_id"] is None else DataRevisionId(str(row["parent_id"])),
        str(row["created_at"]), _optional_text(row["note"]),
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        str(row["id"]), RunId(str(row["run_id"])), str(row["kind"]), str(row["path"]),
        str(row["sha256"]), int(row["bytes"]), str(row["created_at"]),
    )


class SessionStore:
    def __init__(self, store_root: str | Path) -> None:
        self.store_root = Path(store_root).resolve()
        self.store_root.mkdir(parents=True, exist_ok=True)
        self.db = open_control_db(self.store_root / "control.db")

    @classmethod
    def open(cls, store_root: str | Path) -> SessionStore:
        return cls(store_root)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def program_repo_path(self, program_id: ProgramId) -> Path:
        return self.store_root / "program-repos" / program_id

    def create_program(self, name: str) -> Program:
        if not name.strip():
            raise VibeError("Program name must not be empty")
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
        row = self.db.execute("SELECT * FROM programs WHERE id=?", (identifier,)).fetchone()
        return None if row is None else _program(row)

    def list_programs(self) -> list[Program]:
        return [_program(row) for row in self.db.execute("SELECT * FROM programs ORDER BY created_at,id")]

    def _session(self, row: sqlite3.Row) -> Session:
        status: Literal["active", "archived"]
        match row["status"]:
            case "active" | "archived" as value:
                status = value
            case value:
                raise VibeError(f"Unknown stored session status: {value}")
        return Session(
            SessionId(str(row["id"])), ProgramId(str(row["program_id"])), _optional_text(row["name"]),
            str(row["draft_branch"]), str(self.store_root / "sessions" / str(row["id"]) / "code"),
            None if row["base_commit"] is None else CodeCommit(str(row["base_commit"])),
            status, str(row["created_at"]), str(row["updated_at"]),
        )

    def get_session(self, identifier: SessionId) -> Session | None:
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
        return None if row is None else self._session(row)

    def _require_session(self, identifier: SessionId) -> Session:
        session = self.get_session(identifier)
        if session is None:
            raise VibeError(f"Session does not exist: {identifier}")
        return session

    def create_session(self, program_id: ProgramId, name: str | None = None) -> Session:
        program = self.get_program(program_id)
        if program is None:
            raise VibeError(f"Program does not exist: {program_id}")
        identifier = SessionId(str(uuid4()))
        branch = f"draft/{identifier[:8]}"
        root = self.store_root / "sessions" / identifier
        root.mkdir(parents=True, exist_ok=True)
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
        if program_id is None:
            rows = self.db.execute("SELECT * FROM sessions ORDER BY created_at,id")
        else:
            rows = self.db.execute(
                "SELECT * FROM sessions WHERE program_id=? ORDER BY created_at,id", (program_id,)
            )
        return [self._session(row) for row in rows]

    def _touch_session(self, identifier: SessionId) -> None:
        self.db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), identifier))

    def archive_session(self, identifier: SessionId) -> None:
        session = self._require_session(identifier)
        git.remove_worktree(self.program_repo_path(session.program_id), session.worktree_path)
        with self.db:
            self.db.execute(
                "UPDATE sessions SET status='archived',updated_at=? WHERE id=?", (now(), identifier)
            )

    def commit_draft(self, identifier: SessionId, message: str) -> CodeCommit | None:
        session = self._require_session(identifier)
        if session.status != "active":
            raise VibeError(f"Cannot edit archived session: {identifier}")
        commit = git.commit_all(session.worktree_path, message)
        if commit is not None:
            with self.db:
                self._touch_session(identifier)
        return None if commit is None else CodeCommit(commit)

    def draft_head(self, identifier: SessionId) -> CodeCommit | None:
        session = self._require_session(identifier)
        commit = git.head_sha(
            self.program_repo_path(session.program_id), session.draft_branch
        )
        return None if commit is None else CodeCommit(commit)

    def begin_run(
        self, identifier: SessionId, *, input_batch_id: str, data_revision_id: DataRevisionId
    ) -> Run:
        session = self._require_session(identifier)
        if session.status != "active":
            raise VibeError("Cannot run an archived session")
        if not input_batch_id or self.get_data_revision(data_revision_id) is None:
            raise VibeError("Run requires an input batch and a registered data revision")
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
            raise VibeError("Run insert did not persist")
        return run

    def get_run(self, identifier: RunId) -> Run | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone()
        return None if row is None else _run(row)

    def list_runs(self, identifier: SessionId) -> list[Run]:
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
        if not is_terminal(status):
            raise VibeError(f"Not a terminal status: {status}")
        with self.db:
            existing = self.get_run(identifier)
            if existing is None:
                raise VibeError(f"Run does not exist: {identifier}")
            if is_terminal(existing.status):
                raise VibeError(f"Run {identifier} is already terminal")
            changed = self.db.execute(
                "UPDATE runs SET status=?,finished_at=?,exit_code=?,note=? "
                "WHERE id=? AND status IN ('pending','running')",
                (status, now(), exit_code, note, identifier),
            )
            if changed.rowcount != 1:
                raise VibeError(f"Run {identifier} became terminal concurrently")
            for item in artifacts:
                self._insert_artifact(identifier, item)
            self._touch_session(existing.session_id)
        run = self.get_run(identifier)
        if run is None:
            raise VibeError("Finished run is missing")
        return run

    def register_data_revision(
        self, *, program_id: ProgramId, session_id: SessionId, sha256: str,
        path: str | Path, bytes: int, parent_id: DataRevisionId | None = None,
        note: str | None = None,
    ) -> DataRevision:
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
            raise VibeError("Data revision insert did not persist")
        return revision

    def get_data_revision(self, identifier: DataRevisionId) -> DataRevision | None:
        row = self.db.execute("SELECT * FROM data_revisions WHERE id=?", (identifier,)).fetchone()
        return None if row is None else _revision(row)

    def list_data_revisions(self, identifier: SessionId) -> list[DataRevision]:
        return [
            _revision(row) for row in self.db.execute(
                "SELECT * FROM data_revisions WHERE session_id=? ORDER BY created_at DESC,rowid DESC",
                (identifier,),
            )
        ]

    def _insert_artifact(self, run_id: RunId, item: ArtifactInput) -> str:
        identifier = str(uuid4())
        self.db.execute(
            "INSERT INTO artifacts(id,run_id,kind,path,sha256,bytes,created_at) VALUES(?,?,?,?,?,?,?)",
            (identifier, run_id, item.kind, item.path, item.sha256, item.bytes, now()),
        )
        return identifier

    def add_artifact(self, run_id: RunId, item: ArtifactInput) -> Artifact:
        with self.db:
            identifier = self._insert_artifact(run_id, item)
        row = self.db.execute("SELECT * FROM artifacts WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise VibeError("Artifact insert did not persist")
        return _artifact(row)

    def list_artifacts(self, identifier: RunId) -> list[Artifact]:
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
        if not clean_run_passed:
            raise VibeError("Clean rerun has not passed: refusing publication")
        session = self._require_session(identifier)
        program = self.get_program(session.program_id)
        if program is None:
            raise VibeError(f"Program does not exist: {session.program_id}")
        if session.status != "active":
            raise VibeError("Cannot publish an archived session")
        if expected_commit is not None and self.draft_head(identifier) != expected_commit:
            raise VibeError("Publish evidence is stale: draft HEAD changed")
        if git.commits_ahead(program.repo_path, session.draft_branch, program.main_branch) == 0:
            raise VibeError("Draft does not lead main; there is nothing to publish")
        if session.base_commit and not git.is_ancestor(
            program.repo_path, session.base_commit, program.main_branch
        ):
            raise VibeError("main history was rewritten: session baseline is no longer its ancestor")
        if not git.is_clean(program.repo_path):
            raise VibeError("Main worktree is dirty: refusing merge")
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
        return [
            PublishRecord(
                str(row["id"]), SessionId(str(row["session_id"])), ProgramId(str(row["program_id"])),
                str(row["commit_sha"]), str(row["at"]), _optional_text(row["note"]),
            ) for row in self.db.execute(
                "SELECT * FROM publishes WHERE session_id=? ORDER BY at DESC,rowid DESC", (identifier,)
            )
        ]

    def recover_session(self, identifier: SessionId) -> RecoveryView:
        session = self._require_session(identifier)
        program = self.get_program(session.program_id)
        if program is None:
            raise VibeError(f"Program does not exist: {session.program_id}")
        runs = self.list_runs(identifier)
        latest = runs[0] if runs else None
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
