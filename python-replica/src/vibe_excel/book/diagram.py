"""Layer files → sheets → tables → recorded compute nodes → tables."""

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
    return {
        "id": node_id, "kind": kind, "ref": ref, "label": label, "rank": rank,
        "run": False, "loose": False, "rows": None, "columns": None, "bytes": None,
        "sheetCount": None, "tableCount": None, "file": None, "elapsedMs": None, "runShort": None,
    }


def build_diagram(
    input: BuildDiagramInput | None = None,
    *,
    inputs: InputsView | None = None,
    graph: GraphView | None = None,
) -> DiagramView | None:
    if input is not None:
        inputs, graph = input["inputs"], input["graph"]
    if inputs is None or (not inputs["files"] and not inputs["orphans"]):
        return None
    nodes: list[DiagramNode] = []
    edges: list[DiagramEdge] = []
    index: dict[str, DiagramNode] = {}
    seen: set[tuple[str, str]] = set()

    def push(node: DiagramNode) -> DiagramNode:
        nodes.append(node)
        index[node["id"]] = node
        return node

    def link(upstream: str, downstream: str, kind: DiagramEdgeKind) -> None:
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
            compute["file"], compute["elapsedMs"] = writer["file"] or None, writer["elapsedMs"]
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
    # Monotonic, capped ranks also make seeded circular dependencies drawable.
    for _ in range(MAX_RANK + 2):
        progressed = False
        for compute in computes.values():
            rank = min(MAX_RANK, max([2, *(ranks.get(name, 2) for name in incoming.get(compute["id"], []))]) + 1)
            if ranks[compute["id"]] != rank:
                ranks[compute["id"]] = compute["rank"] = rank
                progressed = True
            for table_id in outgoing.get(compute["id"], []):
                table = index.get(table_id)
                if table is not None and ranks[table_id] != rank + 1:
                    ranks[table_id] = table["rank"] = rank + 1
                    progressed = True
        if not progressed:
            break

    rank_count = max((node["rank"] + 1 for node in nodes), default=0)
    loose_tables = [node for node in nodes if node["kind"] == "table" and node["loose"]]
    if loose_tables:
        for node in loose_tables:
            node["rank"] = rank_count
        rank_count += 1

    for node in nodes:
        if node["kind"] == "table" and node["run"]:
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
        upstream, downstream = index.get(edge["from"]), index.get(edge["to"])
        edge["run"] = upstream is not None and downstream is not None and upstream["run"] and downstream["run"]
    return {
        "nodes": nodes,
        "edges": edges,
        "rankCount": rank_count,
        "fileCount": len(inputs["files"]),
        "tableCount": len(table_nodes),
    }


buildDiagram = build_diagram
