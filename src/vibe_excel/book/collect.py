"""只读地读取 store；缺失的可选页面会附带诊断信息。

数据来源是 store/control.db（会话、运行、数据版本、发布），以及
sessions/<id>/ 下的 run.json、author.json、对话 JSONL 与产物数据库。
全程用只读连接打开数据库；缺失或损坏的页面降级为空数据并写入 warnings，
不修改 store 里的任何文件。
"""

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from ..util import VibeError, now, quote_identifier, read_json, readonly_database, require_object
from ._values import as_list, as_number, as_object, as_text, basename, first_round, strings
from .diagram import build_diagram
from .graph import build_graph
from .inputs import build_inputs
from .transcript import parse_conversation, parse_transcript
from .types import (
    BatchFile,
    BatchView,
    BookView,
    Cell,
    CollectOptions,
    ConversationView,
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
    """control.db runs 表的一行。

    这是把运行 id、代码提交、数据版本与起止时间拼在一起的中间记录，
    用于后续加载 run.json 与填充运行列表视图模型。
    """

    id: str
    code_commit: str | None
    revision: str | None
    status: str
    started_at: str | None
    finished_at: str | None
    # note 里可能带 identical=true 标记，用于在 run.json 缺字段时回填 identical。
    note: str | None


@dataclass(frozen=True)
class _Author:
    """runs/author-* 目录里 author.json 的摘要。

    它把代码提交与对话文件关联起来：session_file 取自 stats.sessionFile，
    用于判断某次运行是否留下了可展示的对话。
    """

    commit: str | None
    # stats.sessionFile 只记文件名，跨平台时需要取 basename 后再匹配。
    session_file: str | None
    label: str | None


@dataclass
class _Focus:
    """被选中运行的聚合上下文。

    graph 是由 run.json 派生的流程视图模型；db_path 是该运行产物数据库的路径，
    为空时统计沿用 run.json 摘要、取样不可用。
    """

    graph: GraphView | None
    revision: str | None
    elapsed_ms: Number | None
    kind: str
    commit: str | None
    db_path: Path | None


def _query(
    db: sqlite3.Connection, sql: str, params: tuple[str | int, ...] = ()
) -> list[dict[str, object]]:
    """把 sqlite3.Row 转成普通字典列表，便于直接拼装视图模型。"""
    return [{key: row[key] for key in row.keys()} for row in db.execute(sql, params)]


def _optional_query(
    db: sqlite3.Connection,
    sql: str,
    params: tuple[str | int, ...],
    warnings: list[str],
    page: str,
) -> list[dict[str, object]]:
    """查询可选页面；失败时记一条诊断并返回空列表。

    这样缺少某张表或视图时，其他页面仍能继续渲染。
    """
    try:
        return _query(db, sql, params)
    except sqlite3.Error as error:
        warnings.append(f"无法读取{page}：{error}")
        return []


def _entries(directory: Path, warnings: list[str], page: str) -> list[Path]:
    """按名称排序列出目录内容；目录不可读时记诊断并返回空列表。"""
    try:
        return sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as error:
        warnings.append(f"无法读取{page} {directory}：{error}")
        return []


def _optional_json(path: Path, warnings: list[str], page: str) -> dict[str, object] | None:
    """读取并校验一个 JSON 对象。

    文件缺失、编码错误或内容不是对象时都记诊断并返回 None，供调用方降级。
    """
    try:
        return require_object(read_json(path), str(path))
    except (VibeError, OSError, UnicodeError, json.JSONDecodeError) as error:
        warnings.append(f"无法读取{page} {path}：{error}")
        return None


def read_table_stat(
    db: sqlite3.Connection, name: str, *, warnings: list[str] | None = None
) -> TableStat:
    """统计表的行数与列数。

    表不存在、被删或没有读权限时返回全 None，而不是让整页失败。
    """
    diagnostics = warnings if warnings is not None else []
    try:
        counted = _query(db, f"SELECT COUNT(*) AS n FROM {quote_identifier(name)}")
        columns = _query(db, "SELECT name FROM pragma_table_info(?)", (name,))
        return {"rows": as_number(counted[0]["n"]) if counted else None, "columns": len(columns)}
    except sqlite3.Error as error:
        diagnostics.append(f"无法读取表「{name}」统计：{error}")
        return {"rows": None, "columns": None}


def _cell(value: object) -> Cell:
    """把 SQLite 单元格规整成可序列化的标量。

    非有限浮点会变成 None；BLOB 没有原生 JSON 表示，转成逗号分隔的字节串。
    """
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
    """取表的前若干行与列供前端预览。

    表不存在或没有列时记诊断并返回 None；max_columns 为 0 表示不截断列。
    """
    if limit < 0 or max_columns < 0:
        raise VibeError("表取样上限必须是非负整数")
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
    """从 _source_file/_source_sheet 列聚合一张表的出处。

    表没有这两列时返回空列表，表示它没有可追溯的 Excel 来源。
    """
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
    """枚举库里的用户表，附带行数、列数与来源。

    跳过 sqlite_ 开头的内部表；单张表读取失败只记诊断，不影响其余表。
    """
    diagnostics = warnings if warnings is not None else []
    facts: list[DbTableFact] = []
    try:
        # 按 rowid 列出，保留建表顺序，让多次导出的表清单保持稳定。
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
    """扫描 runs/author-* 子目录里的 author.json。

    只有目录名以 author- 开头且能读出 JSON 对象的记录才会被保留。
    """
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


def _read_episodes(
    directory: Path, warnings: list[str]
) -> tuple[list[EpisodeView], list[ConversationView]]:
    """读取目录下所有 JSONL 对话。

    返回两份：片段摘要（按开始时间正序）与逐条消息的对话（新的在前）。
    同一份文件只读一次，两种视图各走一遍解析。
    """
    episodes: list[EpisodeView] = []
    conversations: list[ConversationView] = []
    for file in _entries(directory, warnings, "对话目录"):
        if file.suffix.lower() != ".jsonl" or not file.is_file():
            continue
        try:
            # 对话文件常带 BOM，用 utf-8-sig 读入，避免首行 JSON 解析失败。
            text = file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            warnings.append(f"无法读取对话 {file}：{error}")
            continue
        episode = parse_transcript(file, text, warnings=warnings)
        if episode is not None:
            episodes.append(episode)
        conversation = parse_conversation(file, text, warnings=warnings)
        if conversation is not None:
            conversations.append(conversation)
    return (
        sorted(episodes, key=lambda episode: episode["start"]),
        sorted(conversations, key=lambda conversation: conversation["end"], reverse=True),
    )


def _read_run_docs(run_root: Path, runs: list[_Run], warnings: list[str]) -> list[RunRecord]:
    """为每个运行读出 run.json，供 graph 与 inputs 复用。

    缺失的 run.json 只记诊断，该运行仍会出现在列表里。
    """
    records: list[RunRecord] = []
    for run in runs:
        doc = _optional_json(run_root / run.id / "run.json", warnings, "运行记录")
        if doc is not None:
            records.append({"id": run.id, "startedAt": run.started_at, "doc": doc})
    return records


def _read_batches(work_dir: Path, warnings: list[str]) -> list[BatchView]:
    """读取 work/inputs 下各批次的 manifest.json。

    清单缺失时跳过该批次；files 不是数组时保留批次但记诊断。
    """
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
    """从 run.json 汇总涉及的表名。

    依次取 graph 节点的读/写集、a/b 轮的 outputs 与 materialize 条目；
    用 dict 去重并保持首次出现的顺序。
    """
    names: dict[str, None] = {}
    for raw in as_list(as_object(doc.get("graph")).get("nodes")):
        node = as_object(raw)
        for name in [*strings(node.get("reads")), *strings(node.get("writes"))]:
            names.setdefault(name, None)
    round_doc = first_round(doc)
    for name in as_object(round_doc.get("outputs")):
        names.setdefault(name, None)
    for raw in as_list(as_object(round_doc.get("materialize")).get("entries")):
        table_name = as_text(as_object(as_object(raw).get("entry")).get("table"))
        if table_name is not None:
            names.setdefault(table_name, None)
    return list(names)


def _find_run_database(run_dir: Path, doc: dict[str, object], warnings: list[str]) -> Path | None:
    """在运行的 a/、根目录、b/ 里查找产出数据库。

    数据库名来自 run.json 的 db 或 flowJson.db，默认 hr.db；
    绝对路径只取文件名，因为 Windows 盘符无法直接拼到 run 目录下。
    """
    db_name = as_text(doc.get("db")) or as_text(as_object(doc.get("flowJson")).get("db")) or "hr.db"
    windows_path = PureWindowsPath(db_name)
    relative = Path(basename(db_name)) if windows_path.is_absolute() or Path(db_name).is_absolute() else Path(*windows_path.parts)
    # 先试规范轮次 a，再试根目录，最后退到 b，兼容只跑单轮或旧布局的运行。
    candidates = [run_dir / "a" / relative, run_dir / relative, run_dir / "b" / relative]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    warnings.append(f"运行 {run_dir.name} 缺少产出数据库「{db_name}」；统计沿用 run.json，取样不可用")
    return None


def _fallback_stats(doc: dict[str, object]) -> dict[str, TableStat]:
    """在拿不到产物数据库时，从 run.json 恢复行数兜底。

    列数没有记录，统一为 None；这些值只在数据库不可读时使用。
    """
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
    """为选中的运行构建 graph 视图模型。

    尽量挂上产物库的表统计与首张输出表的样例行；数据库缺失时降级为
    run.json 摘要，并记录诊断。
    """
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
    """在会话工作目录里挑选工作数据库。

    先取体积最大的 .db 作为“可用”的启发式（体积与新旧的近似，不保证最新）；
    没有再退回最新数据版本的快照路径。
    """
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
    """组装 inputs 视图模型。

    选批次时优先用选中运行的 batchId，不可用则退回最新可读批次；
    工作数据库读不出来时仍保留批次清单，只是没有表事实。
    """
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
    """从起止时间算出毫秒耗时。

    缺失、非法或结束早于开始时返回 None 并记诊断；无时区的时间按 UTC 处理。
    """
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
        started = started.replace(tzinfo=UTC)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=UTC)
    ms = (finished - started).total_seconds() * 1000
    # 整毫秒转 int，避免前端出现 1234.0 毫秒这类多余的浮点尾数。
    return (int(ms) if ms.is_integer() else ms) if ms >= 0 else None


