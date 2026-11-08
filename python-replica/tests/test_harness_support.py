"""harness 支撑层的回归场景（迁移自 TypeScript packages/harness/test）。

覆盖 optimize / workspace-facts / telemetry / agent-pi / ask-user 与 run-flow
的离线、确定性场景。真实模型、真实 pi、网络一律不参与：

- optimize：建议文件形状、按 id 取、版本对齐、两段提示词；
- workspace_facts：探测真实临时文件、缺项不抛错、渲染与 AGENTS.md；
- telemetry：JSONL 事件流、流式增量折叠、时间线与 brief；
- agent_pi：临时 fake pi 脚本驱动 RPC，验证成功 / 提问回写 / 超时；
- sessions：真实 store / workspace / git，exec 用桩，验证 run 双跑与发布守卫。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest

from vibe_excel.kernel import AgentRequest, ExecOutcome
from vibe_excel.optimize import (
    assert_same_flow,
    build_apply_prompt,
    build_review_prompt,
    parse_proposal_file,
    select_proposal,
)
from vibe_excel.plugins import agent_pi as AGENT_PI_PLUGIN
from vibe_excel.plugins.agent_pi import (
    AgentPiConfig,
    AgentPiService,
    resolve_pi_cli,
)
from vibe_excel.plugins.commands import CommandRegistry
from vibe_excel.plugins.sessions import InputConfig, SessionsConfig, SessionsFeature
from vibe_excel.plugins.telemetry import JsonlSink, TelemetryConfig, TelemetryService, brief
from vibe_excel.plugins.workspace import WorkspaceService
from vibe_excel.session import SessionStore
from vibe_excel.util import VibeError
from vibe_excel.workspace_facts import (
    CodeFacts,
    DbFacts,
    FlowFacts,
    InputFile,
    InputsFacts,
    OutFacts,
    PackageState,
    ProbeDeps,
    PythonFacts,
    TableState,
    UvFacts,
    VenvFacts,
    WorkspaceFacts,
    fmt_bytes,
    probe_workspace,
    render_workspace_facts,
    write_agent_facts_file,
)

# ---------------------------------------------------------------------------
# optimize
# ---------------------------------------------------------------------------

VALID_PROPOSAL = {
    "id": "p1",
    "title": "把 seed 并进 build_report",
    "kind": "fold-nodes",
    "nodes": ["seed", "build_report"],
    "saves": {"nodes": 1, "tables": 1},
    "risk": "如果以后要单独看 people 表，就不能合",
    "verify": "合并后 report 的行数与内容哈希不变",
}


def _proposal_text(overrides: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {
        "flowDigest": "abc123def456",
        "proposals": [dict(VALID_PROPOSAL)],
    }
    if overrides:
        payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _proposal_file(overrides: dict[str, object] | None = None):
    return parse_proposal_file(_proposal_text(overrides))


def test_parse_trims_fields_and_defaults_saves_to_zero() -> None:
    """正常文件：字段被 trim，缺省的 saves 落到 0。"""

    parsed = parse_proposal_file(
        json.dumps(
            {
                "flowDigest": " abc123 ",
                "proposals": [
                    {
                        "id": " p1 ",
                        "title": " 合并 ",
                        "kind": "fold-nodes",
                        "nodes": [" a ", ""],
                        "risk": " 有风险 ",
                        "verify": " 对哈希 ",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    assert parsed.flow_digest == "abc123"
    proposal = parsed.proposals[0]
    assert proposal.id == "p1"
    assert proposal.title == "合并"
    assert proposal.nodes == ["a"]
    assert proposal.saves.nodes == 0
    assert proposal.saves.tables == 0
    assert proposal.risk == "有风险"
    assert proposal.verify == "对哈希"


def test_parse_rejects_whole_file_when_shape_is_wrong() -> None:
    """非法 JSON / 不是对象 / proposals 为空都整份拒绝。"""

    with pytest.raises(VibeError, match="不是合法 JSON"):
        parse_proposal_file("{oops")
    with pytest.raises(VibeError, match="应该是一个对象"):
        parse_proposal_file("[]")
    with pytest.raises(VibeError, match="空的"):
        parse_proposal_file(json.dumps({"flowDigest": "a", "proposals": []}))


def test_parse_requires_risk_and_verify() -> None:
    """缺 risk 或 verify 的建议不接受：不能让人闭眼批准。"""

    with pytest.raises(VibeError, match="risk 不能为空"):
        _proposal_file({"proposals": [{**VALID_PROPOSAL, "risk": "   "}]})
    with pytest.raises(VibeError, match="verify 不能为空"):
        _proposal_file({"proposals": [{**VALID_PROPOSAL, "verify": ""}]})


def test_parse_rejects_duplicate_id_unknown_kind_and_negative_saves() -> None:
    with pytest.raises(VibeError, match="重复"):
        _proposal_file({"proposals": [dict(VALID_PROPOSAL), dict(VALID_PROPOSAL)]})
    with pytest.raises(VibeError, match="kind 不认识"):
        _proposal_file({"proposals": [{**VALID_PROPOSAL, "kind": "magic"}]})
    with pytest.raises(VibeError, match="非负整数"):
        _proposal_file({"proposals": [{**VALID_PROPOSAL, "saves": {"nodes": -1, "tables": 0}}]})


def test_parse_rejects_missing_title_and_bad_nodes() -> None:
    """缺字段与非字符串数组同样整份拒绝。"""

    proposal = dict(VALID_PROPOSAL)
    proposal.pop("title")
    with pytest.raises(VibeError, match=r"title 应该是字符串"):
        _proposal_file({"proposals": [proposal]})
    with pytest.raises(VibeError, match="字符串数组"):
        _proposal_file({"proposals": [{**VALID_PROPOSAL, "nodes": "seed"}]})


def test_select_proposal_by_id_and_lists_available_ids() -> None:
    selected = select_proposal(_proposal_file(), "p1")
    assert selected.title == "把 seed 并进 build_report"
    with pytest.raises(VibeError, match="没有这条建议：p9"):
        select_proposal(_proposal_file(), "p9")
    with pytest.raises(VibeError, match="p1"):
        select_proposal(_proposal_file(), "p9")


def test_assert_same_flow_reports_both_digests() -> None:
    assert_same_flow(_proposal_file(), "abc123def456")
    with pytest.raises(VibeError, match="abc123def456"):
        assert_same_flow(_proposal_file(), "ffffffffffff")
    with pytest.raises(VibeError, match="ffffffffffff"):
        assert_same_flow(_proposal_file(), "ffffffffffff")


def test_build_review_prompt_is_read_only_and_carries_hints() -> None:
    review = build_review_prompt(
        flow_json='{"flow":"demo"}',
        flow_digest="abc123def456",
        plan_lines=["flow demo@abc123  节点 2"],
        hints=[
            {
                "kind": "single-consumer",
                "nodes": ["seed", "build_report"],
                "tables": ["people"],
                "note": "seed 只喂 build_report",
            }
        ],
        proposal_path="C:/runs/optimize-review-1/optimize-proposal.json",
        code_root="C:/runs/optimize-review-1/clean",
    )
    assert "只读" in review
    assert "C:/runs/optimize-review-1/optimize-proposal.json" in review
    assert "seed 只喂 build_report" in review
    assert '"flowDigest": "abc123def456"' in review
    assert "不要执行 git" in review
    assert "没有**更短且等价" in review
    assert '"proposals": []' in review


def test_build_apply_prompt_only_implements_the_approved_proposal() -> None:
    proposal = _proposal_file().proposals[0]
    apply = build_apply_prompt(
        proposal=proposal,
        approved_at="2026-01-01T00:00:00.000Z",
        code_root="C:/code",
        flow_json='{"flow":"demo"}',
        plan_lines=["flow demo@abc123  节点 2"],
    )
    assert "已被人批准" in apply
    assert "合并后 report 的行数与内容哈希不变" in apply
    assert "除了这一条，不许做别的优化" in apply
    assert "停下来" in apply
    assert "C:/code" in apply
    assert '"id": "p1"' in apply
    assert '"saves"' in apply
    assert "2026-01-01T00:00:00.000Z" in apply


# ---------------------------------------------------------------------------
# workspace_facts
# ---------------------------------------------------------------------------


def _full_workspace(root: Path) -> dict[str, Path]:
    """造一个「什么都齐」的临时工作区；解释器用当前测试解释器（不去建 venv）。"""

    code = root / "code"
    agent = root / "agent"
    work = root / "work"
    inputs = work / "inputs" / "batch-a" / "files"
    out = work / "out"
    for directory in (code, agent, work, inputs, out):
        directory.mkdir(parents=True, exist_ok=True)

    (code / "pipeline.py").write_text("def build(): pass\n", encoding="utf-8")
    (code / "helpers.py").write_text("", encoding="utf-8")
    (code / "flow.json").write_text("{}", encoding="utf-8")
    (inputs / "可用岗位表.xlsx").write_text("x" * 2048, encoding="utf-8")
    (inputs / "所有员工.XLS").write_text("y" * 1024, encoding="utf-8")

    db_path = work / "hr.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            "CREATE TABLE employees(id INTEGER);"
            "INSERT INTO employees VALUES (1),(2),(3);"
            "CREATE TABLE positions(id INTEGER);"
        )
    finally:
        connection.close()

    return {
        "code": code,
        "agent": agent,
        "work": work,
        "inputs": inputs,
        "out": out,
        "db": db_path,
    }


def _deps(root: Path, ws: dict[str, Path], python: str) -> ProbeDeps:
    return ProbeDeps(
        program="molex-prep",
        session_id="s1",
        work=ws["work"],
        code=ws["code"],
        agent=ws["agent"],
        python=python,
        db_path=ws["db"],
        flow_path=ws["code"] / "flow.json",
        inputs_dir=ws["inputs"],
        out_dir=ws["out"],
        as_of="2026-10-23",
    )


def test_probe_workspace_reports_real_files(tmp_path: Path) -> None:
    """探测真实文件：解释器在、库里有表、输入有两个文件、flow 在。"""

    ws = _full_workspace(tmp_path)
    facts = probe_workspace(tmp_path, _deps(tmp_path, ws, sys.executable))

    assert facts.python.exists is True
    assert facts.python.path == sys.executable
    assert facts.python.version is not None
    assert facts.python.venv is not None
    assert facts.python.venv.managed_by in ("uv", "venv", "unknown")
    assert [pkg.name for pkg in facts.python.packages] == ["pandas", "openpyxl", "xlrd"]

    assert facts.db.exists is True
    assert [(table.name, table.rows) for table in facts.db.tables] == [
        ("employees", 3),
        ("positions", 0),
    ]
    assert sorted(file.name for file in facts.inputs.files) == sorted(
        ["可用岗位表.xlsx", "所有员工.XLS"]
    )
    assert facts.out.count == 0
    assert facts.code.py_files == 2
    assert facts.flow.exists is True
    assert facts.program == "molex-prep"
    assert facts.session_id == "s1"
    assert facts.as_of == "2026-10-23"
    assert facts.probe_at[:4].isdigit()


def test_probe_workspace_records_missing_without_raising(tmp_path: Path) -> None:
    """什么都缺的时候不抛异常，而是把「缺」记下来。"""

    root = tmp_path / "empty"
    deps = ProbeDeps(
        program="p",
        session_id="s",
        work=root / "work",
        code=root / "code",
        agent=root / "agent",
        python=str(root / "nope" / "python.exe"),
        db_path=root / "work" / "hr.db",
        flow_path=root / "code" / "flow.json",
        inputs_dir=root / "inputs",
        out_dir=root / "out",
        as_of="2026-10-23",
    )
    facts = probe_workspace(root, deps)

    assert facts.python.exists is False
    assert facts.python.venv is None
    assert facts.db.exists is False
    assert facts.db.tables == []
    assert facts.inputs.files == []
    assert facts.inputs.exists is False
    assert facts.flow.exists is False
    assert facts.code.py_files == 0


def _render_base() -> WorkspaceFacts:
    return WorkspaceFacts(
        probe_at="2026-11-04 14:52",
        program="molex-prep",
        session_id="23e16fce",
        as_of="2026-10-23",
        root="C:/store/sessions/23e16fce",
        code=CodeFacts(path="C:/store/sessions/23e16fce/code", py_files=3),
        python=PythonFacts(
            path="C:/store/runtime-env/.venv/Scripts/python.exe",
            exists=True,
            version="3.13.7",
            venv=VenvFacts(dir="C:/store/runtime-env/.venv", managed_by="uv"),
            packages=[
                PackageState(name="pandas", version="2.2.3"),
                PackageState(name="openpyxl", version="3.1.5"),
                PackageState(name="xlrd", version=None),
            ],
        ),
        uv=UvFacts(path="C:/Users/x/.local/bin/uv.exe", version="0.12.9"),
        db=DbFacts(
            path="C:/store/work/hr.db",
            exists=True,
            bytes=74 * 1024 * 1024,
            mtime="2026-11-03 16:20",
            tables=[
                TableState(name="employees", rows=117777),
                TableState(name="positions", rows=6337),
            ],
        ),
        inputs=InputsFacts(
            dir="C:/store/inputs/batch-a/files",
            exists=True,
            files=[InputFile(name="所有员工.XLS", bytes=66_060_288)],
        ),
        out=OutFacts(dir="C:/store/work/out", exists=True, count=0),
        flow=FlowFacts(path="C:/store/code/flow.json", exists=True),
    )


def test_render_workspace_facts_includes_paths_and_next_steps() -> None:
    text = render_workspace_facts(_render_base())
    assert "vibectl 探测于 2026-11-04 14:52" in text
    assert "C:/store/work/hr.db" in text
    assert "74.0 MB" in text
    assert "2 张表" in text
    assert "uv 建的 venv" in text
    assert "3.13.7" in text
    assert "employees(117777)" in text
    assert "所有员工.XLS (63.0 MB)" in text
    assert "输出目录" in text
    assert "| 空 |" in text
    assert "### 缺的东西（照这个做，别猜）" in text
    assert "`uv pip install xlrd`" in text


def test_render_workspace_facts_missing_items_become_next_steps() -> None:
    base = _render_base()
    empty = replace(
        base,
        db=replace(base.db, exists=False, bytes=0, mtime=None, tables=[]),
        flow=replace(base.flow, exists=False),
        inputs=replace(base.inputs, files=[]),
    )
    text = render_workspace_facts(empty)
    assert "业务库还不存在" in text
    assert "vibectl setup" in text
    assert "流程声明还不存在" in text
    assert "输入批次是空的" in text
    assert "**不存在**" in text


def test_write_agent_facts_file_targets_agents_markdown(tmp_path: Path) -> None:
    ws = _full_workspace(tmp_path)
    facts = probe_workspace(tmp_path, _deps(tmp_path, ws, sys.executable))
    path = write_agent_facts_file(ws["agent"], facts)

    assert path == ws["agent"] / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    assert "工作区事实" in text
    assert "不要再去找" in text
    assert "2026-10-23" in text


def test_fmt_bytes_boundaries() -> None:
    assert fmt_bytes(0) == "0 B"
    assert fmt_bytes(-5) == "0 B"
    assert fmt_bytes(999) == "999 B"
    assert fmt_bytes(1024) == "1.0 KB"
    assert fmt_bytes(2048) == "2.0 KB"
    assert fmt_bytes(66_060_288) == "63.0 MB"
    assert fmt_bytes(5 * 1024**3) == "5.0 GB"


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------


def test_jsonl_sink_folds_stream_deltas_and_writes_timeline(tmp_path: Path) -> None:
    sink = JsonlSink(str(tmp_path / "run"), echo=False)
    sink.event({"type": "message_update", "delta": "a"})
    sink.event({"type": "tool_execution_start", "name": "read", "toolName": "read"})
    sink.event({"type": "message_update", "delta": "b"})
    sink.event({"type": "agent_settled"})

    assert sink.event_count() == 4
    assert sink.type_counts() == {
        "message_update": 2,
        "tool_execution_start": 1,
        "agent_settled": 1,
    }

    timeline = sink.timeline()
    assert "折叠 2 条流式增量" in timeline
    assert "message_update=2" in timeline
    assert "tool=read" in timeline
    assert "agent_settled" in timeline

    sink.close()
    written = (tmp_path / "run" / "timeline.txt").read_text(encoding="utf-8")
    assert written.startswith(timeline)
    events = (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8")
    assert len([line for line in events.splitlines() if line]) == 4
    assert json.loads(events.splitlines()[0])["type"] == "message_update"

    # close 之后再 flush 不重复写 timeline（保持幂等）。
    sink.flush()
    assert (tmp_path / "run" / "timeline.txt").read_text(encoding="utf-8") == written


def test_telemetry_service_begin_closes_all_sinks(tmp_path: Path) -> None:
    service = TelemetryService(TelemetryConfig(False))
    sink = service.begin(str(tmp_path / "run"))
    sink.event({"type": "agent_settled"})
    service.close_all()
    assert (tmp_path / "run" / "timeline.txt").exists()


def test_brief_reads_strings_objects_and_tool_events() -> None:
    assert brief("plain") == "plain"
    assert brief({"toolName": "read", "isError": False, "output": "  many   spaces "}) == (
        "tool=read isError=False «many spaces»"
    )
    tool = brief({"type": "tool_execution_start", "name": "bash", "input": {"cmd": "ls"}})
    assert "tool=bash" in tool
    assert '{"cmd": "ls"}' in tool
    assert "message_update" in brief({"type": "message_update", "delta": "x"})


# ---------------------------------------------------------------------------
# agent_pi
# ---------------------------------------------------------------------------


def _parse_agent_config(raw: object) -> AgentPiConfig:
    validator = AGENT_PI_PLUGIN.config
    assert validator is not None
    parsed = validator(raw)
    assert isinstance(parsed, AgentPiConfig)
    return parsed


def _agent_config(cli: str, *, ask_timeout_ms: int = 5000) -> AgentPiConfig:
    return AgentPiConfig(
        cli=cli,
        provider="stub",
        model="stub",
        api_key_env="OPENCODE_API_KEY",
        timeout_ms=5000,
        ask_timeout_ms=ask_timeout_ms,
    )


def _write_script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return path


FAKE_PI_COMMON = """
import json
import os
import sys


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


