"""项目索引上的写操作：新建与归档。

**新建**只写两样东西：``programs`` 表里一行记录，以及 ``program-repos/<id>`` 下的
空仓库；不碰会话、输入与运行。新项目停在「还没开过会话」的状态，页面上如实说明。
名字在落盘前就校验完（空、路径字符、长度、同 store 重名），非法名字不会留下半个目录。

**归档**与 ``inputs_edit`` 同一套约定：

1. **没有真删**：项目仓库与它的全部会话目录移进 ``<store>/.archive/projects/``，
   归档目录里留一份 ``packet.json``（项目名、会话/运行/发布数、理由、原路径、归档路径）。
   将来要找回项目，或者 agent 想知道「这个项目为什么不见了」，只要读这个包。
2. **先落盘再改登记**：目录全部移完、包写完，才往 control.db 写归档表；任何一步失败
   都把已经移走的目录放回原处，绝不留下「登记里有、磁盘上没有」这种半成品。
3. **事件留痕**：每次归档往 ``<store>/project-events.jsonl`` 追加一条。

归档表由本模块在第一次归档时用 ``CREATE TABLE IF NOT EXISTS`` 建立。旧 store 没有
这张表也必须能打开页面，所以读的一方（``collect_projects``）把「表不存在」当作
「没有归档」，而不是把整个索引报错。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..session import Program, SessionStore
from ..util import VibeError, now, readonly_database

__all__ = [
    "ARCHIVE_ROOT",
    "EVENTS_NAME",
    "FORBIDDEN_NAME_CHARS",
    "MAX_PROJECT_NAME",
    "PACKET_NAME",
    "PROJECTS_NAME",
    "TABLE_NAME",
    "archive_project",
    "create_project",
]

ARCHIVE_ROOT = ".archive"
PROJECTS_NAME = "projects"
PACKET_NAME = "packet.json"
EVENTS_NAME = "project-events.jsonl"
TABLE_NAME = "archived_projects"

# 这些字符既是 Windows 的非法文件名，也会让项目名看起来像一条路径。
FORBIDDEN_NAME_CHARS = '/\\:*?"<>|'
MAX_PROJECT_NAME = 64

ARCHIVED_TABLE = """
CREATE TABLE IF NOT EXISTS archived_projects (
  program_id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  reason TEXT,
  dir TEXT NOT NULL
)
"""


def _clean_name(raw: object) -> str:
    """校验项目名；返回去掉首尾空白后的名字。

    在任何写盘动作之前调用：不合法的名字不建目录、不写库。
    """
    name = ("" if raw is None else str(raw)).strip()
    if not name:
        raise VibeError("项目名不能为空")
    bad = next((char for char in name if char in FORBIDDEN_NAME_CHARS), None)
    if bad is not None:
        # 报出具体是哪个字符：用户往往看不出全角与半角、斜杠与反斜杠的区别。
        raise VibeError(f'项目名不能包含 / \\ : * ? " < > | 这些字符（发现「{bad}」）')
    if name in (".", ".."):
        raise VibeError("项目名不能是 . 或 ..")
    if len(name) > MAX_PROJECT_NAME:
        raise VibeError(f"项目名最长 {MAX_PROJECT_NAME} 个字符，当前 {len(name)} 个")
    return name


def _duplicate_name(store: Path, name: str) -> tuple[str, bool] | None:
    """同 store 内按名字找重复（忽略大小写），连已归档的一起算。

    归档只把项目移出索引，programs 里那一行还在；而 ``vibectl setup`` 是按名字
    复用程序的，若允许同名，它可能落到归档的那个项目上，新项目就永远开不出会话。
    """
    path = store / "control.db"
    if not path.is_file():
        raise VibeError(f"不是 store（缺 control.db）：{store}")
    try:
        with readonly_database(path) as db:
            rows = [
                (str(row["id"]), str(row["name"]))
                for row in db.execute("SELECT id, name FROM programs")
            ]
            archived: set[str] = set()
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE_NAME,)
            ).fetchone() is not None:
                archived = {
                    str(row["program_id"])
                    for row in db.execute("SELECT program_id FROM archived_projects")
                }
    except sqlite3.Error as error:
        raise VibeError(f"无法读取 control.db：{path}：{error}") from error
    wanted = name.casefold()
    for identifier, existing in rows:
        if existing.casefold() == wanted:
            return existing, identifier in archived
    return None


def create_project(store: str | Path, name: str) -> Program:
    """在 store 里建一个空项目：只有空仓库与 programs 记录，没有会话。

    返回新项目；索引页每次请求都会重读 control.db，所以建完不需要再补别的东西。
    """
    root = Path(store).resolve()
    clean = _clean_name(name)
    duplicate = _duplicate_name(root, clean)
    if duplicate is not None:
        existing, archived = duplicate
        suffix = "，它已归档" if archived else ""
        raise VibeError(f"这个 store 里已有同名项目「{existing}」（不区分大小写{suffix}）")
    with SessionStore.open(root) as db:
        return db.create_program(clean)


@dataclass(frozen=True)
class _ProjectFacts:
    """归档前从 control.db 读到的事实；目录按它来搬。"""

    name: str
    session_ids: list[str]
    runs: int
    publishes: int


def _count(row: sqlite3.Row | None) -> int:
    """COUNT(*) 回来的是 int；取不到就按 0，不硬转字符串。"""
    return 0 if row is None else int(row[0])


def _clean_id(value: object) -> str:
    """只接受单段目录名：项目 id 来自 HTTP，不能让它借 ``..`` 逃出 store。"""
    text = "" if value is None else str(value).strip()
    if not text or text in (".", "..") or "/" in text or "\\" in text or Path(text).name != text:
        raise VibeError(f"项目 id 不合法：{value!r}")
    return text


def _facts(store: Path, program_id: str) -> _ProjectFacts:
    """读项目名与计数，并在这里挡住「已经归档」的重复操作。"""
    path = store / "control.db"
    if not path.is_file():
        raise VibeError(f"不是 store（缺 control.db）：{store}")
    try:
        with readonly_database(path) as db:
            row = db.execute("SELECT name FROM programs WHERE id=?", (program_id,)).fetchone()
            if row is None:
                raise VibeError(f"项目不存在：{program_id}")
            name = str(row["name"])
            archived = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE_NAME,)
            ).fetchone()
            if archived is not None and db.execute(
                "SELECT 1 FROM archived_projects WHERE program_id=?", (program_id,)
            ).fetchone() is not None:
                raise VibeError(f"项目已经归档：{name}")
            session_ids = [
                _clean_id(item["id"])
                for item in db.execute(
                    "SELECT id FROM sessions WHERE program_id=? ORDER BY created_at, id",
                    (program_id,),
                )
            ]
            runs = _count(
                db.execute("SELECT COUNT(*) FROM runs WHERE program_id=?", (program_id,)).fetchone()
            )
            publishes = _count(
                db.execute(
                    "SELECT COUNT(*) FROM publishes WHERE program_id=?", (program_id,)
                ).fetchone()
            )
    except sqlite3.Error as error:
        raise VibeError(f"无法读取 control.db：{path}：{error}") from error
    return _ProjectFacts(name, session_ids, runs, publishes)


def _stamp() -> str:
    """归档目录名用本地时间到秒，与输入归档保持同一种读法。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _make_archive_dir(store: Path, program_id: str) -> Path:
    """在建目录这一步就把重名挡掉：同一项目不能被归档两次。"""
    parent = store / ARCHIVE_ROOT / PROJECTS_NAME
    parent.mkdir(parents=True, exist_ok=True)
    stem = f"{_stamp()}-{program_id[:8]}"
    candidate = parent / stem
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{stem}-{suffix}"
        suffix += 1
    candidate.mkdir()
    return candidate


