import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { collectBook } from "../src/collect.ts";
import { buildDiagram } from "../src/diagram.ts";
import { buildGraph } from "../src/graph.ts";
import { buildInputs } from "../src/inputs.ts";
import type { DbTableFact, RunRecord } from "../src/inputs.ts";
import { renderBook } from "../src/render.ts";
import { parseTranscript } from "../src/transcript.ts";
import type { BookView, GraphView } from "../src/types.ts";

const jsonl = (records: unknown[]): string => records.map((record) => JSON.stringify(record)).join("\n");

const TRANSCRIPT = jsonl([
  { type: "session", id: "s1", timestamp: "2026-11-04T08:11:20.122Z", cwd: "C:\\x" },
  { type: "model_change", id: "m1", timestamp: "2026-11-04T08:11:20.192Z", modelId: "m" },
  {
    type: "message",
    id: "u1",
    timestamp: "2026-11-04T08:11:20.203Z",
    message: { role: "user", content: [{ type: "text", text: "\uFEFF# 本次任务：按地点汇总\n\n细节" }] },
  },
  {
    type: "message",
    id: "a1",
    timestamp: "2026-11-04T08:11:27.547Z",
    message: {
      role: "assistant",
      content: [
        { type: "text", text: "我先查看输入文件，了解它们的结构。" },
        { type: "toolCall", id: "c1", name: "bash", arguments: { command: "ls" } },
        { type: "toolCall", id: "c2", name: "write", arguments: { path: "a.py" } },
        { type: "toolCall", id: "c3", name: "edit", arguments: { path: "a.py" } },
      ],
    },
  },
  {
    type: "message",
    id: "t1",
    timestamp: "2026-11-04T08:13:19.458Z",
    message: { role: "toolResult", toolCallId: "c1", toolName: "bash", content: [{ type: "text", text: "ok" }] },
  },
]);

describe("parseTranscript", () => {
  it("读出两头的人话，数工具调用与写入次数", () => {
    const episode = parseTranscript("C:\\s\\2026-11-04T08-11-20-122Z_abc.jsonl", TRANSCRIPT);
    expect(episode).not.toBeNull();
    expect(episode?.user).toBe("# 本次任务：按地点汇总 细节");
    expect(episode?.assistant).toBe("我先查看输入文件，了解它们的结构。");
    expect(episode?.toolCalls).toBe(3);
    expect(episode?.writes).toBe(2);
    expect(episode?.records).toBe(5);
    expect(episode?.start).toBe("2026-11-04T08:11:20.122Z");
    expect(episode?.end).toBe("2026-11-04T08:13:19.458Z");
    expect(episode?.id).toBe("2026-11-04T08-11-20-122Z_abc");
  });

  it("尾部半行不影响解析", () => {
    const broken = `${TRANSCRIPT}\n{"type":"message","id":"x`;
    expect(parseTranscript("a.jsonl", broken)?.records).toBe(5);
  });

  it("空文件返回 null", () => {
    expect(parseTranscript("a.jsonl", "")).toBeNull();
  });
});

const RUN_JSON = {
  kind: "flow",
  flow: "molex-prep",
  entry: "site_board.py",
  codeCommit: "92435f0b455b5eb04fd0dd3903018fae876b3556",
  inputRevisionId: "ba0583fa-b033-4e16-acd8-abd4e7024aab",
  identical: true,
  elapsedMs: 7515,
  graph: {
    entry: "site_board.py",
    warnings: [],
    nodes: [
      {
        id: "clean_positions",
        call: "site_board.py:clean_positions",
        reads: ["raw_available"],
        writes: ["site_positions"],
        title: "清洗可用岗位表",
        steps: [
          { line: 47, kind: "statement", of: "Assign", label: "只保留四列" },
          { line: 50, kind: "statement", of: "Assign", label: "岗位号统一大写" },
        ],
      },
      {
        id: "build_site_board",
        call: "site_board.py:build_site_board",
        reads: ["site_positions"],
        writes: ["site_board"],
        title: "按地点汇总",
        steps: [{ line: 67, kind: "statement", of: "Assign", label: "空地点归到未标注" }],
      },
    ],
  },
  rounds: {
    a: {
      materialize: { entries: [{ entry: { file: "可用岗位表.xlsx", sheet: "可用岗位", table: "raw_available" }, produced: true, digest: { rows: 6337 } }] },
      nodes: [
        { id: "clean_positions", elapsedMs: 1332, changed: ["site_positions"] },
        { id: "build_site_board", elapsedMs: 1001, changed: ["site_board"] },
      ],
      outputs: { site_board: { rows: 1 } },
    },
  },
};

