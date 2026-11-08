"""迁移自 TS 原版的三个 flow 测试。

对应 packages/harness/test 下的 flow.test.ts、flow-code.test.ts、flow-run.test.ts：
- flow 解析/校验、拓扑、摘要、优化提示、describe_flow；
- 代码即流程（真实 python + 真实 ast，不 mock）；
- 一轮执行的事实核对（用假回调，不启动子进程）。

所有用例离线运行，不依赖 git 仓库。
"""

from __future__ import annotations

import copy
import dataclasses
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from vibe_excel.flow import (
    FlowNode,
    FlowPlan,
    FlowSpec,
    FlowStep,
    FlowWarning,
    MaterializeEntry,
    describe_flow,
    digest_of,
    format_flow_warning,
    parse_call,
    parse_flow,
    parse_flow_json,
    plan_flow,
    suggest_optimizations,
    validate_flow,
)
from vibe_excel.flow_code import CodeFlow, parse_code_flow
from vibe_excel.process import ProcessResult
from vibe_excel.util import Digest, TableDigest, VibeError
from vibe_excel.workflow import (
    FlowRoundOptions,
    FlowRoundResult,
    MaterializeReport,
    diff_rounds,
    execute_sql_node,
    run_flow_round,
    table_names,
)


def error_of(run: Callable[[], object]) -> str:
    """跑一次并拿到错误信息；一个用例要核对多个关键字时用它。"""
    with pytest.raises(VibeError) as caught:
        run()
    return str(caught.value)


def minimal_flow(overrides: dict[str, object] | None = None) -> FlowSpec:
    """最小可用 spec：一个物化 + 一个节点，节点直接写产出表。"""
    spec = FlowSpec(
        flow="molex-prep",
        db="hr.db",
        materialize=[MaterializeEntry("headcount.xlsx", "raw_headcount")],
        outputs=["result"],
        nodes=[
            FlowNode(
                id="build",
                call="pipeline.py:build",
                reads=["raw_headcount"],
                writes=["result"],
            )
        ],
    )
    return dataclasses.replace(spec, **overrides) if overrides else spec


def raw_flow(overrides: dict[str, object] | None = None) -> dict[str, object]:
    """未校验的 JSON 形状：故意留给 parse_flow 去挑错。"""
    raw: dict[str, object] = {
        "flow": "molex-prep",
        "db": "hr.db",
        "materialize": [{"file": "headcount.xlsx", "table": "raw_headcount"}],
        "outputs": ["result"],
        "nodes": [raw_node()],
    }
    raw.update(overrides or {})
    return raw


def raw_node(overrides: dict[str, object] | None = None) -> dict[str, object]:
    node: dict[str, object] = {
        "id": "build",
        "call": "pipeline.py:build",
        "reads": ["raw_headcount"],
        "writes": ["result"],
    }
    node.update(overrides or {})
    return node


def step(node_id: str, reads: list[str], writes: list[str], call: str | None = None) -> FlowNode:
    """拓扑用例里的节点：call 与 id 对齐，省得每次手写。"""
    return FlowNode(
        id=node_id,
        call=call or f"pipeline.py:{node_id}",
        reads=reads,
        writes=writes,
    )


def clone_spec(spec: FlowSpec) -> FlowSpec:
    return copy.deepcopy(spec)


def reversed_spec(spec: FlowSpec) -> FlowSpec:
    """把 spec 里所有「顺序无关」的数组和对象都反过来，用于摘要不变性。"""
    materialize = [
        dataclasses.replace(
            entry,
            columns=dict(reversed(list(entry.columns.items()))) if entry.columns else None,
        )
        for entry in reversed(spec.materialize)
    ]
    nodes = [
        dataclasses.replace(
            node,
            reads=list(reversed(node.reads)),
            writes=list(reversed(node.writes)),
            args=dict(reversed(list(node.args.items()))) if node.args else None,
        )
        for node in reversed(spec.nodes)
    ]
    return dataclasses.replace(
        spec,
        materialize=materialize,
        outputs=list(reversed(spec.outputs)),
        nodes=nodes,
    )


# ---------------------------------------------------------------------------
# parseCall
# ---------------------------------------------------------------------------


def test_parse_call_accepts_two_shapes_and_normalizes_backslashes() -> None:
    assert parse_call("  pick.py:pick  ", "call") == ("pick.py", "pick")
    assert parse_call("src\\etl\\load.py:run", "call") == ("src/etl/load.py", "run")
    assert parse_call("sql/agg.sql", "call") == ("sql/agg.sql", None)


def test_parse_call_keeps_file_extension() -> None:
    # 回归：捕获组曾经把 .py/.sql 吃掉，下游拿不到真路径。
    assert parse_call("pipeline.py:compute", "call") == ("pipeline.py", "compute")
    assert parse_call("src/etl/build.py:build", "call") == ("src/etl/build.py", "build")
    assert parse_call("sql/agg.sql", "call") == ("sql/agg.sql", None)


def test_parse_call_rejects_absolute_paths() -> None:
    for value in ("/etc/pipeline.py:run", "C:\\work\\pipeline.py:run",
                  "\\\\server\\share\\pipeline.py:run"):
        assert "必须位于其目录内" in error_of(lambda value=value: parse_call(value, "call"))


def test_parse_call_rejects_parent_escape() -> None:
    for value in ("../pipeline.py:run", "src/../pipeline.py:run", "src\\..\\pipeline.py:run"):
        assert "必须位于其目录内" in error_of(lambda value=value: parse_call(value, "call"))


def test_parse_call_rejects_bad_shape_and_empty() -> None:
    for value in ("pipeline.py", "pipeline.py:", "pipeline.js:run"):
        assert "必须是 file.py:function 或 file.sql" in error_of(
            lambda value=value: parse_call(value, "call")
        )
    assert "不能为空" in error_of(lambda: parse_call("   ", "call"))


# ---------------------------------------------------------------------------
# parseFlow：合法输入
# ---------------------------------------------------------------------------


def test_parse_flow_minimal_defaults_and_trim() -> None:
    spec = parse_flow(
        {
            "flow": "  molex-prep  ",
            "nodes": [{"id": "build", "call": " pipeline.py:build ", "reads": [],
                      "writes": ["result"]}],
        }
    )
    assert spec.flow == "molex-prep"
    assert spec.db == "hr.db"
    assert spec.materialize == []
    assert spec.outputs == []
    assert [node.id for node in spec.nodes] == ["build"]
    assert spec.nodes[0].call == "pipeline.py:build"
    assert parse_flow(raw_flow()) == minimal_flow()


def test_parse_flow_full_entry_keeps_args_and_timeout() -> None:
    spec = parse_flow(
        {
            "flow": "molex-prep",
            "db": "molex.db",
            "materialize": [
                {
                    "file": "sub/molex.xlsx",
                    "sheet": "Sheet1",
                    "table": "raw_molex",
                    "columns": {"源列": "src_col", "数量": "qty"},
                    "load": "loaders.py:load_molex",
                }
            ],
            "outputs": ["result"],
            "nodes": [
                {
                    "id": "build",
                    "call": "src/etl/build.py:build",
                    "reads": ["raw_molex"],
                    "writes": ["result"],
                    "args": {"year": "2025", "label": ""},
                    "timeoutMs": "1500",
                }
            ],
        }
    )
    assert spec.db == "molex.db"
    assert spec.materialize[0].to_spec() == {
        "file": "sub/molex.xlsx",
        "sheet": "Sheet1",
        "table": "raw_molex",
        "columns": {"源列": "src_col", "数量": "qty"},
        "load": "loaders.py:load_molex",
    }
    assert spec.nodes[0].args == {"year": "2025", "label": ""}
    assert spec.nodes[0].timeout_ms == 1500


