/**
 * 命令行：一条命令产出两份东西——lineage.json（机器可读）与 index.html（人可点）。
 *
 *   node packages/sqlmap/src/main.ts build --db <sqlite> --lineage <声明.json> --out <目录> [--csv <目录>]
 *
 * 声明里只写"谁来自谁、怎么算"，行数与列名以真实的库/CSV 为准，写错就报错。
 */

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { buildLineage } from "./build.ts";
import { renderHtml } from "./render.ts";
import type { BuiltLineage } from "./lineage.ts";

const USAGE = `用法：
  build --db <sqlite 路径> --lineage <血缘声明.json> --out <输出目录> [--csv <输出 CSV 目录>]

产出：
  <out>/lineage.json   校验后的血缘数据（含真实行数与列名）
  <out>/index.html     自包含的两级界面，可直接双击打开`;

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
      if (next === undefined || next.startsWith("--")) throw new Error(`参数 --${name} 缺少值`);
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

function summarize(built: BuiltLineage): void {
  const inputs = built.tables.filter((t) => t.kind === "input");
  const outputs = built.tables.filter((t) => t.kind === "output");
  console.log(`案例  ${built.case.title}（${built.case.id}）`);
  console.log(`输入  ${inputs.length} 张表：${inputs.map((t) => `${t.label} ${t.rows ?? "?"} 行`).join(" · ")}`);
  console.log(`输出  ${outputs.length} 张表：${outputs.map((t) => `${t.label} ${t.rows ?? "?"} 行`).join(" · ")}`);
  console.log(`列级   ${built.columns.length} 条映射，其中自引用 ${built.columns.filter((c) => c.self).length} 条`);
  console.log(`行约束 ${built.filters.length} 条`);
  for (const warning of built.warnings) console.log(`注意  ${warning}`);
}

function main(argv: string[]): void {
  const args = parseArgs(argv);
  const command = args.positionals[0];
  if (command !== "build") throw new Error(USAGE);

  const dbPath = required(args, "db");
  const lineagePath = required(args, "lineage");
  const outDir = required(args, "out");
  const csvDir = args.flags.get("csv");

  const doc: unknown = JSON.parse(readFileSync(lineagePath, "utf8"));
  const built = buildLineage({ doc, dbPath, ...(csvDir ? { csvDir } : {}) });
  mkdirSync(outDir, { recursive: true });
  writeFileSync(join(outDir, "lineage.json"), `${JSON.stringify(built, null, 2)}\n`, "utf8");
  writeFileSync(join(outDir, "index.html"), renderHtml(built), "utf8");

  summarize(built);
  console.log(`\n落盘  ${join(outDir, "lineage.json")}`);
  console.log(`      ${join(outDir, "index.html")}`);
}

try {
  main(process.argv.slice(2));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