def session_dir_of(argv):
    for index, value in enumerate(argv):
        if value == "--session-dir" and index + 1 < len(argv):
            return argv[index + 1]
    return ""


SESSION_DIR = session_dir_of(sys.argv[1:])


def write_session_file():
    if not SESSION_DIR:
        return
    os.makedirs(SESSION_DIR, exist_ok=True)
    with open(os.path.join(SESSION_DIR, "s1.jsonl"), "w", encoding="utf-8") as handle:
        handle.write("{}\\n")
"""

FAKE_PI_OK = FAKE_PI_COMMON + """

for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    kind = message.get("type")
    if kind == "prompt":
        send({"type": "response", "id": message.get("id"), "success": True})
        send({"type": "tool_execution_start", "name": "read", "toolName": "read"})
        send({"type": "message_update", "delta": "..."})
        write_session_file()
        send({"type": "agent_settled"})
    elif kind == "get_session_stats":
        send({
            "type": "response",
            "id": message.get("id"),
            "success": True,
            "data": {"turns": 1},
        })
"""

FAKE_PI_ASK = FAKE_PI_COMMON + """

for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    kind = message.get("type")
    if kind == "prompt":
        send({"type": "response", "id": message.get("id"), "success": True})
        send({
            "type": "extension_ui_request",
            "id": "q-1",
            "method": "select",
            "title": "按哪个日期切？",
            "options": ["入职日", "生效日"],
        })
    elif kind == "extension_ui_response":
        with open("ui-answer.json", "w", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False))
        send({"type": "agent_settled"})
    elif kind == "get_session_stats":
        send({
            "type": "response",
            "id": message.get("id"),
            "success": True,
            "data": {"turns": 1},
        })
