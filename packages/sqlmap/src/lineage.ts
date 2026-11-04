/**
 * 列级血缘的数据模型：输入表 → 输出表，每一列都能追到来源列和计算式。
 *
 * 为什么是"列级"而不是"表级"：表级血缘（A 喂 B）只能回答"数据从哪来"，
 * 回答不了"这个数怎么算的"。业务要复核的是后者。
 *
 * 三条约束：
 *  1. 只描述**已经发生过**的映射：来源列必须在库里真实存在，build 阶段校验。
 *  2. 计算式是**文本**，不是可执行 SQL：它给人和模型读，执行仍由程序负责。
 *  3. 行过滤单独建模：它决定"哪些行进得来"，属于另一种来源，不能混进列公式。
 */

export const LINEAGE_VERSION = "0.1" as const;

export interface ColumnRef {
  table: string;
  column: string;
}

export interface ColumnEdge {
  /** 输出表 id。 */
  output: string;
  /** 输出列名。 */
  name: string;
  /** 人读的计算式；空串表示直通或未标注（看 note）。 */
  formula: string;
  note?: string;
  /** 来源列。可以指向输出表自身（派生列依赖）。 */
  from: ColumnRef[];
}

export const FILTER_KINDS = ["rows", "group-key"] as const;
export type FilterKind = (typeof FILTER_KINDS)[number];

export interface RowFilter {
  output: string;
  name: string;
  kind: FilterKind;
  formula: string;
  note?: string;
  from: ColumnRef[];
}

/**
 * 输入表的原始出处：界面按 file 分组，把"一个文件里的 n 张 sheet"框在一起。
 * 声明里写清楚，图上的分区才是数据推出来的，而不是画图时硬编码的。
 */
export interface TableSource {
  /** 文件名（含扩展名，不含目录）。同一个文件里的表共用一个框。 */
  file: string;
  /** 文件内的 sheet / 页名；单 sheet 的文件可以省略。 */
  sheet?: string;
}

export interface TableInput {
  id: string;
  label?: string;
  origin?: string;
  source?: TableSource;
  rows?: number;
  columns?: string[];
}

export interface TableOutput extends TableInput {
  /** 输出文件（csv/json）的文件名，仅用于展示与定位。 */
  artifact?: string;
}

export interface LineageCase {
  id: string;
  title: string;
  asOf?: string;
  /** 这份映射是怎么来的：人工提取 / agent 声明 / SQL 解析。 */
  provenance: string;
}

export interface LineageDoc {
  version: typeof LINEAGE_VERSION;
  case: LineageCase;
  inputs: TableInput[];
  outputs: TableOutput[];
  columns: ColumnEdge[];
  filters: RowFilter[];
}

/** build 之后的产物：补上了真实行数、真实列清单与校验结果。 */
export interface BuiltTable {
  id: string;
  label: string;
  kind: "input" | "output";
  rows: number | null;
  columns: string[];
  origin?: string;
  source?: TableSource;
  artifact?: string;
}

