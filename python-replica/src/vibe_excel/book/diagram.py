"""分层：文件 → 工作表 → 表 → 记录的算子节点 → 表。

输入是 inputs 与 graph 两个视图模型，输出是纯几何的 diagram 视图模型；
不读数据库、不写文件。没有文件也没有孤儿表时返回 None。
"""

from .types import (
    BuildDiagramInput,
    DiagramEdge,
    DiagramEdgeKind,
    DiagramKind,
    DiagramNode,
    DiagramView,
    GraphView,
    InputsView,
    InputTableView,
)

MAX_RANK = 24


def _node(node_id: str, kind: DiagramKind, ref: str, label: str, rank: int) -> DiagramNode:
    """创建一个所有可选字段为空的节点，具体数值由调用方按需填充。"""
    return {
        "id": node_id, "name": label, "kind": kind, "ref": ref, "label": label, "rank": rank,
        "run": False, "loose": False, "rows": None, "columns": None, "bytes": None,
        "sheetCount": None, "tableCount": None, "file": None, "elapsedMs": None, "runShort": None,
    }


def build_diagram(
    input: BuildDiagramInput | None = None,
    *,
    inputs: InputsView | None = None,
    graph: GraphView | None = None,
) -> DiagramView | None:
    """把文件、工作表、表和算子排成分层图。

    同一张表被多条路径引用时复用节点；循环依赖通过有限层级上限保证仍可绘制。
    """
    if input is not None:
        inputs, graph = input["inputs"], input["graph"]
    if inputs is None or (not inputs["files"] and not inputs["orphans"]):
        return None
    nodes: list[DiagramNode] = []
    edges: list[DiagramEdge] = []
    index: dict[str, DiagramNode] = {}
    seen: set[tuple[str, str]] = set()

    def push(node: DiagramNode) -> DiagramNode:
        """追加节点并登记到 id 索引。"""
        nodes.append(node)
        index[node["id"]] = node
        return node

    def link(upstream: str, downstream: str, kind: DiagramEdgeKind) -> None:
        """追加一条去重后的有向边；自环直接跳过。"""
        key = upstream, downstream
        if upstream != downstream and key not in seen:
            seen.add(key)
            edges.append({"from": upstream, "to": downstream, "kind": kind, "run": False})

    run_fns = {
        (graph["runShort"], node["fn"]) for node in graph["nodes"]
    } if graph is not None else set()
    sheets: dict[str, DiagramNode] = {}
    table_nodes: dict[str, DiagramNode] = {}

    def ensure_table(row: InputTableView) -> DiagramNode:
        """按表名复用或创建表节点。"""
        node_id = f"t:{row['name']}"
        hit = table_nodes.get(node_id)
        if hit is not None:
            return hit
        node = _node(node_id, "table", row["name"], row["name"], 2)
        node["run"], node["loose"] = row["usage"] is not None, row["kind"] == "unknown"
        node["rows"], node["columns"] = row["rows"], row["columns"]
        table_nodes[node_id] = push(node)
        return node

    for file in inputs["files"]:
        file_id = f"f:{file['name']}"
        if file_id not in index:
            file_node = _node(file_id, "file", file["name"], file["name"], 0)
            file_node["bytes"] = file["bytes"]
            push(file_node)
        sheet_names: set[str] = set()
        for row in file["direct"]:
            table = ensure_table(row)
            if row["sheet"] is None:
                continue
            sheet_names.add(row["sheet"])
            sheet_id = f"s:{file['name']}\0{row['sheet']}"
            sheet = sheets.get(sheet_id)
            if sheet is None:
                sheet = push(_node(sheet_id, "sheet", f"{file['name']}\0{row['sheet']}", row["sheet"], 1))
                sheets[sheet_id] = sheet
            link(file_id, sheet_id, "contain")
            link(sheet_id, table["id"], "make")
            if sheet["rows"] is None or (row["rows"] is not None and row["rows"] > sheet["rows"]):
                sheet["rows"] = row["rows"]
            if sheet["columns"] is None:
                sheet["columns"] = row["columns"]
        index[file_id]["sheetCount"] = len(sheet_names)

    derived = [row for file in inputs["files"] for row in file["derived"]] + list(inputs["orphans"])
    derived.sort(key=lambda row: (row["depth"], row["name"]))
    computes: dict[str, DiagramNode] = {}
    for row in derived:
        table = ensure_table(row)
        writer = row["writer"]
        if writer is None or not row["from"]:
            table["loose"] = True
            continue
        compute_id = f"c:{writer['runShort']}\0{writer['fn']}"
        compute = computes.get(compute_id)
        current = (writer["runShort"], writer["fn"]) in run_fns and row["usage"] is not None
        if compute is None:
            compute = _node(compute_id, "compute", writer["fn"], writer["fn"], 3)
            compute["name"], compute["file"], compute["elapsedMs"] = (
                writer["name"] or writer["fn"], writer["file"] or None, writer["elapsedMs"]
            )
            compute["runShort"] = writer["runShort"]
            computes[compute_id] = push(compute)
        compute["run"] = compute["run"] or current
        for upstream in row["from"]:
            link(f"t:{upstream}", compute_id, "read")
        link(compute_id, table["id"], "write")

    ranks = {node["id"]: node["rank"] if node["kind"] in ("file", "sheet") else 2 for node in nodes}
    incoming: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        incoming.setdefault(edge["to"], []).append(edge["from"])
        outgoing.setdefault(edge["from"], []).append(edge["to"])
    # 单调且有上限的层级编号，也能让刻意构造的循环依赖可绘制。
    for _ in range(MAX_RANK + 2):
        progressed = False
        for compute in computes.values():
            rank = min(MAX_RANK, max([2, *(ranks.get(name, 2) for name in incoming.get(compute["id"], []))]) + 1)
            if ranks[compute["id"]] != rank:
                ranks[compute["id"]] = compute["rank"] = rank
                progressed = True
            for table_id in outgoing.get(compute["id"], []):
                table_node = index.get(table_id)
                if table_node is not None and ranks[table_id] != min(MAX_RANK + 1, rank + 1):
                    ranks[table_id] = table_node["rank"] = min(MAX_RANK + 1, rank + 1)
                    progressed = True
        if not progressed:
            break

    rank_count = max((node["rank"] + 1 for node in nodes), default=0)
    loose_tables = [node for node in nodes if node["kind"] == "table" and node["loose"]]
    if loose_tables:
        # 孤儿表统一放到最末一层，避免打断主链的层级递进。
        for node in loose_tables:
            node["rank"] = rank_count
        rank_count += 1

    for node in nodes:
        if node["kind"] == "table" and node["run"]:
            # 表被当前运行用到时，把 run 标记沿 make（sheet→table）与 contain（file→sheet）边向上传给工作表和文件。
            for upstream in incoming.get(node["id"], []):
                parent = index.get(upstream)
                if parent is not None and parent["kind"] in ("sheet", "file"):
                    parent["run"] = True
    for node in nodes:
        if node["kind"] == "sheet" and node["run"]:
            for upstream in incoming.get(node["id"], []):
                parent = index.get(upstream)
                if parent is not None:
                    parent["run"] = True
        if node["kind"] == "sheet":
            node["tableCount"] = len(outgoing.get(node["id"], []))
    for edge in edges:
        source_node, target_node = index.get(edge["from"]), index.get(edge["to"])
        edge["run"] = (
            source_node is not None
            and target_node is not None
            and source_node["run"]
            and target_node["run"]
        )
    return {
        "nodes": nodes,
        "edges": edges,
        "rankCount": rank_count,
        "fileCount": len(inputs["files"]),
        "tableCount": len(table_nodes),
    }


buildDiagram = build_diagram