def test_parse_flow_node_call_is_trimmed_but_not_normalized() -> None:
    spec = parse_flow(raw_flow({"nodes": [raw_node({"call": " src\\etl.py:run "})]}))
    assert spec.nodes[0].call == "src\\etl.py:run"
    # parseCall 会把 \ 归一化成 /，但 parseFlow 把原字符串原样留下了。
    assert parse_call(spec.nodes[0].call, "call") == ("src/etl.py", "run")


# ---------------------------------------------------------------------------
# parseFlow：形状与字段校验
# ---------------------------------------------------------------------------


def test_parse_flow_rejects_bad_root_and_flow_name() -> None:
    assert "flow 必须是字符串键的对象" in error_of(lambda: parse_flow(None))
    assert "flow 必须是字符串键的对象" in error_of(lambda: parse_flow([]))
    assert "flow.flow" in error_of(lambda: parse_flow({}))
    assert "不能为空" in error_of(lambda: parse_flow(raw_flow({"flow": "   "})))


def test_parse_flow_validates_db_file_name() -> None:
    assert "必须以 .db 结尾" in error_of(lambda: parse_flow(raw_flow({"db": "hr.sqlite"})))
    # db 是唯一会被 join 成真实路径再覆盖的字段：带目录成分一律拒绝。
    for value in ("../hr.db", "a/../hr.db", "C:\\code\\hr.db"):
        assert "必须位于其目录内" in error_of(
            lambda value=value: parse_flow(raw_flow({"db": value}))
        )
    for value in ("sub/hr.db", "sub\\hr.db"):
        assert "必须是不含目录的文件名" in error_of(
            lambda value=value: parse_flow(raw_flow({"db": value}))
        )
    assert parse_flow(raw_flow({"db": "hr.db"})).db == "hr.db"


def test_parse_flow_rejects_bad_nodes_and_materialize_shapes() -> None:
    assert "flow.nodes 必须是数组" in error_of(lambda: parse_flow(raw_flow({"nodes": "nope"})))
    assert "必须是字符串键的对象" in error_of(lambda: parse_flow(raw_flow({"nodes": [None]})))
    assert "flow.materialize 必须是数组" in error_of(
        lambda: parse_flow(raw_flow({"materialize": {}}))
    )


def test_parse_flow_defaults_entry_when_nodes_absent_or_empty() -> None:
    absent = raw_flow()
    del absent["nodes"]
    assert parse_flow(absent).nodes == []
    assert parse_flow(absent).entry == "pipeline.py"
    empty = parse_flow(raw_flow({"nodes": []}))
    assert empty.nodes == []
    assert empty.entry == "pipeline.py"
    assert parse_flow(raw_flow({"nodes": [], "entry": "src/pipeline.py"}).copy()).entry == (
        "src/pipeline.py"
    )


def test_parse_flow_entry_must_be_relative_py_and_exclusive() -> None:
    for value in ("/abs/pipeline.py", "C:\\code\\pipeline.py", "../pipeline.py"):
        assert "必须位于其目录内" in error_of(
            lambda value=value: parse_flow(raw_flow({"nodes": [], "entry": value}))
        )
    assert "必须以 .py 结尾" in error_of(
        lambda: parse_flow(raw_flow({"nodes": [], "entry": "pipeline.sql"}))
    )
    assert "二选一" in error_of(lambda: parse_flow(raw_flow({"entry": "pipeline.py"})))


def test_parse_flow_rejects_bad_and_duplicate_node_ids() -> None:
    for value in ("bad-id", "1st"):
        assert "ASCII 表标识符" in error_of(
            lambda value=value: parse_flow(raw_flow({"nodes": [raw_node({"id": value})]}))
        )
    duplicate = [
        raw_node({"id": "build"}),
        raw_node({"id": "build", "reads": [], "writes": ["other"]}),
    ]
    assert "id 重复" in error_of(lambda: parse_flow(raw_flow({"nodes": duplicate})))


def test_parse_flow_rejects_bad_node_calls() -> None:
    for value in ("/abs/pipeline.py:build", "../pipeline.py:build"):
        assert "必须位于其目录内" in error_of(
            lambda value=value: parse_flow(raw_flow({"nodes": [raw_node({"call": value})]}))
        )
    for value in ("pipeline.py", "pipeline.py:"):
        assert "必须是 file.py:function 或 file.sql" in error_of(
            lambda value=value: parse_flow(raw_flow({"nodes": [raw_node({"call": value})]}))
        )


def test_parse_flow_table_names_and_duplicates() -> None:
    assert "ASCII 表标识符" in error_of(
        lambda: parse_flow(raw_flow({"nodes": [raw_node({"writes": ["result", "bad-name"]})]}))
    )
    assert "ASCII 表标识符" in error_of(
        lambda: parse_flow(
            raw_flow({"nodes": [raw_node({"reads": ["1st_table", "result"],
                                            "writes": ["result"]})]})
        )
    )
    assert "包含重复的表名" in error_of(
        lambda: parse_flow(raw_flow({"outputs": ["result", "result"]}))
    )
    assert "包含重复的表名" in error_of(
        lambda: parse_flow(
            raw_flow({"nodes": [raw_node({"reads": ["raw_headcount", "raw_headcount"]})]})
        )
    )
    # 「既不读也不写」属于图的有效性，放在 planFlow 这一关。
    spec = parse_flow(raw_flow({"nodes": [raw_node({"reads": [], "writes": []})]}))
    assert "既未读取也未写入" in error_of(lambda: plan_flow(spec))


def test_parse_flow_materialized_table_cannot_be_written() -> None:
    spec = parse_flow(raw_flow({"nodes": [raw_node({"writes": ["raw_headcount", "result"]})]}))
    message = error_of(lambda: plan_flow(spec))
    assert "物化" in message
    assert "raw_headcount" in message


def test_parse_flow_outputs_need_producer() -> None:
    spec = parse_flow(raw_flow({"outputs": ["result", "ghost"]}))
    assert "没有生产者" in error_of(lambda: plan_flow(spec))


def test_parse_flow_args_and_timeout() -> None:
    assert "ASCII 表标识符" in error_of(
        lambda: parse_flow(raw_flow({"nodes": [raw_node({"args": {"bad-key": "x"}})]}))
    )
    assert "必须是字符串键的对象" in error_of(
        lambda: parse_flow(raw_flow({"nodes": [raw_node({"args": "x"})]}))
    )
    assert "必须是字符串" in error_of(
        lambda: parse_flow(raw_flow({"nodes": [raw_node({"args": {"year": 2025}})]}))
    )
    for value in (0, -5):
        assert "必须是正数" in error_of(
            lambda value=value: parse_flow(raw_flow({"nodes": [raw_node({"timeoutMs": value})]}))
        )