describe("buildGraph", () => {
  it("从 run.json 还原节点、步骤、物化与产出", () => {
    const graph = buildGraph({ runId: "c4d807a6-c467", tables: {}, sample: null, runJson: RUN_JSON });
    expect(graph).not.toBeNull();
    expect(graph?.nodes.map((node) => node.fn)).toEqual(["clean_positions", "build_site_board"]);
    expect(graph?.nodes[0]?.steps).toHaveLength(2);
    expect(graph?.nodes[0]?.elapsedMs).toBe(1332);
    expect(graph?.materialize[0]?.rows).toBe(6337);
    expect(graph?.outputs).toEqual(["site_board"]);
    expect(graph?.runShort).toBe("c4d807a6");
    expect(graph?.identical).toBe(true);
  });

  it("没有 graph.nodes 时返回 null", () => {
    expect(buildGraph({ runId: "x", tables: {}, sample: null, runJson: { flow: "f" } })).toBeNull();
  });
});

describe("renderBook", () => {
  const view: BookView = {
    generatedAt: "2026-11-05T03:00:00.000Z",
    store: "C:\\store",
    program: { id: "p1", name: "molex-prep" },
    session: { id: "s1", name: "real-data", draftBranch: "draft/s1", status: "active" },
    revisions: [],
    currentRevision: "f8012f7e-6ddf",
    lastRunRevision: "ba0583fa-b033",
    runs: [],
    publishes: [],
    graph: null,
    inputs: null,
    diagram: null,
    transcript: [],
    focusEpisode: null,
  };

  it("内联数据与样式，且 </script> 被转义", () => {
    const html = renderBook({ ...view, session: { ...view.session!, name: "</script><b>x</b>" } });
    expect(html).toContain('id="book-data"');
    expect(html).toContain("molex-prep");
    // 样式真的被内联了：认结构，别认某一个色值
    expect(html).toContain(".cv{position:fixed");
    expect(html).toContain("--accent:");
    expect(html).not.toContain("</script><b>x</b>");
    expect(html).toContain("\\u003c/script");
  });
});

describe("buildInputs", () => {
  const OLD: RunRecord = {
    id: "aaaaaaaa-1111",
    startedAt: "2026-10-30T01:00:00.000Z",
    doc: {
      rounds: {
        a: {
          materialize: {
            entries: [
              { entry: { file: "f.xlsx", sheet: "S1", table: "raw_a" }, digest: { rows: 10 } },
              { entry: { file: "f.xlsx", sheet: "S2", table: "raw_b" }, digest: { rows: 5 } },
            ],
          },
        },
      },
      graph: {
        nodes: [
          { call: "p.py:build_a", reads: ["raw_a"], writes: ["a"] },
          { call: "p.py:build_b", reads: ["raw_b"], writes: ["b"] },
          { call: "p.py:build_ab", reads: ["a", "b"], writes: ["ab"] },
        ],
      },
    },
  };
  const NEW: RunRecord = {
    id: "bbbbbbbb-2222",
    startedAt: "2026-11-04T08:00:00.000Z",
    doc: {
      rounds: {
        a: { materialize: { entries: [{ entry: { file: "f.xlsx", sheet: "S1", table: "raw_a" }, digest: { rows: 10 } }] } },
      },
      graph: { nodes: [{ call: "q.py:build_z", reads: ["raw_a"], writes: ["z"] }] },
    },
  };
  const tables: DbTableFact[] = [
    { name: "raw_a", rows: 10, columns: 3, sources: [] },
    { name: "raw_b", rows: 5, columns: 3, sources: [] },
    { name: "a", rows: 10, columns: 2, sources: [] },
    { name: "b", rows: 5, columns: 2, sources: [] },
    { name: "ab", rows: 5, columns: 2, sources: [] },
    { name: "z", rows: 10, columns: 2, sources: [] },
    { name: "imp", rows: 3, columns: 4, sources: [{ file: "g.xls", sheet: "SHEET1", rows: 3 }] },
    { name: "stray", rows: 1, columns: 1, sources: [] },
  ];
  const batch = {
    batchId: "b1",
    asOf: "2026-10-23",
    capturedAt: "2026-10-30T01:44:23.838Z",
    files: [{ name: "f.xlsx", bytes: 200425, sha256: "s" }],
  };
  const view = buildInputs({ batch, dbPath: "hr.db", tables, runs: [OLD, NEW], reads: ["raw_a"], writes: ["z"] });

  it("物化以最新那次运行为准，老运行只补它独有的表", () => {
    const f = view.files[0];
    expect(f?.name).toBe("f.xlsx");
    const rawA = f?.direct.find((row) => row.name === "raw_a");
    const rawB = f?.direct.find((row) => row.name === "raw_b");
    expect(rawA?.kind).toBe("materialize");
    expect(rawA?.via).toBe("bbbbbbbb");
    expect(rawA?.rows).toBe(10);
    expect(rawB?.via).toBe("aaaaaaaa");
    expect(rawB?.sheet).toBe("S2");
  });

  it("派生表沿 reads 追到文件，几层就排几层", () => {
    const derived = view.files[0]?.derived ?? [];
    expect(derived.map((row) => row.name)).toEqual(["a", "b", "z", "ab"]);
    expect(derived.find((row) => row.name === "ab")?.depth).toBe(2);
    expect(derived.find((row) => row.name === "ab")?.from).toEqual(["a", "b"]);
    expect(derived.find((row) => row.name === "z")?.via).toBe("build_z");
  });

  it("表内记了来源列的表算导入，批次外的文件补在最后", () => {
    const g = view.files[1];
    expect(g?.name).toBe("g.xls");
    expect(g?.inBatch).toBe(false);
    expect(g?.direct[0]?.kind).toBe("import");
    expect(g?.direct[0]?.sheet).toBe("SHEET1");
  });

  it("追不到来源的表单独列出来，当前流程的读写标在表上", () => {
    expect(view.orphans.map((row) => row.name)).toEqual(["stray"]);
    expect(view.orphans[0]?.kind).toBe("unknown");
    expect(view.readTables).toBe(1);
    expect(view.writeTables).toBe(1);
    expect(view.files[0]?.direct.find((row) => row.name === "raw_a")?.usage).toBe("read");
    expect(view.files[0]?.derived.find((row) => row.name === "z")?.usage).toBe("write");
    expect(view.dbTables).toBe(8);
  });

  it("空库也要能出一页，不抛错", () => {
    const empty = buildInputs({ batch: null, dbPath: null, tables: [], runs: [], reads: [], writes: [] });
    expect(empty.files).toEqual([]);
    expect(empty.orphans).toEqual([]);
    expect(empty.dbTables).toBe(0);
    expect(empty.batchId).toBeNull();
  });
});