def collect_book(
    store: str | Path | CollectOptions,
    *,
    program_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
) -> BookView:
    """采集整本 book 的视图模型。

    program_id：只在该项目下选会话。站点模式一次渲染多个项目时必须给，
    否则每个项目都会落到全库最近活跃的那个会话上。
    默认选取最新会话，并优先选 kind=flow 且可构建 graph 的运行（不是单纯最新）。
    视图同时区分当前数据版本（revisions[0]）与选中运行使用的输入版本。
    是 book 的唯一公开采集入口，对 control.db 与运行库都只用只读连接。
    """
    if isinstance(store, dict):
        program_id = store.get("programId", program_id)
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
            # 按 updated_at 倒序，未指定会话时默认落到最近活跃的那个。
            sessions = _query(db, "SELECT * FROM sessions ORDER BY updated_at DESC")
            if program_id is not None:
                sessions = [row for row in sessions if as_text(row.get("program_id")) == program_id]
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
    episodes, conversations = _read_episodes(store_path / "sessions" / chosen_id / "sessions", warnings)
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
        # 用 basename 匹配，兼容绝对路径与不同平台的目录分隔符。
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
        # 优先选 kind=flow 的运行；runs 已按开始时间倒序，找到即是最新的流程运行。
        if as_text(doc.get("kind")) == "flow":
            focus = run
            break
    focus_result = _read_focus(run_root / focus.id, focus, docs_by_id.get(focus.id), warnings) if focus is not None else None
    if focus_result is None:
        warnings.append("没有可展示的流程运行；其他账页仍可浏览")
    graph = focus_result.graph if focus_result is not None else None

    runs: list[RunView] = []
    for run in run_rows:
        author_record = (
            author_by_commit.get(run.code_commit) if run.code_commit is not None else None
        )
        document = as_object(docs_by_id.get(run.id))
        identical = document.get("identical")
        runs.append(
            {
                "id": run.id, "short": run.id[:8],
                "kind": focus_result.kind if focus_result is not None and focus is not None and focus.id == run.id else "run",
                "status": run.status,
                # run.json 没有 identical 时，从 runs 表 note 里的标记回填。
                "identical": identical if isinstance(identical, bool) else True if run.note and "identical=true" in run.note else None,
                "startedAt": run.started_at, "finishedAt": run.finished_at,
                "elapsedMs": _elapsed(run.started_at, run.finished_at, warnings, run.id),
                "codeCommit": run.code_commit, "revision": run.revision,
                "hasTranscript": author_record is not None
                and author_record.session_file is not None
                and basename(author_record.session_file) in episode_by_file,
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
        # 关联不到作者记录时，退到最近一条对话作为焦点。
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
        # 把采集阶段的诊断也挂到 graph 上，前端只需读一处就能显示所有缺页提示。
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
        "conversations": conversations,
        "focusEpisode": focus_episode["id"] if focus_episode is not None else None,
        "warnings": warnings,
    }