export interface BuiltLineage {
  version: typeof LINEAGE_VERSION;
  case: LineageCase;
  builtAt: string;
  source: { db: string; dbTables: string[]; csvDir: string | null };
  tables: BuiltTable[];
  columns: Array<ColumnEdge & { self: boolean }>;
  filters: RowFilter[];
  warnings: string[];
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const fail = (path: string, message: string): never => {
  throw new Error(`lineage 校验失败：${path} ${message}`);
};

const asObject = (value: unknown, path: string): Record<string, unknown> =>
  isObject(value) ? value : fail(path, "必须是对象");

const asArray = (value: unknown, path: string): unknown[] => (Array.isArray(value) ? value : fail(path, "必须是数组"));

const str = (value: unknown, path: string): string =>
  typeof value === "string" && value.trim() !== "" ? value : fail(path, "必须是非空字符串");

const optStr = (value: unknown, path: string): string | undefined => {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") return fail(path, "必须是字符串");
  return value;
};

const refs = (value: unknown, path: string): ColumnRef[] => {
  if (value === undefined || value === null) return [];
  return asArray(value, path).map((item, i) => {
    const record = asObject(item, `${path}[${i}]`);
    return {
      table: str(record.table, `${path}[${i}].table`),
      column: str(record.column, `${path}[${i}].column`),
    };
  });
};

function parseSource(value: unknown, path: string): TableSource | undefined {
  if (value === undefined || value === null) return undefined;
  const record = asObject(value, path);
  const sheet = optStr(record.sheet, `${path}.sheet`);
  return { file: str(record.file, `${path}.file`), ...(sheet ? { sheet } : {}) };
}

function parseTable(value: unknown, path: string, kind: "input" | "output"): TableInput & { artifact?: string } {
  const record = asObject(value, path);
  const columns = record.columns;
  const artifact = kind === "output" ? optStr(record.artifact, `${path}.artifact`) : undefined;
  const source = parseSource(record.source, `${path}.source`);
  return {
    id: str(record.id, `${path}.id`),
    ...(optStr(record.label, `${path}.label`) ? { label: optStr(record.label, `${path}.label`) } : {}),
    ...(optStr(record.origin, `${path}.origin`) ? { origin: optStr(record.origin, `${path}.origin`) } : {}),
    ...(source ? { source } : {}),
    ...(typeof record.rows === "number" ? { rows: record.rows } : {}),
    ...(Array.isArray(columns) ? { columns: columns.map((c, i) => str(c, `${path}.columns[${i}]`)) } : {}),
    ...(artifact ? { artifact } : {}),
  };
}

/** 解析并做结构校验。语义校验（列是否存在）在 build 阶段做，因为需要库。 */
export function parseLineage(raw: unknown): LineageDoc {
  const root = asObject(raw, "$");
  const version = str(root.version, "$.version");
  if (version !== LINEAGE_VERSION) fail("$.version", `只支持 ${LINEAGE_VERSION}，实际是 ${version}`);

  const caseRecord = asObject(root.case, "$.case");
  const asOf = optStr(caseRecord.asOf, "$.case.asOf");
  const caseInfo: LineageCase = {
    id: str(caseRecord.id, "$.case.id"),
    title: str(caseRecord.title, "$.case.title"),
    provenance: str(caseRecord.provenance, "$.case.provenance"),
    ...(asOf ? { asOf } : {}),
  };

  const inputs = asArray(root.inputs, "$.inputs").map((t, i) => parseTable(t, `$.inputs[${i}]`, "input"));
  const outputs = asArray(root.outputs, "$.outputs").map((t, i) => parseTable(t, `$.outputs[${i}]`, "output"));
  if (inputs.length === 0) fail("$.inputs", "至少需要一张输入表");
  if (outputs.length === 0) fail("$.outputs", "至少需要一张输出表");

  const columns: ColumnEdge[] = asArray(root.columns, "$.columns").map((item, i) => {
    const record = asObject(item, `$.columns[${i}]`);
    const note = optStr(record.note, `$.columns[${i}].note`);
    return {
      output: str(record.output, `$.columns[${i}].output`),
      name: str(record.name, `$.columns[${i}].name`),
      formula: optStr(record.formula, `$.columns[${i}].formula`) ?? "",
      ...(note ? { note } : {}),
      from: refs(record.from, `$.columns[${i}].from`),
    };
  });

  const filters: RowFilter[] = asArray(root.filters ?? [], "$.filters").map((item, i) => {
    const record = asObject(item, `$.filters[${i}]`);
    const kind = str(record.kind, `$.filters[${i}].kind`) as FilterKind;
    if (!FILTER_KINDS.includes(kind)) fail(`$.filters[${i}].kind`, `只能是 ${FILTER_KINDS.join(" / ")}`);
    const note = optStr(record.note, `$.filters[${i}].note`);
    return {
      output: str(record.output, `$.filters[${i}].output`),
      name: str(record.name, `$.filters[${i}].name`),
      kind,
      formula: str(record.formula, `$.filters[${i}].formula`),
      ...(note ? { note } : {}),
      from: refs(record.from, `$.filters[${i}].from`),
    };
  });

  return { version: LINEAGE_VERSION, case: caseInfo, inputs, outputs, columns, filters };
}
