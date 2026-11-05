from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from vibe_excel.util import (
    VibeError,
    json_text,
    positive_number,
    relative_path,
    require_list,
    require_object,
    require_text,
    sha256_text,
)

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PY_CALL = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_./\\-]*\.py):([A-Za-z_][A-Za-z0-9_]*)$")
SQL_CALL = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.sql$")
DEFAULT_ENTRY = "pipeline.py"
DEFAULT_FLOW_PATH = "flow.json"


@dataclass(frozen=True)
class MaterializeEntry:
    file: str
    table: str
    sheet: str | None = None
    columns: dict[str, str] | None = None
    load: str | None = None

    def to_spec(self) -> dict[str, object]:
        result: dict[str, object] = {"file": self.file, "table": self.table}
        if self.sheet is not None:
            result["sheet"] = self.sheet
        if self.columns is not None:
            result["columns"] = self.columns
        if self.load is not None:
            result["load"] = self.load
        return result


@dataclass(frozen=True)
class FlowStep:
    line: int
    kind: Literal["block", "statement"]
    of: str
    label: str


@dataclass(frozen=True)
class FlowWarning:
    code: str
    message: str
    line: int | None


@dataclass(frozen=True)
class FlowNode:
    id: str
    call: str
    reads: list[str]
    writes: list[str]
    args: dict[str, str] | None = None
    timeout_ms: int | None = None
    title: str | None = None
    steps: list[FlowStep] = field(default_factory=list)


@dataclass(frozen=True)
class FlowSpec:
    flow: str
    db: str
    materialize: list[MaterializeEntry]
    outputs: list[str]
    nodes: list[FlowNode]
    entry: str | None = None
    warnings: list[FlowWarning] = field(default_factory=list)


@dataclass(frozen=True)
class FlowEdge:
    table: str
    from_node: str
    to_node: str


@dataclass(frozen=True)
class FlowPlan:
    spec: FlowSpec
    order: list[str]
    edges: list[FlowEdge]
    produced_by: dict[str, str]
    materialized: set[str]
    unknown_reads: list[str]
    digest: str


@dataclass(frozen=True)
class OptimizationHint:
    kind: Literal["dead-node", "single-consumer", "unused-materialize"]
    nodes: list[str]
    tables: list[str]
    note: str


def table_name(value: object, where: str) -> str:
    text = require_text(value, where)
    if not IDENT.fullmatch(text):
        raise VibeError(f"{where} must be an ASCII table identifier: {text}")
    return text


def table_list(value: object, where: str) -> list[str]:
    values = require_list(value, where)
    result = [table_name(item, f"{where}[{index}]") for index, item in enumerate(values)]
    if len(set(result)) != len(result):
        raise VibeError(f"{where} contains duplicate table names")
    return result


def db_file_name(value: object, where: str = "flow.db") -> str:
    text = relative_path(value, where, suffix=".db")
    if "/" in text:
        raise VibeError(f"{where} must be a filename without directories: {text}")
    return text


def parse_call(call: str, where: str = "call") -> tuple[str, str | None]:
    text = relative_path(call, where)
    python = PY_CALL.fullmatch(text)
    if python is not None:
        return python[1], python[2]
    if SQL_CALL.fullmatch(text):
        return text, None
    raise VibeError(f"{where} must be file.py:function or file.sql: {text}")


