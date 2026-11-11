"""流程的声明形式与静态语义（ADR-0008）。

这里定义流程规格（FlowSpec）、节点（FlowNode）、物化清单（MaterializeEntry）以及由它们
推导出的执行计划（FlowPlan）。流程有两种来源：flow.json 里逐条声明 nodes，或只声明入口
文件、由代码本身的 main 推导节点（“代码即流程”）；本模块只负责声明形式、校验与计划，
不执行任何节点，也不触碰数据库。
"""

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
    """一条物化声明：把批次目录里的 file（某个 sheet）落成 SQLite 表 table。

    columns 是源列名到目标列名的改名表，None 表示原样落表；load 是显式偏离纯映射的例外
    通道，指向一个 Python 函数，由它自己建表。
    """

    file: str
    table: str
    sheet: str | None = None
    columns: dict[str, str] | None = None  # 源列名 -> 目标列名；None 表示原样落表
    load: str | None = None  # file.py:函数名；例外通道，不走纯映射

    def to_spec(self) -> dict[str, object]:
        """转成可直接写进 materialize.json 的字典，只保留显式给出的可选字段。"""
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
    """节点内部的一步说明：来自块首字符串，用于解释这段代码在做什么。"""

    line: int
    kind: Literal["block", "statement"]
    of: str
    label: str


@dataclass(frozen=True)
class FlowWarning:
    """不致命的结构警告（如带表标注却未被调用的函数）；line 可为空。"""

    code: str
    message: str
    line: int | None


@dataclass(frozen=True)
class FlowNode:
    """流程里的一个节点：一个调用点对应一次函数或 SQL 文件执行。

    id 同时被当作表标识符使用，因此必须是 ASCII 标识符；reads/writes 声明的表构成图的边，
    args 是传给节点的字符串参数，timeout_ms 覆盖执行器的默认超时。
    """

    id: str  # 节点标识；也会被当作 SQL 标识符，必须是 ASCII
    call: str
    reads: list[str]
    writes: list[str]
    args: dict[str, str] | None = None
    timeout_ms: int | None = None
    title: str | None = None
    steps: list[FlowStep] = field(default_factory=list)
    name: str | None = None


@dataclass(frozen=True)
class FlowSpec:
    """流程的静态规格；由 flow.json 声明或由入口代码解析而来。

    entry 与 nodes 互斥：有 nodes 是声明式流程，否则 entry 指向“代码即流程”的入口文件。
    outputs 声明必须存在的输出表；materialize 是上游 Excel 到表的映射清单。
    """

    flow: str
    db: str
    materialize: list[MaterializeEntry]
    outputs: list[str]
    nodes: list[FlowNode]
    entry: str | None = None
    warnings: list[FlowWarning] = field(default_factory=list)


@dataclass(frozen=True)
class FlowEdge:
    """数据依赖边：from_node 写出 table，to_node 读入同一张表。"""

    table: str
    from_node: str
    to_node: str


@dataclass(frozen=True)
class FlowPlan:
    """由规格推导出的可执行计划。

    order 是拓扑序；produced_by 记录每张表的唯一生产者；unknown_reads 是需要由输入库提供的
    外部表。digest 是规格的规范化摘要，用来标识“这次跑的是哪个版本的流程”。
    """

    spec: FlowSpec
    order: list[str]
    edges: list[FlowEdge]
    produced_by: dict[str, str]
    materialized: set[str]
    unknown_reads: list[str]
    digest: str


@dataclass(frozen=True)
class OptimizationHint:
    """只读的优化建议（未用物化、死节点、单消费者）；不改变计划本身。"""

    kind: Literal["dead-node", "single-consumer", "unused-materialize"]
    nodes: list[str]
    tables: list[str]
    note: str


def table_name(value: object, where: str) -> str:
    """校验表名是 ASCII 标识符并返回；表名要能直接拼进 SQL，中文/带点/空格都拒绝。"""
    text = require_text(value, where)
    if not IDENT.fullmatch(text):
        raise VibeError(f"{where} 必须是 ASCII 表标识符: {text}")
    return text