# ---------------------------------------------------------------------------
# parseFlow：物化条目
# ---------------------------------------------------------------------------


def test_materialize_file_must_stay_inside_batch_directory() -> None:
    for value in ("/abs/x.xlsx", "C:/data/x.xlsx", "../x.xlsx", "sub/../../x.xlsx"):
        assert "必须位于其目录内" in error_of(
            lambda value=value: parse_flow(
                raw_flow({"materialize": [{"file": value, "table": "raw_headcount"}]})
            )
        )
    assert "必须是字符串键的对象" in error_of(
        lambda: parse_flow(raw_flow({"materialize": [None]}))
    )


def test_materialize_table_must_be_unique_and_identifier() -> None:
    duplicated = [
        {"file": "a.xlsx", "table": "raw_headcount"},
        {"file": "b.xlsx", "table": "raw_headcount"},
    ]
    assert "重复物化" in error_of(lambda: parse_flow(raw_flow({"materialize": duplicated})))
    assert "ASCII 表标识符" in error_of(
        lambda: parse_flow(raw_flow({"materialize": [{"file": "a.xlsx", "table": "bad name"}]}))
    )


def test_materialize_columns_and_load() -> None:
    assert "必须是字符串键的对象" in error_of(
        lambda: parse_flow(
            raw_flow({"materialize": [{"file": "a.xlsx", "table": "raw_headcount",
                                       "columns": []}]})
        )
    )
    assert "ASCII 表标识符" in error_of(
        lambda: parse_flow(
            raw_flow({"materialize": [{"file": "a.xlsx", "table": "raw_headcount",
                                       "columns": {"源列": "bad name"}}]})
        )
    )
    assert "必须指定一个 Python 函数" in error_of(
        lambda: parse_flow(
            raw_flow({"materialize": [{"file": "a.xlsx", "table": "raw_headcount",
                                       "load": "loaders.sql"}]})
        )
    )
    assert "必须位于其目录内" in error_of(
        lambda: parse_flow(
            raw_flow({"materialize": [{"file": "a.xlsx", "table": "raw_headcount",
                                       "load": "/abs/loaders.py:load"}]})
        )
    )


# ---------------------------------------------------------------------------
# parseFlowJson
# ---------------------------------------------------------------------------


def test_parse_flow_json_matches_parse_flow() -> None:
    text = json.dumps(raw_flow(), ensure_ascii=False)
    assert parse_flow_json(text) == parse_flow(raw_flow())
    assert parse_flow_json(text).flow == "molex-prep"


def test_parse_flow_json_reports_syntax_and_semantic_errors() -> None:
    assert "无效" in error_of(lambda: parse_flow_json("{ not json"))
    # 空节点不再在解析期报错（要从代码里解析），但 planFlow 会指出「这条流没有节点」。
    assert "没有节点" in error_of(lambda: plan_flow(parse_flow_json('{"flow":"x","nodes":[]}')))
    text = json.dumps(raw_flow({"nodes": [raw_node({"call": "/abs/a.py:run"})]}))
    assert "必须位于其目录内" in error_of(lambda: parse_flow_json(text))


# ---------------------------------------------------------------------------
# validateFlow 直接调用
# ---------------------------------------------------------------------------


def test_validate_flow_rejects_empty_and_duplicate_nodes() -> None:
    empty = FlowSpec("x", "hr.db", [], [], [], entry="pipeline.py")
    assert "没有节点" in error_of(lambda: validate_flow(empty))
    duplicate = FlowSpec(
        "x", "hr.db", [], [],
        [step("a", [], ["t1"]), step("a", [], ["t2"])],
    )
    assert "重复的节点 id" in error_of(lambda: validate_flow(duplicate))


# ---------------------------------------------------------------------------
# planFlow：拓扑与边
# ---------------------------------------------------------------------------


def test_topological_order_is_deterministic() -> None:
    nodes = [
        step("zeta", [], ["t_z"]),
        step("mid", ["t_a"], ["t_m"]),
        step("alpha", [], ["t_a"]),
    ]
    spec = minimal_flow({"materialize": [], "outputs": [], "nodes": nodes})
    first = plan_flow(spec)
    assert first.order == ["alpha", "mid", "zeta"]

    shuffled = minimal_flow(
        {"materialize": [], "outputs": [], "nodes": [nodes[2], nodes[0], nodes[1]]}
    )
    assert plan_flow(shuffled).order == first.order


def test_edges_are_derived_from_tables() -> None:
    spec = minimal_flow(
        {
            "materialize": [MaterializeEntry("src.xlsx", "raw")],
            "outputs": ["result"],
            "nodes": [
                step("a", ["raw"], ["t1"]),
                step("b", ["raw"], ["t2"]),
                step("c", ["t1", "t2"], ["result"]),
            ],
        }
    )
    plan = plan_flow(spec)
    assert plan.order == ["a", "b", "c"]
    assert [(edge.table, edge.from_node, edge.to_node) for edge in plan.edges] == [
        ("t1", "a", "c"),
        ("t2", "b", "c"),
    ]
    assert plan.produced_by.get("t1") == "a"
    assert plan.produced_by.get("t2") == "b"
    assert "raw" not in plan.produced_by
    assert plan.materialized == {"raw"}
    assert plan.unknown_reads == []


def test_self_connection_is_in_place_update_not_dependency() -> None:
    spec = minimal_flow(
        {
            "materialize": [MaterializeEntry("src.xlsx", "raw")],
            "outputs": ["snapshot"],
            "nodes": [step("solo", ["raw", "snapshot"], ["snapshot"])],
        }
    )
    plan = plan_flow(spec)
    assert plan.order == ["solo"]
    assert plan.edges == []
    assert plan.unknown_reads == []


def test_unknown_reads_are_sorted_and_deduplicated() -> None:
    spec = minimal_flow(
        {
            "materialize": [MaterializeEntry("src.xlsx", "raw")],
            "outputs": ["result"],
            "nodes": [
                step("build", ["raw", "zeta_base", "alpha_base"], ["result"]),
                step("audit", ["alpha_base"], ["snapshot"]),
            ],
        }
    )
    assert plan_flow(spec).unknown_reads == ["alpha_base", "zeta_base"]


def test_two_producers_are_rejected_with_both_ids() -> None:
    spec = minimal_flow(
        {
            "outputs": ["result"],
            "nodes": [
                step("one", ["raw_headcount"], ["result"]),
                step("two", [], ["result"]),
            ],
        }
    )
    message = error_of(lambda: plan_flow(spec))
    assert "有两个生产者" in message
    assert "one" in message
    assert "two" in message


def test_cycle_lists_only_nodes_on_the_cycle() -> None:
    spec = minimal_flow(
        {
            "materialize": [],
            "outputs": [],
            "nodes": [
                step("alpha", ["t_beta"], ["t_alpha"]),
                step("beta", ["t_gamma"], ["t_beta"]),
                step("gamma", ["t_alpha"], ["t_gamma"]),
                step("delta", [], ["t_delta"]),
            ],
        }
    )
    message = error_of(lambda: plan_flow(spec))
    assert "依赖环" in message
    assert "alpha" in message
    assert "beta" in message
    assert "gamma" in message
    assert "delta" not in message


