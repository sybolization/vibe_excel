from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from vibe_excel.encoder import (
    CLAIM_KINDS,
    CLAIM_STATUSES,
    BuildTraceOptions,
    ClaimCounts,
    ClaimOp,
    ConfirmOp,
    CorpusEntry,
    InteractionTrace,
    RejectOp,
    TraceOutcome,
    append_claim_event,
    build_trace,
    corpus_digest,
    count_claims,
    extract_run,
    fold_claims,
    list_corpus,
    parse_claim_line,
    parse_claims,
    read_trace,
    save_trace,
    serialize_claim_event,
    sha256_text,
    stable_stringify,
    trace_hash,
)
from vibe_excel.encoder.cli import main, run_cli
from vibe_excel.util import VibeError, json_text


@pytest.fixture
def workspace() -> Iterator[Path]:
    root = Path(__file__).resolve().parents[1] / ".test-work"
    directory = root / f"encoder-{uuid.uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)
        if root.exists() and not any(root.iterdir()):
            root.rmdir()


def _base_events() -> list[object]:
    return [
        {"type": "turn_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "bash",
            "args": {"command": 'python -c "print(1)"', "timeout": 60},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "isError": False,
            "durationMs": 120,
            "result": {
                "content": [{"type": "text", "text": "1\n"}],
                "structuredContent": {"exit_code": 0},
            },
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "c2",
            "toolName": "write",
            "args": {"path": r"C:\work\x.py", "content": "print(2)"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "c2",
            "toolName": "write",
            "isError": True,
            "durationMs": 5,
            "result": {"content": [{"type": "text", "text": "permission denied"}]},
        },
        {"type": "turn_start"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "usage": {
                    "input": 10,
                    "output": 5,
                    "cacheRead": 2,
                    "cacheWrite": 0,
                    "reasoning": 0,
                    "totalTokens": 17,
                    "cost": {
                        "input": 0.0004,
                        "output": 0.0006,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "total": 0.001,
                    },
                },
            },
        },
        {"type": "message_end", "message": {"role": "user", "usage": {"input": 99}}},
        {
            "type": "tool_execution_start",
            "toolCallId": "c3",
            "toolName": "bash",
            "args": {"command": "sleep 100"},
        },
    ]


def _write_events(directory: Path, events: list[object]) -> bytes:
    content = ("\n".join(json_text(event) for event in events) + "\n").encode("utf-8")
    (directory / "events.jsonl").write_bytes(content)
    return content


def _run_directory(workspace: Path) -> Path:
    directory = workspace / "run"
    directory.mkdir()
    (directory / "author.json").write_text(
        json_text(
            {
                "provider": "opencode-go",
                "model": "deepseek-v4.1-flash",
                "piVersion": "1.4.0",
                "commit": "abc123",
                "elapsedMs": 1000,
            }
        ),
        encoding="utf-8",
    )
    _write_events(directory, _base_events())
    return directory


def _trace(
    directory: Path, *, case_id: str = "case-1", outcome: TraceOutcome | None = None
) -> InteractionTrace:
    return build_trace(
        BuildTraceOptions(
            run_dir=directory,
            id=case_id,
            family="import",
            question="把表入库",
            outcome=outcome if outcome is not None else TraceOutcome(label="partial"),
            recorded_at="2026-01-01T00:00:00.000Z",
        )
    )