def table_list(value: object, where: str) -> list[str]:
    """逐项校验表名并拒绝重复；重复的表声明会让图的边含义不清。"""
    values = require_list(value, where)
    result = [table_name(item, f"{where}[{index}]") for index, item in enumerate(values)]
    if len(set(result)) != len(result):
        raise VibeError(f"{where} 包含重复的表名")
    return result


def db_file_name(value: object, where: str = "flow.db") -> str:
    """校验数据库字段是单个 .db 文件名（不含目录），因为它相对会话工作目录解析。"""
    text = relative_path(value, where, suffix=".db")
    if "/" in text:
        raise VibeError(f"{where} 必须是不含目录的文件名: {text}")
    return text


def parse_call(call: str, where: str = "call") -> tuple[str, str | None]:
    """解析节点调用：返回（相对文件路径，函数名）；SQL 文件返回的函数名为 None。"""
    text = relative_path(call, where)
    python = PY_CALL.fullmatch(text)
    if python is not None:
        return python[1], python[2]
    if SQL_CALL.fullmatch(text):
        return text, None
    raise VibeError(f"{where} 必须是 file.py:function 或 file.sql: {text}")


def parse_flow(raw: object) -> FlowSpec:
    """从已反序列化的 flow.json 对象构建 FlowSpec，逐项做形状校验。

    nodes 与 entry 二选一：声明了 nodes 就是声明式流程，否则退化为“代码即流程”，由入口文件
    的 main 推导节点。重复表、重复节点 id、非法标注都在这里报错。
    """
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
            raise VibeError(f"{where} 重复物化了同一张表: {table}")
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
                raise VibeError(f"{where}.load 必须指定一个 Python 函数")
        materialize.append(MaterializeEntry(file, table, sheet, columns, load))
    nodes: list[FlowNode] = []
    ids: set[str] = set()
    for index, item in enumerate(require_list(root.get("nodes", []), "flow.nodes")):
        where = f"flow.nodes[{index}]"
        record = require_object(item, where)
        identifier = table_name(record.get("id"), f"{where}.id")
        if identifier in ids:
            raise VibeError(f"节点 id 重复: {identifier}")
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
                raise VibeError(f"{where}.timeoutMs 至少为一毫秒")
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
        raise VibeError("flow.json 必须在 nodes 与 entry 之间二选一")
    if not nodes:
        entry_path = entry_path or DEFAULT_ENTRY
    return FlowSpec(name, db, materialize, outputs, nodes, entry_path)


def parse_flow_json(text: str) -> FlowSpec:
    """解析 flow.json 文本；JSON 语法错误转成带行号的 VibeError。"""
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise VibeError(f"flow.json 无效:{error.lineno}: {error.msg}") from error
    return parse_flow(value)


def validate_flow(spec: FlowSpec) -> None:
    """交叉校验规格的一致性：有节点、id 唯一、读写非空、不覆盖物化表、输出有生产者。

    这些检查只看单点约束；全局依赖（生产者归属、成环、外部读取）由 plan_flow 负责。
    """
    if not spec.nodes:
        raise VibeError(f"流程没有节点；请声明 nodes 或在 {spec.entry or DEFAULT_ENTRY} 中实现 main")
    if len({node.id for node in spec.nodes}) != len(spec.nodes):
        raise VibeError("流程包含重复的节点 id")
    materialized = {entry.table for entry in spec.materialize}
    for node in spec.nodes:
        parse_call(node.call, f"nodes.{node.id}.call")
        if not node.reads and not node.writes:
            raise VibeError(f"节点 {node.id} 既未读取也未写入任何表")
        for table in node.writes:
            if table in materialized:
                raise VibeError(f"节点 {node.id} 不能覆盖已物化的表 {table}")
    for output in spec.outputs:
        if not any(output in node.writes for node in spec.nodes):
            raise VibeError(f"声明的输出 {output} 没有生产者")


