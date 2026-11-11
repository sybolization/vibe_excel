"""book 页面的回归场景，迁移自 packages/book/test/book.test.ts。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from vibe_excel.book import (
    build_diagram,
    build_graph,
    build_inputs,
    collect_book,
    conversation_title,
    parse_conversation,
    parse_transcript,
    render_book,
)
from vibe_excel.session import SessionStore

# ---------------------------------------------------------------------------
# 共用数据：与 TS 里的 TRANSCRIPT / RUN_JSON 一一对应，JSON 键保持原样。
# ---------------------------------------------------------------------------


def _jsonl(records: list[object]) -> str:
    """把记录序列拼成 JSONL 文本，与 TS 的 jsonl 辅助函数同一语义。"""
    return "\n".join(json.dumps(record) for record in records)


TRANSCRIPT = _jsonl(
    [
        {"type": "session", "id": "s1", "timestamp": "2026-11-04T08:11:20.122Z", "cwd": "C:\\x"},
        {
            "type": "model_change",
            "id": "m1",
            "timestamp": "2026-11-04T08:11:20.192Z",
            "modelId": "m",
        },
        {
            "type": "message",
            "id": "u1",
            "timestamp": "2026-11-04T08:11:20.203Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "\ufeff# 本次任务：按地点汇总\n\n细节"}],
            },
        },
        {
            "type": "message",
            "id": "a1",
            "timestamp": "2026-11-04T08:11:27.547Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "我先查看输入文件，了解它们的结构。"},
                    {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                    {"type": "toolCall", "id": "c2", "name": "write", "arguments": {"path": "a.py"}},
                    {"type": "toolCall", "id": "c3", "name": "edit", "arguments": {"path": "a.py"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "t1",
            "timestamp": "2026-11-04T08:13:19.458Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "toolName": "bash",
                "content": [{"type": "text", "text": "ok"}],
            },
        },
    ]
)

RUN_JSON = {
    "kind": "flow",
    "flow": "molex-prep",
    "entry": "site_board.py",
    "codeCommit": "92435f0b455b5eb04fd0dd3903018fae876b3556",
    "inputRevisionId": "ba0583fa-b033-4e16-acd8-abd4e7024aab",
    "identical": True,
    "elapsedMs": 7515,
    "graph": {
        "entry": "site_board.py",
        "warnings": [],
        "nodes": [
            {
                "id": "clean_positions",
                "call": "site_board.py:clean_positions",
                "reads": ["raw_available"],
                "writes": ["site_positions"],
                "title": "清洗可用岗位表",
                "steps": [
                    {"line": 47, "kind": "statement", "of": "Assign", "label": "只保留四列"},
                    {"line": 50, "kind": "statement", "of": "Assign", "label": "岗位号统一大写"},
                ],
            },
            {
                "id": "build_site_board",
                "call": "site_board.py:build_site_board",
                "reads": ["site_positions"],
                "writes": ["site_board"],
                "title": "按地点汇总",
                "steps": [
                    {"line": 67, "kind": "statement", "of": "Assign", "label": "空地点归到未标注"}
                ],
            },
        ],
    },
    "rounds": {
        "a": {
            "materialize": {
                "entries": [
                    {
                        "entry": {
                            "file": "可用岗位表.xlsx",
                            "sheet": "可用岗位",
                            "table": "raw_available",
                        },
                        "produced": True,
                        "digest": {"rows": 6337},
                    }
                ]
            },
            "nodes": [
                {"id": "clean_positions", "elapsedMs": 1332, "changed": ["site_positions"]},
                {"id": "build_site_board", "elapsedMs": 1001, "changed": ["site_board"]},
            ],
            "outputs": {"site_board": {"rows": 1}},
        }
    },
}


# ---------------------------------------------------------------------------
# parse_transcript：三段用例
# ---------------------------------------------------------------------------


def test_transcript_reads_human_text_and_counts_tool_calls() -> None:
    """读出两头的人话，数工具调用与写入次数。"""
    episode = parse_transcript(
        "C:\\s\\2026-11-04T08-11-20-122Z_abc.jsonl", TRANSCRIPT, warnings=[]
    )
    assert episode is not None
    assert episode["user"] == "# 本次任务：按地点汇总 细节"
    assert episode["assistant"] == "我先查看输入文件，了解它们的结构。"
    assert episode["toolCalls"] == 3
    # bash 不算写入，write 与 edit 各算一次。
    assert episode["writes"] == 2
    assert episode["records"] == 5
    assert episode["start"] == "2026-11-04T08:11:20.122Z"
    assert episode["end"] == "2026-11-04T08:13:19.458Z"
    assert episode["id"] == "2026-11-04T08-11-20-122Z_abc"


def test_transcript_ignores_incomplete_last_line() -> None:
    """尾部半行不影响解析。"""
    broken = f'{TRANSCRIPT}\n{{"type":"message","id":"x'
    assert parse_transcript("a.jsonl", broken, warnings=[])["records"] == 5



def test_conversation_keeps_each_message_and_tool_result() -> None:
    """逐条留消息；工具结果按 toolCallId 回填，缺结果的按成功算。"""
    conversation = parse_conversation(
        "C:\\s\\2026-11-04T08-11-20-122Z_abc.jsonl", TRANSCRIPT, warnings=[]
    )
    assert conversation is not None
    assert conversation["title"] == "本次任务：按地点汇总"
    assert [block["kind"] for block in conversation["blocks"]] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "tool",
    ]
    tools = [block for block in conversation["blocks"] if block["kind"] == "tool"]
    assert [block["name"] for block in tools] == ["bash", "write", "edit"]
    assert tools[0]["brief"] == "ls"
    assert tools[0]["result"] == "ok"
    assert tools[0]["ok"] is True
    # 没有结果记录的工具（会话被中断）不能当成失败
    assert tools[1]["ok"] is True
    assert tools[1]["result"] == ""


def test_conversation_title_strips_markdown_heading() -> None:
    assert conversation_title("\ufeff## 标题\n正文") == "标题"
    assert conversation_title("   ") == "未命名对话"


def test_conversation_empty_text_returns_none() -> None:
    assert parse_conversation("a.jsonl", "", warnings=[]) is None

def test_transcript_returns_none_for_empty_file() -> None:
    """空文件返回 None。"""
    assert parse_transcript("a.jsonl", "", warnings=[]) is None


# ---------------------------------------------------------------------------
# build_graph：从 run.json 还原图
# ---------------------------------------------------------------------------


def test_build_graph_restores_nodes_steps_materialize_and_outputs() -> None:
    """从 run.json 还原节点、步骤、物化与产出。"""
    graph = build_graph({"runId": "c4d807a6-c467", "tables": {}, "sample": None, "runJson": RUN_JSON})
    assert graph is not None
    assert [node["fn"] for node in graph["nodes"]] == ["clean_positions", "build_site_board"]
    assert len(graph["nodes"][0]["steps"]) == 2
    assert graph["nodes"][0]["elapsedMs"] == 1332
    assert graph["materialize"][0]["rows"] == 6337
    assert graph["outputs"] == ["site_board"]
    assert graph["runShort"] == "c4d807a6"
    assert graph["identical"] is True


def test_build_graph_returns_none_without_graph_nodes() -> None:
    """没有 graph.nodes 时返回 None。"""
    assert build_graph({"runId": "x", "tables": {}, "sample": None, "runJson": {"flow": "f"}}) is None


# ---------------------------------------------------------------------------
# render_book：内联资源与脚本转义
# ---------------------------------------------------------------------------


def _book_view() -> dict[str, object]:
    """构造一份最小的 BookView，字段名与 camelCase 视图模型一致。"""
    return {
        "generatedAt": "2026-11-05T03:00:00.000Z",
        "store": "C:\\store",
        "program": {"id": "p1", "name": "molex-prep"},
        "session": {"id": "s1", "name": "real-data", "draftBranch": "draft/s1", "status": "active"},
        "revisions": [],
        "currentRevision": "f8012f7e-6ddf",
        "lastRunRevision": "ba0583fa-b033",
        "runs": [],
        "publishes": [],
        "graph": None,
        "inputs": None,
        "diagram": None,
        "transcript": [],
        "focusEpisode": None,
    }


def test_render_book_inlines_data_and_escapes_script_tag() -> None:
    """内联数据与样式，且 </script> 被转义。"""
    view = _book_view()
    view["session"] = {
        "id": "s1",
        "name": "</script><b>x</b>",
        "draftBranch": "draft/s1",
        "status": "active",
    }
    html = render_book(view)
    assert 'id="book-data"' in html
    assert "molex-prep" in html
    # 样式真的被内联了：认结构，别认某一个色值。
    assert ".cv{position:fixed" in html
    assert "--accent:" in html
    assert "</script><b>x</b>" not in html
    # 转义后是字面量反斜杠 u003c，而非真实的小于号。
    assert "\\u003c/script" in html


# ---------------------------------------------------------------------------
# build_inputs：物化优先、派生链、来源列、孤儿、空库
# ---------------------------------------------------------------------------

_OLD_RUN = {
    "id": "aaaaaaaa-1111",
    "startedAt": "2026-10-30T01:00:00.000Z",
    "doc": {
        "rounds": {
            "a": {
                "materialize": {
                    "entries": [
                        {
                            "entry": {"file": "f.xlsx", "sheet": "S1", "table": "raw_a"},
                            "digest": {"rows": 10},
                        },
                        {
                            "entry": {"file": "f.xlsx", "sheet": "S2", "table": "raw_b"},
                            "digest": {"rows": 5},
                        },
                    ]
                }
            }
        },
        "graph": {
            "nodes": [
                {"call": "p.py:build_a", "reads": ["raw_a"], "writes": ["a"]},
                {"call": "p.py:build_b", "reads": ["raw_b"], "writes": ["b"]},
                {"call": "p.py:build_ab", "reads": ["a", "b"], "writes": ["ab"]},
            ]
        },
    },
}

_NEW_RUN = {
    "id": "bbbbbbbb-2222",
    "startedAt": "2026-11-04T08:00:00.000Z",
    "doc": {
        "rounds": {
            "a": {
                "materialize": {
                    "entries": [
                        {
                            "entry": {"file": "f.xlsx", "sheet": "S1", "table": "raw_a"},
                            "digest": {"rows": 10},
                        }
                    ]
                }
            }
        },
        "graph": {"nodes": [{"call": "q.py:build_z", "reads": ["raw_a"], "writes": ["z"]}]},
    },
}

_INPUT_TABLES = [
    {"name": "raw_a", "rows": 10, "columns": 3, "sources": []},
    {"name": "raw_b", "rows": 5, "columns": 3, "sources": []},
    {"name": "a", "rows": 10, "columns": 2, "sources": []},
    {"name": "b", "rows": 5, "columns": 2, "sources": []},
    {"name": "ab", "rows": 5, "columns": 2, "sources": []},
    {"name": "z", "rows": 10, "columns": 2, "sources": []},
    {
        "name": "imp",
        "rows": 3,
        "columns": 4,
        "sources": [{"file": "g.xls", "sheet": "SHEET1", "rows": 3}],
    },
    {"name": "stray", "rows": 1, "columns": 1, "sources": []},
]

_INPUT_BATCH = {
    "batchId": "b1",
    "asOf": "2026-10-23",
    "capturedAt": "2026-10-30T01:44:23.838Z",
    "files": [{"name": "f.xlsx", "bytes": 200425, "sha256": "s"}],
}


def _inputs_view() -> dict[str, object]:
    """构造 TS 里 buildInputs 的同一份输入，返回输入视图。"""
    return build_inputs(
        {
            "batch": _INPUT_BATCH,
            "dbPath": "hr.db",
            "tables": _INPUT_TABLES,
            "runs": [_OLD_RUN, _NEW_RUN],
            "reads": ["raw_a"],
            "writes": ["z"],
        }
    )


def _find(rows: list[dict[str, object]], name: str) -> dict[str, object]:
    """按表名取一行，找不到就让测试直接失败。"""
    return next(row for row in rows if row["name"] == name)


def test_inputs_materialize_prefers_latest_run_and_backfills_old_tables() -> None:
    """物化以最新那次运行为准，老运行只补它独有的表。"""
    view = _inputs_view()
    first = view["files"][0]
    assert first["name"] == "f.xlsx"
    raw_a = _find(first["direct"], "raw_a")
    raw_b = _find(first["direct"], "raw_b")
    assert raw_a["kind"] == "materialize"
    assert raw_a["via"] == "bbbbbbbb"
    assert raw_a["rows"] == 10
    assert raw_b["via"] == "aaaaaaaa"
    assert raw_b["sheet"] == "S2"


def test_inputs_derived_chain_walks_reads_to_file() -> None:
    """派生表沿 reads 追到文件，几层就排几层。"""
    view = _inputs_view()
    derived = view["files"][0]["derived"]
    assert [row["name"] for row in derived] == ["a", "b", "z", "ab"]
    assert _find(derived, "ab")["depth"] == 2
    assert _find(derived, "ab")["from"] == ["a", "b"]
    assert _find(derived, "z")["via"] == "build_z"


def test_inputs_source_columns_mean_import_and_out_of_batch_file_appended() -> None:
    """表内记了来源列的表算导入，批次外的文件补在最后。"""
    view = _inputs_view()
    second = view["files"][1]
    assert second["name"] == "g.xls"
    assert second["inBatch"] is False
    assert second["direct"][0]["kind"] == "import"
    assert second["direct"][0]["sheet"] == "SHEET1"


def test_inputs_orphans_and_usage_flags() -> None:
    """追不到来源的表单独列出来，当前流程的读写标在表上。"""
    view = _inputs_view()
    assert [row["name"] for row in view["orphans"]] == ["stray"]
    assert view["orphans"][0]["kind"] == "unknown"
    assert view["readTables"] == 1
    assert view["writeTables"] == 1
    assert _find(view["files"][0]["direct"], "raw_a")["usage"] == "read"
    assert _find(view["files"][0]["derived"], "z")["usage"] == "write"
    assert view["dbTables"] == 8


def test_inputs_empty_database_still_renders_a_page() -> None:
    """空库也要能出一页，不抛错。"""
    empty = build_inputs(
        {"batch": None, "dbPath": None, "tables": [], "runs": [], "reads": [], "writes": []}
    )
    assert empty["files"] == []
    assert empty["orphans"] == []
    assert empty["dbTables"] == 0
    assert empty["batchId"] is None


# ---------------------------------------------------------------------------
# build_diagram：分层、连线、点亮、loose
# ---------------------------------------------------------------------------

_DIAGRAM_TABLES = [
    {"name": "raw_a", "rows": 10, "columns": 3, "sources": []},
    {"name": "a", "rows": 10, "columns": 2, "sources": []},
    {"name": "ab", "rows": 5, "columns": 2, "sources": []},
    {"name": "imp", "rows": 3, "columns": 4, "sources": [{"file": "g.xls", "sheet": "SHEET1", "rows": 3}]},
    {"name": "stray", "rows": 1, "columns": 1, "sources": []},
]

_DIAGRAM_RUN = {
    "id": "cccccccc-3333",
    "startedAt": "2026-11-01T00:00:00.000Z",
    "doc": {
        "rounds": {
            "a": {
                "materialize": {
                    "entries": [
                        {
                            "entry": {"file": "f.xlsx", "sheet": "S1", "table": "raw_a"},
                            "digest": {"rows": 10},
                        }
                    ]
                },
                "nodes": [{"id": "build_a", "elapsedMs": 1200}],
            }
        },
        "graph": {
            "nodes": [
                {"id": "build_a", "call": "p.py:build_a", "reads": ["raw_a"], "writes": ["a"]},
                {"id": "build_ab", "call": "p.py:build_ab", "reads": ["a"], "writes": ["ab"]},
            ]
        },
    },
}

_DIAGRAM_BATCH = {
    "batchId": "b1",
    "asOf": "2026-10-23",
    "capturedAt": None,
    "files": [{"name": "f.xlsx", "bytes": 2048, "sha256": "s"}],
}


def _diagram_case() -> tuple[dict[str, object], dict[str, object] | None]:
    """构造输入视图与对应的依赖图，供多段用例复用。"""
    inputs = build_inputs(
        {
            "batch": _DIAGRAM_BATCH,
            "dbPath": "hr.db",
            "tables": _DIAGRAM_TABLES,
            "runs": [_DIAGRAM_RUN],
            "reads": ["raw_a"],
            "writes": ["a", "ab"],
        }
    )
    return inputs, build_diagram({"inputs": inputs, "graph": None})


def _node_by_label(diagram: dict[str, object], label: str) -> dict[str, object]:
    """按显示标签找节点；标签与 ref 在本组用例里一致。"""
    return next(node for node in diagram["nodes"] if node["label"] == label)


def _node_id(diagram: dict[str, object], label: str) -> str:
    return str(_node_by_label(diagram, label)["id"])


def _has_edge(diagram: dict[str, object], source: str, target: str) -> bool:
    return any(
        edge["from"] == _node_id(diagram, source) and edge["to"] == _node_id(diagram, target)
        for edge in diagram["edges"]
    )


def test_diagram_contains_file_sheet_table_compute_and_increasing_rank() -> None:
    """文件、sheet、表、计算节点都在，rank 按依赖深度递增。"""
    _, diagram = _diagram_case()
    assert diagram is not None
    kinds = {node["kind"] for node in diagram["nodes"]}
    assert sorted(kinds) == ["compute", "file", "sheet", "table"]
    assert _node_by_label(diagram, "f.xlsx")["rank"] == 0
    assert _node_by_label(diagram, "S1")["rank"] == 1
    assert _node_by_label(diagram, "raw_a")["rank"] == 2
    assert _node_by_label(diagram, "build_a")["rank"] == 3
    assert _node_by_label(diagram, "a")["rank"] == 4
    assert _node_by_label(diagram, "ab")["rank"] == 6


def test_diagram_edges_chain_file_sheet_table_node_table() -> None:
    """连线把来路和计算串起来：文件 → sheet → 表 → 节点 → 表。"""
    _, diagram = _diagram_case()
    assert diagram is not None
    assert _has_edge(diagram, "f.xlsx", "S1") is True
    assert _has_edge(diagram, "S1", "raw_a") is True
    assert _has_edge(diagram, "raw_a", "build_a") is True
    assert _has_edge(diagram, "build_a", "a") is True
    assert _has_edge(diagram, "a", "build_ab") is True
    assert _has_edge(diagram, "build_ab", "ab") is True


def test_diagram_lights_up_tables_touched_by_this_run() -> None:
    """本次运行碰过的表和它的上游点亮，别的灰着。"""
    _, diagram = _diagram_case()
    assert diagram is not None
    assert _node_by_label(diagram, "raw_a")["run"] is True
    assert _node_by_label(diagram, "ab")["run"] is True
    assert _node_by_label(diagram, "imp")["run"] is False
    assert _node_by_label(diagram, "f.xlsx")["run"] is True
    assert _node_by_label(diagram, "S1")["run"] is True
    edge = next(
        item
        for item in diagram["edges"]
        if item["from"] == _node_id(diagram, "S1") and item["to"] == _node_id(diagram, "raw_a")
    )
    assert edge["run"] is True
    edge = next(
        item
        for item in diagram["edges"]
        if item["from"] == _node_id(diagram, "SHEET1") and item["to"] == _node_id(diagram, "imp")
    )
    assert edge["run"] is False


def test_diagram_loose_table_sits_in_last_column() -> None:
    """没有来路的表排在最后一列并标 loose。"""
    _, diagram = _diagram_case()
    assert diagram is not None
    stray = _node_by_label(diagram, "stray")
    assert stray["loose"] is True
    assert stray["rank"] == diagram["rankCount"] - 1


def test_diagram_compute_node_lit_when_written_in_this_run() -> None:
    """写它的节点在本次运行里时，节点本身也点亮。"""
    inputs, _ = _diagram_case()
    graph = {
        "runId": "cccccccc-3333",
        "runShort": "cccccccc",
        "flow": "f",
        "entry": "e",
        "asOf": None,
        "codeCommit": None,
        "revision": None,
        "identical": True,
        "elapsedMs": 1000,
        "materialize": [],
        "nodes": [
            {
                "id": "build_a",
                "fn": "build_a",
                "file": "p.py",
                "title": "",
                "name": "生成 A 表",
                "reads": ["raw_a"],
                "writes": ["a"],
                "elapsedMs": 1200,
                "changed": [],
                "steps": [],
            }
        ],
        "outputs": ["a"],
        "tables": {},
        "sample": None,
        "warnings": [],
    }
    lit = build_diagram({"inputs": inputs, "graph": graph})
    assert lit is not None
    compute = next(
        node for node in lit["nodes"] if node["kind"] == "compute" and node["label"] == "build_a"
    )
    assert compute["run"] is True
    assert compute["elapsedMs"] == 1200
    assert compute["file"] == "p.py"


# ---------------------------------------------------------------------------
# collect_book：tmp_path 最小 store + 可选真实 store
# ---------------------------------------------------------------------------

PROGRAM_ID = "p1"
SESSION_ID = "s1"
RUN_ID = "c4d807a6-c467-4a1e-9c3f-9f0a1b2c3d4e"
CODE_COMMIT = "92435f0b455b5eb04fd0dd3903018fae876b3556"
REVISION_ID = "ba0583fa-b033-4e16-acd8-abd4e7024aab"
EPISODE_ID = "2026-11-04T08-11-20-122Z_abc"

_COLLECT_RUN_DOC = {
    "kind": "flow",
    "flow": "molex-prep",
    "entry": "site_board.py",
    "codeCommit": CODE_COMMIT,
    "inputRevisionId": REVISION_ID,
    "batchId": "b1",
    "db": "hr.db",
    "identical": True,
    "elapsedMs": 7515,
    "graph": {
        "entry": "site_board.py",
        "warnings": [],
        "nodes": [
            {
                "id": "clean_positions",
                "call": "site_board.py:clean_positions",
                "reads": ["raw_a"],
                "writes": ["raw_a"],
                "title": "清洗可用岗位表",
                "steps": [
                    {"line": 47, "kind": "statement", "of": "Assign", "label": "只保留四列"}
                ],
            },
            {
                "id": "build_site_board",
                "call": "site_board.py:build_site_board",
                "reads": ["raw_a"],
                "writes": ["site_board"],
                "title": "按地点汇总",
                "steps": [
                    {"line": 67, "kind": "statement", "of": "Assign", "label": "空地点归到未标注"}
                ],
            },
        ],
    },
    "rounds": {
        "a": {
            "materialize": {
                "entries": [
                    {
                        "entry": {"file": "f.xlsx", "sheet": "S1", "table": "raw_a"},
                        "produced": True,
                        "digest": {"rows": 2},
                    }
                ]
            },
            "nodes": [{"id": "clean_positions", "elapsedMs": 1332, "changed": ["raw_a"]}],
            "outputs": {"site_board": {"rows": 1}},
        }
    },
}


def _create_tables(path: Path, script: str) -> None:
    """建一个小 sqlite 库；库文件名与 run.json 的 db 字段一致。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(script)