def test_cycle_also_lists_blocked_downstream_nodes() -> None:
    # 实际行为：环下游被阻塞的节点也会被列进错误信息，不只是环上的节点。
    spec = minimal_flow(
        {
            "materialize": [],
            "outputs": [],
            "nodes": [
                step("a", ["t_back"], ["t_a"]),
                step("b", ["t_a"], ["t_back"]),
                step("c", ["t_back"], ["t_c"]),
            ],
        }
    )
    message = error_of(lambda: plan_flow(spec))
    assert "依赖环" in message
    assert "a" in message
    assert "b" in message
    assert "c" in message


# ---------------------------------------------------------------------------
# digestOf
# ---------------------------------------------------------------------------


def test_digest_is_twelve_hex_and_matches_plan() -> None:
    spec = minimal_flow()
    assert len(digest_of(spec)) == 12
    assert all(character in "0123456789abcdef" for character in digest_of(spec))
    assert plan_flow(spec).digest == digest_of(spec)
    assert digest_of(spec) == digest_of(clone_spec(spec))


def test_digest_ignores_order_of_arrays_and_keys() -> None:
    spec = minimal_flow(
        {
            "materialize": [
                MaterializeEntry(
                    "b.xlsx", "raw_b", columns={"y": "col_y", "x": "col_x"}
                ),
                MaterializeEntry("a.xlsx", "raw_a"),
            ],
            "outputs": ["out_b", "out_a"],
            "nodes": [
                FlowNode(
                    id="build_b",
                    call="pipeline.py:build_b",
                    reads=["raw_b", "raw_a"],
                    writes=["out_b"],
                    args={"k2": "v2", "k1": "v1"},
                ),
                step("build_a", ["raw_a"], ["out_a"]),
            ],
        }
    )
    digest = digest_of(spec)
    reversed_one = reversed_spec(spec)
    assert reversed_one != spec  # 确认真的换过顺序，别让断言空转
    assert digest_of(reversed_one) == digest


def test_digest_changes_with_any_semantic_field() -> None:
    base = parse_flow(
        {
            "flow": "molex-prep",
            "db": "hr.db",
            "materialize": [
                {
                    "file": "molex.xlsx",
                    "sheet": "S1",
                    "table": "raw_molex",
                    "columns": {"源列": "src_col"},
                    "load": "loaders.py:load_molex",
                }
            ],
            "outputs": ["result"],
            "nodes": [
                {
                    "id": "build",
                    "call": "pipeline.py:build",
                    "reads": ["raw_molex"],
                    "writes": ["result"],
                    "args": {"year": "2025"},
                    "timeoutMs": 5000,
                }
            ],
        }
    )
    base_digest = digest_of(base)
    entry = base.materialize[0]
    node = base.nodes[0]
    variants: list[tuple[str, FlowSpec]] = [
        ("flow", dataclasses.replace(base, flow="other-flow")),
        ("db", dataclasses.replace(base, db="other.db")),
        ("outputs", dataclasses.replace(base, outputs=["result", "extra"])),
        ("materialize.file", dataclasses.replace(base, materialize=[
            dataclasses.replace(entry, file="molex-v2.xlsx")])),
        ("materialize.table", dataclasses.replace(base, materialize=[
            dataclasses.replace(entry, table="raw_molex_v2")])),
        ("materialize.sheet", dataclasses.replace(base, materialize=[
            dataclasses.replace(entry, sheet="S2")])),
        ("materialize.sheet 删除", dataclasses.replace(base, materialize=[
            dataclasses.replace(entry, sheet=None)])),
        ("materialize.columns 的值", dataclasses.replace(base, materialize=[
            dataclasses.replace(entry, columns={"源列": "src_col2"})])),
        ("materialize.columns 删除", dataclasses.replace(base, materialize=[
            dataclasses.replace(entry, columns=None)])),
        ("materialize.load", dataclasses.replace(base, materialize=[
            dataclasses.replace(entry, load="loaders.py:load_other")])),
        ("node.call", dataclasses.replace(base, nodes=[
            dataclasses.replace(node, call="pipeline.py:build2")])),
        ("node.reads", dataclasses.replace(base, nodes=[
            dataclasses.replace(node, reads=["raw_molex", "extra_base"])])),
        ("node.writes", dataclasses.replace(base, nodes=[
            dataclasses.replace(node, writes=["result", "extra"])])),
        ("node.args 改值", dataclasses.replace(base, nodes=[
            dataclasses.replace(node, args={"year": "2026"})])),
        ("node.args 加键", dataclasses.replace(base, nodes=[
            dataclasses.replace(node, args={"year": "2025", "extra": ""})])),
        ("node.timeoutMs", dataclasses.replace(base, nodes=[
            dataclasses.replace(node, timeout_ms=6001)])),
    ]
    for label, variant in variants:
        assert digest_of(variant) != base_digest, label


def test_digest_ignores_name_title_and_steps() -> None:
    plain = minimal_flow()
    decorated = dataclasses.replace(
        plain,
        nodes=[
            dataclasses.replace(
                plain.nodes[0],
                name="中文名",
                title="把原始表算成结果表",
                steps=[FlowStep(3, "block", "if", "空表就别往下走")],
            )
        ],
    )
    assert digest_of(decorated) == digest_of(plain)


# ---------------------------------------------------------------------------
# suggestOptimizations
# ---------------------------------------------------------------------------


def test_no_hints_for_a_normal_flow() -> None:
    plan = plan_flow(
        parse_flow(
            {
                "flow": "normal",
                "materialize": [
                    {"file": "a.xlsx", "table": "raw_a"},
                    {"file": "b.xlsx", "table": "raw_b"},
                ],
                "outputs": ["result", "audit_log"],
                "nodes": [
                    {"id": "ingest", "call": "pipeline.py:ingest",
                     "reads": ["raw_a", "raw_b"], "writes": ["facts"]},
                    {"id": "report", "call": "pipeline.py:report",
                     "reads": ["facts"], "writes": ["result"]},
                    {"id": "audit", "call": "audit.sql",
                     "reads": ["facts"], "writes": ["audit_log"]},
                ],
            }
        )
    )
    assert suggest_optimizations(plan) == []
    assert suggest_optimizations(plan_flow(minimal_flow())) == []


def test_dead_node_hint() -> None:
    plan = plan_flow(
        parse_flow(
            {
                "flow": "dead",
                "materialize": [{"file": "a.xlsx", "table": "raw_a"}],
                "outputs": ["result"],
                "nodes": [
                    {"id": "main", "call": "pipeline.py:main",
                     "reads": ["raw_a"], "writes": ["result"]},
                    {"id": "orphan", "call": "pipeline.py:orphan",
                     "reads": ["raw_a"], "writes": ["scratch"]},
                ],
            }
        )
    )
    hints = suggest_optimizations(plan)
    assert len(hints) == 1
    assert hints[0].kind == "dead-node"
    assert hints[0].nodes == ["orphan"]
    assert hints[0].tables == ["scratch"]
    assert "orphan" in hints[0].note


