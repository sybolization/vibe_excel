"""项目索引页：采集、渲染与站点模式的产物。

这里测的是「首页 → 项目页」这条路能不能自己立住：
数据库里有什么就列出什么，采不到的项目照实标出来，不 pretending。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vibe_excel.book import (
    collect_projects,
    page_name,
    render_book,
    render_index,
    run_cli,
)
from vibe_excel.session import SessionStore
from vibe_excel.util import VibeError

PROJECT_ID = "9bf30f76-f9ef-4fa4-bf0d-4da27481e9c6"


def _store_with_runs(tmp_path: Path) -> tuple[Path, str, str]:
    """造一个有两项目、两运行、一发布的 store，返回 (root, program_id, session_id)。"""
    root = tmp_path / "store"
    store = SessionStore.open(root)
    program = store.create_program("demo-sales")
    session = store.create_session(program.id, "real-data")
    store.create_program("empty-one")
    connection = sqlite3.connect(root / "control.db")
    try:
        connection.executemany(
            "INSERT INTO runs (id, session_id, program_id, code_commit, input_batch_id,"
            " data_revision_id, status, started_at, finished_at, exit_code, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "run-old", session.id, program.id, "c1", "b1", "d1",
                    "failed", "2026-11-10T01:00:00.000Z", "2026-11-10T01:00:09.000Z", 1, "boom",
                ),
                (
                    "run-new", session.id, program.id, "c2", "b2", "d2",
                    "succeeded", "2026-11-11T02:00:00.000Z",
                    "2026-11-11T02:00:05.000Z", 0, "flow=demo identical=true",
                ),
            ],
        )
        connection.execute(
            "INSERT INTO publishes (id, session_id, program_id, commit_sha, at, note)"
            " VALUES (?,?,?,?,?,?)",
            ("pub-1", session.id, program.id, "abc", "2026-11-11T02:00:06.000Z", None),
        )
        connection.commit()
    finally:
        connection.close()
    return root, program.id, session.id


def test_collect_projects_reports_counts_and_latest_run(tmp_path: Path) -> None:
    root, program_id, _ = _store_with_runs(tmp_path)
    projects = collect_projects(root)

    by_name = {project["name"]: project for project in projects}
    assert set(by_name) == {"demo-sales", "empty-one"}
    assert by_name["demo-sales"]["id"] == program_id
    assert by_name["demo-sales"]["runCount"] == 2
    assert by_name["demo-sales"]["publishCount"] == 1
    # 最近一次按 started_at 取，不是按插入顺序
    last = by_name["demo-sales"]["lastRun"]
    assert last is not None
    assert last["id"] == "run-new"
    assert last["status"] == "succeeded"
    assert last["identical"] is True
    # 没跑过的项目不能编一个 lastRun 出来
    assert by_name["empty-one"]["lastRun"] is None
    assert by_name["empty-one"]["runCount"] == 0


def test_collect_projects_marks_missing_identical_as_unknown(tmp_path: Path) -> None:
    root, program_id, session_id = _store_with_runs(tmp_path)
    connection = sqlite3.connect(root / "control.db")
    try:
        connection.execute(
            "INSERT INTO runs (id, session_id, program_id, input_batch_id, data_revision_id,"
            " status, started_at) VALUES (?,?,?,?,?,?,?)",
            ("run-late", session_id, program_id, "b3", "d3", "succeeded",
             "2026-11-12T00:00:00.000Z"),
        )
        connection.commit()
    finally:
        connection.close()

    by_name = {project["name"]: project for project in collect_projects(root)}
    # note 里没有标记时是「未知」，不能被当成「不一致」
    assert by_name["demo-sales"]["lastRun"]["identical"] is None


def test_collect_projects_requires_control_db(tmp_path: Path) -> None:
    with pytest.raises(VibeError, match="control.db"):
        collect_projects(tmp_path / "nowhere")


def _summary(name: str, project_id: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": project_id,
        "name": name,
        "createdAt": "2026-11-11T00:00:00.000Z",
        "sessions": [
            {
                "id": "s-1",
                "name": "real-data",
                "status": "active",
                "draftBranch": "draft/2f498378",
                "updatedAt": "2026-11-11T00:00:00.000Z",
            }
        ],
        "activeSessionId": "s-1",
        "runCount": 2,
        "publishCount": 1,
        "lastRun": {
            "id": "run-new",
            "status": "succeeded",
            "at": "2026-11-11T02:00:05.000Z",
            "identical": True,
        },
    }
    base.update(overrides)
    return base


def test_render_index_links_the_pages_it_was_given() -> None:
    projects = [
        _summary("demo-sales", PROJECT_ID),
        _summary("second", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", lastRun=None, runCount=0),
    ]
    html = render_index(
        projects, store_label="store", pages={PROJECT_ID: "p-9bf30f76.html"}
    )

    assert 'href="p-9bf30f76.html"' in html
    # 没出页面的项目仍然列着，但不给链接
    assert 'href="p-aaaaaaaa.html"' not in html
    assert "ix-row off" in html
    assert '@font-face{font-family:"HarmonyOS Sans SC"' in html
    assert 'url("fonts/HarmonyOS_Sans_SC_Regular.ttf")' in html
    # 首页只说「进哪个项目」，不解释字体出处
    assert "HarmonyOS Sans（未修改）" not in html


def test_render_index_escapes_project_names() -> None:
    html = render_index(
        [_summary("<script>alert(1)</script>", PROJECT_ID)], store_label="store"
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_index_empty_store_points_at_setup() -> None:
    html = render_index([], store_label="store")
    assert "还没有项目" in html
    assert "vibectl setup" in html
    assert "0 个项目" in html


def _book_view() -> dict[str, object]:
    """render_book 只碰这几个键，其余原样嵌进 JSON。"""
    return {
        "program": {"name": "demo-sales"},
        "session": {"name": "real-data", "id": "s-1", "draftBranch": "draft/s-1"},
        "graph": None,
        "inputs": None,
        "runs": [],
        "revisions": [],
        "conversations": [],
    }


def test_render_book_with_fonts_points_at_the_sidecar_files() -> None:
    html = render_book(_book_view(), fonts="fonts/", home="index.html")

    assert '@font-face{font-family:"HarmonyOS Sans SC"' in html
    assert 'url("fonts/HarmonyOS_Sans_SC_Regular.ttf")' in html
    assert 'url("fonts/HarmonyOS_Sans_SC_Medium.ttf")' in html
    assert 'data-home="index.html"' in html
    # 字体说明不往页面里塞：许可跟着 fonts/ 走，运维说明留在 README
    assert "data-font-notice" not in html


def test_render_book_carries_the_runtime_state() -> None:
    static = render_book(_book_view())
    # 静态导出没有后台：页面据此把导入/删除置灰
    assert '"mode":"static"' in static
    assert '"editable":false' in static

    live = render_book(_book_view(), runtime={"mode": "live", "editable": True})
    assert '"mode":"live"' in live
    assert '"editable":true' in live


def test_render_book_single_file_mode_has_no_fonts_or_back_link() -> None:
    html = render_book(_book_view())

    # 旧的单页模式不能悄悄改变产物：不注入字体，也不出现返回入口
    # （app.js 里有一句讲到 @font-face 的注释，所以只认真正的规则开头）
    assert "@font-face{" not in html
    assert "data-home" not in html


def test_render_book_puts_diagnostics_in_a_bottom_left_panel() -> None:
    view = _book_view()
    view["warnings"] = ["表 meta 追不到原始文件", "表 quarantine 追不到原始文件"]
    html = render_book(view)

    assert 'id="book-warnings"' in html
    # 收起态只占一个方块：原来钉在顶栏右上，会盖住主题开关
    assert 'class="warn collapse"' in html
    assert 'data-count="2"' in html
    assert "表 meta 追不到原始文件" in html
    # 位置与尺寸交给 app.css 统一管，HTML 里不再留内联定位
    assert "position:fixed;top:12px" not in html


def test_render_book_without_warnings_has_no_panel() -> None:
    # 没有诊断时不留一个空方块
    assert 'id="book-warnings"' not in render_book(_book_view())


def test_build_site_writes_index_project_page_and_fonts(tmp_path: Path) -> None:
    root, program_id, _ = _store_with_runs(tmp_path)
    out = tmp_path / "site"

    assert run_cli(["build", "--store", str(root), "--out", str(out)]) == 0

    index = out / "index.html"
    page = out / page_name(program_id)
    assert index.is_file()
    assert page.is_file()
    assert page_name(project_id=program_id) in index.read_text(encoding="utf-8")
    # 字体原样随站点走，许可文件必须跟着
    assert (out / "fonts" / "HarmonyOS_Sans_SC_Regular.ttf").is_file()
    assert (out / "fonts" / "LICENSE.txt").is_file()
    # 项目页带着返回入口和字体声明
    text = page.read_text(encoding="utf-8")
    assert 'data-home="index.html"' in text
    assert "fonts/HarmonyOS_Sans_SC_Regular.ttf" in text


def test_build_site_merges_several_stores(tmp_path: Path) -> None:
    first, program_id, _ = _store_with_runs(tmp_path)
    second = tmp_path / "second" / "store"
    SessionStore.open(second).create_program("other-project")
    out = tmp_path / "site"

    assert (
        run_cli(
            ["build", "--store", str(first), "--store", str(second), "--out", str(out)]
        )
        == 0
    )

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "demo-sales" in html
    assert "other-project" in html
    # 多个 store 时每一行都要标出来源，否则同名项目分不清
    assert "second/store" in html
    assert "2 个 store" in html
    assert (out / page_name(program_id)).is_file()


def test_single_store_rows_have_no_source_label(tmp_path: Path) -> None:
    root, _, _ = _store_with_runs(tmp_path)
    out = tmp_path / "site"

    assert run_cli(["build", "--store", str(root), "--out", str(out)]) == 0
    html = (out / "index.html").read_text(encoding="utf-8")
    # 只有一个 store 时不必每行都重复一遍来源
    assert "store · " not in html


def test_build_site_keeps_project_without_session_listed(tmp_path: Path) -> None:
    root = tmp_path / "store"
    SessionStore.open(root).create_program("no-session")
    out = tmp_path / "site"

    # 采不到页面的项目不能把整站拖下水
    assert run_cli(["build", "--store", str(root), "--out", str(out)]) == 0
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "no-session" in html
    assert "ix-row off" in html


def test_build_site_refuses_a_file_where_the_directory_should_be(tmp_path: Path) -> None:
    root, _, _ = _store_with_runs(tmp_path)
    blocker = tmp_path / "site"
    blocker.write_text("not a directory", encoding="utf-8")

    assert run_cli(["build", "--store", str(root), "--out", str(blocker)]) == 1
