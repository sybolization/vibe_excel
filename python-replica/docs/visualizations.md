# Offline book and SQL lineage exports

Run these commands from `python-replica` after `uv sync`. No Node, SQL parser,
browser service, or network connection is needed.

```powershell
uv run python -m vibe_excel.book build --store C:\data\store --out .\exports\book.html
uv run python -m vibe_excel.book build --store C:\data\store --session <id> --run <id> --out .\exports\selected.html
uv run python -m vibe_excel.sqlmap build --db C:\data\hr.db --lineage .\lineage.json --csv C:\data\reports --out .\exports\sqlmap
```

Book writes only the requested HTML. SQL map writes `lineage.json` and
`index.html` in the output directory. Existing databases are opened read-only,
and exports refuse to overwrite their source databases/declarations/CSVs.
Both HTML files include all CSS, JavaScript, and JSON. The original frontend
assets are bundled unchanged; JSON escapes `<`, U+2028, and U+2029.

## Book

```python
from pathlib import Path
from vibe_excel.book import collect_book, render_book

view = collect_book(Path("store"), session_id=None, run_id=None)
Path("book.html").write_text(render_book(view), encoding="utf-8")
```

The default is the most recently updated session and its latest readable flow
run. `currentRevision` is the latest program snapshot; `lastRunRevision` is the
selected run's **input** revision, even when a newer snapshot exists.
Each run's `db` filename (or `flowJson.db`) selects its database: round `a`,
run root, then round `b`. Old records without this field use `hr.db`.

Input provenance precedence matches the original: latest materialization
record, then `_source_file`/`_source_sheet` columns, then the latest recorded
writer's `reads`/`writes`. Older runs fill otherwise unrecorded tables. Multi-sheet
imports retain `sheetCount`. Derived tables follow known inputs to their file;
untraceable tables remain orphans, not invented sources. Cycles terminate and
produce diagnostics. The diagram highlights the selected run's tables,
matching writers, and upstream sheets/files.

Pi JSONL episodes summarize the first user/assistant text, tool calls, and write
tools. `author.json` associates episodes with runs by commit and session filename.
`focusEpisode` is the episode ID consumed by the original app, not a filename
with its `.jsonl` extension.

Missing or corrupt optional run databases, snapshots, transcripts, manifests,
and control pages do not prevent navigation. Explicit diagnostics appear in
`warnings`, in graph warnings when present, and in an expandable warning panel
in the HTML. A missing/unreadable `control.db`, missing required sessions, or an
unknown explicit session/run is an error.

The independently usable APIs are `build_graph`, `build_inputs`,
`build_diagram`, `parse_transcript`, `one_line`, `sample_table`,
`read_table_stat`, and `read_db_facts`. The builders also accept the original
camelCase input mappings. JSON view keys stay camelCase. The corresponding
camelCase API aliases and typed view/input models are exported.

## SQL map: declarations, not SQL inference

**SQL map deliberately does not parse SQL or infer lineage.** `formula` is
author-declared explanatory text, never executed. `from` references and row
constraints are checked against actual source tables/columns, and output CSVs
override declared output row counts and columns.

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

Required malformed declarations, missing input tables, unknown source columns,
unknown output columns/tables, and invalid filter references are explicit
errors. Missing optional CSV files produce warnings and retain declared values;
without either CSV or declared output columns, building fails. CSV reading uses
Python's standard CSV reader, including BOM, escaped quotes, and multiline fields.
Output self-dependencies are supported and marked `self: true`.

The two-level UI groups inputs by original file, opens output tables and columns,
and supports links such as `index.html#/out/headcount/count`. Unmapped columns
remain unannotated. An original sample declaration is bundled at
`src\vibe_excel\assets\sqlmap\molex.lineage.json`.

SQL map additionally exports `read_database`, `read_csv`, `split_csv_line`,
`escape_for_script`, constants and typed models, and original camelCase aliases.
Both packages expose `run_cli(argv: list[str]) -> int`; console entry points use
`vibe_excel.book.cli:main` and `vibe_excel.sqlmap.cli:main`.