def test_stable_serialization_and_hash() -> None:
    assert (
        stable_stringify({"b": [2, {"z": None, "a": "A→B"}], "a": True})
        == '{"a":true,"b":[2,{"a":"A→B","z":null}]}'
    )
    assert stable_stringify({"\ue000": 1, "\U00010000": 2}) == '{"𐀀":2,"\ue000":1}'
    assert stable_stringify("\ud800") == '"\\ud800"'
    assert stable_stringify("\ud83d\ude00") == '"😀"'
    assert (
        sha256_text("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert sha256_text("\ud800") == hashlib.sha256("�".encode()).hexdigest()
    with pytest.raises(VibeError, match="序列化"):
        stable_stringify(object())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, "1"),
        (-0.0, "0"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (-1e-7, "-1e-7"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (9007199254740993, "9007199254740992"),
        (float("inf"), "null"),
        (float("-inf"), "null"),
        (float("nan"), "null"),
    ],
)
def test_javascript_number_hash_compatibility(value: int | float, expected: str) -> None:
    assert stable_stringify(value) == expected


def test_exact_event_encoding(workspace: Path) -> None:
    _write_events(workspace, _base_events())
    extracted = extract_run(workspace / "events.jsonl")
    command_hash = hashlib.sha256(
        b'{"command":"python -c \\"print(1)\\"","timeout":60}'
    ).hexdigest()
    write_hash = hashlib.sha256(b'{"content":"print(2)","path":"C:\\\\work\\\\x.py"}').hexdigest()
    sleep_hash = hashlib.sha256(b'{"command":"sleep 100"}').hexdigest()
    assert extracted.to_dict() == {
        "steps": [
            {
                "seq": 0,
                "tool": "bash",
                "ok": True,
                "argsSha256": command_hash,
                "argsPreview": 'python -c "print(1)"',
                "files": [],
                "exitCode": 0,
                "durationMs": 120,
                "outputPreview": "1",
            },
            {
                "seq": 1,
                "tool": "write",
                "ok": False,
                "argsSha256": write_hash,
                "argsPreview": r"C:\work\x.py",
                "files": [r"C:\work\x.py"],
                "durationMs": 5,
                "outputPreview": "permission denied",
            },
            {
                "seq": 2,
                "tool": "bash",
                "ok": False,
                "argsSha256": sleep_hash,
                "argsPreview": "sleep 100",
                "files": [],
                "outputPreview": "(未完成：toolCallId=c3 没有 end 事件，可能是取消或崩溃)",
            },
        ],
        "turns": 2,
        "messages": {"assistant": 1, "user": 1, "toolResults": 0},
        "usage": {
            "input": 10,
            "output": 5,
            "cacheRead": 2,
            "cacheWrite": 0,
            "reasoning": 0,
            "totalTokens": 17,
            "costUsd": 0.001,
        },
        "toolNames": ["bash", "write"],
        "badLines": 0,
        "eventCount": 9,
    }


def test_bad_lines_do_not_interrupt_extraction(workspace: Path) -> None:
    (workspace / "events.jsonl").write_text(
        '\n{"type":"turn_start"}\nnot json\nnull\n[]\ntrue\n"event"\n{"invalid":NaN}\n{"type":"turn_start"}\n',
        encoding="utf-8",
    )
    extracted = extract_run(workspace / "events.jsonl")
    assert extracted.bad_lines == 6
    assert extracted.turns == 2
    assert extracted.event_count == 2


def test_event_bom_crlf_and_utf8_replacement(workspace: Path) -> None:
    (workspace / "events.jsonl").write_bytes(
        b'\xef\xbb\xbf{"type":"turn_start"}\r\n'
        b'{"type":"tool_execution_end","result":{"content":[{"text":"\xff"}]}}\r\n'
    )
    extracted = extract_run(workspace / "events.jsonl")
    assert extracted.bad_lines == 0
    assert extracted.turns == 1
    assert extracted.steps[0].output_preview == "�"


def test_bare_carriage_return_is_not_a_line_separator(workspace: Path) -> None:
    (workspace / "events.jsonl").write_bytes(b'{"type":"turn_start"}\r{"type":"turn_start"}')
    extracted = extract_run(workspace / "events.jsonl")
    assert extracted.bad_lines == 1
    assert extracted.turns == 0


def test_unmatched_ends_missing_ids_and_duplicate_starts(workspace: Path) -> None:
    _write_events(
        workspace,
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "a",
                "toolName": "read",
                "args": {"path": "old"},
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "a",
                "toolName": "bash",
                "args": {"command": "new"},
            },
            {"type": "tool_execution_start", "toolName": "ignored", "args": None},
            {
                "type": "tool_execution_end",
                "toolCallId": "a",
                "toolName": "wrong",
                "isError": "true",
                "result": {"structuredContent": {"exit_code": 7}},
            },
            {"type": "tool_execution_end", "toolCallId": "a", "toolName": "orphan"},
            {"type": "tool_execution_end", "toolCallId": 7, "isError": True},
        ],
    )
    extracted = extract_run(workspace / "events.jsonl")
    assert len(extracted.steps) == 3
    assert extracted.steps[0].tool == "bash"
    assert extracted.steps[0].args_preview == "new"
    assert extracted.steps[0].ok
    assert extracted.steps[0].exit_code == 7
    assert extracted.steps[1].args_sha256 == ""
    assert extracted.steps[1].args_preview == "(配对失败：没有对应的 start 事件)"
    assert extracted.steps[2].tool == "unknown"
    assert not extracted.steps[2].ok
    assert extracted.tool_names == ["bash", "ignored", "orphan", "read", "unknown"]