def test_single_consumer_hint() -> None:
    plan = plan_flow(
        parse_flow(
            {
                "flow": "chain",
                "materialize": [{"file": "a.xlsx", "table": "raw_a"}],
                "outputs": ["result"],
                "nodes": [
                    {"id": "clean", "call": "pipeline.py:clean",
                     "reads": ["raw_a"], "writes": ["stage"]},
                    {"id": "build", "call": "pipeline.py:build",
                     "reads": ["stage"], "writes": ["result"]},
                ],
            }
        )
    )
    hints = suggest_optimizations(plan)
    assert len(hints) == 1
    assert hints[0].kind == "single-consumer"
    assert hints[0].nodes == ["clean", "build"]
    assert hints[0].tables == ["stage"]
    assert "clean" in hints[0].note
    assert "build" in hints[0].note


def test_single_consumer_hint_disappears_after_fanout() -> None:
    plan = plan_flow(
        parse_flow(
            {
                "flow": "fanout",
                "materialize": [{"file": "a.xlsx", "table": "raw_a"}],
                "outputs": ["result", "peek_result"],
                "nodes": [
                    {"id": "clean", "call": "pipeline.py:clean",
                     "reads": ["raw_a"], "writes": ["stage"]},
                    {"id": "build", "call": "pipeline.py:build",
                     "reads": ["stage"], "writes": ["result"]},
                    {"id": "peek", "call": "pipeline.py:peek",
                     "reads": ["stage"], "writes": ["peek_result"]},
                ],
            }
        )
    )
    assert suggest_optimizations(plan) == []


def test_unused_materialize_hint() -> None:
    plan = plan_flow(
        parse_flow(
            {
                "flow": "unused",
                "materialize": [{"file": "unused.xlsx", "table": "raw_unused"}],
                "outputs": ["result"],
                "nodes": [{"id": "main", "call": "pipeline.py:main",
                           "reads": [], "writes": ["result"]}],
            }
        )
    )
    hints = suggest_optimizations(plan)
    assert len(hints) == 1
    assert hints[0].kind == "unused-materialize"
    assert hints[0].nodes == []
    assert hints[0].tables == ["raw_unused"]
    assert "raw_unused" in hints[0].note


def test_hint_order_is_materialize_first_then_node_order() -> None:
    plan = plan_flow(
        parse_flow(
            {
                "flow": "mixed",
                "materialize": [
                    {"file": "unused.xlsx", "table": "raw_unused"},
                    {"file": "src.xlsx", "table": "raw_src"},
                ],
                "outputs": ["result"],
                "nodes": [
                    {"id": "a_clean", "call": "pipeline.py:a_clean",
                     "reads": ["raw_src"], "writes": ["stage"]},
                    {"id": "b_build", "call": "pipeline.py:b_build",
                     "reads": ["stage"], "writes": ["result"]},
                    {"id": "c_orphan", "call": "pipeline.py:c_orphan",
                     "reads": ["raw_src"], "writes": ["scratch"]},
                ],
            }
        )
    )
    assert [hint.kind for hint in suggest_optimizations(plan)] == [
        "unused-materialize",
        "single-consumer",
        "dead-node",
    ]


def test_read_only_node_is_not_a_dead_node() -> None:
    plan = plan_flow(
        parse_flow(
            {
                "flow": "sink",
                "materialize": [{"file": "a.xlsx", "table": "raw_a"}],
                "outputs": ["result"],
                "nodes": [
                    {"id": "reader", "call": "pipeline.py:reader",
                     "reads": ["raw_a"], "writes": []},
                    {"id": "main", "call": "pipeline.py:main",
                     "reads": ["raw_a"], "writes": ["result"]},
                ],
            }
        )
    )
    assert suggest_optimizations(plan) == []


# ---------------------------------------------------------------------------
# formatFlowWarning / describeFlow
# ---------------------------------------------------------------------------


def test_format_flow_warning_includes_location_and_code() -> None:
    assert format_flow_warning(
        "pipeline.py", FlowWarning("UNUSED_TABLE_FUNC", "死代码", 40)
    ) == "pipeline.py:40 [UNUSED_TABLE_FUNC] 死代码"
    assert format_flow_warning(None, FlowWarning("X", "消息", None)) == "[X] 消息"


def test_describe_flow_prints_plan_in_topological_order() -> None:
    spec = parse_flow(
        {
            "flow": "molex-prep",
            "db": "hr.db",
            "materialize": [
                {"file": "molex.xlsx", "sheet": "Sheet1", "table": "raw_molex",
                 "load": "loaders.py:load_molex"}
            ],
            "outputs": ["result"],
            "nodes": [
                {"id": "build", "call": "pipeline.py:build",
                 "reads": ["stage"], "writes": ["result"]},
                {"id": "clean", "call": "pipeline.py:clean",
                 "reads": ["raw_molex", "base_table"], "writes": ["stage"]},
            ],
        }
    )
    plan = plan_flow(spec)
    lines = describe_flow(plan)

    assert lines[0] == (
        f"流程 molex-prep@{plan.digest} db=hr.db nodes=2 edges=1 graph=flow.json"
    )
    assert lines[1] == "  outputs: result"
    assert "  [物化] molex.xlsx#Sheet1 -> raw_molex (load=loaders.py:load_molex)" in lines

    node_lines = [line for line in lines if "(pipeline.py:" in line]
    assert node_lines == [
        f"  {'clean':18} raw_molex+base_table -> stage (pipeline.py:clean)",
        f"  {'build':18} stage -> result (pipeline.py:build)",
    ]
    assert "  外部读取: base_table（必须存在于输入数据库）" in lines


def test_describe_flow_placeholders_and_missing_sections() -> None:
    plan = plan_flow(minimal_flow({"outputs": []}))
    lines = describe_flow(plan)

    assert lines[1] == "  outputs: (未声明)"
    assert "  [物化] headcount.xlsx -> raw_headcount" in lines
    assert f"  {'build':18} raw_headcount -> result (pipeline.py:build)" in lines
    assert not any("外部读取" in line for line in lines)


def test_describe_flow_code_graph_with_titles_steps_and_warnings() -> None:
    spec = FlowSpec(
        flow="molex-prep",
        db="hr.db",
        materialize=[],
        outputs=["result"],
        entry="pipeline.py",
        warnings=[FlowWarning("UNUSED_TABLE_FUNC", "dead_code 带表标注但 main 没调用它", 40)],
        nodes=[
            FlowNode(
                id="build",
                call="pipeline.py:build",
                reads=["raw"],
                writes=["result"],
                title="把原始表算成结果表",
                steps=[FlowStep(12, "block", "if", "空表就别往下走")],
            )
        ],
    )
    plan = plan_flow(spec)
    quiet = describe_flow(plan)

    assert "graph=code:pipeline.py" in quiet[0]
    assert (
        f"  {'build':18} raw -> result (pipeline.py:build) | 把原始表算成结果表"
    ) in quiet
    assert "  ! pipeline.py:40 [UNUSED_TABLE_FUNC] dead_code 带表标注但 main 没调用它" in quiet
    assert not any("空表就别往下走" in line for line in quiet)

    verbose = describe_flow(plan, steps=True)
    assert "      [if] 空表就别往下走 (L12)" in verbose


# ---------------------------------------------------------------------------
# 代码即流程：合法代码
# ---------------------------------------------------------------------------

