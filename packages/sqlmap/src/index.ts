/**
 * @vibe-excel/sqlmap —— SQL 表的列级血缘与两级可视化。
 *
 *   lineage.ts  数据模型（输入表 / 输出表 / 列映射 / 行过滤）
 *   build.ts    把声明的血缘与真实的库、CSV 对齐并校验
 *   render.ts   渲染成自包含 HTML（一级：表→表；二级：列→列 + 计算式）
 */

export { FILTER_KINDS, LINEAGE_VERSION, parseLineage } from "./lineage.ts";
export type {
  BuiltLineage,
  BuiltTable,
  ColumnEdge,
  ColumnRef,
  FilterKind,
  LineageCase,
  LineageDoc,
  RowFilter,
  TableInput,
  TableOutput,
  TableSource,
} from "./lineage.ts";

export { buildLineage, readCsv, readDatabase, splitCsvLine } from "./build.ts";
export type { BuildInput, CsvTable, DbTable } from "./build.ts";

export { escapeForScript, renderHtml } from "./render.ts";
