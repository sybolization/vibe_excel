/**
 * 画布：把「文件 → sheet → 表 → 计算节点 → 表」拼成一张分层的图。
 *
 * 这不是新的事实来源，只是把 inputs（来路）和 graph（这次运行）摆到同一张图上：
 *
 *   rank 0  文件
 *   rank 1  sheet
 *   rank 2  物化 / 导入出来的表
 *   rank 3+ 计算节点，每个节点后面跟着它写出来的表
 *
 * 位置由依赖深度算，不由人摆；节点身份就是它的 id，前端只按 rank 排版。
 */

import type {
  DiagramEdge,
  DiagramEdgeKind,
  DiagramNode,
  DiagramView,
  GraphView,
  InputTableView,
  InputsView,
} from "./types.ts";

export interface BuildDiagramInput {
  inputs: InputsView | null;
  graph: GraphView | null;
}

/** 依赖成环时不至于死循环；正常数据三代就到底了。 */
const MAX_RANK = 24;

const tableIdOf = (name: string): string => `t:${name}`;
const fileIdOf = (name: string): string => `f:${name}`;
const sheetIdOf = (file: string, sheet: string): string => `s:${file}\u0000${sheet}`;
const computeIdOf = (runShort: string, fn: string): string => `c:${runShort}\u0000${fn}`;

const blank = {
  rows: null,
  columns: null,
  bytes: null,
  sheetCount: null,
  tableCount: null,
  file: null,
  elapsedMs: null,
  runShort: null,
} as const;