GOOD = '''
def main(db, as_of):
    """把原始表算成两张交付表。"""
    build_clean(db)
    enrich(db, as_of)


def build_clean(db) -> "t:clean_table":
    """清洗：去重、补空串。"""
    """同一 ID 只留一行"""
    rows = 1
    if rows:
        """空表就别往下走"""
        return None
    for name in ("a", "b"):
        """文本列补空串"""
        pass
    return None


def enrich(db, source: "t:clean_table", as_of: str) -> "t:result":
    """补业务日期。"""
    return None
'''


def parse_code(source: str, entry: str = "pipeline.py") -> CodeFlow:
    """直接传源码字符串；python 默认就是当前解释器，无需显式传入。"""
    return parse_code_flow(source, entry)


def test_parse_code_flow_reads_order_edges_and_titles() -> None:
    flow = parse_code(GOOD)
    assert flow.entry == "pipeline.py"
    assert flow.warnings == []
    assert [node.id for node in flow.nodes] == ["build_clean", "enrich"]

    clean, enrich = flow.nodes
    assert clean.reads == []
    assert clean.writes == ["clean_table"]
    assert clean.title == "清洗：去重、补空串。"
    # as_of: str 是固定入参，不要求表标注
    assert enrich.reads == ["clean_table"]
    assert enrich.writes == ["result"]
    assert enrich.title == "补业务日期。"
    # Python 的 FlowNode 没有 line 字段（TS 版有）：用解析出的步骤证明节点来自代码。
    assert clean.steps


def test_parse_code_flow_collects_steps() -> None:
    clean = parse_code(GOOD).nodes[0]
    assert clean.steps == [
        FlowStep(10, "statement", "Assign", "同一 ID 只留一行"),
        FlowStep(12, "block", "if", "空表就别往下走"),
        FlowStep(15, "block", "for", "文本列补空串"),
    ]


def test_parse_code_flow_translates_nodes_with_id_and_call() -> None:
    nodes = parse_code(GOOD, "src/build.py").nodes
    assert len(nodes) == 2
    first = nodes[0]
    assert first.id == "build_clean"
    assert first.call == "src/build.py:build_clean"
    assert first.reads == []
    assert first.writes == ["clean_table"]
    assert first.title == "清洗：去重、补空串。"
    assert first.args is None


def test_parse_code_flow_ignores_runtime_params_and_pandas_plumbing() -> None:
    flow = parse_code(
        """
import sqlite3
import pandas as pd


def main(db, as_of):
    \"\"\"入口。\"\"\"
    conn = sqlite3.connect(db)
    raw = pd.read_sql("select * from raw_t", conn)
    conn.close()
    print("start", as_of)
    write_t(db, raw)


def write_t(db, raw: "t:raw_t") -> "t:t":
    \"\"\"写 t。\"\"\"
"""
    )
    assert [node.id for node in flow.nodes] == ["write_t"]
    assert flow.warnings == []


def test_parse_code_flow_deduplicates_reads_for_self_join() -> None:
    node = parse_code(
        """
def main(db):
    \"\"\"入口。\"\"\"
    join_t(db)


def join_t(db, left: "t:employees", right: "t:employees") -> "t:joined":
    \"\"\"自连接。\"\"\"
"""
    ).nodes[0]
    assert node.reads == ["employees"]
    assert node.writes == ["joined"]


def test_parse_code_flow_allows_in_place_update() -> None:
    node = parse_code(
        """
def main(db):
    \"\"\"入口。\"\"\"
    refresh(db)


def refresh(db, current: "t:board") -> "t:board":
    \"\"\"原地刷新看板。\"\"\"
"""
    ).nodes[0]
    assert node.reads == ["board"]
    assert node.writes == ["board"]


def test_parse_code_flow_reads_name_comment() -> None:
    # 紧挨着 def 上一行的 `# name:` 注释就是流程图上的中文名。
    flow = parse_code(
        '''
# name: 清洗节点
def build(db) -> "t:a":
    """写 a。"""


def main(db):
    """入口。"""
    build(db)
'''
    )
    assert flow.nodes[0].name == "清洗节点"


def test_parse_code_flow_warns_about_unused_table_func() -> None:
    flow = parse_code(
        """
def main(db):
    \"\"\"入口。\"\"\"
    real(db)


def real(db) -> "t:used":
    \"\"\"真的会跑。\"\"\"


def never_called(db) -> "t:orphan":
    \"\"\"没人调，多半是忘记在 main 里加一行。\"\"\"
"""
    )
    assert [node.id for node in flow.nodes] == ["real"]
    assert len(flow.warnings) == 1
    assert flow.warnings[0].code == "UNUSED_TABLE_FUNC"
    assert flow.warnings[0].line == 11
    assert "never_called" in flow.warnings[0].message


def test_parse_code_flow_handles_future_annotations_bom_and_crlf() -> None:
    future = parse_code(
        """
from __future__ import annotations


def main(db):
    \"\"\"入口。\"\"\"
    build(db, 1)


def build(db, raw: "t:raw_a") -> "t:a":
    \"\"\"写 a。\"\"\"
"""
    )
    assert [node.id for node in future.nodes] == ["build"]

    messy = "\ufeff" + GOOD.replace("\n", "\r\n")
    assert [node.id for node in parse_code(messy).nodes] == ["build_clean", "enrich"]


def test_code_graph_and_declared_graph_share_the_same_digest() -> None:
    code = parse_code(GOOD)
    declared = parse_flow(
        {
            "flow": "molex-prep",
            "db": "hr.db",
            "outputs": ["result"],
            "materialize": [],
            "nodes": [
                {"id": "build_clean", "call": "pipeline.py:build_clean",
                 "reads": [], "writes": ["clean_table"]},
                {"id": "enrich", "call": "pipeline.py:enrich",
                 "reads": ["clean_table"], "writes": ["result"]},
            ],
        }
    )
    from_code = dataclasses.replace(
        parse_flow({"flow": "molex-prep", "db": "hr.db", "outputs": ["result"],
                    "materialize": []}),
        entry=code.entry,
        nodes=code.nodes,
        warnings=code.warnings,
    )
    assert plan_flow(from_code).digest == digest_of(declared)
    assert plan_flow(from_code).order == plan_flow(declared).order


# ---------------------------------------------------------------------------
# 代码即流程：main 必须是线性的
# ---------------------------------------------------------------------------


def test_main_missing_is_reported() -> None:
    message = error_of(lambda: parse_code('def build(db) -> "t:a":\n    """x"""\n'))
    assert "MAIN_MISSING" in message
    assert "def main" in message