def test_numeric_facts_are_not_inferred_or_coerced(workspace: Path) -> None:
    _write_events(
        workspace,
        [
            {
                "type": "tool_execution_end",
                "durationMs": True,
                "result": {"structuredContent": {"exit_code": "0"}},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input": "10",
                        "output": True,
                        "cacheRead": 2,
                        "cacheWrite": 3,
                        "reasoning": 4,
                        "totalTokens": 9,
                        "cost": {"total": 0.0000005},
                    },
                },
            },
            {"type": "message_end", "message": {"role": "user", "usage": {"input": 1000}}},
            {"type": "message_end", "message": {"role": "toolResult"}},
            {"type": "message_end", "message": None},
        ],
    )
    extracted = extract_run(workspace / "events.jsonl")
    assert extracted.steps[0].ok
    assert "exitCode" not in extracted.steps[0].to_dict()
    assert "durationMs" not in extracted.steps[0].to_dict()
    assert extracted.usage.to_dict() == {
        "input": 0,
        "output": 0,
        "cacheRead": 2,
        "cacheWrite": 3,
        "reasoning": 4,
        "totalTokens": 9,
        "costUsd": 0.000001,
    }
    assert extracted.messages.to_dict() == {"assistant": 1, "user": 1, "toolResults": 1}


def test_previews_are_prioritized_collapsed_and_truncated(workspace: Path) -> None:
    long_text = "x" * 230
    _write_events(
        workspace,
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "a",
                "toolName": "bash",
                "args": {"path": "not-command", "command": "\ufeff  echo\n\t hello  "},
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "b",
                "toolName": "read",
                "args": {"path": "file\nname", "pattern": "ignored"},
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "c",
                "toolName": "grep",
                "args": {"pattern": "x\n y"},
            },
            {"type": "tool_execution_start", "toolCallId": "d", "toolName": "other", "args": [1]},
            {
                "type": "tool_execution_end",
                "toolCallId": "a",
                "result": {"content": [{"text": long_text}, None, {"text": 1}]},
            },
        ],
    )
    steps = extract_run(workspace / "events.jsonl").steps
    assert steps[0].args_preview == "echo hello"
    assert steps[0].output_preview == "x" * 220 + "…"
    assert steps[0].files == ["not-command"]
    assert steps[1].args_preview == "file name"
    assert steps[1].files == ["file\nname"]
    assert steps[2].args_preview == "pattern=x y"
    assert steps[3].args_preview == "sha256:" + hashlib.sha256(b"[1]").hexdigest()[:12]


def test_unicode_preview_counts_utf16_units(workspace: Path) -> None:
    _write_events(
        workspace,
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "u",
                "toolName": "bash",
                "args": {"command": "x" * 219 + "😀"},
            },
        ],
    )
    step = extract_run(workspace / "events.jsonl").steps[0]
    assert step.args_preview == "x" * 219 + "\ud83d…"


def test_truncated_surrogate_preview_can_be_saved_and_read(workspace: Path) -> None:
    _write_events(
        workspace,
        [
            {
                "type": "tool_execution_start",
                "toolCallId": "u",
                "toolName": "bash",
                "args": {"command": "x" * 219 + "😀"},
            },
        ],
    )
    trace = _trace(workspace)
    result = save_trace(workspace / "corpus", trace)
    assert b"\\ud83d" in Path(result.path).read_bytes()
    assert (
        read_trace(workspace / "corpus", trace.id).steps[0].args_preview
        == trace.steps[0].args_preview
    )


def test_claim_bom_and_well_formed_surrogate_serialization() -> None:
    text = '{"op":"claim","id":"c1","kind":"alias","subject":"s","value":"v"}'
    assert len(parse_claims("\ufeff" + text + "\r\n")) == 1
    serialized = serialize_claim_event(ClaimOp(id="c1", kind="alias", subject="\ud800", value="v"))
    assert '"subject":"\\ud800"' in serialized
    assert serialized.encode("utf-8")


def test_claims_fold_history_and_repeated_confirmations() -> None:
    events = parse_claims(
        '{"op":"claim","id":"c1","kind":"alias","subject":"部门","value":"HRBP→人力资源业务伙伴","at":"t1"}\n'
        '{"op":"confirm","id":"c1","by":"jinny","note":"对","at":"t2"}\n'
        '{"op":"confirm","id":"c1","by":"jinny","at":"t3"}\n'
        '{"op":"reject","id":"c1","by":"jinny","reason":"改口","at":"t4"}'
    )
    states = fold_claims(events)
    assert len(states) == 1
    assert states[0].source == "agent"
    assert states[0].status == "rejected"
    assert [entry.op for entry in states[0].history] == ["claim", "confirm", "confirm", "reject"]
    assert states[0].history[1].by == "jinny"
    assert states[0].history[1].note == "对"
    assert states[0].history[-1].note == "改口"
    assert states[0].history[-1].at == "t4"