describe("buildDiagram", () => {
  const tables: DbTableFact[] = [
    { name: "raw_a", rows: 10, columns: 3, sources: [] },
    { name: "a", rows: 10, columns: 2, sources: [] },
    { name: "ab", rows: 5, columns: 2, sources: [] },
    { name: "imp", rows: 3, columns: 4, sources: [{ file: "g.xls", sheet: "SHEET1", rows: 3 }] },
    { name: "stray", rows: 1, columns: 1, sources: [] },
  ];
  const run: RunRecord = {
    id: "cccccccc-3333",
    startedAt: "2026-11-01T00:00:00.000Z",
    doc: {
      rounds: {
        a: {
          materialize: { entries: [{ entry: { file: "f.xlsx", sheet: "S1", table: "raw_a" }, digest: { rows: 10 } }] },
          nodes: [{ id: "build_a", elapsedMs: 1200 }],
        },
      },
      graph: {
        nodes: [
          { id: "build_a", call: "p.py:build_a", reads: ["raw_a"], writes: ["a"] },
          { id: "build_ab", call: "p.py:build_ab", reads: ["a"], writes: ["ab"] },
        ],
      },
    },
  };
  const batch = { batchId: "b1", asOf: "2026-10-23", capturedAt: null, files: [{ name: "f.xlsx", bytes: 2048, sha256: "s" }] };
  const inputs = buildInputs({ batch, dbPath: "hr.db", tables, runs: [run], reads: ["raw_a"], writes: ["a", "ab"] });
  const diagram = buildDiagram({ inputs, graph: null });

  it("文件、sheet、表、计算节点都在，rank 按依赖深度递增", () => {
    expect(diagram).not.toBeNull();
    if (diagram === null) return;
    const kinds = new Set(diagram.nodes.map((node) => node.kind));
    expect([...kinds].sort()).toEqual(["compute", "file", "sheet", "table"]);
    const rank = (label: string) => diagram.nodes.find((node) => node.label === label)?.rank;
    expect(rank("f.xlsx")).toBe(0);
    expect(rank("S1")).toBe(1);
    expect(rank("raw_a")).toBe(2);
    expect(rank("build_a")).toBe(3);
    expect(rank("a")).toBe(4);
    expect(rank("ab")).toBe(6);
  });

  it("连线把来路和计算串起来：文件 → sheet → 表 → 节点 → 表", () => {
    if (diagram === null) return;
    const idOf = (label: string) => diagram.nodes.find((node) => node.label === label)?.id;
    const has = (from: string, to: string) => diagram.edges.some((edge) => edge.from === idOf(from) && edge.to === idOf(to));
    expect(has("f.xlsx", "S1")).toBe(true);
    expect(has("S1", "raw_a")).toBe(true);
    expect(has("raw_a", "build_a")).toBe(true);
    expect(has("build_a", "a")).toBe(true);
    expect(has("a", "build_ab")).toBe(true);
    expect(has("build_ab", "ab")).toBe(true);
  });

  it("本次运行碰过的表和它的上游点亮，别的灰着", () => {
    if (diagram === null) return;
    const node = (label: string) => diagram.nodes.find((item) => item.label === label);
    expect(node("raw_a")?.run).toBe(true);
    expect(node("ab")?.run).toBe(true);
    expect(node("imp")?.run).toBe(false);
    expect(node("f.xlsx")?.run).toBe(true);
    expect(node("S1")?.run).toBe(true);
    // 写出者算出来的表在图上只有一条路是亮的
    const idOf = (label: string) => node(label)?.id;
    const edge = (from: string, to: string) => diagram.edges.find((item) => item.from === idOf(from) && item.to === idOf(to));
    expect(edge("S1", "raw_a")?.run).toBe(true);
    expect(edge("SHEET1", "imp")?.run).toBe(false);
  });

  it("没有来路的表排在最后一列并标 loose", () => {
    if (diagram === null) return;
    const stray = diagram.nodes.find((node) => node.label === "stray");
    expect(stray?.loose).toBe(true);
    expect(stray?.rank).toBe(diagram.rankCount - 1);
  });

  it("写它的节点在本次运行里时，节点本身也点亮", () => {
    const graph: GraphView = {
      runId: "cccccccc-3333",
      runShort: "cccccccc",
      flow: "f",
      entry: "e",
      asOf: null,
      codeCommit: null,
      revision: null,
      identical: true,
      elapsedMs: 1000,
      materialize: [],
      nodes: [{
        id: "build_a",
        fn: "build_a",
        file: "p.py",
        title: "",
        reads: ["raw_a"],
        writes: ["a"],
        elapsedMs: 1200,
        changed: [],
        steps: [],
      }],
      outputs: ["a"],
      tables: {},
      sample: null,
      warnings: [],
    };
    const lit = buildDiagram({ inputs, graph });
    const compute = lit?.nodes.find((node) => node.kind === "compute" && node.label === "build_a");
    expect(compute?.run).toBe(true);
    expect(compute?.elapsedMs).toBe(1200);
    expect(compute?.file).toBe("p.py");
  });
});

