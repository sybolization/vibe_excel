# Author a versioned Excel workflow

{{WORKSPACE}}

Use only the interpreter `{{PYTHON}}`. Work in `{{CODE}}`.
Input files are in `{{INPUTS}}`, the business database is `{{DB}}`,
output files go in `{{OUT}}`, and the explicit business date is `{{ASOF}}`.

Write `{{FLOW}}` and `pipeline.py` into the program repository.
Do not run Git commands: the Python harness commits your draft.
Ask the user through `ask_user` when a business rule cannot be established from evidence.
Record unconfirmed assumptions instead of pretending that the user agreed.

## Contract

- `flow.json` contains `flow`, `db`, `materialize`, `outputs`, and optional `entry`.
- Materialization maps a file and sheet to a table, optionally projecting/renaming columns.
  Do not put business filtering, deduplication, or normalization into materialization.
- `main` is linear. Each top-level bare call to a local function is one workflow node.
- Annotate input parameters with the literal string `"t:table_name"`.
  The platform injects those tables as pandas DataFrames.
- Annotate return values with `"t:output_table"`. Each node must actually write that table.
- Runtime parameters are `db`, `inputs`, `out`, and `as_of`, and are supplied as keywords.
- Every node needs a docstring. Block-leading string literals describe internal steps.
- Keep business-table names ASCII identifiers. Do not hide nodes behind cross-file calls,
  decorators, repeated calls, positional-only arguments, dynamic dispatch, or control flow in `main`.
- Declare every final output table. Two empty output sets are not a successful verification.
- Export deterministic files (for example CSV); use the supplied business date, not today's date.
- Put reusable business semantics in `semantics/claims.jsonl`, as append-only claim events.

## Minimal valid Python shape

```python
import sqlite3
import pandas as pd


def summarize(db, raw: "t:raw_orders") -> "t:totals":
    """Aggregate sales quantities by department."""
    totals = raw.groupby("department", sort=True, as_index=False)["quantity"].sum()
    with sqlite3.connect(db) as connection:
        totals.to_sql("totals", connection, if_exists="replace", index=False)
    return totals


def main(db, inputs, out, as_of):
    connection = sqlite3.connect(db)
    raw = pd.read_sql_query("SELECT * FROM raw_orders", connection)
    connection.close()
    summarize(db, raw)
```
