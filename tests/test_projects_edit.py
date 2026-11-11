"""项目索引上的归档：搬目录、写包、记事件、登记，失败要能回滚。

这里盯的是「不留半成品」：control.db 说已归档，磁盘上就必须已经移走；
反过来，登记失败时项目目录必须还在原来的位置。归档不是删除，所以包也要能读。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

import vibe_excel.book.projects_edit as projects_edit
from vibe_excel.book.index_render import render_index
from vibe_excel.book.projects import collect_projects
from vibe_excel.book.projects_edit import (
    ARCHIVE_ROOT,
    EVENTS_NAME,
    PACKET_NAME,
    PROJECTS_NAME,
    TABLE_NAME,
    archive_project,
    create_project,
    create_workspace,
)
from vibe_excel.book.serve import resolve_store
from vibe_excel.book.types import ProjectSummary
from vibe_excel.session import SessionStore
from vibe_excel.util import VibeError


def _store(
    tmp_path: Path, *, name: str = "demo-sales", with_session: bool = True
) -> tuple[Path, str, str | None]:
    """造一个带一次运行、一次发布的 store；with_session=False 时只有项目仓库。"""
    root = tmp_path / "store"
    store = SessionStore.open(root)
    try:
        program = store.create_program(name)
        session = store.create_session(program.id, "real-data") if with_session else None
    finally:
        store.close()
    session_id = None if session is None else session.id
    connection = sqlite3.connect(root / "control.db")
    try:
        if session_id is not None:
            connection.execute(
                "INSERT INTO runs"
                " (id, session_id, program_id, code_commit, input_batch_id,"
                "  data_revision_id, status, started_at, finished_at, exit_code, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "run-1", session_id, program.id, "c1", "b1", "d1",
                    "succeeded", "2026-11-11T02:00:00.000Z",
                    "2026-11-11T02:00:05.000Z", 0, "identical=true",
                ),
            )
            connection.execute(
                "INSERT INTO publishes (id, session_id, program_id, commit_sha, at, note)"
                " VALUES (?,?,?,?,?,?)",
                ("pub-1", session_id, program.id, "abc", "2026-11-11T02:00:06.000Z", None),
            )
        connection.commit()
    finally:
        connection.close()
    return root, program.id, session_id


def _empty_store(tmp_path: Path) -> Path:
    """只有 control.db、一个项目都没有的 store。"""
    root = tmp_path / "store"
    store = SessionStore.open(root)
    store.close()
    return root


def test_archive_moves_dirs_writes_packet_and_registers(tmp_path: Path) -> None:
    root, program_id, session_id = _store(tmp_path)
    assert session_id is not None
    repo = root / "program-repos" / program_id
    session = root / "sessions" / session_id
    assert repo.is_dir()
    assert session.is_dir()

    event = archive_project(root, program_id, reason="供应商换了导出结构")

    # 原位置必须已经空了，否则索引过滤掉之后文件还留在原地，等于没归档
    assert not repo.exists()
    assert not session.exists()
    archive = Path(str(event["archive"]))
    assert archive.is_dir()
    assert archive.parent == root / ARCHIVE_ROOT / PROJECTS_NAME
    assert re.fullmatch(rf"\d{{8}}-\d{{6}}-{program_id[:8]}", archive.name)
    # 归档目录里留着可读的包与原始目录
    assert (archive / "program-repo").is_dir()
    assert (archive / "sessions" / session_id).is_dir()
    packet = json.loads((archive / PACKET_NAME).read_text(encoding="utf-8"))
    assert packet["programId"] == program_id
    assert packet["name"] == "demo-sales"
    assert packet["counts"] == {"sessions": 1, "runs": 1, "publishes": 1}
    assert packet["reason"] == "供应商换了导出结构"
    assert packet["original"]["programRepo"] == str(repo)
    assert packet["original"]["sessions"] == [str(session)]
    assert packet["archive"]["dir"] == str(archive)

    connection = sqlite3.connect(root / "control.db")
    try:
        row = connection.execute(
            "SELECT at, reason, dir FROM archived_projects WHERE program_id=?", (program_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row[1] == "供应商换了导出结构"
    assert Path(row[2]).resolve() == archive.resolve()

    lines = (root / EVENTS_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["kind"] == "archive"
    assert logged["programId"] == program_id
    assert logged["packet"] == str(archive / PACKET_NAME)


def test_collect_projects_hides_archived_project(tmp_path: Path) -> None:
    root, program_id, _ = _store(tmp_path)
    store = SessionStore.open(root)
    try:
        other = store.create_program("other-project")
    finally:
        store.close()
    assert {project["name"] for project in collect_projects(root)} == {
        "demo-sales",
        "other-project",
    }

    archive_project(root, program_id)

    remaining = collect_projects(root)
    assert [project["id"] for project in remaining] == [other.id]
    # 另一个项目的仓库不能被顺手搬走
    assert (root / "program-repos" / other.id).is_dir()


def test_collect_projects_treats_missing_table_as_no_archive(tmp_path: Path) -> None:
    root, program_id, _ = _store(tmp_path)
    connection = sqlite3.connect(root / "control.db")
    try:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    # 旧 store 没有归档表：索引页照常列出，不能因为缺表就打不开
    assert TABLE_NAME not in names
    assert [project["id"] for project in collect_projects(root)] == [program_id]


def test_archive_project_without_session_only_moves_repo(tmp_path: Path) -> None:
    root, program_id, _ = _store(tmp_path, with_session=False)

    event = archive_project(root, program_id)

    archive = Path(str(event["archive"]))
    packet = json.loads((archive / PACKET_NAME).read_text(encoding="utf-8"))
    assert packet["counts"]["sessions"] == 0
    assert (archive / "program-repo").is_dir()
    assert not (archive / "sessions").exists()


def test_archive_unknown_project_raises(tmp_path: Path) -> None:
    root, _, _ = _store(tmp_path)
    with pytest.raises(VibeError, match="项目不存在"):
        archive_project(root, "00000000-0000-0000-0000-000000000000")


def test_archive_twice_raises(tmp_path: Path) -> None:
    root, program_id, _ = _store(tmp_path)
    archive_project(root, program_id, reason="第一次")
    with pytest.raises(VibeError, match="已经归档"):
        archive_project(root, program_id)


@pytest.mark.parametrize("identifier", ["", ".", "..", "a/b", "a\\b", "../evil"])
def test_archive_rejects_bad_identifiers(tmp_path: Path, identifier: str) -> None:
    root, _, _ = _store(tmp_path)
    with pytest.raises(VibeError, match="不合法"):
        archive_project(root, identifier)


def test_archive_rolls_back_when_registry_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, program_id, session_id = _store(tmp_path)
    assert session_id is not None
    repo = root / "program-repos" / program_id
    session = root / "sessions" / session_id

    def boom(*args: object, **kwargs: object) -> None:
        raise VibeError("control.db 被锁")

    monkeypatch.setattr(projects_edit, "_record_archive", boom)
    with pytest.raises(VibeError, match="control.db 被锁"):
        archive_project(root, program_id)

    # 登记没写成，目录必须回到原位；归档目录里也不能留下包
    assert repo.is_dir()
    assert session.is_dir()
    assert list((root / ARCHIVE_ROOT).rglob(PACKET_NAME)) == []
    assert not (root / EVENTS_NAME).exists()
    connection = sqlite3.connect(root / "control.db")
    try:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    assert TABLE_NAME not in names


def test_archive_rolls_back_when_packet_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, program_id, session_id = _store(tmp_path)
    assert session_id is not None
    repo = root / "program-repos" / program_id
    session = root / "sessions" / session_id

    def boom(*args: object, **kwargs: object) -> Path:
        raise OSError("模拟磁盘写满")

    monkeypatch.setattr(projects_edit, "_write_packet", boom)
    with pytest.raises(VibeError, match="写入归档包失败"):
        archive_project(root, program_id)

    assert repo.is_dir()
    assert session.is_dir()
    assert list((root / ARCHIVE_ROOT).rglob(PACKET_NAME)) == []


def test_render_index_offers_delete_only_when_server_hosted() -> None:
    project: ProjectSummary = {
        "id": "9bf30f76-f9ef-4fa4-bf0d-4da27481e9c6",
        "name": "demo-sales",
        "createdAt": "2026-11-11T00:00:00.000Z",
        "sessions": [
            {
                "id": "s-1",
                "name": "real-data",
                "status": "active",
                "draftBranch": "draft/s-1",
                "updatedAt": "2026-11-11T00:00:00.000Z",
            }
        ],
        "activeSessionId": "s-1",
        "runCount": 2,
        "publishCount": 1,
        "lastRun": None,
    }

    # build 出来的静态站点没有接口可调，不能给一个点下去只会报错的按钮
    static = render_index([project], store_label="store")
    assert 'data-act="archive"' not in static
    assert 'id="ix-note"' not in static
    assert "confirm=" not in static

    live = render_index([project], store_label="store", can_archive=True)
    assert 'data-act="archive"' in live
    assert 'data-sessions="1"' in live
    assert 'data-runs="2"' in live
    assert 'data-publishes="1"' in live
    assert 'id="ix-note"' in live


def test_create_project_adds_an_empty_project_to_the_index(tmp_path: Path) -> None:
    root = _empty_store(tmp_path)

    program = create_project(root, "  销售分析  ")

    assert program.name == "销售分析"
    assert (root / "program-repos" / program.id).is_dir()
    projects = collect_projects(root)
    assert [(item["id"], item["name"]) for item in projects] == [(program.id, "销售分析")]
    # 新建的项目是空的：没有会话、没有运行，索引要如实这么显示
    assert projects[0]["sessions"] == []
    assert projects[0]["activeSessionId"] is None
    assert projects[0]["runCount"] == 0


def test_create_project_rejects_case_insensitive_duplicates(tmp_path: Path) -> None:
    root = _empty_store(tmp_path)
    create_project(root, "Sales")

    with pytest.raises(VibeError, match="同名项目"):
        create_project(root, "  sales ")
    # 被拒的那次不能留下第二个项目
    assert len(collect_projects(root)) == 1


def test_create_project_rejects_archived_duplicate(tmp_path: Path) -> None:
    root, program_id, _ = _store(tmp_path, name="archived-one")
    archive_project(root, program_id)

    # 归档只移出索引，programs 里那行还在；setup 按名字复用程序，同名会落到它上面
    with pytest.raises(VibeError, match="已归档"):
        create_project(root, "ARCHIVED-ONE")


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("", "不能为空"),
        ("   ", "不能为空"),
        ("a/b", "不能包含"),
        ("a\\b", "不能包含"),
        ("a:b", "不能包含"),
        ("a*b", "不能包含"),
        ("a?b", "不能包含"),
        ('a"b', "不能包含"),
        ("a<b", "不能包含"),
        ("a>b", "不能包含"),
        ("a|b", "不能包含"),
        (".", "不能是 . 或 .."),
        ("..", "不能是 . 或 .."),
        ("长" * 65, "最长 64"),
    ],
)
def test_create_project_rejects_bad_names(tmp_path: Path, name: str, reason: str) -> None:
    root = _empty_store(tmp_path)

    with pytest.raises(VibeError, match=reason):
        create_project(root, name)
    # 校验在写盘之前：非法名字不该留下仓库目录
    assert not (root / "program-repos").exists()


def test_create_project_accepts_name_at_the_length_limit(tmp_path: Path) -> None:
    root = _empty_store(tmp_path)
    name = "长" * 64

    assert create_project(root, name).name == name


def test_resolve_store_only_accepts_the_stores_the_service_opened(tmp_path: Path) -> None:
    known = (tmp_path / "store").resolve()
    elsewhere = (tmp_path / "elsewhere").resolve()

    assert resolve_store([known], str(known)) == known
    with pytest.raises(VibeError, match="未知的 store"):
        resolve_store([known], str(elsewhere))
    with pytest.raises(VibeError, match="缺少 store"):
        resolve_store([known], "")


def test_create_workspace_makes_dir_and_program(tmp_path: Path) -> None:
    """新建项目 = 新建工作区：目录、control.db、空项目三样都要有。"""
    root = tmp_path / "root"
    root.mkdir()
    workspace, program = create_workspace(root, "季度分析")

    assert workspace == root / "季度分析"
    assert (workspace / "control.db").is_file()
    assert program.name == "季度分析"
    # 空项目还没有会话：页面会走「这个项目还没跑过」
    assert [item["id"] for item in collect_projects(workspace)] == [str(program.id)]


def test_create_workspace_rejects_duplicate_and_rolls_back(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    create_workspace(root, "同名")

    with pytest.raises(VibeError, match="同名的工作区"):
        create_workspace(root, "同名")

    # 名字被别的文件占着：报错，而不是把那个文件当目录用
    (root / "占位").write_text("x", encoding="utf-8")
    with pytest.raises(VibeError, match="占用"):
        create_workspace(root, "占位")

    # 非法字符在 _clean_name 那一步就被挡住
    with pytest.raises(VibeError):
        create_workspace(root, "a/b")


def test_render_index_offers_create_only_when_server_hosted() -> None:
    # build 出来的静态站点没有接口可写盘：新建入口整个不渲染
    static = render_index([], store_label="store")
    assert 'data-act="create"' not in static
    assert "/api/projects/create" not in static
    assert ">新建项目</button>" not in static

    live = render_index([], store_label="0 个项目", can_create=True)
    assert 'data-act="create"' in live
    assert ">新建项目</button>" in live
    assert "/api/projects/create" in live
    # 一个项目就是一个工作区：弹层里只问名字，不再让人选库
    assert 'class="ix-store"' not in live
    assert "store</label>" not in live
    assert 'name="store"' not in live