def test_claim_transitions_are_rejected_without_reviving_old_claims() -> None:
    first = ClaimOp(id="c1", kind="unit", subject="金额", value="千元")
    events: list[ClaimOp | ConfirmOp | RejectOp] = [
        first,
        RejectOp(id="c1", by="jinny", note="其实是元"),
    ]
    with pytest.raises(VibeError, match="需重新 claim"):
        fold_claims([*events, ConfirmOp(id="c1", by="jinny")])
    with pytest.raises(VibeError, match="id 重复"):
        fold_claims([*events, first])
    with pytest.raises(VibeError, match="未声明的 id"):
        fold_claims([RejectOp(id="missing", by="x")])
    with pytest.raises(VibeError, match="未声明的 id"):
        fold_claims([ConfirmOp(id="missing", by="x")])
    states = fold_claims([*events, ClaimOp(id="c2", kind="unit", subject="金额", value="元")])
    assert [state.id for state in states] == ["c1", "c2"]
    assert count_claims(states).to_dict() == {
        "total": 2,
        "unconfirmed": 1,
        "confirmed": 0,
        "rejected": 1,
    }


@pytest.mark.parametrize("evidence", ["import_hr.py:88", ["import_hr.py:88"], [], None])
def test_claim_evidence_normalization(evidence: object) -> None:
    event = parse_claim_line(
        json_text(
            {
                "op": "claim",
                "id": "c",
                "kind": "unit",
                "subject": "金额",
                "value": "元",
                "source": None,
                "evidence": evidence,
            }
        ),
        7,
    )
    assert isinstance(event, ClaimOp)
    assert event.source == "agent"
    assert event.evidence == (["import_hr.py:88"] if evidence else [] if evidence == [] else None)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"op": "unknown"},
        {"op": "claim", "id": "c", "kind": "whatever", "subject": "s", "value": "v"},
        {"op": "claim", "id": " ", "kind": "alias", "subject": "s", "value": "v"},
        {"op": "claim", "id": "c", "kind": "alias", "subject": 7, "value": "v"},
        {
            "op": "claim",
            "id": "c",
            "kind": "alias",
            "subject": "s",
            "value": "v",
            "source": "other",
        },
        {"op": "claim", "id": "c", "kind": "alias", "subject": "s", "value": "v", "evidence": [1]},
        {"op": "claim", "id": "c", "kind": "alias", "subject": "s", "value": "v", "evidence": 1},
        {"op": "confirm", "id": "c", "by": " "},
        {"op": "reject", "id": "c", "by": "x", "reason": 1},
        {"op": "confirm", "id": "c", "by": "x", "at": False},
    ],
)
def test_claim_validation_includes_physical_line_number(raw: object) -> None:
    with pytest.raises(VibeError, match="第 3 行"):
        parse_claims("\n\n" + json_text(raw))
    with pytest.raises(VibeError, match="第 5 行.*JSON"):
        parse_claim_line("not json", 5)


def test_claim_closed_enums_and_ordered_serialization() -> None:
    assert "measure-definition" in CLAIM_KINDS
    assert len(set(CLAIM_KINDS)) == len(CLAIM_KINDS)
    assert CLAIM_STATUSES == ("unconfirmed", "confirmed", "rejected")
    claim = ClaimOp(id="c1", kind="alias", subject="部门", value="A→B", evidence=["timeline#12"])
    assert (
        serialize_claim_event(claim)
        == '{"op":"claim","id":"c1","kind":"alias","subject":"部门","value":"A→B","source":"agent","evidence":["timeline#12"]}'
    )
    assert (
        serialize_claim_event(ConfirmOp(id="c1", by="x", note="对", at="t"))
        == '{"op":"confirm","id":"c1","by":"x","note":"对","at":"t"}'
    )
    assert (
        serialize_claim_event(RejectOp(id="c1", by="x", note="", at=""))
        == '{"op":"reject","id":"c1","by":"x"}'
    )
    event = parse_claim_line(
        '{"op":"reject","id":"c1","by":"x","note":"","reason":"unused","at":null}', 1
    )
    assert isinstance(event, RejectOp)
    assert event.note is None


def test_folding_does_not_mutate_event_evidence() -> None:
    event = ClaimOp(id="c1", kind="alias", subject="s", value="v", evidence=["a"])
    states = fold_claims([event])
    states[0].evidence.append("b")
    assert event.evidence == ["a"]


