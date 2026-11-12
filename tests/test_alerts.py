"""诊断面板的两块数据：告警摊平、失败汇总。

以前这两块都没有用例——`graph` 的结构化告警被静默丢弃，`run.json` 里的
节点级 stderr 页面从来没读过。这里把两头钉住。
"""

from __future__ import annotations

from vibe_excel.book.collect import _alerts
from vibe_excel.book.graph import _graph_warnings


def test_graph_warnings_render_structured_entries() -> None:
    """run.json 存的是 {code,message,line}，不能因为不是字符串就整段丢掉。"""
    warnings = _graph_warnings(
        {
            "entry": "pipeline.py",
            "warnings": [
                {"code": "unused", "message": "带表标注却未被调用", "line": 42},
                {"code": "no-line", "message": "没有行号"},
                "纯字符串告警",
                {"message": "缺 code，跳过"},
                {"code": "缺 message", "line": 1},
                "",
            ],
        }
    )

    assert warnings == [
        "pipeline.py:42 [unused] 带表标注却未被调用",
        "pipeline.py [no-line] 没有行号",
        "纯字符串告警",
    ]


def test_alerts_are_two_levels_and_keep_the_stderr() -> None:
    """节点失败要带 stderr：用户要的是「复制去问 agent」，不是自己去翻终端。"""
    run_doc = {
        "status": "failed",
        "rounds": {
            "a": {
                "nodes": [
                    {"id": "clean_orders", "exitCode": 0},
                    {"id": "build_totals", "exitCode": 3, "stderr": "KeyError: 'amount'"},
                ],
                "materialize": {"exitCode": 0},
                "warnings": ["输出表 totals 未生成"],
            }
        },
    }

    alerts = _alerts(["无法读取页 inputs"], run_doc, "d227ed76-c1f5-4309-ae88-430a3a683372")

    kinds = [(item["level"], item["text"]) for item in alerts]
    # 失败排前面，提示按原顺序跟在后面
    assert kinds == [
        ("error", "运行 d227ed76：节点 build_totals 退出码 3"),
        ("notice", "无法读取页 inputs"),
        ("notice", "输出表 totals 未生成"),
    ]
    failure = alerts[0]
    assert failure["node"] == "build_totals"
    assert failure["detail"] == "KeyError: 'amount'"
    assert failure["runId"] == "d227ed76-c1f5-4309-ae88-430a3a683372"


def test_alerts_say_timeout_and_cancel_in_words() -> None:
    run_doc = {
        "rounds": {
            "a": {
                "nodes": [
                    {"id": "slow", "exitCode": 124, "timedOut": True},
                    {"id": "stopped", "exitCode": 130, "cancelled": True},
                ]
            }
        }
    }

    texts = [item["text"] for item in _alerts([], run_doc, "abcd1234")]

    assert texts == [
        "运行 abcd1234：节点 slow 超时",
        "运行 abcd1234：节点 stopped 被取消",
    ]


def test_alerts_flag_a_failure_without_node_codes() -> None:
    """老记录没有 exitCode：说清「失败了但不知道在哪」，不要瞎编节点。"""
    alerts = _alerts([], {"status": "failed"}, "oldrun00")

    assert [(item["level"], item["text"]) for item in alerts] == [
        ("error", "运行 oldrun00 失败，记录里没有节点级的退出码")
    ]


def test_alerts_do_not_repeat_and_keep_the_whole_stderr() -> None:
    long_error = "x" * 5000
    run_doc = {
        "rounds": {
            "a": {
                "nodes": [{"id": "n", "exitCode": 1, "stderr": long_error}],
                "warnings": ["同一句", "同一句"],
            },
            "b": {
                "nodes": [{"id": "n", "exitCode": 1, "stderr": "第二轮的同一句错"}],
            },
        }
    }

    alerts = _alerts(["同一句"], run_doc, "r1")

    texts = [item["text"] for item in alerts]
    assert texts.count("同一句") == 1, texts
    # a、b 两轮同一个节点都失败：只报一条，不按轮次重复
    assert texts.count("运行 r1：节点 n 退出码 1") == 1, texts
    # stderr 原样保留：人要能整段复制给 agent
    assert alerts[0]["detail"] == long_error

def test_a_brand_new_project_has_no_alerts() -> None:
    """没有运行、没有文件、没有库：这是空状态，不是三条告警。"""
    from vibe_excel.book.collect import _alerts

    assert _alerts(["没有可展示的流程运行；其他账页仍可浏览"], {}, None, untouched=True) == []
    # 只是提到「没运行」还不够：一旦有文件或运行，同样的 warning 要照常显示
    assert _alerts(["没有可展示的流程运行；其他账页仍可浏览"], {}, None, untouched=False) != []