export function buildDiagram(input: BuildDiagramInput): DiagramView | null {
  const inputs = input.inputs;
  if (inputs === null) return null;
  const graph = input.graph;
  if (inputs.files.length === 0 && inputs.orphans.length === 0) return null;

  const nodes: DiagramNode[] = [];
  const edges: DiagramEdge[] = [];
  const index = new Map<string, DiagramNode>();
  const seen = new Set<string>();

  const push = (node: DiagramNode): DiagramNode => {
    nodes.push(node);
    index.set(node.id, node);
    return node;
  };
  const link = (from: string, to: string, kind: DiagramEdgeKind): void => {
    if (from === to) return;
    const key = `${from}\u0000${to}`;
    if (seen.has(key)) return;
    seen.add(key);
    edges.push({ from, to, kind, run: false });
  };

  const runFns = new Set<string>();
  if (graph !== null) {
    for (const node of graph.nodes) runFns.add(`${graph.runShort}\u0000${node.fn}`);
  }

  /** sheet 节点：同一文件同名 sheet 只建一次。 */
  const sheets = new Map<string, DiagramNode>();
  const ensureSheet = (file: string, sheet: string): DiagramNode => {
    const id = sheetIdOf(file, sheet);
    const hit = sheets.get(id);
    if (hit !== undefined) return hit;
    const node = push({ ...blank, id, kind: "sheet", ref: `${file}\u0000${sheet}`, label: sheet, rank: 1, run: false, loose: false });
    sheets.set(id, node);
    return node;
  };

  const tableNodes = new Map<string, DiagramNode>();
  const ensureTable = (row: InputTableView): DiagramNode => {
    const id = tableIdOf(row.name);
    const hit = tableNodes.get(id);
    if (hit !== undefined) return hit;
    const node = push({
      ...blank,
      id,
      kind: "table",
      ref: row.name,
      label: row.name,
      rank: 2,
      run: row.usage !== null,
      loose: row.kind === "unknown",
      rows: row.rows,
      columns: row.columns,
    });
    tableNodes.set(id, node);
    return node;
  };

  /* 1) 文件 → sheet → 直接物化 / 导入的表 */
  for (const file of inputs.files) {
    const id = fileIdOf(file.name);
    if (!index.has(id)) {
      push({
        ...blank,
        id,
        kind: "file",
        ref: file.name,
        label: file.name,
        rank: 0,
        run: false,
        loose: false,
        bytes: file.bytes,
      });
    }
    const sheetNames = new Set<string>();
    for (const row of file.direct) {
      const table = ensureTable(row);
      if (row.sheet === null) continue;
      sheetNames.add(row.sheet);
      const sheet = ensureSheet(file.name, row.sheet);
      table.rank = 2;
      link(id, sheet.id, "contain");
      link(sheet.id, table.id, "make");
      if (sheet.rows === null || (row.rows !== null && row.rows > sheet.rows)) sheet.rows = row.rows;
      if (sheet.columns === null) sheet.columns = row.columns;
    }
    const fileNode = index.get(id);
    if (fileNode !== undefined) fileNode.sheetCount = sheetNames.size;
  }

  /* 2) 派生表：有写出者就画计算节点，没有就只连表 */
  const derived: InputTableView[] = [];
  for (const file of inputs.files) derived.push(...file.derived);
  derived.push(...inputs.orphans);
  derived.sort((a, b) => a.depth - b.depth || a.name.localeCompare(b.name));

  const computes = new Map<string, DiagramNode>();
  for (const row of derived) {
    const table = ensureTable(row);
    const writer = row.writer;
    if (writer === null || row.from.length === 0) {
      table.loose = true;
      continue;
    }
    const id = computeIdOf(writer.runShort, writer.fn);
    let node = computes.get(id);
    if (node === undefined) {
      node = push({
        ...blank,
        id,
        kind: "compute",
        ref: writer.fn,
        label: writer.fn,
        rank: 3,
        run: runFns.has(`${writer.runShort}\u0000${writer.fn}`) && row.usage !== null,
        loose: false,
        file: writer.file === "" ? null : writer.file,
        elapsedMs: writer.elapsedMs,
        runShort: writer.runShort,
      });
      computes.set(id, node);
    }
    for (const upstream of row.from) link(tableIdOf(upstream), id, "read");
    link(id, table.id, "write");
  }

  /* 3) 排第几列：按依赖深度一轮推一遍，推不动就按最左算 */
  const rankOf = new Map<string, number>();
  for (const node of nodes) {
    if (node.kind === "file") rankOf.set(node.id, 0);
    else if (node.kind === "sheet") rankOf.set(node.id, 1);
    else if (node.kind === "table" && node.loose) rankOf.set(node.id, 2);
    else rankOf.set(node.id, 2);
  }

  const rankOfReads = (node: DiagramNode): number => {
    let top = -1;
    for (const edge of edges) {
      if (edge.to !== node.id) continue;
      const value = rankOf.get(edge.from);
      if (value !== undefined && value > top) top = value;
    }
    return top;
  };

  for (const row of derived) {
    const table = index.get(tableIdOf(row.name));
    if (table === undefined) continue;
    const writer = row.writer;
    if (writer === null) {
      table.rank = 2;
      continue;
    }
    const compute = computes.get(computeIdOf(writer.runShort, writer.fn));
    if (compute === undefined) continue; // 孤立表：留到最后面
    const base = Math.max(2, rankOfReads(compute));
    compute.rank = Math.min(MAX_RANK, base + 1);
    rankOf.set(compute.id, compute.rank);
    table.rank = Math.min(MAX_RANK + 1, compute.rank + 1);
    rankOf.set(table.id, table.rank);
  }

  let rankCount = 0;
  for (const node of nodes) rankCount = Math.max(rankCount, node.rank + 1);
  const looseTables = nodes.filter((node) => node.kind === "table" && node.loose);
  if (looseTables.length > 0) {
    for (const node of looseTables) {
      node.rank = rankCount;
      rankOf.set(node.id, node.rank);
    }
    rankCount += 1;
  }

  /* 4) 往上带：sheet 和文件算不算"本次运行用到" */
  for (const node of nodes) {
    if (node.kind !== "table" || !node.run) continue;
    for (const edge of edges) {
      if (edge.to !== node.id) continue;
      const parent = index.get(edge.from);
      if (parent !== undefined && (parent.kind === "sheet" || parent.kind === "file")) parent.run = true;
    }
  }
  for (const node of nodes) {
    if (node.kind !== "sheet" || !node.run) continue;
    for (const edge of edges) {
      if (edge.to === node.id) {
        const parent = index.get(edge.from);
        if (parent !== undefined) parent.run = true;
      }
    }
  }

  /* 5) 边亮不亮：两端都在本次运行里才算 */
  for (const edge of edges) {
    const from = index.get(edge.from);
    const to = index.get(edge.to);
    edge.run = from !== undefined && to !== undefined && from.run && to.run;
  }

  /* 6) 计数 */
  for (const node of nodes) {
    if (node.kind === "table") continue;
    if (node.kind === "sheet") {
      node.tableCount = edges.filter((edge) => edge.from === node.id).length;
    }
  }
  let tableCount = 0;
  for (const node of nodes) if (node.kind === "table") tableCount += 1;

  return {
    nodes,
    edges,
    rankCount,
    fileCount: inputs.files.length,
    tableCount,
  };
}