def parse_flow(raw: object) -> FlowSpec:
    root = require_object(raw, "flow")
    name = require_text(root.get("flow"), "flow.flow")
    db = db_file_name(root.get("db", "hr.db"))
    outputs = table_list(root.get("outputs", []), "flow.outputs")
    materialize: list[MaterializeEntry] = []
    tables: set[str] = set()
    for index, item in enumerate(require_list(root.get("materialize", []), "flow.materialize")):
        where = f"flow.materialize[{index}]"
        entry = require_object(item, where)
        file = relative_path(entry.get("file"), f"{where}.file")
        table = table_name(entry.get("table"), f"{where}.table")
        if table in tables:
            raise VibeError(f"{where} materializes the same table twice: {table}")
        tables.add(table)
        sheet = require_text(entry["sheet"], f"{where}.sheet") if "sheet" in entry else None
        columns = None
        if "columns" in entry:
            columns = {
                require_text(key, f"{where}.columns key"): table_name(value, f"{where}.columns.{key}")
                for key, value in require_object(entry["columns"], f"{where}.columns").items()
            }
        load = None
        if "load" in entry:
            load = require_text(entry["load"], f"{where}.load")
            _, function = parse_call(load, f"{where}.load")
            if function is None:
                raise VibeError(f"{where}.load must name a Python function")
        materialize.append(MaterializeEntry(file, table, sheet, columns, load))
    nodes: list[FlowNode] = []
    ids: set[str] = set()
    for index, item in enumerate(require_list(root.get("nodes", []), "flow.nodes")):
        where = f"flow.nodes[{index}]"
        record = require_object(item, where)
        identifier = table_name(record.get("id"), f"{where}.id")
        if identifier in ids:
            raise VibeError(f"Duplicate node id: {identifier}")
        ids.add(identifier)
        call = require_text(record.get("call"), f"{where}.call")
        parse_call(call, f"{where}.call")
        args = None
        if "args" in record:
            args = {
                table_name(key, f"{where}.args key"): require_text(
                    value, f"{where}.args.{key}", allow_empty=True
                )
                for key, value in require_object(record["args"], f"{where}.args").items()
            }
        timeout = None
        if "timeoutMs" in record:
            timeout = int(positive_number(record["timeoutMs"], f"{where}.timeoutMs"))
            if timeout == 0:
                raise VibeError(f"{where}.timeoutMs must be at least one millisecond")
        nodes.append(
            FlowNode(
                identifier, call, table_list(record.get("reads", []), f"{where}.reads"),
                table_list(record.get("writes", []), f"{where}.writes"), args, timeout,
            )
        )
    entry_path = (
        relative_path(root["entry"], "flow.entry", suffix=".py") if "entry" in root else None
    )
    if nodes and entry_path is not None:
        raise VibeError("flow.json must choose nodes or entry, not both")
    if not nodes:
        entry_path = entry_path or DEFAULT_ENTRY
    return FlowSpec(name, db, materialize, outputs, nodes, entry_path)


def parse_flow_json(text: str) -> FlowSpec:
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise VibeError(f"Invalid flow.json:{error.lineno}: {error.msg}") from error
    return parse_flow(value)


def validate_flow(spec: FlowSpec) -> None:
    if not spec.nodes:
        raise VibeError(f"Flow has no nodes; declare nodes or implement main in {spec.entry or DEFAULT_ENTRY}")
    if len({node.id for node in spec.nodes}) != len(spec.nodes):
        raise VibeError("Flow has duplicate node ids")
    materialized = {entry.table for entry in spec.materialize}
    for node in spec.nodes:
        parse_call(node.call, f"nodes.{node.id}.call")
        if not node.reads and not node.writes:
            raise VibeError(f"Node {node.id} neither reads nor writes a table")
        for table in node.writes:
            if table in materialized:
                raise VibeError(f"Node {node.id} cannot overwrite materialized table {table}")
    for output in spec.outputs:
        if not any(output in node.writes for node in spec.nodes):
            raise VibeError(f"Declared output {output} has no producer")


def digest_of(spec: FlowSpec) -> str:
    mappings: list[dict[str, object]] = []
    for entry in sorted(spec.materialize, key=lambda item: item.table):
        item: dict[str, object] = {"file": entry.file, "table": entry.table}
        if entry.sheet is not None:
            item["sheet"] = entry.sheet
        if entry.columns is not None:
            item["columns"] = dict(sorted(entry.columns.items()))
        if entry.load is not None:
            item["load"] = entry.load
        mappings.append(item)
    nodes: list[dict[str, object]] = []
    for node in sorted(spec.nodes, key=lambda item: item.id):
        item = {
            "id": node.id, "call": node.call, "reads": sorted(node.reads), "writes": sorted(node.writes)
        }
        if node.args is not None:
            item["args"] = dict(sorted(node.args.items()))
        if node.timeout_ms is not None:
            item["timeoutMs"] = node.timeout_ms
        nodes.append(item)
    return sha256_text(
        json_text(
            {
                "flow": spec.flow, "db": spec.db, "outputs": sorted(spec.outputs),
                "materialize": mappings, "nodes": nodes,
            }
        )
    )[:12]


