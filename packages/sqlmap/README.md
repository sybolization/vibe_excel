# @vibe-excel/sqlmap

SQL 表的**列级血缘**与两级可视化。输入是真实的库与产物，输出是一个能双击打开、能点开每一列的 HTML。

## 两级界面

| 级别 | 看什么 | 交互 |
|---|---|---|
| 一级 | 左侧输入区按**原始文件**分框（一个文件里的 n 张 sheet 框在一起），右侧输出区 | 点输出表进入二级（有按下动效） |
| 二级 | 某张输出表的全部列 | 点一列 → 左侧只显示**构成这一列**的来源列，画连线到该列 |

一级只画"真的喂了数"的表；没参与任何列映射的输入表收在底部一行小标签里（`显示全部 N 张` 可以展开），
不会为了让画面满而摆一堆没有连线的框。

连线可点：点开就是这一列的计算式、来源列、说明与该输出的行约束。深链接可以直接打开某一列：

```text
index.html#/out/population/证件编号
index.html#/out/pn_available/under_threshold   ← 派生列依赖本表 remaining，画虚线
```

## 用法

```powershell
node packages/sqlmap/src/main.ts build --db <sqlite> --lineage <声明.json> --out <目录> [--csv <输出 CSV 目录>]
```

产物：`<out>/lineage.json`（校验后的数据）+ `<out>/index.html`（自包含，无外部依赖）。

- 行数、列名以**库与 CSV 为准**，声明里写错了直接报错，不会渲染出一张看起来挺好的假图；
- 声明里的 `from` 必须是真实存在的表.列；指向输出表自身的，标记为派生列依赖（虚线）。

## 血缘声明

见 `data/molex.lineage.json`。最小形状：

```json
{
  "version": "0.1",
  "case": { "id": "molex-2026-10-23", "title": "Molex HR 月报", "asOf": "2026-10-23", "provenance": "人工提取自 analyze.py" },
  "inputs": [{ "id": "employees", "origin": "所有员工_202610231434.XLS / SHEET1",
               "source": { "file": "所有员工_202610231434.XLS", "sheet": "SHEET1" } }],
  "outputs": [{ "id": "population", "label": "population.csv", "artifact": "population.csv" }],
  "columns": [
    { "output": "population", "name": "证件编号", "formula": "s(employees.\"证件编号\")",
      "from": [{ "table": "employees", "column": "证件编号" }] }
  ],
  "filters": [
    { "output": "population", "name": "strict 口径", "kind": "rows",
      "formula": "\"员工状态\" NOT IN ('离职','退休') ...",
      "from": [{ "table": "employees", "column": "员工状态" }] }
  ]
}
```

`source` 决定一级图上的**分区**：同一个 `file` 的表会被框在一个"原始文件框"里，`sheet` 显示成卡片上的小标签。
不写 `source` 的表会落到"无原始文件"那一框——分区是数据推出来的，不是画图时硬编码的。

`formula` 是**人读的文本**，不是可执行 SQL；执行仍由程序负责。它的用途是让人复核"这个数怎么来的"，以及以后作为 RSI 的评分对象。

## 刻意不做的事

- **不做 SQL 解析**：从任意 SQL 反推列级血缘不可靠（子查询、窗口函数、别名覆盖）。当前的映射由人/agent 声明，再由 `build` 用真实库校验。
- **不连服务、不依赖网络**：单文件 HTML，发给 HR 就能看。
- **不猜测**：没有映射的列显示"未标注"，不会编一个公式填上去。
