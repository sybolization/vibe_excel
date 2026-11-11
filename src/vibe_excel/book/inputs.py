"""合并物化记录、来源列与最近记录的写出者。

输入是 control.db 读出的表事实、批次清单与 run 文档；输出是 inputs 视图模型。
本模块不打开数据库，只做纯合并、排序与诊断，因此没有副作用。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._values import as_list, as_number, as_object, as_text, strings
from .types import (
    BatchView,
    BuildInputsInput,
    DbTableFact,
    InputFileView,
    InputsView,
    InputTableView,
    Number,
    RunRecord,
    SeedKind,
    SourceRef,
    TableData,
    Usage,
)


@dataclass(frozen=True)
class _MaterializeEdge:
    """一条物化记录。

    表示某张表由哪个文件/工作表生成，以及最近一次生成它的运行。
    """

    file: str
    sheet: str
    rows: Number | None
    run_id: str


@dataclass(frozen=True)
class _DeriveEdge:
    """一条派生记录。

    保存写表节点的读集、函数、耗时与运行短 id，用于回溯派生链。
    """

    reads: list[str]
    via: str
    file: str
    name: str
    elapsed_ms: Number | None
    run_short: str


def _newest_first(runs: Sequence[RunRecord]) -> list[RunRecord]:
    """按 startedAt 从新到旧排序；没有开始时间的运行排到最后。"""
    return sorted(runs, key=lambda run: run["startedAt"] or "", reverse=True)


def _elapsed_by_node(doc: dict[str, object]) -> dict[str, Number]:
    """从 a/b 两轮执行记录里凑出各节点的耗时。

    同一节点在两轮都有记录时保留先遇到的（a 轮优先）。
    """
    elapsed: dict[str, Number] = {}
    rounds = as_object(doc.get("rounds"))
    for key in ("a", "b"):
        for raw in as_list(as_object(rounds.get(key)).get("nodes")):
            node = as_object(raw)
            node_id, ms = as_text(node.get("id")), as_number(node.get("elapsedMs"))
            if node_id is not None and ms is not None:
                elapsed.setdefault(node_id, ms)
    return elapsed


def _materialize_edges(
    runs: Sequence[RunRecord],
) -> tuple[dict[str, _MaterializeEdge], dict[str, int]]:
    """收集每张表的物化边，并给文件/工作表分配稳定序号。

    同时返回 sheet_rank，用于之后按清单顺序排列同一文件里的直接表。
    """
    edges: dict[str, _MaterializeEdge] = {}
    sheet_rank: dict[str, int] = {}
    for run in _newest_first(runs):
        rounds = as_object(as_object(run["doc"]).get("rounds"))
        for key in ("a", "b"):
            materialize = as_object(as_object(rounds.get(key)).get("materialize"))
            for raw in as_list(materialize.get("entries")):
                item = as_object(raw)
                entry = as_object(item.get("entry"))
                table, file = as_text(entry.get("table")), as_text(entry.get("file"))
                if table is None or file is None:
                    continue
                sheet = as_text(entry.get("sheet")) or "—"
                sheet_rank.setdefault(f"{file}\0{sheet}", len(sheet_rank))
                # 从新到旧遍历，setdefault 保证保留最近一次的物化边。
                edges.setdefault(
                    table,
                    _MaterializeEdge(
                        file, sheet, as_number(as_object(item.get("digest")).get("rows")), run["id"]
                    ),
                )
    return edges, sheet_rank


def _derive_edges(runs: Sequence[RunRecord]) -> dict[str, _DeriveEdge]:
    """收集每张表的最近写出者。

    节点耗时优先取 graph 里的值，缺失时回退到执行轮次记录。
    """
    edges: dict[str, _DeriveEdge] = {}
    for run in _newest_first(runs):
        doc = as_object(run["doc"])
        elapsed = _elapsed_by_node(doc)
        for raw in as_list(as_object(doc.get("graph")).get("nodes")):
            node = as_object(raw)
            call = as_text(node.get("call")) or as_text(node.get("id")) or "—"
            file, separator, fn = call.rpartition(":")
            node_id = as_text(node.get("id"))
            ms = as_number(node.get("elapsedMs"))
            if ms is None and node_id is not None:
                ms = elapsed.get(node_id)
            edge = _DeriveEdge(
                strings(node.get("reads")), fn if separator else call, file if separator else "",
                as_text(node.get("name")) or "", ms, run["id"][:8],
            )
            for table in strings(node.get("writes")):
                # 同样保留最新写出者，旧运行的节点不覆盖。
                edges.setdefault(table, edge)
    return edges


def _cyclic_tables(edges: dict[str, _DeriveEdge], names: set[str]) -> list[str]:
    """用拓扑消解找出无法确定顺序的表。

    反复移除没有未满足上游的表；剩下的就是循环依赖（含自环）。
    """
    dependencies = {
        name: set(edges[name].reads).intersection(names) for name in names if name in edges
    }
    while dependencies:
        ready = {name for name, upstream in dependencies.items() if not upstream}
        if not ready:
            return sorted(dependencies)
        dependencies = {
            name: upstream.difference(ready)
            for name, upstream in dependencies.items() if name not in ready
        }
    return []


def build_inputs(
    input: BuildInputsInput | None = None,
    *,
    batch: BatchView | None = None,
    db_path: str | Path | None = None,
    tables: Sequence[DbTableFact] = (),
    runs: Sequence[RunRecord] = (),
    reads: Sequence[str] = (),
    writes: Sequence[str] = (),
    sample: Callable[[str], TableData | None] | None = None,
) -> InputsView:
    """组装 inputs 视图模型。

    来源优先级是物化记录、表内来源列、最近写出者的派生链；追不到来源的表
    归入 orphans 并写明原因（缺失输入或循环依赖）。
    """
    if input is not None:
        batch, db_path, tables, runs = input["batch"], input["dbPath"], input["tables"], input["runs"]
        reads, writes, sample = input["reads"], input["writes"], input.get("sample")
    materialize, sheet_rank = _materialize_edges(runs)
    derive = _derive_edges(runs)
    read_set, write_set = set(reads), set(writes)
    by_name = {table["name"]: table for table in tables}
    warnings: list[str] = []

    def usage_of(name: str) -> Usage:
        if name in read_set and name in write_set:
            return "both"
        if name in read_set:
            return "read"
        return "write" if name in write_set else None

    def make_row(name: str, kind: SeedKind) -> InputTableView:
        fact = by_name.get(name)
        return {
            "name": name,
            "rows": fact["rows"] if fact is not None else None,
            "columns": fact["columns"] if fact is not None else None,
            "kind": kind,
            "writer": None,
            "sheet": None,
            "sheetCount": 0,
            "from": [],
            "via": "",
            "depth": 0,
            "usage": usage_of(name),
            "sample": sample(name) if sample is not None else None,
        }

    rows: dict[str, InputTableView] = {}
    file_of: dict[str, str] = {}
    depth_of: dict[str, int] = {}
    for table in tables:
        name = table["name"]
        materialized = materialize.get(name)
        if materialized is not None:
            row = make_row(name, "materialize")
            row["sheet"], row["sheetCount"] = materialized.sheet, 1
            row["via"] = materialized.run_id[:8]
            if row["rows"] is None:
                row["rows"] = materialized.rows
            rows[name], file_of[name], depth_of[name] = row, materialized.file, 0
        elif table["sources"]:
            source = table["sources"][0]
            row = make_row(name, "import")
            row["sheet"], row["sheetCount"] = source["sheet"], len(table["sources"])
            if row["rows"] is None:
                row["rows"] = source["rows"]
            rows[name], file_of[name], depth_of[name] = row, source["file"], 0

    direct_names = set(rows)
    pending = [table["name"] for table in tables if table["name"] not in rows]
    # 每轮成功处理都会移除一张表，因此再深的链和环也能终止。
    while pending:
        progressed = False
        # 从尾部回扫，能在同一轮里让短链先落位，减少循环次数。
        for index in range(len(pending) - 1, -1, -1):
            name = pending[index]
            edge = derive.get(name)
            if edge is None:
                continue
            known = [upstream for upstream in edge.reads if upstream in file_of]
            if not known:
                continue
            row = make_row(name, "derive")
            row["from"] = list(edge.reads)
            row["via"] = edge.via
            row["writer"] = {
                "fn": edge.via, "name": edge.name, "file": edge.file,
                "elapsedMs": edge.elapsed_ms, "runShort": edge.run_short
            }
            row["depth"] = max(depth_of[upstream] for upstream in known) + 1
            rows[name], file_of[name], depth_of[name] = row, file_of[known[0]], row["depth"]
            pending.pop(index)
            progressed = True
        if not progressed:
            break

    orphans = [make_row(name, "unknown") for name in pending]
    for name in pending:
        # 能查到派生边说明是链断了或成环；否则就是完全没有来源记录。
        reason = "缺失输入或循环依赖" if name in derive else "没有物化、导入或写出者记录"
        warnings.append(f"表「{name}」追不到原始文件：{reason}")
    for name, row in rows.items():
        missing = [upstream for upstream in row["from"] if upstream not in file_of]
        if missing:
            warnings.append(f"派生表「{name}」部分上游无来源：{', '.join(missing)}；沿已知来源展示")
    cyclic = _cyclic_tables(derive, set(by_name).difference(direct_names))
    if cyclic:
        warnings.append(f"血缘包含循环依赖或依赖循环的表：{', '.join(cyclic)}")

    manifest = {file["name"]: file for file in batch["files"]} if batch is not None else {}
    order = list(manifest)
    for file in dict.fromkeys(file_of.values()):
        if file not in order:
            order.append(file)
    files: list[InputFileView] = []
    for name in order:
        meta = manifest.get(name)
        own = [row for row in rows.values() if file_of[row["name"]] == name]
        direct = [row for row in own if row["kind"] != "derive"]
        # 同一文件内先按 manifest 里工作表的出现顺序，再让导入排在派生之前。
        direct.sort(
            key=lambda row: (
                sheet_rank.get(f"{name}\0{row['sheet']}", 2**53),
                1 if row["kind"] == "import" else 0,
            )
        )
        files.append(
            {
                "name": name,
                "bytes": meta["bytes"] if meta is not None else None,
                "sha256": meta["sha256"] if meta is not None else None,
                "direct": direct,
                "derived": sorted(
                    (row for row in own if row["kind"] == "derive"),
                    key=lambda row: (row["depth"], row["name"]),
                ),
                "inBatch": meta is not None,
            }
        )
    return {
        "batchId": batch["batchId"] if batch is not None else None,
        "capturedAt": batch["capturedAt"] if batch is not None else None,
        "asOf": batch["asOf"] if batch is not None else None,
        "dbPath": str(db_path) if db_path is not None else None,
        "dbTables": len(tables),
        "files": files,
        "orphans": orphans,
        "readTables": len(read_set.intersection(by_name)),
        "writeTables": len(write_set.intersection(by_name)),
        "warnings": warnings,
    }



__all__ = ["BatchView", "BuildInputsInput", "DbTableFact", "RunRecord", "SourceRef", "build_inputs"]
