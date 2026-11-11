from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd


def clean_orders(db: str, raw: "t:raw_orders") -> "t:orders":
    """Convert quantities and prices according to the explicit sales contract."""
    orders = raw.copy()
    orders["quantity"] = pd.to_numeric(orders["quantity"], errors="raise")
    orders["unit_price"] = pd.to_numeric(orders["unit_price"], errors="raise")
    if (orders[["quantity", "unit_price"]] < 0).any().any():
        """Reject negative values rather than hiding invalid sales facts."""
        raise ValueError("Quantity and unit price must not be negative")
    orders["revenue"] = orders["quantity"] * orders["unit_price"]
    with sqlite3.connect(db) as connection:
        orders.to_sql("orders", connection, if_exists="replace", index=False)
    return orders


def build_totals(
    db: str, orders: "t:orders", out: str, as_of: str
) -> "t:totals":
    """Aggregate department sales and export a reusable, deterministic CSV."""
    totals = orders.groupby("department", sort=True, as_index=False).agg(
        quantity=("quantity", "sum"), revenue=("revenue", "sum")
    )
    totals["as_of"] = as_of
    with sqlite3.connect(db) as connection:
        totals.to_sql("totals", connection, if_exists="replace", index=False)
    output = Path(out)
    output.mkdir(parents=True, exist_ok=True)
    totals.to_csv(output / "totals.csv", index=False, encoding="utf-8", lineterminator="\n")
    return totals


def main(db: str, inputs: str, out: str, as_of: str) -> None:
    connection = sqlite3.connect(db)
    raw = pd.read_sql_query("SELECT * FROM raw_orders", connection)
    connection.close()
    orders = clean_orders(db, raw)
    build_totals(db, orders, out, as_of)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--inputs", default=".")
    parser.add_argument("--out", required=True)
    parser.add_argument("--as-of", required=True)
    arguments = parser.parse_args()
    main(arguments.db, arguments.inputs, arguments.out, arguments.as_of)