@pytest.mark.parametrize(
    ("kind", "block"),
    [
        ("for", "    for f in (a, b):\n        f(db)\n"),
        ("if", "    if db:\n        build(db)\n"),
        ("while", "    while False:\n        build(db)\n"),
        ("try", "    try:\n        build(db)\n    except Exception:\n        pass\n"),
        ("with", "    with open(db) as fh:\n        build(db)\n"),
    ],
)
def test_main_control_flow_is_rejected(kind: str, block: str) -> None:
    message = error_of(
        lambda: parse_code(
            f'def main(db):\n    """入口。"""\n{block}\n'
            'def build(db) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "MAIN_CONTROL_FLOW" in message
    assert "pipeline.py:" in message
    assert kind in message
    assert "线性" in message


def test_main_defining_a_function_is_not_linear() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    def inner():\n        pass\n'
            '    build(db)\n\ndef build(db) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "MAIN_CONTROL_FLOW" in message


def test_main_nested_node_calls_are_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            """
def main(db):
    \"\"\"入口。\"\"\"
    outer(db, inner(db))


def inner(db) -> "t:i":
    \"\"\"内。\"\"\"


def outer(db, x: "t:i") -> "t:o":
    \"\"\"外。\"\"\"
"""
        )
    )
    assert "MAIN_NESTED_CALL" in message
    assert "inner / outer" in message or "outer / inner" in message


def test_main_without_local_calls_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'import helpers\n\ndef main(db):\n    """入口。"""\n    helpers.clean(db)\n'
        )
    )
    assert "MAIN_NO_NODES" in message
    assert "import" in message


def test_main_call_not_local_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    clean_employes(db)\n\n'
            'def build(db) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "MAIN_CALL_NOT_LOCAL" in message
    assert "clean_employes" in message


def test_duplicate_node_call_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    build(db)\n    build(db)\n\n'
            'def build(db) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "NODE_DUPLICATE" in message


def test_main_with_table_annotation_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db, raw: "t:raw_t"):\n    """入口。"""\n    build(db)\n\n'
            'def build(db) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "MAIN_TABLE_PARAM" in message


# ---------------------------------------------------------------------------
# 代码即流程：节点签名必须是表标注
# ---------------------------------------------------------------------------


def test_node_without_docstring_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    build(db)\n\n'
            'def build(db) -> "t:a":\n    pass\n'
        )
    )
    assert "NODE_NO_DOC" in message


def test_unannotated_node_param_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    build(db, 1)\n\n'
            'def build(db, thing) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "NODE_PARAM_UNANNOTATED" in message
    assert "thing" in message
    assert "db / as_of / inputs / out" in message


def test_report_param_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    build(db)\n\n'
            'def build(db, report) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "NODE_PARAM_UNANNOTATED" in message
    assert "report" in message


def test_node_without_table_return_is_rejected() -> None:
    for signature in (
        'def build(db):\n    """写 a。"""\n',
        'def build(db) -> None:\n    """写 a。"""\n',
    ):
        message = error_of(
            lambda signature=signature: parse_code(
                'def main(db):\n    """入口。"""\n    build(db)\n\n' + signature
            )
        )
        assert "NODE_RETURN_NOT_TABLE" in message


@pytest.mark.parametrize(
    "annotation",
    [
        'raw: f"t:{name}"',
        "raw: TABLE_NAME",
        'raw: Table["raw"]',
        "raw: str",
    ],
)
def test_non_literal_table_annotation_is_rejected(annotation: str) -> None:
    source = (
        'name = "raw"\nTABLE_NAME = "raw"\n\n\n'
        'def main(db):\n    """入口。"""\n    build(db)\n\n\n'
        "def build(db, " + annotation + ') -> "t:a":\n    """写 a。"""\n'
    )
    message = error_of(lambda: parse_code(source))
    assert "NODE_PARAM_NOT_TABLE" in message
    assert "字面量" in message


def test_non_ascii_table_name_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    build(db)\n\n'
            'def build(db) -> "t:员工表":\n    """写 a。"""\n'
        )
    )
    assert "NODE_TABLE_NAME_BAD" in message
    assert "ASCII" in message


def test_async_node_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    build(db)\n\n'
            'async def build(db) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "NODE_ASYNC" in message


def test_positional_only_node_param_is_rejected() -> None:
    message = error_of(
        lambda: parse_code(
            'def main(db):\n    """入口。"""\n    build(db)\n\n'
            'def build(db, raw, /) -> "t:a":\n    """写 a。"""\n'
        )
    )
    assert "NODE_POSONLY" in message


def test_syntax_error_reports_line_number() -> None:
    message = error_of(lambda: parse_code('def main(db)\n    """缺冒号"""\n'))
    assert "SYNTAX_ERROR" in message
    assert "pipeline.py:" in message


# ---------------------------------------------------------------------------
# flow-run：SQL 节点执行
# ---------------------------------------------------------------------------


def make_db(directory: Path) -> Path:
    """造一个空库，带上 sqlite 之外的一张 meta 表（贴近真实业务库）。"""
    path = directory / "hr.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)", ("created_at", "fixed-for-test")
        )
        connection.commit()
    finally:
        connection.close()
    return path