def _write_minimal_store(root: Path) -> None:
    """用 SessionStore 建 control.db，再手写运行目录、批次、对话与作者记录。"""
    store = SessionStore.open(root)
    try:
        with store.db:
            store.db.execute(
                "INSERT INTO programs(id,name,repo_path,main_branch,created_at) VALUES(?,?,?,?,?)",
                (PROGRAM_ID, "molex-prep", str(root / "repo"), "main", "2026-11-01T00:00:00.000Z"),
            )
            store.db.execute(
                "INSERT INTO sessions(id,program_id,name,draft_branch,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    SESSION_ID,
                    PROGRAM_ID,
                    "real-data",
                    "draft/s1",
                    "active",
                    "2026-11-04T08:00:00.000Z",
                    "2026-11-04T08:00:00.000Z",
                ),
            )
            store.db.execute(
                "INSERT INTO data_revisions"
                "(id,program_id,session_id,sha256,path,bytes,parent_id,created_at,note) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    REVISION_ID,
                    PROGRAM_ID,
                    SESSION_ID,
                    "sha256-rev",
                    "hr.db",
                    1024,
                    None,
                    "2026-11-03T00:00:00.000Z",
                    None,
                ),
            )
            store.db.execute(
                "INSERT INTO runs"
                "(id,session_id,program_id,code_commit,input_batch_id,data_revision_id,status,"
                "started_at,finished_at,exit_code,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    SESSION_ID,
                    PROGRAM_ID,
                    CODE_COMMIT,
                    "b1",
                    REVISION_ID,
                    "succeeded",
                    "2026-11-04T08:00:00.000Z",
                    "2026-11-04T08:00:07.515Z",
                    0,
                    None,
                ),
            )
            store.db.execute(
                "INSERT INTO publishes(id,session_id,program_id,commit_sha,at,note) "
                "VALUES(?,?,?,?,?,?)",
                (
                    "pub-1",
                    SESSION_ID,
                    PROGRAM_ID,
                    CODE_COMMIT,
                    "2026-11-04T09:00:00.000Z",
                    None,
                ),
            )
    finally:
        store.close()

    run_dir = root / "sessions" / SESSION_ID / "runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(_COLLECT_RUN_DOC, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # collect.py 依次尝试 <run>/a/<db>、<run>/<db>、<run>/b/<db>。
    _create_tables(
        run_dir / "a" / "hr.db",
        "CREATE TABLE raw_a(id INTEGER, site TEXT);"
        "INSERT INTO raw_a VALUES (1,'A'),(2,'B');"
        "CREATE TABLE site_board(site TEXT, n INTEGER);"
        "INSERT INTO site_board VALUES ('A',1);",
    )

    # 输入页取工作目录里最大的 .db；批次清单放在 work/inputs/<batch>/manifest.json。
    work_dir = root / "sessions" / SESSION_ID / "work"
    _create_tables(
        work_dir / "hr.db",
        "CREATE TABLE raw_a(id INTEGER, site TEXT);"
        "INSERT INTO raw_a VALUES (1,'A'),(2,'B');",
    )
    batch_dir = work_dir / "inputs" / "b1"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "manifest.json").write_text(
        json.dumps(
            {
                "batchId": "b1",
                "asOf": "2026-10-23",
                "capturedAt": "2026-10-30T01:44:23.838Z",
                "files": [{"name": "f.xlsx", "bytes": 200425, "sha256": "s"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    episode_path = root / "sessions" / SESSION_ID / "sessions" / f"{EPISODE_ID}.jsonl"
    episode_path.parent.mkdir(parents=True, exist_ok=True)
    episode_path.write_text(TRANSCRIPT, encoding="utf-8")
    author_dir = root / "sessions" / SESSION_ID / "runs" / "author-1"
    author_dir.mkdir(parents=True, exist_ok=True)
    (author_dir / "author.json").write_text(
        json.dumps(
            {
                "commit": CODE_COMMIT,
                "label": "agent",
                "stats": {"sessionFile": f"sessions/{SESSION_ID}/sessions/{EPISODE_ID}.jsonl"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def minimal_store(tmp_path: Path) -> Path:
    """每个用例独立的最小 store，避免共享状态。"""
    root = tmp_path / "store"
    _write_minimal_store(root)
    return root


def test_collect_book_reads_session_runs_flow_and_transcript(minimal_store: Path) -> None:
    """读到会话、运行与一条可画的流程（真实 store 用例的最小化版本）。"""
    view = collect_book(str(minimal_store))
    assert view["session"] is not None
    assert view["session"]["id"] == SESSION_ID
    assert view["program"] is not None
    assert view["program"]["name"] == "molex-prep"
    assert len(view["runs"]) > 0
    assert len(view["revisions"]) > 0
    assert view["currentRevision"] == REVISION_ID
    assert view["graph"] is not None
    assert len(view["graph"]["nodes"]) > 0
    # 选中的运行总得和它的数据版本对得上。
    assert view["lastRunRevision"] == REVISION_ID
    # 每个节点都带步骤，流程图才有内容可画。
    assert all(len(node["steps"]) > 0 for node in view["graph"]["nodes"])
    assert len(view["transcript"]) > 0
    assert view["focusEpisode"] == EPISODE_ID
    assert view["diagram"] is not None
    # 作者记录把对话关联回了这次运行。
    assert view["runs"][0]["hasTranscript"] is True


def test_collect_book_inputs_trace_real_files_and_tables(minimal_store: Path) -> None:
    """输入视图追到真实文件与库里的表。"""
    view = collect_book(str(minimal_store))
    inputs = view["inputs"]
    assert inputs is not None
    assert inputs["dbPath"] is not None
    assert len(inputs["files"]) > 0
    assert inputs["dbTables"] > 0
    assert inputs["batchId"] is not None
    # 物化出来的表一定挂在某个有 sheet 的行上。
    direct = [row for file in inputs["files"] for row in file["direct"]]
    assert len(direct) > 0
    assert all(row["sheet"] is not None and row["rows"] is not None for row in direct)
    # 取样也得真的取到。
    assert any(row["sample"] is not None for row in direct)


def _real_store_path() -> Path | None:
    """环境变量优先，其次找仓库根目录下的 vibe_excel-store。"""
    configured = os.environ.get("VIBE_EXCEL_BOOK_STORE")
    candidate = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parents[2] / "vibe_excel-store"
    )
    return candidate if (candidate / "control.db").is_file() else None


_REAL_STORE = _real_store_path()
_skip_without_store = pytest.mark.skipif(
    _REAL_STORE is None,
    reason="没有真实 store：设置 VIBE_EXCEL_BOOK_STORE 或放置 vibe_excel-store/control.db",
)


@_skip_without_store
def test_collect_book_real_store_flow_and_transcript() -> None:
    """真实 store：读到会话、运行与一条可画的流程。"""
    assert _REAL_STORE is not None
    view = collect_book(str(_REAL_STORE))
    assert view["session"] is not None
    assert len(view["runs"]) > 0
    assert len(view["revisions"]) > 0
    assert view["graph"] is not None
    assert len(view["graph"]["nodes"]) > 0
    assert all(len(node["steps"]) > 0 for node in view["graph"]["nodes"])
    assert "lastRunRevision" in view
    assert len(view["transcript"]) > 0


@_skip_without_store
def test_collect_book_real_store_inputs_and_samples() -> None:
    """真实 store：输入视图追到真实文件与库里的表。"""
    assert _REAL_STORE is not None
    inputs = collect_book(str(_REAL_STORE))["inputs"]
    assert inputs is not None
    assert inputs["dbPath"] is not None
    assert len(inputs["files"]) > 0
    assert inputs["dbTables"] > 0
    assert inputs["batchId"] is not None
    direct = [row for file in inputs["files"] for row in file["direct"]]
    assert len(direct) > 0
    assert all(row["sheet"] is not None and row["rows"] is not None for row in direct)
    assert any(row["sample"] is not None for row in direct)
