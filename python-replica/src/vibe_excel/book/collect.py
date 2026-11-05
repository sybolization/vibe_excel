"""Read a store without changing it; missing optional pages carry diagnostics."""

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from ..util import VibeError, now, quote_identifier, read_json, readonly_database, require_object
from ._values import as_list, as_number, as_object, as_text, basename, first_round, strings
from .diagram import build_diagram
from .graph import build_graph
from .inputs import build_inputs
from .transcript import parse_transcript
from .types import (
    BatchFile,
    BatchView,
    BookView,
    Cell,
    CollectOptions,
    DbTableFact,
    EpisodeView,
    GraphView,
    InputsView,
    Number,
    PublishView,
    RevisionView,
    RunRecord,
    RunView,
    SourceRef,
    TableData,
    TableStat,
)


@dataclass(frozen=True)
class _Run:
    id: str
    code_commit: str | None
    revision: str | None
    status: str
    started_at: str | None
    finished_at: str | None
    note: str | None


@dataclass(frozen=True)
class _Author:
    commit: str | None
    session_file: str | None
    label: str | None


@dataclass
class _Focus:
    graph: GraphView | None
    revision: str | None
    elapsed_ms: Number | None
    kind: str
    commit: str | None
    db_path: Path | None


def _query(
    db: sqlite3.Connection, sql: str, params: tuple[str | int, ...] = ()
) -> list[dict[str, object]]:
    return [{key: row[key] for key in row.keys()} for row in db.execute(sql, params)]


def _optional_query(
    db: sqlite3.Connection,
    sql: str,
    params: tuple[str | int, ...],
    warnings: list[str],
    page: str,
) -> list[dict[str, object]]:
    try:
        return _query(db, sql, params)
    except sqlite3.Error as error:
        warnings.append(f"无法读取{page}：{error}")
        return []


def _entries(directory: Path, warnings: list[str], page: str) -> list[Path]:
    try:
        return sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as error:
        warnings.append(f"无法读取{page} {directory}：{error}")
        return []


def _optional_json(path: Path, warnings: list[str], page: str) -> dict[str, object] | None:
    try:
        return require_object(read_json(path), str(path))
    except (VibeError, OSError, UnicodeError, json.JSONDecodeError) as error:
        warnings.append(f"无法读取{page} {path}：{error}")
        return None


def read_table_stat(
    db: sqlite3.Connection, name: str, *, warnings: list[str] | None = None
) -> TableStat:
    diagnostics = warnings if warnings is not None else []
    try:
        counted = _query(db, f"SELECT COUNT(*) AS n FROM {quote_identifier(name)}")
        columns = _query(db, "SELECT name FROM pragma_table_info(?)", (name,))
        return {"rows": as_number(counted[0]["n"]) if counted else None, "columns": len(columns)}
    except sqlite3.Error as error:
        diagnostics.append(f"无法读取表「{name}」统计：{error}")
        return {"rows": None, "columns": None}


def _cell(value: object) -> Cell:
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return ",".join(str(byte) for byte in value)
    return str(value)


def sample_table(
    db: sqlite3.Connection,
    name: str,
    note: str,
    limit: int = 40,
    max_columns: int = 0,
    *,
    warnings: list[str] | None = None,
) -> TableData | None:
    if limit < 0 or max_columns < 0:
        raise VibeError("Table sample limits must be non-negative")
    diagnostics = warnings if warnings is not None else []
    try:
        info = _query(db, "SELECT name FROM pragma_table_info(?)", (name,))
        if not info:
            diagnostics.append(f"表「{name}」不存在或没有列，无法取样")
            return None
        all_columns = [column for row in info if (column := as_text(row.get("name"))) is not None]
        columns = all_columns[:max_columns] if max_columns > 0 else all_columns
        rows = _query(db, f"SELECT * FROM {quote_identifier(name)} LIMIT ?", (limit,))
        counted = _query(db, f"SELECT COUNT(*) AS n FROM {quote_identifier(name)}")
        total = as_number(counted[0]["n"]) if counted else None
        return {
            "name": name,
            "columns": columns,
            "columnTotal": len(all_columns),
            "rows": [[_cell(row.get(column)) for column in columns] for row in rows],
            "total": total if total is not None else len(rows),
            "note": note,
        }
    except sqlite3.Error as error:
        diagnostics.append(f"无法取样表「{name}」：{error}")
        return None