"""

FAKE_PI_TIMEOUT = FAKE_PI_COMMON + """

for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    kind = message.get("type")
    if kind == "prompt":
        send({"type": "response", "id": message.get("id"), "success": True})
    elif kind == "get_session_stats":
        send({
            "type": "response",
            "id": message.get("id"),
            "success": True,
            "data": {"turns": 0},
        })
"""


def _agent_request(
    tmp_path: Path,
    prompt: str,
    *,
    timeout_ms: int | None = None,
    on_question=None,
) -> AgentRequest:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(exist_ok=True)
    session_dir = tmp_path / "pi-sessions"
    session_dir.mkdir(exist_ok=True)
    return AgentRequest(
        cwd=str(tmp_path),
        agent_dir=str(agent_dir),
        session_dir=str(session_dir),
        prompt=prompt,
        timeout_ms=timeout_ms,
        on_question=on_question,
    )


def test_agent_config_requires_provider_and_model() -> None:
    with pytest.raises(VibeError, match="provider"):
        _parse_agent_config({})
    with pytest.raises(VibeError, match="model"):
        _parse_agent_config({"provider": "stub"})
    with pytest.raises(VibeError, match="正数"):
        _parse_agent_config({"provider": "stub", "model": "stub", "timeoutMs": 0})


def test_agent_config_defaults_are_explicit() -> None:
    config = _parse_agent_config({"provider": "stub", "model": "stub"})
    assert config.cli == ""
    assert config.api_key_env == "OPENCODE_API_KEY"
    assert config.timeout_ms == 20 * 60 * 1000
    assert config.ask_timeout_ms == 5 * 60 * 1000


def test_resolve_pi_cli_reports_missing_path(tmp_path: Path) -> None:
    with pytest.raises(VibeError, match="不存在"):
        resolve_pi_cli(_agent_config(str(tmp_path / "missing.js")))


def test_export_html_returns_false_when_cli_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIBE_EXCEL_PI", raising=False)
    service = AgentPiService(_agent_config(""))
    assert service.export_html("session.jsonl", str(tmp_path / "out.html")) is False


def test_agent_run_success_collects_events_and_session_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    script = _write_script(tmp_path, "fake_pi_ok.py", FAKE_PI_OK)
    service = AgentPiService(_agent_config(str(script)))

    outcome = service.run(_agent_request(tmp_path, "写一个程序"))

    assert outcome.ok is True
    assert outcome.error is None
    assert outcome.event_count >= 3
    assert outcome.tool_names == ["read"]
    assert outcome.tool_call_count == 1
    assert outcome.session_files == [str(tmp_path / "pi-sessions" / "s1.jsonl")]
    assert outcome.stats == {"turns": 1}


def test_agent_run_writes_ui_answer_back_to_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    script = _write_script(tmp_path, "fake_pi_ask.py", FAKE_PI_ASK)
    service = AgentPiService(_agent_config(str(script)))
    seen = []

    def answer(question):
        seen.append(question)
        return "生效日"

    outcome = service.run(_agent_request(tmp_path, "按哪个日期切？", on_question=answer))

    assert outcome.ok is True
    assert len(seen) == 1
    assert seen[0].id == "q-1"
    assert seen[0].question == "按哪个日期切？"
    assert seen[0].options == ["入职日", "生效日"]
    written = json.loads((tmp_path / "ui-answer.json").read_text(encoding="utf-8"))
    assert written == {"type": "extension_ui_response", "id": "q-1", "value": "生效日"}


def test_agent_run_timeout_becomes_failed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    script = _write_script(tmp_path, "fake_pi_timeout.py", FAKE_PI_TIMEOUT)
    service = AgentPiService(_agent_config(str(script)))

    outcome = service.run(_agent_request(tmp_path, "永远不结束", timeout_ms=100))

    assert outcome.ok is False
    assert outcome.error is not None
    assert "超时" in outcome.error
    # response 不是事件：只有事件类消息才会进 on_event。
    assert outcome.event_count == 0


def test_agent_question_timeout_is_written_back_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_question 超时不能挂住 run：回写 cancelled，让模型按未确认继续。"""

    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    script = _write_script(tmp_path, "fake_pi_ask.py", FAKE_PI_ASK)
    service = AgentPiService(_agent_config(str(script), ask_timeout_ms=100))

    def slow_answer(question):
        time.sleep(0.5)
        return "迟到的答案"

    outcome = service.run(_agent_request(tmp_path, "按哪个日期切？", on_question=slow_answer))

    assert outcome.ok is True
    written = json.loads((tmp_path / "ui-answer.json").read_text(encoding="utf-8"))
    assert written == {"type": "extension_ui_response", "id": "q-1", "cancelled": True}