def test_append_only_claim_log_handles_missing_newline_and_refuses_invalid_writes(
    workspace: Path,
) -> None:
    file = workspace / "claims.jsonl"
    first = serialize_claim_event(
        ClaimOp(id="c1", kind="alias", subject="部门", value="A→B")
    ).encode("utf-8")
    file.write_bytes(first)
    append_claim_event(file, ConfirmOp(id="c1", by="jinny"))
    after_confirm = file.read_bytes()
    assert after_confirm.startswith(first + b"\n")
    assert fold_claims(parse_claims(after_confirm.decode()))[0].status == "confirmed"
    append_claim_event(file, RejectOp(id="c1", by="jinny"))
    before_invalid = file.read_bytes()
    with pytest.raises(VibeError, match="需重新 claim"):
        append_claim_event(file, ConfirmOp(id="c1", by="jinny"))
    assert file.read_bytes() == before_invalid
    file.write_bytes(b"not json\r\n")
    with pytest.raises(VibeError, match="第 1 行"):
        append_claim_event(file, ClaimOp(id="c2", kind="unit", subject="s", value="v"))
    assert file.read_bytes() == b"not json\r\n"


def test_exact_trace_schema_and_stable_hash(workspace: Path) -> None:
    directory = _run_directory(workspace)
    trace = _trace(directory)
    events_bytes = (directory / "events.jsonl").read_bytes()
    author_bytes = (directory / "author.json").read_bytes()
    expected: dict[str, object] = {
        "traceVersion": "0.1",
        "id": "case-1",
        "family": "import",
        "question": "把表入库",
        "recordedAt": "2026-01-01T00:00:00.000Z",
        "policy": {
            "provider": "opencode-go",
            "model": "deepseek-v4.1-flash",
            "piVersion": "1.4.0",
            "commit": "abc123",
        },
        "source": {
            "runDir": str(directory),
            "runDirName": "run",
            "kind": "author",
            "events": {
                "sha256": hashlib.sha256(events_bytes).hexdigest(),
                "bytes": len(events_bytes),
            },
        },
        "steps": extract_run(directory / "events.jsonl").to_dict()["steps"],
        "claims": [],
        "claimCounts": {"total": 0, "unconfirmed": 0, "confirmed": 0, "rejected": 0},
        "artifacts": [
            {
                "path": "author.json",
                "sha256": hashlib.sha256(author_bytes).hexdigest(),
                "bytes": len(author_bytes),
            },
            {
                "path": "events.jsonl",
                "sha256": hashlib.sha256(events_bytes).hexdigest(),
                "bytes": len(events_bytes),
            },
        ],
        "metrics": {
            "turns": 2,
            "toolCalls": 3,
            "toolErrors": 2,
            "elapsedMs": 1000,
            "usage": {
                "input": 10,
                "output": 5,
                "cacheRead": 2,
                "cacheWrite": 0,
                "reasoning": 0,
                "totalTokens": 17,
                "costUsd": 0.001,
            },
            "messages": {"assistant": 1, "user": 1, "toolResults": 0},
            "badLines": 0,
        },
        "outcome": {"label": "partial"},
    }
    assert trace.to_dict() == expected
    canonical = dict(expected)
    canonical.pop("recordedAt")
    expected_hash = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert trace_hash(trace) == expected_hash
    assert trace_hash(replace(trace, recorded_at="2026-02-02T00:00:00.000Z")) == expected_hash
    assert InteractionTrace.from_dict(expected) == trace


def test_claim_and_outcome_are_snapshotted(workspace: Path) -> None:
    claims = fold_claims(
        [ClaimOp(id="c1", kind="unit", subject="金额", value="元", evidence=["file:2"])]
    )
    outcome = TraceOutcome(
        label="fail", score=0, failure_category="unit-confusion", notes="外部核对"
    )
    trace = build_trace(
        BuildTraceOptions(
            run_dir=workspace,
            id="case",
            family="import",
            question="q",
            outcome=outcome,
            claims=claims,
        )
    )
    before = trace_hash(trace)
    claims[0].evidence.append("later")
    claims[0].value = "千元"
    outcome.label = "pass"
    assert trace.claims[0].value == "元"
    assert trace.claims[0].evidence == ["file:2"]
    assert trace.outcome.label == "fail"
    assert trace.claim_counts.unconfirmed == 1
    assert trace_hash(trace) == before


def test_verify_without_events_records_facts_not_a_success_label(workspace: Path) -> None:
    (workspace / "verify.json").write_text(
        '{"stage":"import","commit":"def456","batchId":"b1","identical":true}', encoding="utf-8"
    )
    nested = workspace / "a"
    nested.mkdir()
    (nested / "business.db").write_bytes(b"not a corpus artifact")
    trace = _trace(workspace, outcome=TraceOutcome(label="fail"))
    assert trace.steps == []
    assert trace.metrics.tool_calls == 0
    assert trace.source.kind == "verify"
    assert trace.source.verify is not None
    assert trace.source.verify.to_dict() == {"stage": "import", "batchId": "b1", "identical": True}
    assert trace.policy.commit == "def456"
    assert [artifact.path for artifact in trace.artifacts] == ["verify.json"]
    assert "events" not in trace.source.to_dict()
    assert "elapsedMs" not in trace.metrics.to_dict()
    assert trace.outcome.label == "fail"


