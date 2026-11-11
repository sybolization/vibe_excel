"""移出文件的影响面：包里写给 agent 的那段链路。

链路和页面上的灰链必须出自同一份视图，所以这里只验纯函数，不碰 store。
"""

from __future__ import annotations

from typing import Any

from vibe_excel.book.serve import impact_from_view


def _view() -> dict[str, Any]:
    return {
        "inputs": {
            "files": [
                {
                    "name": "orders.xlsx",
                    "direct": [{"name": "raw_orders", "sheet": "Orders"}],
                    "derived": [
                        {"name": "orders", "sheet": None},
                        {"name": "totals", "sheet": None},
                    ],
                },
                {"name": "other.xlsx", "direct": [{"name": "unrelated"}], "derived": []},
            ]
        },
        "graph": {
            "nodes": [
                {"fn": "clean_orders", "reads": ["raw_orders"], "writes": ["orders"]},
                {"fn": "build_totals", "reads": ["orders"], "writes": ["totals"]},
                {"fn": "elsewhere", "reads": ["unrelated"], "writes": ["other"]},
            ],
            "outputs": ["totals", "other"],
        },
    }


def test_impact_lists_tables_steps_and_outputs() -> None:
    tables, chain = impact_from_view(_view(), "orders.xlsx")

    assert tables == [
        {"table": "raw_orders", "sheet": "Orders"},
        {"table": "orders", "sheet": None},
        {"table": "totals", "sheet": None},
    ]
    # 只写被牵连的两步，别把无关的 elsewhere 也写进包里
    assert [step["fn"] for step in chain["steps"]] == ["clean_orders", "build_totals"]
    assert chain["tables"] == ["raw_orders", "orders", "totals"]
    assert chain["outputs"] == ["totals"]


def test_impact_is_empty_for_unknown_file() -> None:
    assert impact_from_view(_view(), "没有这个文件.xlsx") == ([], {})


def test_impact_handles_file_without_tables() -> None:
    view = _view()
    view["inputs"]["files"].append({"name": "空表.xlsx", "direct": [], "derived": []})
    assert impact_from_view(view, "空表.xlsx") == ([], {})


def test_impact_without_graph_still_reports_tables() -> None:
    view = _view()
    view["graph"] = None
    tables, chain = impact_from_view(view, "orders.xlsx")

    assert [item["table"] for item in tables] == ["raw_orders", "orders", "totals"]
    assert chain == {"tables": ["raw_orders", "orders", "totals"], "steps": []}


def test_impact_keeps_only_reached_outputs() -> None:
    # 产物里只有真正被牵连到的才写进包
    _, chain = impact_from_view(_view(), "other.xlsx")
    assert chain["outputs"] == ["other"]