def test_agent_run_requires_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    script = _write_script(tmp_path, "fake_pi_ok.py", FAKE_PI_OK)
    service = AgentPiService(_agent_config(str(script)))
    with pytest.raises(VibeError, match="缺少凭证"):
        service.run(_agent_request(tmp_path, "写一个程序"))


# ---------------------------------------------------------------------------
# sessions 集成：双跑与发布守卫
# ---------------------------------------------------------------------------


class _StoreAdapter:
    """把 SessionStore 包成 kernel.StoreService（db 是方法，不是属性）。"""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def db(self) -> SessionStore:
        return self._store

    def store_root(self) -> str:
        return str(self._store.store_root)


class _FakeExec:
    """桩 exec：只认 seed / build_report 两个节点，直接写 SQLite。

    `diverge=True` 时让 b 轮的 report 多一行，用来制造「双跑不一致」。
    """

    def __init__(self, diverge: bool = False) -> None:
        self._diverge = diverge

    def interpreter(self) -> str:
        return sys.executable

    def run(self, request):
        arguments = list(request.entry.args)
        db_path = Path(arguments[arguments.index("--db") + 1])
        func = arguments[1] if len(arguments) > 1 else ""
        label = Path(request.run_root).parent.parent.name
        connection = sqlite3.connect(db_path)
        try:
            if func == "seed":
                connection.execute("CREATE TABLE IF NOT EXISTS people (id INTEGER, name TEXT)")
                connection.execute("DELETE FROM people")
                connection.executemany(
                    "INSERT INTO people VALUES (?, ?)", [(1, "ann"), (2, "bob")]
                )
            elif func == "build_report":
                rows = [(1, "ann"), (2, "bob")]
                if self._diverge and label == "b":
                    rows.append((3, "carol"))
                connection.execute("CREATE TABLE IF NOT EXISTS report (id INTEGER, tag TEXT)")
                connection.execute("DELETE FROM report")
                connection.executemany("INSERT INTO report VALUES (?, ?)", rows)
            connection.commit()
        finally:
            connection.close()
        return ExecOutcome(
            exit_code=0,
            timed_out=False,
            stdout=f"{func} ok",
            stderr="",
            manifest_path=str(Path(request.run_root) / "manifest.json"),
            work_dir=str(request.run_root),
            elapsed_ms=1,
        )