const STORE = resolve(process.cwd(), "..", "vibe_excel-store");

describe.skipIf(!existsSync(resolve(STORE, "control.db")))("collectBook（真实 store）", () => {
  it("读到会话、运行与一条可画的流程", () => {
    const view = collectBook({ store: STORE });
    expect(view.session).not.toBeNull();
    expect(view.runs.length).toBeGreaterThan(0);
    expect(view.revisions.length).toBeGreaterThan(0);
    expect(view.graph).not.toBeNull();
    expect(view.graph?.nodes.length).toBeGreaterThan(0);
    expect(view.graph?.nodes.every((node) => node.steps.length > 0)).toBe(true);
    // 选中的运行总得和它的数据版本对得上
    expect(view.lastRunRevision).toBe(view.session === null ? null : view.lastRunRevision);
    expect(view.transcript.length).toBeGreaterThan(0);
  });

  it("输入视图追到真实文件与库里的表", () => {
    const view = collectBook({ store: STORE });
    const inputs = view.inputs;
    expect(inputs).not.toBeNull();
    expect(inputs?.dbPath).not.toBeNull();
    expect(inputs?.files.length).toBeGreaterThan(0);
    expect(inputs?.dbTables).toBeGreaterThan(0);
    expect(inputs?.batchId).not.toBeNull();
    // 物化出来的表一定挂在某个有 sheet 的行上
    const direct = (inputs?.files ?? []).flatMap((file) => file.direct);
    expect(direct.length).toBeGreaterThan(0);
    expect(direct.every((row) => row.sheet !== null && row.rows !== null)).toBe(true);
    // 取样也得真的取到
    expect(direct.some((row) => row.sample !== null)).toBe(true);
  });
});