def test_source_priority_empty_objects_and_nullable_commit(workspace: Path) -> None:
    (workspace / "author.json").write_text(
        '{"commit":"","elapsedMs":false,"provider":1,"model":""}', encoding="utf-8"
    )
    (workspace / "verify.json").write_text(
        '{"commit":"verify","identical":false,"stage":""}', encoding="utf-8"
    )
    (workspace / "run.json").write_text('{"commit":"run"}', encoding="utf-8")
    trace = _trace(workspace)
    assert trace.source.kind == "author"
    assert trace.policy.to_dict() == {}
    assert trace.source.verify is not None
    assert trace.source.verify.to_dict() == {"identical": False}
    assert trace.metrics.elapsed_ms is None
    (workspace / "author.json").write_text("{}", encoding="utf-8")
    assert _trace(workspace).source.kind == "author"
    (workspace / "author.json").write_text("not json", encoding="utf-8")
    assert _trace(workspace).policy.commit == "verify"
    (workspace / "verify.json").write_text("[]", encoding="utf-8")
    assert _trace(workspace).source.kind == "run"
    (workspace / "run.json").write_text("null", encoding="utf-8")
    assert _trace(workspace).source.kind == "unknown"


def test_optional_descriptor_io_errors_are_not_silenced(workspace: Path) -> None:
    (workspace / "author.json").mkdir()
    with pytest.raises(OSError):
        _trace(workspace)