class _FakeAgent:
    def run(self, request):
        raise AssertionError("run_flow 不应该调用 agent")

    def export_html(self, session_file: str, out_file: str) -> bool:
        return False


def _flow_json() -> str:
    return json.dumps(
        {
            "flow": "integ",
            "db": "hr.db",
            "materialize": [],
            "outputs": ["report"],
            "nodes": [
                {"id": "seed", "call": "pipeline.py:seed", "reads": [], "writes": ["people"]},
                {
                    "id": "build_report",
                    "call": "pipeline.py:build_report",
                    "reads": ["people"],
                    "writes": ["report"],
                },
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _sessions_fixture(tmp_path: Path, *, diverge: bool = False) -> dict[str, object]:
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "dummy.txt").write_text("placeholder\n", encoding="utf-8")

    store = SessionStore.open(tmp_path / "store")
    store_service = _StoreAdapter(store)
    workspace = WorkspaceService(store_service)
    exec_service = _FakeExec(diverge=diverge)
    telemetry = TelemetryService(TelemetryConfig(False))
    commands = CommandRegistry()
    config = SessionsConfig(
        program="integ",
        db_file="hr.db",
        as_of="2026-01-01",
        out_dir="out",
        flow_path="flow.json",
        ask_enabled=False,
        optimize_timeout_ms=1000,
        inputs=InputConfig(str(inputs_dir), ["dummy.txt"]),
    )
    feature = SessionsFeature(
        config,
        store_service,
        workspace,
        exec_service,
        _FakeAgent(),
        telemetry,
        commands,
    )
    setup = feature.setup()
    paths = workspace.ensure(setup.session_id)
    (paths.code / "pipeline.py").write_text(
        "def seed(db):\n    pass\n\n\ndef build_report(db, as_of):\n    pass\n",
        encoding="utf-8",
    )
    (paths.code / "flow.json").write_text(_flow_json(), encoding="utf-8")
    commit = store.commit_draft(setup.session_id, "integ: flow + pipeline")
    assert commit is not None
    return {
        "store": store,
        "store_service": store_service,
        "workspace": workspace,
        "feature": feature,
        "config": config,
        "session_id": setup.session_id,
        "paths": paths,
    }


def _read_run_json(run_dir: str) -> dict[str, object]:
    return json.loads((Path(run_dir) / "run.json").read_text(encoding="utf-8"))


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


@needs_git
def test_run_flow_twice_identical_promotes_outputs(tmp_path: Path) -> None:
    """两遍一致才提升产物、才登记数据版本。"""

    fixture = _sessions_fixture(tmp_path)
    feature = fixture["feature"]
    paths = fixture["paths"]
    store = fixture["store"]
    try:
        result = feature.run_flow()

        assert result.status == "succeeded"
        assert result.exit_code == 0
        assert result.identical is True
        assert result.out_revision_id is not None
        assert (paths.work / "out").exists()
        assert (paths.work / "hr.db").exists()

        record = _read_run_json(result.run_dir)
        assert record["status"] == "succeeded"
        assert record["identical"] is True
        assert record["outRevisionId"] == result.out_revision_id
        assert record["graph"]["source"] == "json"
        assert [node["id"] for node in record["graph"]["nodes"]] == ["seed", "build_report"]
        assert record["rounds"]["a"]["ok"] is True
        assert record["rounds"]["b"]["ok"] is True

        revisions = store.list_data_revisions(fixture["session_id"])
        assert len(revisions) == 2
    finally:
        store.close()


@needs_git
def test_run_flow_divergence_fails_and_refuses_publish(tmp_path: Path) -> None:
    """两遍不一致就是失败：不提升产物、不登记数据版本、publish 拒绝。"""

    fixture = _sessions_fixture(tmp_path, diverge=True)
    store = fixture["store"]
    try:
        result = fixture["feature"].run_flow()

        assert result.status == "failed"
        assert result.exit_code == 1
        assert result.identical is False
        assert result.out_revision_id is None
        record = _read_run_json(result.run_dir)
        assert record["status"] == "failed"
        assert record["diff"]["identical"] is False

        with pytest.raises(VibeError, match="没有「两次一致」的证据"):
            fixture["feature"].publish()
    finally:
        store.close()


@needs_git
def test_publish_rejects_unverified_commit_after_run(tmp_path: Path) -> None:
    """证据必须属于当前草稿头：通过之后再提交，publish 拒绝。"""

    fixture = _sessions_fixture(tmp_path)
    feature = fixture["feature"]
    paths = fixture["paths"]
    store = fixture["store"]
    try:
        result = feature.run_flow()
        assert result.status == "succeeded"

        (paths.code / "pipeline.py").write_text(
            "def seed(db):\n    pass\n"
            "# 只在通过之后加的一行注释\n"
            "\n\ndef build_report(db, as_of):\n    pass\n",
            encoding="utf-8",
        )
        assert store.commit_draft(fixture["session_id"], "integ: unverified") is not None

        with pytest.raises(VibeError, match="证据已经过期"):
            feature.publish()
    finally:
        store.close()