def _read_sources(db: sqlite3.Connection, name: str, warnings: list[str]) -> list[SourceRef]:
    try:
        columns = _query(db, "SELECT name FROM pragma_table_info(?)", (name,))
        names = {as_text(row.get("name")) for row in columns}
        if not {"_source_file", "_source_sheet"}.issubset(names):
            return []
        grouped = _query(
            db,
            f'SELECT "_source_file" AS file, "_source_sheet" AS sheet, COUNT(*) AS n '
            f"FROM {quote_identifier(name)} GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2",
        )
        sources: list[SourceRef] = []
        for row in grouped:
            file = as_text(row.get("file"))
            if file is not None:
                sources.append(
                    {
                        "file": file, "sheet": as_text(row.get("sheet")) or "—",
                        "rows": as_number(row.get("n")),
                    }
                )
        return sources
    except sqlite3.Error as error:
        warnings.append(f"无法读取表「{name}」的来源列：{error}")
        return []


def read_db_facts(db: sqlite3.Connection, *, warnings: list[str] | None = None) -> list[DbTableFact]:
    diagnostics = warnings if warnings is not None else []
    facts: list[DbTableFact] = []
    try:
        tables = _query(db, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")
    except sqlite3.Error as error:
        diagnostics.append(f"无法读取数据库表清单：{error}")
        return []
    for item in tables:
        name = as_text(item.get("name"))
        if name is None or name.startswith("sqlite_"):
            continue
        stat = read_table_stat(db, name, warnings=diagnostics)
        facts.append(
            {
                "name": name, "rows": stat["rows"], "columns": stat["columns"],
                "sources": _read_sources(db, name, diagnostics),
            }
        )
    return facts


def _read_authors(run_root: Path, warnings: list[str]) -> list[_Author]:
    records: list[_Author] = []
    for entry in _entries(run_root, warnings, "运行目录"):
        if not entry.is_dir() or not entry.name.startswith("author-"):
            continue
        doc = _optional_json(entry / "author.json", warnings, "作者记录")
        if doc is not None:
            stats = as_object(doc.get("stats"))
            records.append(
                _Author(as_text(doc.get("commit")), as_text(stats.get("sessionFile")), as_text(doc.get("label")))
            )
    return records


def _read_episodes(directory: Path, warnings: list[str]) -> list[EpisodeView]:
    episodes: list[EpisodeView] = []
    for file in _entries(directory, warnings, "对话目录"):
        if file.suffix.lower() != ".jsonl" or not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            warnings.append(f"无法读取对话 {file}：{error}")
            continue
        episode = parse_transcript(file, text, warnings=warnings)
        if episode is not None:
            episodes.append(episode)
    return sorted(episodes, key=lambda episode: episode["start"])


def _read_run_docs(run_root: Path, runs: list[_Run], warnings: list[str]) -> list[RunRecord]:
    records: list[RunRecord] = []
    for run in runs:
        doc = _optional_json(run_root / run.id / "run.json", warnings, "运行记录")
        if doc is not None:
            records.append({"id": run.id, "startedAt": run.started_at, "doc": doc})
    return records


def _read_batches(work_dir: Path, warnings: list[str]) -> list[BatchView]:
    batches: list[BatchView] = []
    for entry in _entries(work_dir / "inputs", warnings, "输入批次目录"):
        if not entry.is_dir():
            continue
        doc = _optional_json(entry / "manifest.json", warnings, "批次清单")
        if doc is None:
            continue
        raw_files = doc.get("files")
        if not isinstance(raw_files, list):
            warnings.append(f"批次 {entry.name} 的 files 必须是数组；文件清单不可用")
        files: list[BatchFile] = []
        for index, raw in enumerate(as_list(raw_files)):
            item = as_object(raw)
            name = as_text(item.get("name"))
            if name is None:
                warnings.append(f"批次 {entry.name} 的 files[{index}] 缺少文件名；已跳过")
                continue
            files.append(
                {"name": name, "bytes": as_number(item.get("bytes")), "sha256": as_text(item.get("sha256"))}
            )
        batches.append(
            {
                "batchId": as_text(doc.get("batchId")) or entry.name,
                "asOf": as_text(doc.get("asOf")), "capturedAt": as_text(doc.get("capturedAt")), "files": files,
            }
        )
    return batches


def _table_names(doc: dict[str, object]) -> list[str]:
    names: dict[str, None] = {}
    for raw in as_list(as_object(doc.get("graph")).get("nodes")):
        node = as_object(raw)
        for name in [*strings(node.get("reads")), *strings(node.get("writes"))]:
            names.setdefault(name, None)
    round_doc = first_round(doc)
    for name in as_object(round_doc.get("outputs")):
        names.setdefault(name, None)
    for raw in as_list(as_object(round_doc.get("materialize")).get("entries")):
        name = as_text(as_object(as_object(raw).get("entry")).get("table"))
        if name is not None:
            names.setdefault(name, None)
    return list(names)


def _find_run_database(run_dir: Path, doc: dict[str, object], warnings: list[str]) -> Path | None:
    db_name = as_text(doc.get("db")) or as_text(as_object(doc.get("flowJson")).get("db")) or "hr.db"
    windows_path = PureWindowsPath(db_name)
    relative = Path(basename(db_name)) if windows_path.is_absolute() or Path(db_name).is_absolute() else Path(*windows_path.parts)
    candidates = [run_dir / "a" / relative, run_dir / relative, run_dir / "b" / relative]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    warnings.append(f"运行 {run_dir.name} 缺少产出数据库「{db_name}」；统计沿用 run.json，取样不可用")
    return None


def _fallback_stats(doc: dict[str, object]) -> dict[str, TableStat]:
    tables: dict[str, TableStat] = {}
    round_doc = first_round(doc)
    for raw in as_list(as_object(round_doc.get("materialize")).get("entries")):
        item = as_object(raw)
        table = as_text(as_object(item.get("entry")).get("table"))
        if table is not None:
            tables[table] = {"rows": as_number(as_object(item.get("digest")).get("rows")), "columns": None}
    for name, value in as_object(round_doc.get("outputs")).items():
        tables[name] = {"rows": as_number(as_object(value).get("rows")), "columns": None}
    return tables


def _read_focus(
    run_dir: Path, run: _Run, raw_doc: object, warnings: list[str]
) -> _Focus:
    if raw_doc is None:
        return _Focus(None, run.revision, None, "run", run.code_commit, None)
    doc = as_object(raw_doc)
    result = _Focus(
        None, as_text(doc.get("inputRevisionId")) or run.revision,
        as_number(doc.get("elapsedMs")), as_text(doc.get("kind")) or "run",
        as_text(doc.get("codeCommit")) or run.code_commit, None,
    )
    tables = _fallback_stats(doc)
    graph = build_graph(run_id=run.id, run_json=doc, tables=tables)
    result.graph = graph
    if graph is None:
        warnings.append(f"运行 {run.id} 没有可展示的 graph.nodes；流程页不可用")
        return result
    db_path = _find_run_database(run_dir, doc, warnings)
    result.db_path = db_path
    if db_path is not None:
        try:
            with readonly_database(db_path) as db:
                graph["tables"] = {name: read_table_stat(db, name, warnings=warnings) for name in _table_names(doc)}
                if graph["outputs"]:
                    graph["sample"] = sample_table(
                        db, graph["outputs"][0], f"该运行落盘的 {db_path.name}", warnings=warnings
                    )
        except (sqlite3.Error, OSError, VibeError) as error:
            warnings.append(f"无法读取运行数据库 {db_path}：{error}；沿用 run.json 摘要")
            graph["tables"], graph["sample"] = tables, None
    return result


def _find_work_database(
    work_dir: Path, snapshot: str | None, store: Path, warnings: list[str]
) -> Path | None:
    best: tuple[int, Path] | None = None
    for entry in _entries(work_dir, warnings, "工作目录"):
        if not entry.is_file() or entry.suffix.lower() != ".db":
            continue
        try:
            size = entry.stat().st_size
        except OSError as error:
            warnings.append(f"无法读取工作数据库大小 {entry}：{error}")
            continue
        if best is None or size > best[0]:
            best = size, entry
    if best is not None:
        return best[1]
    if snapshot is not None:
        path = Path(snapshot)
        for candidate in (path, store / path):
            if candidate.is_file():
                return candidate
        warnings.append(f"最新数据版本的快照不存在：{snapshot}")
    warnings.append("没有可读取的工作数据库或数据快照；输入文件仍可浏览，表清单不可用")
    return None


def _read_inputs(
    store: Path,
    session_id: str,
    run_docs: list[RunRecord],
    batch_id: str | None,
    reads: list[str],
    writes: list[str],
    snapshot: str | None,
    warnings: list[str],
) -> InputsView:
    work_dir = store / "sessions" / session_id / "work"
    batches = _read_batches(work_dir, warnings)
    batch = next((item for item in batches if item["batchId"] == batch_id), None)
    if batch is None:
        if batch_id is not None:
            warnings.append(f"选中运行的输入批次「{batch_id}」不可用；改用最新可读批次")
        batch = max(batches, key=lambda item: item["capturedAt"] or "", default=None)
    db_path = _find_work_database(work_dir, snapshot, store, warnings)
    if db_path is None:
        return build_inputs(batch=batch, tables=[], runs=run_docs, reads=reads, writes=writes)
    try:
        with readonly_database(db_path) as db:
            return build_inputs(
                batch=batch, db_path=db_path, tables=read_db_facts(db, warnings=warnings),
                runs=run_docs, reads=reads, writes=writes,
                sample=lambda name: sample_table(db, name, f"{db_path.name} · 取样", 12, 10, warnings=warnings),
            )
    except (sqlite3.Error, OSError, VibeError) as error:
        warnings.append(f"无法读取输入数据库 {db_path}：{error}；输入文件仍可浏览")
        return build_inputs(batch=batch, db_path=db_path, tables=[], runs=run_docs, reads=reads, writes=writes)


def _elapsed(start: str | None, finish: str | None, warnings: list[str], run_id: str) -> Number | None:
    if start is None or finish is None:
        return None
    try:
        started, finished = (
            datetime.fromisoformat(text.replace("Z", "+00:00")) for text in (start, finish)
        )
    except ValueError as error:
        warnings.append(f"运行 {run_id} 的起止时间无效：{error}")
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    ms = (finished - started).total_seconds() * 1000
    return (int(ms) if ms.is_integer() else ms) if ms >= 0 else None


def collect_book(
    store: str | Path | CollectOptions,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
) -> BookView:
    """Select the latest session/flow by default and distinguish current/input revisions."""
    if isinstance(store, dict):
        session_id = store.get("sessionId", session_id)
        run_id = store.get("runId", run_id)
        store = store["store"]
    store_path = Path(store)
    control_path = store_path / "control.db"
    if not control_path.is_file():
        raise VibeError(f"找不到 control.db：{control_path}")
    warnings: list[str] = []
    try:
        with readonly_database(control_path) as db:
            sessions = _query(db, "SELECT * FROM sessions ORDER BY updated_at DESC")
            chosen = next((row for row in sessions if row.get("id") == session_id), None) if session_id is not None else next(iter(sessions), None)
            if chosen is None:
                raise VibeError(f"control.db 里找不到会话「{session_id}」" if session_id is not None else "control.db 里没有会话")
            chosen_id = as_text(chosen.get("id"))
            if chosen_id is None:
                raise VibeError("选中会话缺少有效 id")
            program_id = as_text(chosen.get("program_id")) or ""
            programs = _optional_query(db, "SELECT * FROM programs WHERE id = ?", (program_id,), warnings, "程序页")
            raw_runs = _optional_query(db, "SELECT * FROM runs WHERE session_id = ? ORDER BY started_at DESC", (chosen_id,), warnings, "运行页")
            revision_rows = _optional_query(db, "SELECT * FROM data_revisions WHERE program_id = ? ORDER BY created_at DESC", (program_id,), warnings, "数据版本页")
            publish_rows = _optional_query(db, "SELECT * FROM publishes WHERE session_id = ? ORDER BY at DESC", (chosen_id,), warnings, "发布页")
    except (sqlite3.Error, OSError) as error:
        raise VibeError(f"无法读取 control.db：{control_path}：{error}") from error

    run_rows: list[_Run] = []
    for row in raw_runs:
        identifier = as_text(row.get("id"))
        if identifier is None:
            warnings.append("运行页含缺少有效 id 的记录；已跳过")
            continue
        run_rows.append(
            _Run(
                identifier, as_text(row.get("code_commit")), as_text(row.get("data_revision_id")),
                as_text(row.get("status")) or "—", as_text(row.get("started_at")),
                as_text(row.get("finished_at")), as_text(row.get("note")),
            )
        )
    if run_id is not None and not any(run.id == run_id for run in run_rows):
        raise VibeError(f"会话「{chosen_id}」里找不到运行「{run_id}」")
    run_root = store_path / "sessions" / chosen_id / "runs"
    authors = _read_authors(run_root, warnings)
    episodes = _read_episodes(store_path / "sessions" / chosen_id / "sessions", warnings)
    run_docs = _read_run_docs(run_root, run_rows, warnings)
    docs_by_id = {record["id"]: record["doc"] for record in run_docs}
    commit_to_run: dict[str, str] = {}
    for run in run_rows:
        if run.code_commit is not None:
            commit_to_run.setdefault(run.code_commit, run.id)
    author_by_commit = {author.commit: author for author in authors if author.commit is not None}
    episode_by_file = {basename(episode["file"]): episode for episode in episodes}
    for author in authors:
        if author.session_file is None:
            warnings.append(f"作者记录 {author.commit or '—'} 缺少 stats.sessionFile；无法关联对话")
            continue
        episode = episode_by_file.get(basename(author.session_file))
        if episode is None:
            warnings.append(f"作者记录对应的对话不存在：{author.session_file}")
        elif author.commit is not None:
            episode["runId"] = commit_to_run.get(author.commit)

    focus: _Run | None = None
    for run in run_rows:
        if run_id is not None:
            if run.id == run_id:
                focus = run
                break
            continue
        doc = as_object(docs_by_id.get(run.id))
        if build_graph(run_id=run.id, run_json=doc) is None:
            continue
        if focus is None:
            focus = run
        if as_text(doc.get("kind")) == "flow":
            focus = run
            break
    focus_result = _read_focus(run_root / focus.id, focus, docs_by_id.get(focus.id), warnings) if focus is not None else None
    if focus_result is None:
        warnings.append("没有可展示的流程运行；其他账页仍可浏览")
    graph = focus_result.graph if focus_result is not None else None

    runs: list[RunView] = []
    for run in run_rows:
        author = author_by_commit.get(run.code_commit) if run.code_commit is not None else None
        document = as_object(docs_by_id.get(run.id))
        identical = document.get("identical")
        runs.append(
            {
                "id": run.id, "short": run.id[:8],
                "kind": focus_result.kind if focus_result is not None and focus is not None and focus.id == run.id else "run",
                "status": run.status,
                "identical": identical if isinstance(identical, bool) else True if run.note and "identical=true" in run.note else None,
                "startedAt": run.started_at, "finishedAt": run.finished_at,
                "elapsedMs": _elapsed(run.started_at, run.finished_at, warnings, run.id),
                "codeCommit": run.code_commit, "revision": as_text(document.get("inputRevisionId")) or run.revision,
                "hasTranscript": author is not None and author.session_file is not None and basename(author.session_file) in episode_by_file,
            }
        )
    revisions: list[RevisionView] = [
        {"id": identifier, "short": identifier[:8], "at": as_text(row.get("created_at")) or "", "bytes": as_number(row.get("bytes"))}
        for row in revision_rows if (identifier := as_text(row.get("id"))) is not None
    ]
    publishes: list[PublishView] = [
        {"short": commit[:7], "at": as_text(row.get("at")) or "", "note": as_text(row.get("note"))}
        for row in publish_rows if (commit := as_text(row.get("commit_sha"))) is not None
    ]
    focus_author = author_by_commit.get(focus.code_commit) if focus is not None and focus.code_commit is not None else None
    focus_episode = episode_by_file.get(basename(focus_author.session_file)) if focus_author is not None and focus_author.session_file is not None else None
    if focus_episode is None and episodes:
        focus_episode = episodes[-1]
    focus_doc = as_object(docs_by_id.get(focus.id)) if focus is not None else {}
    inputs = _read_inputs(
        store_path, chosen_id, run_docs, as_text(focus_doc.get("batchId")),
        [table for node in graph["nodes"] for table in node["reads"]] if graph is not None else [],
        [table for node in graph["nodes"] for table in node["writes"]] if graph is not None else [],
        as_text(revision_rows[0].get("path")) if revision_rows else None, warnings,
    )
    warnings.extend(inputs.get("warnings", []))
    warnings = list(dict.fromkeys(warnings))
    if graph is not None:
        graph["warnings"] = list(dict.fromkeys([*graph["warnings"], *warnings]))
    program = programs[0] if programs else None
    return {
        "generatedAt": now(), "store": str(store_path),
        "program": {
            "id": program_id, "name": as_text(program.get("name")) or as_text(program.get("title")) or program_id
        } if program is not None else None,
        "session": {
            "id": chosen_id, "name": as_text(chosen.get("name")) or "—",
            "draftBranch": as_text(chosen.get("draft_branch")) or "—", "status": as_text(chosen.get("status")) or "—",
        },
        "revisions": revisions,
        "currentRevision": revisions[0]["id"] if revisions else None,
        "lastRunRevision": focus_result.revision if focus_result is not None else None,
        "runs": runs, "publishes": publishes, "graph": graph, "inputs": inputs,
        "diagram": build_diagram(inputs=inputs, graph=graph), "transcript": episodes,
        "focusEpisode": focus_episode["id"] if focus_episode is not None else None,
        "warnings": warnings,
    }


collectBook = collect_book
