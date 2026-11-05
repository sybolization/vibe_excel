/**
 * 命令行：读一个 store，写一张自包含的账页。
 *   node packages/book/src/main.ts build --store <store 目录> --out <html 路径>
 *                                    [--session <会话 id>] [--run <运行 id>]
 *
 * 只读：除 --out 指定的那个文件之外不写任何东西。
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { collectBook } from "./collect.ts";
import { renderBook } from "./render.ts";
import type { BookView } from "./types.ts";

const USAGE = `用法：
  build --store <store 目录> --out <html 路径> [--session <会话 id>] [--run <运行 id>]

产出：
  <out>   自包含的账页，双击打开，不依赖服务器与网络`;

interface Parsed {
  positionals: string[];
  flags: Map<string, string>;
}

function parseArgs(argv: string[]): Parsed {
  const positionals: string[] = [];
  const flags = new Map<string, string>();
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i] ?? "";
    if (token.startsWith("--")) {
      const name = token.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) throw new Error(`参数 --${name} 缺少值\n\n${USAGE}`);
      flags.set(name, next);
      i += 1;
    } else {
      positionals.push(token);
    }
  }
  return { positionals, flags };
}

function required(args: Parsed, name: string): string {
  const value = args.flags.get(name);
  if (value === undefined || value === "") throw new Error(`缺少参数 --${name}\n\n${USAGE}`);
  return value;
}

function summarize(view: BookView): void {
  const session = view.session === null ? "（无会话）" : `${view.session.name}（${view.session.id.slice(0, 8)}）`;
  console.log(`程序   ${view.program === null ? "—" : view.program.name}`);
  console.log(`会话   ${session}`);
  console.log(`运行   ${view.runs.length} 次，其中 ${view.runs.filter((run) => run.hasTranscript).length} 次有对话`);
  console.log(`数据   ${view.revisions.length} 个版本，工作集 ${view.currentRevision === null ? "—" : view.currentRevision.slice(0, 8)}`);
  if (view.inputs !== null) {
    const rows = view.inputs.files.flatMap((file) => file.direct.concat(file.derived));
    const count = (kind: string): number => rows.filter((row) => row.kind === kind).length;
    const orphans = view.inputs.orphans.length === 0 ? "" : `，另有 ${view.inputs.orphans.length} 张无来源`;
    console.log(`输入   ${view.inputs.files.length} 个文件 → ${view.inputs.dbTables} 张表`);
    console.log(`       物化 ${count("materialize")} · 导入 ${count("import")} · 派生 ${count("derive")}${orphans}`);
    console.log(`       当前流程读 ${view.inputs.readTables} 张 · 写 ${view.inputs.writeTables} 张`);
  }
  if (view.graph === null) {
    console.log("流程   这次运行没有 run.json");
    return;
  }
  const steps = view.graph.nodes.reduce((sum, node) => sum + node.steps.length, 0);
  console.log(`流程   ${view.graph.flow} · ${view.graph.nodes.length} 个节点 · ${steps} 步 · 入口 ${view.graph.entry}`);
  for (const node of view.graph.nodes) {
    const io = `${node.reads.join("+") || "—"} → ${node.writes.join("+") || "—"}`;
    console.log(`       ${node.fn.padEnd(18)} ${String(node.steps.length)} 步  ${io}`);
  }
  for (const name of view.graph.outputs) {
    const stat = view.graph.tables[name];
    console.log(`产出   ${name} ${stat === undefined || stat.rows === null ? "?" : stat.rows} 行`);
  }
  for (const warning of view.graph.warnings) console.log(`注意   ${warning}`);
}

function main(argv: string[]): void {
  const args = parseArgs(argv);
  if (args.positionals[0] !== "build") throw new Error(USAGE);

  const store = resolve(required(args, "store"));
  const out = resolve(required(args, "out"));
  const sessionId = args.flags.get("session");
  const runId = args.flags.get("run");

  const view = collectBook({
    store,
    ...(sessionId === undefined ? {} : { sessionId }),
    ...(runId === undefined ? {} : { runId }),
  });
  const html = renderBook(view);
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, html, "utf8");

  summarize(view);
  console.log(`\n落盘   ${out}（${Math.round(html.length / 1024)} KB）`);
}

try {
  main(process.argv.slice(2));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
