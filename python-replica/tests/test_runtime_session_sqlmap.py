"""runtime / session / sqlmap 的核心回归场景。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from vibe_excel import git
from vibe_excel.runtime import (
    AcquireOptions,
    CleanRunEntry,
    CleanRunOptions,
    acquire_lock,
    clean_rerun,
    restore_database,
    snapshot_database,
)
from vibe_excel.session import SessionStore
from vibe_excel.sqlmap.build import build_lineage
from vibe_excel.sqlmap.lineage import parse_lineage
from vibe_excel.sqlmap.render import render_html
from vibe_excel.util import VibeError


def _write_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE employees(id INTEGER PRIMARY KEY, department TEXT)")
    connection.executemany(
        "INSERT INTO employees(id, department) VALUES(?,?)", [(1, "HR"), (2, "ENG")]
    )
    connection.commit()
    return connection


def test_snapshot_restore_and_content_addressing(tmp_path: Path) -> None:
    db_path = tmp_path / "hr.db"
    connection = _write_database(db_path)
    revisions = tmp_path / "revisions"
    first = snapshot_database(db_path, revisions)
    second = snapshot_database(db_path, revisions)
    assert first.path == second.path

    restored = tmp_path / "restored.db"
    restore_database(first.path, restored)
    with sqlite3.connect(restored) as probe:
        rows = probe.execute("SELECT count(*) FROM employees").fetchone()[0]
    assert rows == 2
    assert not Path(str(restored) + "-wal").exists()
    connection.close()


def test_corrupt_snapshot_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "hr.db"
    connection = _write_database(db_path)
    revisions = tmp_path / "revisions"
    snapshot = snapshot_database(db_path, revisions)
    Path(snapshot.path).write_text("not a database", encoding="utf-8")
    with pytest.raises(VibeError, match="已损坏"):
        snapshot_database(db_path, revisions)
    connection.close()


def test_lock_is_exclusive_and_releasable(tmp_path: Path) -> None:
    path = tmp_path / "session.lock"
    lock = acquire_lock(path, AcquireOptions(timeout_ms=0))
    with pytest.raises(VibeError, match="已被占用"):
        acquire_lock(path, AcquireOptions(timeout_ms=0))
    lock.release()
    again = acquire_lock(path, AcquireOptions(timeout_ms=0))
    again.release()


def test_stale_lock_from_dead_process_is_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / "session.lock"
    path.write_text('{"pid": 999999, "at": "2020-01-01T00:00:00Z"}', encoding="utf-8")
    lock = acquire_lock(path, AcquireOptions(timeout_ms=0))
    assert lock.pid > 0
    lock.release()


def test_clean_rerun_is_deterministic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    git.init_repo(repo)
    (repo / "pipeline.py").write_text(
        "import pathlib\n"
        "out = pathlib.Path('out')\n"
        "out.mkdir(exist_ok=True)\n"
        "(out / 'result.txt').write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )
    commit = git.commit_all(repo, "init")
    assert commit is not None
    options = CleanRunOptions(
        repo_path=repo,
        commit=commit,
        run_root=tmp_path / "runs",
        entry=CleanRunEntry(sys.executable, ["pipeline.py"]),
        timeout_ms=60_000,
    )
    first = clean_rerun(options)
    second = clean_rerun(options)
    assert first.exit_code == 0
    assert [(item.path, item.sha256) for item in first.files] == [
        (item.path, item.sha256) for item in second.files
    ]


def test_session_lifecycle_and_publish_guards(tmp_path: Path) -> None:
    store = SessionStore.open(tmp_path / "store")
    try:
        program = store.create_program("demo")
        session = store.create_session(program.id, "s")
        worktree = Path(session.worktree_path)
        (worktree / "pipeline.py").write_text("print('ok')\n", encoding="utf-8")
        commit = store.commit_draft(session.id, "init")
        assert commit is not None
        revision = store.register_data_revision(
            program_id=program.id,
            session_id=session.id,
            sha256="abc",
            path=tmp_path / "snapshot.db",
            bytes=1,
            note="基线",
        )
        duplicate = store.register_data_revision(
            program_id=program.id,
            session_id=session.id,
            sha256="abc",
            path=tmp_path / "other.db",
            bytes=1,
        )
        assert duplicate.id == revision.id
        run = store.begin_run(
            session.id, input_batch_id="batch-1", data_revision_id=revision.id
        )
        store.finish_run(run.id, status="succeeded", exit_code=0, note="ok")
        with pytest.raises(VibeError, match="终态"):
            store.finish_run(run.id, status="failed", exit_code=1)
        with pytest.raises(VibeError, match="拒绝发布"):
            store.publish(session.id, clean_run_passed=False)
        view = store.recover_session(session.id)
        assert view.runs[0].status == "succeeded"
    finally:
        store.close()


def _lineage(case_title: str = "员工") -> dict[str, object]:
    return {
        "version": "0.1",
        "case": {"id": "employees", "title": case_title, "provenance": "author-declared"},
        "inputs": [{"id": "employees", "source": {"file": "staff.xlsx", "sheet": "Staff"}}],
        "outputs": [
            {"id": "headcount", "columns": ["department", "count"], "artifact": "headcount.csv"}
        ],
        "columns": [
            {
                "output": "headcount",
                "name": "department",
                "formula": "",
                "from": [{"table": "employees", "column": "department"}],
            },
            {
                "output": "headcount",
                "name": "count",
                "formula": "按部门计数",
                "from": [{"table": "employees", "column": "id"}],
            },
        ],
        "filters": [],
    }


def test_lineage_uses_database_rows_and_csv_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "hr.db"
    _write_database(db_path).close()
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "headcount.csv").write_text(
        "\ufeffdepartment,count\n\"HR, \"\"lead\"\"\",3\nENG,2\n", encoding="utf-8"
    )
    built = build_lineage(parse_lineage(_lineage()), db_path, csv_dir=csv_dir)
    tables = {table["id"]: table for table in built["tables"]}
    assert tables["employees"]["rows"] == 2
    assert tables["headcount"]["rows"] == 2


def test_lineage_rejects_unknown_source_column(tmp_path: Path) -> None:
    db_path = tmp_path / "hr.db"
    _write_database(db_path).close()
    document = _lineage()
    columns = document["columns"]
    assert isinstance(columns, list)
    first = columns[0]
    assert isinstance(first, dict)
    first["from"] = [{"table": "employees", "column": "missing"}]
    with pytest.raises(VibeError, match="不存在"):
        build_lineage(document, db_path)


def test_lineage_marks_self_dependency_and_escapes_render(tmp_path: Path) -> None:
    db_path = tmp_path / "hr.db"
    _write_database(db_path).close()
    document = _lineage("</script> 注入")
    columns = document["columns"]
    assert isinstance(columns, list)
    first = columns[0]
    assert isinstance(first, dict)
    first["from"] = [{"table": "headcount", "column": "department"}]
    built = build_lineage(document, db_path)
    edges = [edge for edge in built["columns"] if edge.get("self") is True]
    assert edges
    html = render_html(built)
    assert "\\u003c/script>" in html


def test_lineage_rejects_duplicate_table_ids(tmp_path: Path) -> None:
    document = _lineage()
    inputs = document["inputs"]
    assert isinstance(inputs, list)
    inputs.append({"id": "employees", "source": {"file": "other.xlsx"}})
    with pytest.raises(VibeError, match="重复"):
        parse_lineage(document)
