"""Rearrange authoritative run.json records; never reparse program code."""

from ..util import VibeError
from ._values import as_list, as_number, as_object, as_text, first_round, strings
from .types import (
    BuildGraphInput,
    GraphView,
    MaterializeView,
    NodeView,
    StepView,
    TableData,
    TableStat,
)


def _steps(value: object) -> list[StepView]:
    steps: list[StepView] = []
    for raw in as_list(value):
        item = as_object(raw)
        line = as_number(item.get("line"))
        label = as_text(item.get("label"))
        if line is not None and label is not None:
            steps.append(
                {
                    "n": len(steps) + 1,
                    "line": line,
                    "kind": as_text(item.get("kind")) or "statement",
                    "label": label,
                }
            )
    return steps


def _nodes(value: object) -> list[NodeView]:
    nodes: list[NodeView] = []
    for raw in as_list(value):
        item = as_object(raw)
        node_id = as_text(item.get("id"))
        if node_id is None:
            continue
        call = as_text(item.get("call")) or node_id
        file, separator, fn = call.rpartition(":")
        nodes.append(
            {
                "id": node_id,
                "fn": fn if separator else node_id,
                "file": file if separator else call,
                "title": as_text(item.get("title")) or "",
                "reads": strings(item.get("reads")),
                "writes": strings(item.get("writes")),
                "elapsedMs": None,
                "changed": [],
                "steps": _steps(item.get("steps")),
            }
        )
    return nodes


def _materialize(value: object) -> list[MaterializeView]:
    entries: list[MaterializeView] = []
    for raw in as_list(value):
        item = as_object(raw)
        entry = as_object(item.get("entry"))
        table = as_text(entry.get("table"))
        if table is not None:
            entries.append(
                {
                    "file": as_text(entry.get("file")) or "—",
                    "sheet": as_text(entry.get("sheet")) or "—",
                    "table": table,
                    "rows": as_number(as_object(item.get("digest")).get("rows")),
                }
            )
    return entries


def build_graph(
    input: BuildGraphInput | None = None,
    *,
    run_id: str | None = None,
    run_json: object = None,
    tables: dict[str, TableStat] | None = None,
    sample: TableData | None = None,
) -> GraphView | None:
    """Accept Python keyword arguments or the original camelCase input mapping."""
    if input is not None:
        run_id, run_json = input["runId"], input["runJson"]
        tables, sample = input["tables"], input["sample"]
    if run_id is None:
        raise VibeError("build_graph requires run_id")

    doc = as_object(run_json)
    graph = as_object(doc.get("graph"))
    nodes = _nodes(graph.get("nodes"))
    if not nodes:
        return None
    round_doc = first_round(doc)
    executed = {
        node_id: item
        for raw in as_list(round_doc.get("nodes"))
        if (item := as_object(raw)) and (node_id := as_text(item.get("id"))) is not None
    }
    for node in nodes:
        execution = executed.get(node["id"])
        if execution is not None:
            node["elapsedMs"] = as_number(execution.get("elapsedMs"))
            node["changed"] = strings(execution.get("changed"))

    outputs = list(as_object(round_doc.get("outputs")))
    flow_json = as_object(doc.get("flowJson"))
    identical = doc.get("identical")
    return {
        "runId": run_id,
        "runShort": run_id[:8],
        "flow": as_text(doc.get("flow")) or as_text(flow_json.get("flow")) or "—",
        "entry": as_text(graph.get("entry")) or as_text(flow_json.get("entry")) or "—",
        "asOf": as_text(doc.get("asOf")),
        "codeCommit": as_text(doc.get("codeCommit")),
        "revision": as_text(doc.get("inputRevisionId")),
        "identical": identical if isinstance(identical, bool) else None,
        "elapsedMs": as_number(doc.get("elapsedMs")),
        "materialize": _materialize(as_object(round_doc.get("materialize")).get("entries")),
        "nodes": nodes,
        "outputs": outputs or [table for node in nodes for table in node["writes"]],
        "tables": tables if tables is not None else {},
        "sample": sample,
        "warnings": strings(graph.get("warnings")),
    }


buildGraph = build_graph
