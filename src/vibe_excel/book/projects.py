"""项目索引：把一个 store 里的项目汇总成首页要的一行行数据。

与 collect_book 的分工：那边要跑完整采集（流程图、输入、对话），重；
首页必须秒开，所以这里只做 control.db 上的聚合查询，不碰 runs 目录。
某个项目缺会话或缺运行都不影响它出现在列表里，缺的部分如实留空。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..util import VibeError, readonly_database
from ._values import as_text
from .types import ProjectSummary, RunBrief, SessionSummary

__all__ = ["collect_projects", "collectProjects", "page_name", "pageName"]


def page_name(project_id: str) -> str:
    """项目页的文件名。索引与 CLI 必须用同一套规则，所以只在这里写一次。"""
    return f"p-{project_id[:8]}.html"


def _rows(db: sqlite3.Connection, sql: str) -> list[dict[str, object]]:
    """把 sqlite3.Row 转成普通字典，便于后面按字段取值。"""
    return [{key: row[key] for key in row.keys()} for row in db.execute(sql)]


def _count(value: object) -> int:
    """COUNT(*) 回来的是 int，但查询结果是 object；不是整数就当 0，不硬转。"""
    return value if isinstance(value, int) else 0


def _identical(note: str | None) -> bool | None:
    """identical 只在 runs.note 里有标记，与 collect_book 用同一条规则。

    没有标记一律返回 None（未知），不能当成「不一致」——那是两件事。
    """
    if note is None or "identical=true" not in note:
        return None
    return True


def collect_projects(store: str | Path) -> list[ProjectSummary]:
    """按最近活跃排序返回全部项目；store 里没有项目时返回空列表。"""
    path = Path(store) / "control.db"
    if not path.is_file():
        raise VibeError(f"找不到 control.db：{path}")
    try:
        with readonly_database(path) as db:
            programs = _rows(db, "SELECT id, name, created_at FROM programs")
            sessions = _rows(
                db,
                "SELECT id, program_id, name, status, draft_branch, updated_at"
                " FROM sessions ORDER BY updated_at DESC",
            )
            runs = _rows(
                db,
                "SELECT id, program_id, status, started_at, finished_at, note"
                " FROM runs ORDER BY started_at DESC",
            )
            published = _rows(
                db, "SELECT program_id, COUNT(*) AS n FROM publishes GROUP BY program_id"
            )
    except (sqlite3.Error, OSError) as error:
        raise VibeError(f"无法读取 control.db：{path}：{error}") from error

    sessions_by_program: dict[str, list[SessionSummary]] = {}
    for row in sessions:
        owner = as_text(row.get("program_id")) or ""
        sessions_by_program.setdefault(owner, []).append(
            {
                "id": as_text(row.get("id")) or "",
                "name": as_text(row.get("name")) or "—",
                "status": as_text(row.get("status")) or "—",
                "draftBranch": as_text(row.get("draft_branch")) or "—",
                "updatedAt": as_text(row.get("updated_at")) or "",
            }
        )

    last_run: dict[str, RunBrief] = {}
    run_count: dict[str, int] = {}
    # runs 已按 started_at 倒序，每个项目第一次遇到的那条就是最近的。
    for row in runs:
        owner = as_text(row.get("program_id")) or ""
        run_count[owner] = run_count.get(owner, 0) + 1
        if owner in last_run:
            continue
        identifier = as_text(row.get("id"))
        if identifier is None:
            continue
        last_run[owner] = {
            "id": identifier,
            "status": as_text(row.get("status")) or "—",
            "at": as_text(row.get("finished_at")) or as_text(row.get("started_at")),
            "identical": _identical(as_text(row.get("note"))),
        }

    publish_count = {
        as_text(row.get("program_id")) or "": _count(row.get("n")) for row in published
    }

    summaries: list[ProjectSummary] = []
    for row in programs:
        identifier = as_text(row.get("id")) or ""
        owned = sessions_by_program.get(identifier, [])
        # 优先进活动会话；没有活动会话就退到最近更新的那个，总比点不进去强。
        active = next((item for item in owned if item["status"] == "active"), owned[0] if owned else None)
        summaries.append(
            {
                "id": identifier,
                "name": as_text(row.get("name")) or identifier,
                "createdAt": as_text(row.get("created_at")) or "",
                "sessions": owned,
                "activeSessionId": None if active is None else active["id"],
                "runCount": run_count.get(identifier, 0),
                "publishCount": publish_count.get(identifier, 0),
                "lastRun": last_run.get(identifier),
            }
        )

    summaries.sort(
        key=lambda item: (
            (item["lastRun"]["at"] if item["lastRun"] is not None else None) or item["createdAt"]
        ),
        reverse=True,
    )
    return summaries


# 与 collect_book / render_book 一样，保留一份 camelCase 别名给上层脚本用。
collectProjects = collect_projects
pageName = page_name
