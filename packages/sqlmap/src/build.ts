/**
 * build：把作者声明的血缘 + 真实的库/文件对齐成可渲染的数据。
 *
 * 这一层的价值全在**校验**：声明的每一列都必须能在真实来源里找到。
 * 如果映射写错了（列名拼错、表没导入），这里必须失败，而不是渲染出一张看起来挺好的假图。
 */

import { existsSync, readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

import {
  parseLineage,
  type BuiltLineage,
  type BuiltTable,
  type ColumnEdge,
  type ColumnRef,
  type LineageDoc,
} from "./lineage.ts";

export interface DbTable {
  name: string;
  rows: number;
  columns: string[];
}

export interface CsvTable {
  name: string;
  rows: number;
  columns: string[];
}

const quote = (name: string): string => `"${name.replace(/"/g, '""')}"`;

/** 只读打开，拿表名、行数、列名。不修改库。 */
export function readDatabase(dbPath: string): Map<string, DbTable> {
  if (!existsSync(dbPath)) throw new Error(`数据库不存在：${dbPath}`);
  const db = new DatabaseSync(dbPath, { readOnly: true });
  try {
    const names = (
      db
        .prepare("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")
        .all() as unknown as { name: string }[]
    ).map((row) => row.name);
    const out = new Map<string, DbTable>();
    for (const name of names) {
      const countRow = db.prepare(`SELECT COUNT(*) AS n FROM ${quote(name)}`).get() as unknown as { n: number };
      const columns = (db.prepare(`PRAGMA table_info(${quote(name)})`).all() as unknown as { name: string }[]).map(
        (col) => col.name,
      );
      out.set(name, { name, rows: Number(countRow.n), columns });
    }
    return out;
  } finally {
    db.close();
  }
}

/** 读 CSV 的表头与行数（输出表就是这些文件）。 */
export function readCsv(csvPath: string): CsvTable {
  const text = readFileSync(csvPath, "utf8").replace(/^\uFEFF/, "");
  const lines = text.split(/\r?\n/);
  const header = (lines[0] ?? "").trim();
  const rows = lines.slice(1).filter((line) => line.trim() !== "").length;
  return { name: csvPath, rows, columns: splitCsvLine(header) };
}

/** 与输出方法一致的最小 CSV 切分：支持双引号包裹与转义双引号。 */
export function splitCsvLine(line: string): string[] {
  const out: string[] = [];
  let current = "";
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (quoted) {
      if (char === '"') {
        if (line[i + 1] === '"') {
          current += '"';
          i += 1;
        } else {
          quoted = false;
        }
      } else {
        current += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      out.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  out.push(current);
  return out.map((value) => value.trim());
}

export interface BuildInput {
  doc: unknown;
  dbPath: string;
  /** 输出 CSV 所在目录；给了就以文件为准覆盖作者声明的行数与列名。 */
  csvDir?: string;
}

export function buildLineage(input: BuildInput): BuiltLineage {
  const doc: LineageDoc = parseLineage(input.doc);
  const db = readDatabase(input.dbPath);
  const warnings: string[] = [];

  const tables = new Map<string, BuiltTable>();
  for (const table of doc.inputs) {
    const real = db.get(table.id);
    if (!real) {
      throw new Error(
        `输入表「${table.id}」在库里不存在。库里有：${[...db.keys()].join(", ")}`,
      );
    }
    if (table.rows !== undefined && table.rows !== real.rows) {
      warnings.push(`输入表「${table.id}」声明 ${table.rows} 行，库里实际 ${real.rows} 行；以库为准`);
    }
    tables.set(table.id, {
      id: table.id,
      label: table.label ?? table.id,
      kind: "input",
      rows: real.rows,
      columns: real.columns,
      ...(table.origin ? { origin: table.origin } : {}),
      ...(table.source ? { source: table.source } : {}),
    });
  }

  for (const output of doc.outputs) {
    let rows: number | null = output.rows ?? null;
    let columns = output.columns ? [...output.columns] : [];
    let artifact: string | undefined;
    if (input.csvDir) {
      const artifactName = output.artifact ?? `${output.id}.csv`;
      const csvPath = `${input.csvDir}\\${artifactName}`.replace(/\\/g, "/");
      if (existsSync(csvPath)) {
        const csv = readCsv(csvPath);
        rows = csv.rows;
        columns = csv.columns;
        artifact = artifactName;
      } else {
        warnings.push(`输出「${output.id}」找不到文件 ${csvPath}，沿用声明值`);
        artifact = artifactName;
      }
    }
    if (columns.length === 0) throw new Error(`输出表「${output.id}」没有列清单，无法渲染`);
    tables.set(output.id, {
      id: output.id,
      label: output.label ?? output.id,
      kind: "output",
      rows,
      columns,
      ...(output.origin ? { origin: output.origin } : {}),
      ...(artifact ? { artifact } : {}),
    });
  }

  const requireRef = (ref: ColumnRef, where: string): void => {
    const table = tables.get(ref.table);
    if (!table) {
      throw new Error(`${where} 引用了不存在的表「${ref.table}」（可用：${[...tables.keys()].join(", ")}）`);
    }
    if (!table.columns.includes(ref.column)) {
      throw new Error(`${where} 引用了表「${ref.table}」里不存在的列「${ref.column}」`);
    }
  };

  const columns: Array<ColumnEdge & { self: boolean }> = doc.columns.map((edge) => {
    const output = tables.get(edge.output);
    if (!output || output.kind !== "output") throw new Error(`列映射指向未知输出表「${edge.output}」`);
    if (!output.columns.includes(edge.name)) {
      throw new Error(`输出「${edge.output}」里没有列「${edge.name}」（输出列：${output.columns.join(", ")}）`);
    }
    for (const ref of edge.from) requireRef(ref, `列映射 ${edge.output}.${edge.name}`);
    return { ...edge, self: edge.from.some((ref) => ref.table === edge.output) };
  });

  for (const filter of doc.filters) {
    const output = tables.get(filter.output);
    if (!output || output.kind !== "output") throw new Error(`行过滤指向未知输出表「${filter.output}」`);
    for (const ref of filter.from) requireRef(ref, `行过滤 ${filter.output}.${filter.name}`);
  }

  const outputsWithoutEdges = doc.outputs.filter((output) => !doc.columns.some((edge) => edge.output === output.id));
  for (const output of outputsWithoutEdges) warnings.push(`输出「${output.id}」没有任何列映射`);

  return {
    version: "0.1",
    case: doc.case,
    builtAt: new Date().toISOString(),
    source: { db: input.dbPath, dbTables: [...db.keys()].sort(), csvDir: input.csvDir ?? null },
    tables: [...tables.values()],
    columns,
    filters: doc.filters,
    warnings,
  };
}
