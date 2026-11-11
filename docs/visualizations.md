# 离线账本与 SQL 血缘导出

在仓库根目录执行 `uv sync` 后运行以下命令。无需 Node、SQL 解析器、浏览器服务或网络连接。

```powershell
uv run python -m vibe_excel.book build --store C:\data\store --out .\exports\book.html
uv run python -m vibe_excel.book build --store C:\data\store --session <id> --run <id> --out .\exports\selected.html
uv run python -m vibe_excel.sqlmap build --db C:\data\hr.db --lineage .\lineage.json --csv C:\data\reports --out .\exports\sqlmap
```

Book 只写出所请求的 HTML。SQL map 在输出目录写入 `lineage.json` 和 `index.html`。既有数据库以只读方式打开，导出会拒绝覆盖其源数据库/声明/CSV。两个 HTML 文件都内嵌全部 CSS、JavaScript 与 JSON。原始前端资源按原样打包；JSON 会转义 `<`、U+2028 和 U+2029。

## Book

```python
from pathlib import Path
from vibe_excel.book import collect_book, render_book

view = collect_book(Path("store"), session_id=None, run_id=None)
Path("book.html").write_text(render_book(view), encoding="utf-8")
```

默认选取最近更新的会话及其最新可读的流程运行。`currentRevision` 是最新的程序快照；`lastRunRevision` 是所选运行的 **输入** 版本，即使存在更新的快照。每个运行的 `db` 文件名（或 `flowJson.db`）决定其数据库：先找轮次 `a`，再找运行根目录，最后找轮次 `b`。缺少该字段的旧记录使用 `hr.db`。

输入血缘的优先级与原项目一致：最新的物化记录，其次是 `_source_file`/`_source_sheet` 列，最后是最近记录的写出者的 `reads`/`writes`。较旧的运行会填补其余未被记录的表。多工作表导入保留 `sheetCount`。派生表沿已知输入追溯到其文件；无法追溯的表仍为孤儿，而不会虚构来源。循环会终止并产生诊断信息。图示高亮所选运行的表、匹配的写出者以及上游工作表/文件。

Pi JSONL 对话片段汇总首条用户/助手文本、工具调用与写入工具。`author.json` 通过 commit 和会话文件名把片段与运行关联。`focusEpisode` 是原应用使用的片段 ID，而非带 `.jsonl` 扩展名的文件名。

缺失或损坏的可选运行数据库、快照、对话记录、清单和控制页不影响导航。显式诊断会出现在 `warnings`、图警告（若存在）以及 HTML 中可展开的警告面板里。`control.db` 缺失/不可读、缺少必需会话，或显式指定的会话/运行不存在，都属于错误。

可独立使用的 API 有 `build_graph`、`build_inputs`、`build_diagram`、`parse_transcript`、`one_line`、`sample_table`、`read_table_stat` 和 `read_db_facts`。各构建器也接受原项目的 camelCase 输入映射。JSON 视图键保持 camelCase。对应的 camelCase API 别名以及带类型的视图/输入模型均已导出。

## SQL map：作者声明，而非 SQL 推断

**SQL map 刻意不解析 SQL，也不推断血缘。** `formula` 是作者声明的说明性文本，绝不会被执行。`from` 引用和行约束会对照真实的源表/列进行校验，输出 CSV 会覆盖声明的输出行数与列。

```json
{
  "version": "0.1",
  "case": {"id": "employees", "title": "Headcount", "provenance": "author-declared"},
  "inputs": [{"id": "employees", "source": {"file": "staff.xlsx", "sheet": "Staff"}}],
  "outputs": [{"id": "headcount", "columns": ["department", "count"], "artifact": "headcount.csv"}],
  "columns": [
    {"output": "headcount", "name": "department", "formula": "", "from": [{"table": "employees", "column": "department"}]},
    {"output": "headcount", "name": "count", "formula": "Count employees per department", "from": [{"table": "employees", "column": "id"}]}
  ],
  "filters": [
    {"output": "headcount", "name": "Departments", "kind": "group-key", "formula": "Group by department", "from": [{"table": "employees", "column": "department"}]}
  ]
}
```

```python
from vibe_excel.sqlmap import build_lineage, parse_lineage, render_html
from vibe_excel.util import read_json

declaration = parse_lineage(read_json("lineage.json"))
built = build_lineage(declaration, "hr.db", csv_dir="reports")
html = render_html(built)
```

必要字段错误的声明、缺失的输入表、未知的源列、未知的输出列/表，以及无效的过滤引用，都会成为显式错误。缺失的可选 CSV 文件会产生警告并保留声明值；如果既没有 CSV 也没有声明的输出列，构建会失败。CSV 读取使用 Python 标准 CSV 解析器，能处理 BOM、转义引号和多行字段。支持输出表自依赖，并标记为 `self: true`。

两级 UI 按原始文件对输入分组，可展开输出表与列，并支持诸如 `index.html#/out/headcount/count` 的链接。未映射的列保持无标注。原始示例声明打包在 `src\vibe_excel\assets\sqlmap\molex.lineage.json`。

SQL map 还导出 `read_database`、`read_csv`、`split_csv_line`、`escape_for_script`、常量与类型模型，以及原项目的 camelCase 别名。两个包都暴露 `run_cli(argv: list[str]) -> int`；控制台入口点使用 `vibe_excel.book.cli:main` 和 `vibe_excel.sqlmap.cli:main`。