def _restore_moves(moves: list[tuple[Path, Path]]) -> str:
    """把已经移走的目录搬回原处；返回搬不动的那些（空串表示全部回滚成功）。"""
    problems: list[str] = []
    for source, destination in reversed(moves):
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
        except OSError as error:
            problems.append(f"{destination} -> {source}：{error}")
    return "；".join(problems)


def _discard_archive(archive: Path) -> None:
    """回滚成功后清掉本次建的空壳；只删空目录，绝不碰搬回去的东西。"""
    if not archive.exists():
        return
    for child in sorted(archive.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass
    # 只 rmdir 空目录：.archive 里如果已有输入归档，这里不会动它。
    for directory in (archive, archive.parent, archive.parent.parent):
        try:
            directory.rmdir()
        except OSError:
            pass


def _write_packet(
    store: Path,
    archive: Path,
    *,
    program_id: str,
    facts: _ProjectFacts,
    at: str,
    reason: str,
    original_repo: Path,
    original_sessions: list[Path],
    moved_sessions: list[str],
    repo_archived: bool,
) -> Path:
    """包放在归档目录根上：先看 packet.json，再看旁边的 program-repo 与 sessions。"""
    payload = {
        "schema": "vibe.projects/1",
        "kind": "archive",
        "at": at,
        "programId": program_id,
        "name": facts.name,
        "counts": {
            "sessions": len(facts.session_ids),
            "runs": facts.runs,
            "publishes": facts.publishes,
        },
        "reason": reason,
        "original": {
            "store": str(store),
            "programRepo": str(original_repo),
            "sessions": [str(item) for item in original_sessions],
        },
        "archive": {
            "dir": str(archive),
            # 仓库本来就不在磁盘上时不要编一个归档路径，如实写 null。
            "programRepo": str(archive / "program-repo") if repo_archived else None,
            "sessions": [str(archive / "sessions" / item) for item in moved_sessions],
        },
        "movedSessions": moved_sessions,
    }
    packet = archive / PACKET_NAME
    packet.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return packet


def _record_archive(
    path: Path, program_id: str, *, at: str, reason: str, archive_dir: Path
) -> None:
    """往 control.db 记一行；建表与插入在同一个事务里，失败不留半个登记。"""
    try:
        # mode=rw：库在这之前必须存在，免得并发删库时在 archive 流程里新建一个空库。
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
        try:
            with connection:
                connection.execute(ARCHIVED_TABLE)
                connection.execute(
                    "INSERT INTO archived_projects(program_id, at, reason, dir) VALUES(?,?,?,?)",
                    (program_id, at, reason, str(archive_dir)),
                )
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise VibeError(f"写入 control.db 归档登记失败：{error}") from error


def _append_event(store: Path, event: dict[str, object]) -> None:
    """追加一条流水；与输入事件一样，一行一条，坏行由读的一方跳过。"""
    with (store / EVENTS_NAME).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def archive_project(store: str | Path, program_id: str, *, reason: str = "") -> dict[str, object]:
    """归档一个项目：搬目录、写包、记事件、登记，任一失败都回滚。

    返回事件本身；服务端只需要把它塞进响应，页面不需要再读一遍 control.db。
    """
    root = Path(store).resolve()
    identifier = _clean_id(program_id)
    facts = _facts(root, identifier)

    repo_source = root / "program-repos" / identifier
    session_sources = [
        (session_id, root / "sessions" / session_id) for session_id in facts.session_ids
    ]
    repo_exists = repo_source.is_dir()
    movable_sessions = [(key, path) for key, path in session_sources if path.is_dir()]
    if not repo_exists and not movable_sessions:
        raise VibeError(f"项目目录不存在，无法归档：{facts.name}")

    at = now()
    archive = _make_archive_dir(root, identifier)
    moves: list[tuple[Path, Path]] = []
    moved_sessions: list[str] = []
    try:
        if repo_exists:
            destination = archive / "program-repo"
            shutil.move(str(repo_source), str(destination))
            moves.append((repo_source, destination))
        if movable_sessions:
            sessions_dir = archive / "sessions"
            sessions_dir.mkdir()
            for session_id, source in movable_sessions:
                destination = sessions_dir / session_id
                shutil.move(str(source), str(destination))
                moves.append((source, destination))
                moved_sessions.append(session_id)
    except OSError as error:
        problems = _restore_moves(moves)
        if not problems:
            _discard_archive(archive)
        if problems:
            raise VibeError(f"移动项目目录失败：{error}；回滚未完成：{problems}") from error
        raise VibeError(f"移动项目目录失败：{error}") from error

    try:
        packet = _write_packet(
            root,
            archive,
            program_id=identifier,
            facts=facts,
            at=at,
            reason=reason,
            original_repo=repo_source,
            original_sessions=[path for _, path in session_sources],
            moved_sessions=moved_sessions,
            repo_archived=repo_exists,
        )
    except OSError as error:
        problems = _restore_moves(moves)
        if not problems:
            _discard_archive(archive)
        if problems:
            raise VibeError(f"写入归档包失败：{error}；回滚未完成：{problems}") from error
        raise VibeError(f"写入归档包失败：{error}") from error

    try:
        _record_archive(
            root / "control.db", identifier, at=at, reason=reason, archive_dir=archive
        )
    except VibeError as error:
        problems = _restore_moves(moves)
        if not problems:
            _discard_archive(archive)
        if problems:
            raise VibeError(f"{error}；回滚未完成：{problems}") from error
        raise

    event: dict[str, object] = {
        "kind": "archive",
        "at": at,
        "programId": identifier,
        "name": facts.name,
        "reason": reason,
        "archive": str(archive),
        "packet": str(packet),
        "counts": {
            "sessions": len(facts.session_ids),
            "runs": facts.runs,
            "publishes": facts.publishes,
        },
    }
    _append_event(root, event)
    return event