def plan_flow(spec: FlowSpec) -> FlowPlan:
    validate_flow(spec)
    produced: dict[str, str] = {}
    for node in spec.nodes:
        for table in node.writes:
            owner = produced.get(table)
            if owner is not None and owner != node.id:
                raise VibeError(f"Table {table} has two producers: {owner} and {node.id}")
            produced[table] = node.id
    materialized = {entry.table for entry in spec.materialize}
    edges: list[FlowEdge] = []
    unknown: set[str] = set()
    for node in spec.nodes:
        for table in node.reads:
            owner = produced.get(table)
            if owner is None:
                if table not in materialized:
                    unknown.add(table)
            elif owner != node.id:
                edges.append(FlowEdge(table, owner, node.id))
    degree = {node.id: 0 for node in spec.nodes}
    for edge in edges:
        degree[edge.to_node] += 1
    ready = sorted(identifier for identifier, count in degree.items() if count == 0)
    order: list[str] = []
    while ready:
        identifier = ready.pop(0)
        order.append(identifier)
        for edge in edges:
            if edge.from_node != identifier:
                continue
            degree[edge.to_node] -= 1
            if degree[edge.to_node] == 0:
                ready.append(edge.to_node)
                ready.sort()
    if len(order) != len(spec.nodes):
        cycle = sorted(degree.keys() - set(order))
        raise VibeError(f"Workflow contains a dependency cycle: {', '.join(cycle)}")
    return FlowPlan(spec, order, edges, produced, materialized, sorted(unknown), digest_of(spec))


def suggest_optimizations(plan: FlowPlan) -> list[OptimizationHint]:
    hints: list[OptimizationHint] = []
    outputs = set(plan.spec.outputs)

    def consumers(table: str) -> list[str]:
        return [node.id for node in plan.spec.nodes if table in node.reads]

    for entry in plan.spec.materialize:
        if entry.table not in outputs and not consumers(entry.table):
            hints.append(
                OptimizationHint("unused-materialize", [], [entry.table], f"{entry.table} has no consumers")
            )
    for node in plan.spec.nodes:
        if not node.writes:
            continue
        if not any(table in outputs or consumers(table) for table in node.writes):
            hints.append(
                OptimizationHint("dead-node", [node.id], sorted(node.writes), f"{node.id} writes unused tables")
            )
            continue
        if any(table in outputs for table in node.writes):
            continue
        downstream = {identifier for table in node.writes for identifier in consumers(table)}
        if len(downstream) == 1:
            into = next(iter(downstream))
            hints.append(
                OptimizationHint(
                    "single-consumer", [node.id, into], sorted(node.writes),
                    f"{node.id} only feeds {into}; folding needs explicit approval",
                )
            )
    return hints


def format_flow_warning(entry: str | None, warning: FlowWarning) -> str:
    where = entry or ""
    if warning.line is not None:
        where += f":{warning.line}"
    return f"{where} [{warning.code}] {warning.message}".strip()


def describe_flow(plan: FlowPlan, *, steps: bool = False) -> list[str]:
    spec = plan.spec
    source = f"code:{spec.entry}" if spec.entry is not None else "flow.json"
    lines = [
        f"flow {spec.flow}@{plan.digest} db={spec.db} nodes={len(spec.nodes)} "
        f"edges={len(plan.edges)} graph={source}",
        f"  outputs: {', '.join(spec.outputs) or '(not declared)'}",
    ]
    for entry in spec.materialize:
        sheet = f"#{entry.sheet}" if entry.sheet else ""
        load = f" (load={entry.load})" if entry.load else ""
        lines.append(f"  [materialize] {entry.file}{sheet} -> {entry.table}{load}")
    by_id = {node.id: node for node in spec.nodes}
    for identifier in plan.order:
        node = by_id[identifier]
        title = f" | {node.title}" if node.title else ""
        lines.append(
            f"  {identifier:18} {'+'.join(node.reads) or '-'} -> "
            f"{'+'.join(node.writes) or '-'} ({node.call}){title}"
        )
        if steps:
            for step in node.steps:
                lines.append(f"      [{step.of}] {step.label} (L{step.line})")
    if plan.unknown_reads:
        lines.append(f"  external reads: {', '.join(plan.unknown_reads)} (must exist in the input database)")
    lines.extend(f"  ! {format_flow_warning(spec.entry, warning)}" for warning in spec.warnings)
    return lines
