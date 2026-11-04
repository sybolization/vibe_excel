import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { describe, expect, it } from "vitest";

import { buildLineage, escapeForScript, readCsv, renderHtml, splitCsvLine } from "../src/index.ts";

const makeDb = (): string => {
  const dir = mkdtempSync(join(tmpdir(), "ve-sqlmap-"));
  const path = join(dir, "t.db");
  const db = new DatabaseSync(path);
  db.exec('CREATE TABLE employees ("证件编号" TEXT, "姓名" TEXT, "员工状态" TEXT)');
  db.exec("INSERT INTO employees VALUES ('001','牛雷','在职'), ('002','张三','离职')");
  db.exec('CREATE TABLE positions ("岗位号" TEXT, "归属主管" TEXT)');
  db.exec("INSERT INTO positions VALUES ('P1','A'), ('','A')");
  db.close();
  return path;
};

const doc = (): Record<string, unknown> => ({
  version: "0.1",
  case: { id: "t-1", title: "测试案例", provenance: "test", asOf: "2026-10-23" },
  inputs: [{ id: "employees" }, { id: "positions" }],
  outputs: [{ id: "population", label: "population.csv", columns: ["证件编号", "姓名", "rule"] }],
  columns: [
    {
      output: "population",
      name: "证件编号",
      formula: 'employees."证件编号"',
      from: [{ table: "employees", column: "证件编号" }],
    },
    { output: "population", name: "姓名", formula: "", from: [{ table: "employees", column: "姓名" }] },
    { output: "population", name: "rule", formula: "'strict' | 'conservative'", from: [] },
  ],
  filters: [
    {
      output: "population",
      name: "在职",
      kind: "rows",
      formula: '"员工状态" = \'在职\'',
      from: [{ table: "employees", column: "员工状态" }],
    },
  ],
});

describe("buildLineage：声明必须与真实的库对齐", () => {
  it("行数与列名以库为准，输出表沿用声明列", () => {
    const built = buildLineage({ doc: doc(), dbPath: makeDb() });
    const employees = built.tables.find((t) => t.id === "employees");
    expect(employees?.rows).toBe(2);
    expect(employees?.columns).toEqual(["证件编号", "姓名", "员工状态"]);
    const population = built.tables.find((t) => t.id === "population");
    expect(population?.kind).toBe("output");
    expect(population?.columns).toEqual(["证件编号", "姓名", "rule"]);
    expect(built.columns).toHaveLength(3);
    expect(built.columns.some((c) => c.self)).toBe(false);
    expect(built.warnings).toEqual([]);
  });

  it("来源列写错时直接失败，而不是渲染出一张假图", () => {
    const broken = doc();
    (broken.columns as Array<Record<string, unknown>>)[0] = {
      output: "population",
      name: "证件编号",
      formula: "x",
      from: [{ table: "employees", column: "不存在的列" }],
    };
    expect(() => buildLineage({ doc: broken, dbPath: makeDb() })).toThrow(/不存在的列/);
  });

  it("输入表不在库里时失败并列出可用表", () => {
    const broken = doc();
    (broken.inputs as Array<Record<string, unknown>>)[1] = { id: "ghost" };
    expect(() => buildLineage({ doc: broken, dbPath: makeDb() })).toThrow(/ghost.*employees/s);
  });

  it("输出列不在声明清单里也失败", () => {
    const broken = doc();
    (broken.columns as Array<Record<string, unknown>>)[1] = {
      output: "population",
      name: "没有这列",
      formula: "",
      from: [],
    };
    expect(() => buildLineage({ doc: broken, dbPath: makeDb() })).toThrow(/没有这列/);
  });

  it("自引用（派生列依赖同表其它列）被标记出来", () => {
    const self = doc();
    (self.outputs as Array<Record<string, unknown>>)[0] = {
      id: "pn",
      label: "pn.csv",
      columns: ["remaining", "under_threshold"],
    };
    (self.columns as Array<Record<string, unknown>>) = [
      { output: "pn", name: "remaining", formula: "count", from: [{ table: "positions", column: "岗位号" }] },
      { output: "pn", name: "under_threshold", formula: "remaining < 50", from: [{ table: "pn", column: "remaining" }] },
    ];
    self.filters = [];
    const built = buildLineage({ doc: self, dbPath: makeDb() });
    expect(built.columns.find((c) => c.name === "under_threshold")?.self).toBe(true);
  });

  it("原始文件信息透传到产物，界面才能按文件分框", () => {
    const withSource = doc();
    (withSource.inputs as Array<Record<string, unknown>>)[0] = {
      id: "employees",
      source: { file: "所有员工_202610231434.XLS", sheet: "SHEET1" },
    };
    const built = buildLineage({ doc: withSource, dbPath: makeDb() });
    expect(built.tables.find((t) => t.id === "employees")?.source).toEqual({
      file: "所有员工_202610231434.XLS",
      sheet: "SHEET1",
    });
    expect(built.tables.find((t) => t.id === "positions")?.source).toBeUndefined();
  });

  it("source 缺少 file 时失败，而不是画出一个没名字的框", () => {
    const broken = doc();
    (broken.inputs as Array<Record<string, unknown>>)[0] = { id: "employees", source: { sheet: "SHEET1" } };
    expect(() => buildLineage({ doc: broken, dbPath: makeDb() })).toThrow(/source\.file/);
  });
});

describe("render：自包含 HTML", () => {
  it("嵌入了数据与两级容器，且能挡住 </script> 截断", () => {
    const built = buildLineage({ doc: doc(), dbPath: makeDb() });
    const html = renderHtml(built);
    expect(html).toContain('id="lineage"');
    expect(html).toContain('id="view1"');
    expect(html).toContain('id="view2"');
    expect(html).toContain("证件编号");
    expect(html.split("</script>")).toHaveLength(3); // 数据块 + 脚本块各一个结束标签，正文里不得再出现
  });

  it("骨架里不写文案：分区与文件框由 app.js 按数据生成", () => {
    const built = buildLineage({ doc: doc(), dbPath: makeDb() });
    const html = renderHtml(built);
    expect(html).toContain('id="lanes"');
    expect(html).toContain('id="stage1"');
    expect(html).not.toContain('class="banner"');
    expect(html).not.toContain("输入表指向输出表");
  });

  it("escapeForScript 把 < 转义，避免提前闭合", () => {
    expect(escapeForScript('{"a":"</script>"}')).toBe('{"a":"\\u003c/script>"}');
  });
});

describe("CSV 读取", () => {
  it("支持带逗号的引号字段与 BOM", () => {
    const dir = mkdtempSync(join(tmpdir(), "ve-sqlmap-csv-"));
    const path = join(dir, "population.csv");
    writeFileSync(path, '\uFEFFrule,姓名,报表部门\nstrict,牛雷,"七分厂,一车间"\nstrict,张三,二分厂\n', "utf8");
    const csv = readCsv(path);
    expect(csv.columns).toEqual(["rule", "姓名", "报表部门"]);
    expect(csv.rows).toBe(2);
  });

  it("splitCsvLine 处理转义双引号", () => {
    expect(splitCsvLine('a,"b,c",d')).toEqual(["a", "b,c", "d"]);
    expect(splitCsvLine('"他说""嗨""",x')).toEqual(['他说"嗨"', "x"]);
  });
});