def digest_of(spec: FlowSpec) -> str:
    """对规格做规范化后取 SHA-256 前缀，作为流程版本摘要。

    序列化前把 materialize/nodes/reads/writes/args 排序：同一语义的规格必须得到同一摘要。
    只取前 12 位十六进制是为了在日志与文件名里可读，同时保持足够低的碰撞概率。
    """
    # 先排序再序列化：声明顺序不该影响流程身份。
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
    # 节点同理：按 id 排序，而不是按声明顺序。
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
    """校验规格并推导执行计划：生产表归属、数据边、拓扑顺序与未知外部读取。

    同一张表有两个生产者直接报错；成环时报出剩余节点。就绪队列每步排序，使同一份规格
    总得到同一个执行顺序——双跑比对与摘要都依赖这种确定性。
    """
    validate_flow(spec)
    produced: dict[str, str] = {}
    for node in spec.nodes:
        for table in node.writes:
            owner = produced.get(table)
            if owner is not None and owner != node.id:
                raise VibeError(f"表 {table} 有两个生产者: {owner} 和 {node.id}")
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
    # 入度即尚未满足的上游依赖数；入度为 0 的节点可以立即执行。
    degree = {node.id: 0 for node in spec.nodes}
    for edge in edges:
        degree[edge.to_node] += 1
    # 初始就绪集排序，保证拓扑序稳定。
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
                ready.sort()  # 多个节点同时就绪时按 id 排序，保持输出确定
    if len(order) != len(spec.nodes):
        cycle = sorted(degree.keys() - set(order))
        raise VibeError(f"流程包含依赖环: {', '.join(cycle)}")
    return FlowPlan(spec, order, edges, produced, materialized, sorted(unknown), digest_of(spec))


def suggest_optimizations(plan: FlowPlan) -> list[OptimizationHint]:
    """给出只读优化提示：未使用的物化、死节点、只被一个节点消费的中间表。"""
    hints: list[OptimizationHint] = []
    outputs = set(plan.spec.outputs)

    def consumers(table: str) -> list[str]:
        return [node.id for node in plan.spec.nodes if table in node.reads]

    for entry in plan.spec.materialize:
        if entry.table not in outputs and not consumers(entry.table):
            hints.append(
                OptimizationHint("unused-materialize", [], [entry.table], f"{entry.table} 没有消费者")
            )
    for node in plan.spec.nodes:
        if not node.writes:
            continue
        if not any(table in outputs or consumers(table) for table in node.writes):
            hints.append(
                OptimizationHint("dead-node", [node.id], sorted(node.writes), f"{node.id} 写入了未被使用的表")
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
                    f"{node.id} 仅供给 {into}；合并需要显式批准",
                )
            )
    return hints


def format_flow_warning(entry: str | None, warning: FlowWarning) -> str:
    """把警告渲染成“入口:行号 [代码] 消息”的单行文本，供命令行输出。"""
    where = entry or ""
    if warning.line is not None:
        where += f":{warning.line}"
    return f"{where} [{warning.code}] {warning.message}".strip()


def describe_flow(plan: FlowPlan, *, steps: bool = False) -> list[str]:
    """把计划渲染成人读的多行摘要；steps=True 时附带节点内部的步骤说明。"""
    spec = plan.spec
    source = f"code:{spec.entry}" if spec.entry is not None else "flow.json"
    lines = [
        f"流程 {spec.flow}@{plan.digest} db={spec.db} nodes={len(spec.nodes)} "
        f"edges={len(plan.edges)} graph={source}",
        f"  outputs: {', '.join(spec.outputs) or '(未声明)'}",
    ]
    for entry in spec.materialize:
        sheet = f"#{entry.sheet}" if entry.sheet else ""
        load = f" (load={entry.load})" if entry.load else ""
        lines.append(f"  [物化] {entry.file}{sheet} -> {entry.table}{load}")
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
        lines.append(f"  外部读取: {', '.join(plan.unknown_reads)}（必须存在于输入数据库）")
    lines.extend(f"  ! {format_flow_warning(spec.entry, warning)}" for warning in spec.warnings)
    return lines