def test_prompt_policy_uses_current_utf8_content_and_basename(workspace: Path) -> None:
    prompt = workspace / "prompt.md"
    content = "第一行\r\n第二行 😀\r\n".encode()
    prompt.write_bytes(content)
    (workspace / "author.json").write_text(json_text({"promptFile": str(prompt)}), encoding="utf-8")
    trace = _trace(workspace)
    assert trace.policy.prompt is not None
    assert trace.policy.prompt.to_dict() == {
        "file": "prompt.md",
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    prompt.write_text("changed", encoding="utf-8")
    assert trace_hash(_trace(workspace)) != trace_hash(trace)
    prompt.unlink()
    missing_prompt = _trace(workspace).policy.prompt
    assert missing_prompt is not None
    assert missing_prompt.sha256 == ""


def test_trace_validation_and_missing_run_directory(workspace: Path) -> None:
    with pytest.raises(VibeError, match="trace.id"):
        _trace(workspace, case_id=" ")
    with pytest.raises(VibeError, match="trace.family"):
        build_trace(
            BuildTraceOptions(
                run_dir=workspace,
                id="case",
                family=" ",
                question="q",
                outcome=TraceOutcome(label="pass"),
            )
        )
    with pytest.raises(VibeError, match="不存在"):
        _trace(workspace / "missing")
    file = workspace / "file"
    file.write_text("file", encoding="utf-8")
    with pytest.raises(VibeError, match="不是目录"):
        _trace(file)


@pytest.mark.parametrize("score", [-1, 1.1, float("inf"), float("nan"), True])
def test_outcome_score_requires_an_external_finite_number(score: int | float) -> None:
    with pytest.raises(VibeError, match="0..1"):
        TraceOutcome(label="partial", score=score)


def test_corpus_is_immutable_and_idempotent_ignoring_record_time(workspace: Path) -> None:
    trace = _trace(_run_directory(workspace))
    corpus = workspace / "corpus"
    added = save_trace(corpus, trace)
    assert added.status == "added"
    path = Path(added.path)
    initial_trace = path.read_bytes()
    initial_index = (corpus / "index.jsonl").read_bytes()
    unchanged = save_trace(corpus, replace(trace, recorded_at="later"))
    assert unchanged.status == "unchanged"
    assert unchanged.hash == added.hash
    assert path.read_bytes() == initial_trace
    assert (corpus / "index.jsonl").read_bytes() == initial_index
    assert read_trace(corpus, trace.id) == trace
    with pytest.raises(VibeError, match="不可变"):
        save_trace(
            corpus,
            replace(trace, outcome=TraceOutcome(label="fail", failure_category="schema-misread")),
        )
    assert path.read_bytes() == initial_trace
    assert (corpus / "index.jsonl").read_bytes() == initial_index
    assert b'"traceVersion": "0.1"' in initial_trace


@pytest.mark.parametrize(
    "case_id", ["../evil", r"..\evil", "/absolute", ".", "_x", "-x", "a/b", r"a\b", "中文"]
)
def test_corpus_id_rejects_path_symbols_before_writing(workspace: Path, case_id: str) -> None:
    with pytest.raises(VibeError, match="只允许字母数字"):
        save_trace(workspace / "corpus", _trace(workspace, case_id=case_id))
    assert not (workspace / "corpus").exists()


def test_existing_unknown_trace_fields_cannot_be_silently_overwritten(workspace: Path) -> None:
    trace = _trace(workspace)
    result = save_trace(workspace / "corpus", trace)
    changed = trace.to_dict()
    changed["extra"] = "must affect the hash"
    Path(result.path).write_text(json_text(changed), encoding="utf-8")
    with pytest.raises(VibeError, match="不可变"):
        save_trace(workspace / "corpus", trace)


def test_index_is_append_only_including_missing_terminal_newline(workspace: Path) -> None:
    corpus = workspace / "corpus"
    first = _trace(workspace, case_id="case-1")
    save_trace(corpus, first)
    index = corpus / "index.jsonl"
    original = index.read_bytes().rstrip(b"\n")
    index.write_bytes(original)
    save_trace(corpus, replace(first, id="case-2"))
    assert index.read_bytes().startswith(original + b"\n")
    assert [entry.id for entry in list_corpus(corpus)] == ["case-1", "case-2"]
    with pytest.raises(VibeError, match="案例不存在"):
        read_trace(corpus, "missing")


@pytest.mark.parametrize("bad_line", ["not json", "null", '{"id":"incomplete"}'])
def test_corrupt_index_reports_physical_line_without_partial_results(
    workspace: Path, bad_line: str
) -> None:
    save_trace(workspace / "corpus", _trace(workspace))
    index = workspace / "corpus" / "index.jsonl"
    with index.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n" + bad_line + "\n")
    with pytest.raises(VibeError, match="第 3 行"):
        list_corpus(workspace / "corpus")


def test_digest_depends_only_on_sorted_id_hash_pairs(workspace: Path) -> None:
    entries = [
        CorpusEntry(
            id="a", family="f", hash="01", outcome="partial", claims=ClaimCounts(), recorded_at="t"
        ),
        CorpusEntry(
            id="A", family="f", hash="02", outcome="pass", claims=ClaimCounts(), recorded_at="t"
        ),
        CorpusEntry(
            id="a-b", family="f", hash="03", outcome="fail", claims=ClaimCounts(), recorded_at="t"
        ),
        CorpusEntry(
            id="a_b", family="f", hash="04", outcome="fail", claims=ClaimCounts(), recorded_at="t"
        ),
    ]
    expected = hashlib.sha256(b"a_b:04\na-b:03\na:01\nA:02").hexdigest()
    assert corpus_digest(entries) == expected
    assert corpus_digest(entries[::-1]) == expected
    assert (
        corpus_digest(
            [replace(entry, family="different", recorded_at="later") for entry in entries]
        )
        == expected
    )
    assert corpus_digest([replace(entries[0], hash="changed"), *entries[1:]]) != expected
    assert list_corpus(workspace / "absent") == []
    assert corpus_digest([]) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_cli_claim_lifecycle_repeated_flags_and_rejected_transition(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = workspace / "claims.jsonl"
    assert (
        run_cli(
            [
                "claims",
                "add",
                "--file",
                str(file),
                "--id",
                "old",
                "--id",
                "c1",
                "--kind",
                "alias",
                "--subject",
                "部门",
                "--value",
                "A→B",
                "--evidence",
                "a",
                "--evidence",
                "",
                "--evidence",
                "b",
            ]
        )
        == 0
    )
    assert "c1\tunconfirmed\talias\t部门\tA→B  evidence=a,b" in capsys.readouterr().out
    assert (
        run_cli(
            [
                "claims",
                "confirm",
                "--file",
                str(file),
                "--id",
                "c1",
                "--by",
                "jinny",
                "--note",
                "对",
            ]
        )
        == 0
    )
    assert "\tconfirmed\t" in capsys.readouterr().out
    assert run_cli(["claims", "--file", str(file)]) == 0
    capsys.readouterr()
    assert (
        run_cli(
            [
                "claims",
                "reject",
                "--file",
                str(file),
                "--id",
                "c1",
                "--by",
                "jinny",
                "--reason",
                "wrong",
            ]
        )
        == 0
    )
    assert "\trejected\t" in capsys.readouterr().out
    before = file.read_bytes()
    assert run_cli(["claims", "confirm", "--file", str(file), "--id", "c1", "--by", "jinny"]) == 1
    assert "需重新 claim" in capsys.readouterr().err
    assert file.read_bytes() == before
    state = fold_claims(parse_claims(file.read_text(encoding="utf-8")))[0]
    assert state.history[-1].note == "wrong"


def test_cli_record_list_and_digest(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = _run_directory(workspace)
    corpus = workspace / "corpus"
    file = workspace / "claims.jsonl"
    append_claim_event(file, ClaimOp(id="c1", kind="unit", subject="金额", value="元"))
    command = [
        "record",
        "--run",
        str(directory),
        "--id",
        "cli-case",
        "--family",
        "import",
        "--question",
        "把表入库",
        "--corpus",
        str(corpus),
        "--claims",
        str(file),
        "--outcome",
        "fail",
        "--score",
        "0",
        "--category",
        "schema-misread",
        "--note",
        "外部核对失败",
    ]
    assert run_cli(command) == 0
    assert "（added）" in capsys.readouterr().out
    trace = read_trace(corpus, "cli-case")
    assert trace.outcome.to_dict() == {
        "label": "fail",
        "score": 0,
        "failureCategory": "schema-misread",
        "notes": "外部核对失败",
    }
    assert trace.claim_counts.unconfirmed == 1
    assert run_cli(command) == 0
    assert "（unchanged）" in capsys.readouterr().out
    assert run_cli(["list", "--corpus", str(corpus)]) == 0
    assert "cli-case\timport\tfail\t语义 0/1" in capsys.readouterr().out
    assert run_cli(["digest", "--corpus", str(corpus)]) == 0
    assert capsys.readouterr().out.strip() == corpus_digest(list_corpus(corpus))


def test_cli_default_outcome_is_partial_even_for_identical_verify(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "verify.json").write_text('{"identical":true}', encoding="utf-8")
    corpus = workspace / "corpus"
    assert (
        run_cli(
            [
                "record",
                "--run",
                str(workspace),
                "--id",
                "verify",
                "--family",
                "f",
                "--question",
                "q",
                "--corpus",
                str(corpus),
                "--claims",
                str(workspace / "absent.jsonl"),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert read_trace(corpus, "verify").outcome.to_dict() == {"label": "partial"}


@pytest.mark.parametrize("score", ["-1", "1.1", "nan", "Infinity", "invalid", "1_0"])
def test_cli_invalid_scores_do_not_create_cases(
    workspace: Path, capsys: pytest.CaptureFixture[str], score: str
) -> None:
    corpus = workspace / "corpus"
    assert (
        run_cli(
            [
                "record",
                "--run",
                str(workspace),
                "--id",
                "case",
                "--family",
                "f",
                "--question",
                "q",
                "--corpus",
                str(corpus),
                "--score",
                score,
            ]
        )
        == 1
    )
    assert "--score 必须在 0..1" in capsys.readouterr().err
    assert not corpus.exists()


@pytest.mark.parametrize("score", ["0x1", "0b1", "0o1", " 1 ", " "])
def test_cli_score_number_syntax_matches_original(
    workspace: Path, capsys: pytest.CaptureFixture[str], score: str
) -> None:
    corpus = workspace / "corpus"
    assert (
        run_cli(
            [
                "record",
                "--run",
                str(workspace),
                "--id",
                "case",
                "--family",
                "f",
                "--question",
                "q",
                "--corpus",
                str(corpus),
                "--score",
                score,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert read_trace(corpus, "case").outcome.score == (0 if not score.strip() else 1)


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ([], "用法"),
        (["unknown"], "用法"),
        (["record"], "缺少参数 --run"),
        (["list"], "缺少参数 --corpus"),
        (["claims", "show"], "缺少参数 --file"),
        (["claims", "unknown", "--file", "claims.jsonl"], "未知子命令"),
        (["claims", "add", "--file", "claims.jsonl", "--kind", "wrong"], "--kind 只能是"),
        (
            ["claims", "add", "--file", "claims.jsonl", "--kind", "unit", "--source", "wrong"],
            "--source 只能是",
        ),
    ],
)
def test_cli_errors_are_reported_as_exit_codes(
    command: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(command) == 1
    assert message in capsys.readouterr().err


def test_cli_empty_claims_and_corpus(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["claims", "show", "--file", str(workspace / "missing.jsonl")]) == 0
    assert "还没有任何语义断言" in capsys.readouterr().out
    assert run_cli(["list", "--corpus", str(workspace / "missing")]) == 0
    assert "案例库为空" in capsys.readouterr().out


def test_main_returns_none_or_exits_on_error(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["encoder", "digest", "--corpus", str(workspace)])
    assert main() is None
    assert capsys.readouterr().out.strip() == corpus_digest([])
    monkeypatch.setattr(sys, "argv", ["encoder"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "用法" in capsys.readouterr().err


def test_module_cli_is_independent_of_node(workspace: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "vibe_excel.encoder.cli", "digest", "--corpus", str(workspace)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == corpus_digest([])
