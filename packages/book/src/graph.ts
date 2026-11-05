/**
 * 把一次运行的 `run.json` 变成界面要的流程图。
 *
 * 不再重新解析 Python：运行结束时就已把 `graph.nodes[]`（含 `t:` 标注推出的读写表、
 * 函数标题、以及被 `"""…"""` 标注过的步骤与行号）写进 run.json 了，那是权威记录。
 * 这里只做重排与缺省处理。
 */

import type { GraphView, MaterializeView, NodeView, StepView, TableData, TableStat } from "./types.ts";

type Json = Record<string, unknown>;

const asObject = (value: unknown): Json =>
  value !== null && typeof value === "object" ? (value as Json) : {};
const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const asString = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);
const asNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
const stringList = (value: unknown): string[] =>
  asArray(value).filter((item): item is string => typeof item === "string" && item !== "");

/** 步骤：`{line, kind, of, label}`，编号按出现顺序。 */
function readSteps(value: unknown): StepView[] {
  const steps: StepView[] = [];
  for (const raw of asArray(value)) {
    const item = asObject(raw);
    const line = asNumber(item["line"]);
    const label = asString(item["label"]);
    if (line === null || label === null) continue;
    steps.push({
      n: steps.length + 1,
      line,
      kind: asString(item["kind"]) ?? "statement",
      label,
    });
  }
  return steps;
}

function readNodes(value: unknown): NodeView[] {
  const nodes: NodeView[] = [];
  for (const raw of asArray(value)) {
    const item = asObject(raw);
    const id = asString(item["id"]);
    if (id === null) continue;
    const call = asString(item["call"]) ?? id;
    nodes.push({
      id,
      fn: call.includes(":") ? call.slice(call.lastIndexOf(":") + 1) : id,
      file: call.includes(":") ? call.slice(0, call.lastIndexOf(":")) : call,
      title: asString(item["title"]) ?? "",
    name: asString(item["name"]) ?? "",
      reads: stringList(item["reads"]),
      writes: stringList(item["writes"]),
      elapsedMs: null,
      changed: [],
      steps: readSteps(item["steps"]),
    });
  }
  return nodes;
}

function readMaterialize(value: unknown): MaterializeView[] {
  const entries: MaterializeView[] = [];
  for (const raw of asArray(value)) {
    const item = asObject(raw);
    const entry = asObject(item["entry"]);
    const table = asString(entry["table"]);
    if (table === null) continue;
    const digest = asObject(item["digest"]);
    entries.push({
      file: asString(entry["file"]) ?? "—",
      sheet: asString(entry["sheet"]) ?? "—",
      table,
      rows: asNumber(digest["rows"]),
    });
  }
  return entries;
}

export interface BuildGraphInput {
  runId: string;
  tables: Record<string, TableStat>;
  sample: TableData | null;
  runJson: unknown;
}

export function buildGraph(input: BuildGraphInput): GraphView | null {
  const doc = asObject(input.runJson);
  const graph = asObject(doc["graph"]);
  const nodes = readNodes(graph["nodes"]);
  if (nodes.length === 0) return null;

  const rounds = asObject(doc["rounds"]);
  const round = asObject(rounds["a"] ?? rounds["b"]);

  // 每个节点的耗时与它实际改动的表
  const executed = new Map<string, { elapsedMs: number | null; changed: string[] }>();
  for (const raw of asArray(round["nodes"])) {
    const item = asObject(raw);
    const id = asString(item["id"]);
    if (id === null) continue;
    executed.set(id, {
      elapsedMs: asNumber(item["elapsedMs"]),
      changed: stringList(item["changed"]),
    });
  }
  for (const node of nodes) {
    const run = executed.get(node.id);
    if (run === undefined) continue;
    node.elapsedMs = run.elapsedMs;
    node.changed = run.changed;
  }

  const outputs = Object.keys(asObject(round["outputs"]));
  const flowJson = asObject(doc["flowJson"]);

  return {
    runId: input.runId,
    runShort: input.runId.slice(0, 8),
    flow: asString(doc["flow"]) ?? asString(flowJson["flow"]) ?? "—",
    entry: asString(graph["entry"]) ?? asString(flowJson["entry"]) ?? "—",
    asOf: asString(doc["asOf"]),
    codeCommit: asString(doc["codeCommit"]),
    revision: asString(doc["inputRevisionId"]),
    identical: typeof doc["identical"] === "boolean" ? doc["identical"] : null,
    elapsedMs: asNumber(doc["elapsedMs"]),
    materialize: readMaterialize(asObject(round["materialize"])["entries"]),
    nodes,
    outputs: outputs.length > 0 ? outputs : nodes.flatMap((node) => node.writes),
    tables: input.tables,
    sample: input.sample,
    warnings: stringList(graph["warnings"]),
  };
}