def rows(db_path: Path, table: str) -> list[dict[str, object]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        return [dict(row) for row in cursor]
    finally:
        connection.close()


def test_sql_node_runs_in_transaction_and_rolls_back(tmp_path: Path) -> None:
    db_path = make_db(tmp_path)

    ok = execute_sql_node(db_path, "CREATE TABLE result AS SELECT 1 AS x;")
    assert ok.exit_code == 0
    assert "result" in table_names(db_path)

    bad = execute_sql_node(db_path, "CREATE TABLE broken AS SELECT 1; INSERT INTO nope VALUES (1);")
    assert bad.exit_code == 1
    assert "nope" in bad.stderr
    # 回滚是硬要求：半张表比没有表更危险。
    assert "broken" not in table_names(db_path)


# ---------------------------------------------------------------------------
# flow-run：一轮执行
# ---------------------------------------------------------------------------


def _materialized(_: object) -> ProcessResult:
    return ProcessResult(0, False, "", "", 0)


def _no_python(_: object) -> ProcessResult:
    raise AssertionError("这个用例不应该跑 python")


def _options(
    plan: FlowPlan,
    db_path: Path,
    directory: Path,
    read_file: Callable[[str], str],
    log: Callable[[str], None] | None = None,
    run_materialize: Callable[[object], ProcessResult] = _materialized,
) -> FlowRoundOptions:
    return FlowRoundOptions(
        plan=plan,
        db_path=db_path,
        inputs_dir=directory,
        out_dir=directory / "out",
        as_of="2026-01-01",
        run_materialize=run_materialize,
        run_python=_no_python,
        read_file=read_file,
        log=log or (lambda _: None),
    )


def test_round_runs_sql_nodes_in_topological_order(tmp_path: Path) -> None:
    directory = tmp_path
    db_path = make_db(directory)
    plan = plan_flow(
        parse_flow(
            {
                "flow": "demo",
                "db": "hr.db",
                "outputs": ["result"],
                "nodes": [
                    {"id": "make_source", "call": "sql/source.sql", "reads": [],
                     "writes": ["source"]},
                    {"id": "make_result", "call": "sql/result.sql", "reads": ["source"],
                     "writes": ["result"]},
                ],
            }
        )
    )
    files = {
        "sql/source.sql": "CREATE TABLE source AS SELECT 1 AS id;",
        "sql/result.sql": (
            "CREATE TABLE result AS SELECT id * 2 AS doubled FROM source;"
        ),
    }

    round_result = run_flow_round(
        _options(plan, db_path, directory, lambda path: files.get(path, ""))
    )

    assert round_result.ok is True
    assert [node.id for node in round_result.nodes] == ["make_source", "make_result"]
    assert round_result.nodes[0].changed == ["source"]
    assert round_result.nodes[1].changed == ["result"]
    assert rows(db_path, "result") == [{"doubled": 2}]
    assert round_result.outputs["result"].rows == 1


def test_missing_read_table_short_circuits(tmp_path: Path) -> None:
    directory = tmp_path
    db_path = make_db(directory)
    plan = plan_flow(
        parse_flow(
            {
                "flow": "demo",
                "db": "hr.db",
                "outputs": ["final"],
                "nodes": [
                    {"id": "first", "call": "sql/first.sql", "reads": ["missing_table"],
                     "writes": ["intermediate"]},
                    {"id": "second", "call": "sql/second.sql", "reads": ["intermediate"],
                     "writes": ["final"]},
                ],
            }
        )
    )

    round_result = run_flow_round(
        _options(plan, db_path, directory, lambda _: "SELECT 1;")
    )

    assert round_result.ok is False
    assert len(round_result.nodes) == 1
    assert round_result.nodes[0].missing_reads == ["missing_table"]
    assert round_result.nodes[0].exit_code == 1


def test_unchanged_write_is_recorded_but_not_a_failure(tmp_path: Path) -> None:
    directory = tmp_path
    db_path = make_db(directory)
    # 先造出 result 并写定内容：节点接下来写的是同一份内容，才叫「真的没变」。
    execute_sql_node(db_path, "CREATE TABLE result (x INTEGER); INSERT INTO result VALUES (1);")
    plan = plan_flow(
        parse_flow(
            {
                "flow": "demo",
                "db": "hr.db",
                "outputs": ["result"],
                "nodes": [{"id": "noop", "call": "sql/noop.sql", "reads": [],
                           "writes": ["result"]}],
            }
        )
    )

    round_result = run_flow_round(
        _options(
            plan,
            db_path,
            directory,
            lambda _: "DELETE FROM result; INSERT INTO result VALUES (1);",
        )
    )

    assert round_result.ok is True
    assert round_result.nodes[0].unchanged == ["result"]
    assert round_result.nodes[0].changed == []
    assert round_result.outputs["result"].rows == 1


def test_declared_output_that_never_appears_fails_the_round(tmp_path: Path) -> None:
    directory = tmp_path
    db_path = make_db(directory)
    plan = plan_flow(
        parse_flow(
            {
                "flow": "demo",
                "db": "hr.db",
                "outputs": ["result"],
                "nodes": [{"id": "noop", "call": "sql/noop.sql", "reads": [],
                           "writes": ["result"]}],
            }
        )
    )
    logs: list[str] = []

    round_result = run_flow_round(
        _options(
            plan,
            db_path,
            directory,
            # 节点跑成功了，但它只建了一张跟声明无关的表：result 从没出现过。
            lambda _: "CREATE TABLE IF NOT EXISTS unrelated (x INTEGER);",
            log=logs.append,
        )
    )

    # 两轮的 outputs 里都没有 result —— 旧实现会因此判 identical。
    assert round_result.ok is False
    assert "result" not in round_result.outputs
    assert any("声明的输出表未出现: result" in line for line in logs)


def test_empty_outputs_fails_the_round(tmp_path: Path) -> None:
    directory = tmp_path
    db_path = make_db(directory)
    plan = plan_flow(
        parse_flow(
            {
                "flow": "demo",
                "db": "hr.db",
                "outputs": [],
                "nodes": [{"id": "make", "call": "sql/make.sql", "reads": [],
                           "writes": ["result"]}],
            }
        )
    )

    round_result = run_flow_round(
        _options(plan, db_path, directory, lambda _: "CREATE TABLE result AS SELECT 1 AS x;")
    )

    assert round_result.ok is False
    assert any("flow.outputs 为空" in warning for warning in round_result.warnings)
    assert len(round_result.nodes) == 1


def test_materialize_success_feeds_downstream_nodes(tmp_path: Path) -> None:
    directory = tmp_path
    db_path = make_db(directory)
    plan = plan_flow(
        parse_flow(
            {
                "flow": "demo",
                "db": "hr.db",
                "outputs": ["result"],
                "materialize": [{"file": "a.xlsx", "table": "available"}],
                "nodes": [{"id": "use", "call": "sql/use.sql", "reads": ["available"],
                           "writes": ["result"]}],
            }
        )
    )

    def materialize(_: object) -> ProcessResult:
        execute_sql_node(db_path, "CREATE TABLE available AS SELECT 1 AS id;")
        return ProcessResult(0, False, "", "", 0)

    round_result = run_flow_round(
        _options(
            plan,
            db_path,
            directory,
            lambda _: "CREATE TABLE result AS SELECT id * 2 AS doubled FROM available;",
            run_materialize=materialize,
        )
    )

    assert round_result.ok is True
    assert round_result.materialize.entries[0].produced is True
    assert [node.id for node in round_result.nodes] == ["use"]
    assert rows(db_path, "result") == [{"doubled": 2}]


def test_materialize_that_produces_nothing_fails_before_nodes(tmp_path: Path) -> None:
    directory = tmp_path
    db_path = make_db(directory)
    plan = plan_flow(
        parse_flow(
            {
                "flow": "demo",
                "db": "hr.db",
                "outputs": ["result"],
                "materialize": [{"file": "a.xlsx", "table": "available"}],
                "nodes": [{"id": "use", "call": "sql/use.sql", "reads": ["available"],
                           "writes": ["result"]}],
            }
        )
    )

    round_result = run_flow_round(
        _options(plan, db_path, directory, lambda _: "SELECT 1;")
    )

    assert round_result.ok is False
    assert round_result.materialize.entries[0].produced is False
    assert len(round_result.nodes) == 0


# ---------------------------------------------------------------------------
# flow-run：两轮比对
# ---------------------------------------------------------------------------


def _round(outputs: dict[str, TableDigest], files: list[Digest]) -> FlowRoundResult:
    return FlowRoundResult(
        ok=True,
        materialize=MaterializeReport(0, False, 0, "", "", []),
        nodes=[],
        outputs=outputs,
        files=files,
    )


def test_identical_rounds_have_empty_diff() -> None:
    first = _round({"result": TableDigest(2, "aaa")}, [Digest("a.csv", "f1", 3)])
    second = _round({"result": TableDigest(2, "aaa")}, [Digest("a.csv", "f1", 3)])
    diff = diff_rounds(first, second)
    assert diff.tables == []
    assert diff.files == []
    assert diff.identical is True


def test_changed_tables_and_files_are_named() -> None:
    first = _round(
        {"result": TableDigest(2, "aaa"), "other": TableDigest(1, "x")},
        [Digest("a.csv", "f1", 3)],
    )
    second = _round(
        {"result": TableDigest(2, "bbb"), "other": TableDigest(1, "x")},
        [Digest("a.csv", "f2", 3)],
    )
    diff = diff_rounds(first, second)
    assert diff.identical is False
    assert diff.tables == ["result"]
    assert diff.files == ["a.csv:f1", "a.csv:f2"]


def test_extra_table_on_one_side_counts_as_different() -> None:
    first = _round({"result": TableDigest(1, "a")}, [])
    second = _round({"result": TableDigest(1, "a"), "extra": TableDigest(1, "b")}, [])
    assert diff_rounds(first, second).tables == ["extra"]
