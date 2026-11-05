from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NoReturn, TypeGuard

from vibe_excel.util import VibeError, require_list, require_object, require_text

from ._json import (
    json_document,
    loads_json,
    optional_text,
    read_utf8,
    require_integer,
    text_list,
    trim_text,
)

type ClaimKind = Literal[
    "alias", "field-meaning", "measure-definition", "join", "filter", "unit", "exception"
]
type ClaimStatus = Literal["unconfirmed", "confirmed", "rejected"]
type ClaimSource = Literal["agent", "human"]
type ClaimOperation = Literal["claim", "confirm", "reject"]

CLAIM_KINDS: tuple[ClaimKind, ...] = (
    "alias",
    "field-meaning",
    "measure-definition",
    "join",
    "filter",
    "unit",
    "exception",
)
CLAIM_STATUSES: tuple[ClaimStatus, ...] = ("unconfirmed", "confirmed", "rejected")


def is_claim_kind(value: str) -> TypeGuard[ClaimKind]:
    return value in CLAIM_KINDS


def is_claim_status(value: str) -> TypeGuard[ClaimStatus]:
    return value in CLAIM_STATUSES


def is_claim_source(value: str) -> TypeGuard[ClaimSource]:
    return value in ("agent", "human")


@dataclass(slots=True)
class ClaimOp:
    id: str
    kind: ClaimKind
    subject: str
    value: str
    source: ClaimSource = "agent"
    evidence: list[str] | None = None
    at: str | None = None
    op: Literal["claim"] = field(default="claim", init=False)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "op": self.op,
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "value": self.value,
            "source": self.source,
        }
        if self.evidence:
            result["evidence"] = list(self.evidence)
        if self.at:
            result["at"] = self.at
        return result


@dataclass(slots=True)
class ConfirmOp:
    id: str
    by: str
    note: str | None = None
    at: str | None = None
    op: Literal["confirm"] = field(default="confirm", init=False)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"op": self.op, "id": self.id, "by": self.by}
        if self.note:
            result["note"] = self.note
        if self.at:
            result["at"] = self.at
        return result


@dataclass(slots=True)
class RejectOp:
    id: str
    by: str
    note: str | None = None
    at: str | None = None
    op: Literal["reject"] = field(default="reject", init=False)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"op": self.op, "id": self.id, "by": self.by}
        if self.note:
            result["note"] = self.note
        if self.at:
            result["at"] = self.at
        return result


type ClaimEvent = ClaimOp | ConfirmOp | RejectOp


@dataclass(slots=True)
class ClaimHistoryEntry:
    op: ClaimOperation
    by: str | None = None
    at: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"op": self.op}
        if self.by is not None:
            result["by"] = self.by
        if self.at:
            result["at"] = self.at
        if self.note:
            result["note"] = self.note
        return result

    @classmethod
    def from_dict(cls, value: object, where: str) -> ClaimHistoryEntry:
        record = require_object(value, where)
        op = record.get("op")
        if op not in ("claim", "confirm", "reject"):
            raise VibeError(f"{where}.op 只能是 claim / confirm / reject")
        if op == "claim":
            return cls(op="claim", at=optional_text(record, "at", where))
        by = require_text(record.get("by"), f"{where}.by")
        return cls(
            op="confirm" if op == "confirm" else "reject",
            by=by,
            at=optional_text(record, "at", where),
            note=optional_text(record, "note", where),
        )


@dataclass(slots=True)
class ClaimState:
    id: str
    kind: ClaimKind
    subject: str
    value: str
    source: ClaimSource
    status: ClaimStatus = "unconfirmed"
    evidence: list[str] = field(default_factory=list)
    history: list[ClaimHistoryEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "value": self.value,
            "source": self.source,
            "status": self.status,
            "evidence": list(self.evidence),
            "history": [entry.to_dict() for entry in self.history],
        }

    @classmethod
    def from_dict(cls, value: object, where: str = "claim") -> ClaimState:
        record = require_object(value, where)
        kind = require_text(record.get("kind"), f"{where}.kind")
        source = require_text(record.get("source"), f"{where}.source")
        status = require_text(record.get("status"), f"{where}.status")
        if not is_claim_kind(kind):
            raise VibeError(f"{where}.kind 非法：{kind}")
        if not is_claim_source(source):
            raise VibeError(f"{where}.source 只能是 agent 或 human")
        if not is_claim_status(status):
            raise VibeError(f"{where}.status 非法：{status}")
        return cls(
            id=require_text(record.get("id"), f"{where}.id"),
            kind=kind,
            subject=require_text(record.get("subject"), f"{where}.subject"),
            value=require_text(record.get("value"), f"{where}.value"),
            source=source,
            status=status,
            evidence=text_list(record.get("evidence"), f"{where}.evidence"),
            history=[
                ClaimHistoryEntry.from_dict(item, f"{where}.history[{index}]")
                for index, item in enumerate(
                    require_list(record.get("history"), f"{where}.history")
                )
            ],
        )


@dataclass(slots=True)
class ClaimCounts:
    total: int = 0
    unconfirmed: int = 0
    confirmed: int = 0
    rejected: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "unconfirmed": self.unconfirmed,
            "confirmed": self.confirmed,
            "rejected": self.rejected,
        }

    @classmethod
    def from_dict(cls, value: object, where: str = "claimCounts") -> ClaimCounts:
        record = require_object(value, where)
        return cls(
            total=require_integer(record.get("total"), f"{where}.total"),
            unconfirmed=require_integer(record.get("unconfirmed"), f"{where}.unconfirmed"),
            confirmed=require_integer(record.get("confirmed"), f"{where}.confirmed"),
            rejected=require_integer(record.get("rejected"), f"{where}.rejected"),
        )


def _line_error(line: int, message: str) -> NoReturn:
    raise VibeError(f"claims 第 {line} 行 {message}")


def _require_string(value: object, line: int, name: str) -> str:
    if not isinstance(value, str) or not trim_text(value):
        _line_error(line, f"的 {name} 必须是非空字符串")
    return value


def _optional_string(value: object, line: int, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        _line_error(line, f"的 {name} 必须是字符串")
    return value or None


def _evidence(value: object, line: int) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        _line_error(line, "的 evidence 必须是字符串或字符串数组")
    evidence: list[str] = []
    for index, item in enumerate(require_list(value, "evidence")):
        if not isinstance(item, str):
            _line_error(line, f"的 evidence[{index}] 必须是字符串")
        evidence.append(item)
    return evidence


def parse_claim_line(text: str, line: int) -> ClaimEvent:
    try:
        value = loads_json(text)
    except json.JSONDecodeError as error:
        raise VibeError(f"claims 第 {line} 行 不是合法 JSON") from error
    if not isinstance(value, dict):
        _line_error(line, "必须是对象")
    record = require_object(value, f"claims 第 {line} 行")
    op = record.get("op")
    if op == "claim":
        kind = _require_string(record.get("kind"), line, "kind")
        if not is_claim_kind(kind):
            _line_error(line, f"的 kind 非法：{kind}")
        source = _require_string(
            record.get("source") if record.get("source") is not None else "agent", line, "source"
        )
        if not is_claim_source(source):
            _line_error(line, "的 source 只能是 agent 或 human")
        return ClaimOp(
            id=_require_string(record.get("id"), line, "id"),
            kind=kind,
            subject=_require_string(record.get("subject"), line, "subject"),
            value=_require_string(record.get("value"), line, "value"),
            source=source,
            evidence=_evidence(record.get("evidence"), line),
            at=_optional_string(record.get("at"), line, "at"),
        )
    if op == "confirm" or op == "reject":
        raw_note = record.get("note")
        note = _optional_string(
            raw_note if raw_note is not None else record.get("reason"),
            line,
            "note" if op == "confirm" else "reason",
        )
        call = ConfirmOp if op == "confirm" else RejectOp
        return call(
            id=_require_string(record.get("id"), line, "id"),
            by=_require_string(record.get("by"), line, "by"),
            note=note,
            at=_optional_string(record.get("at"), line, "at"),
        )
    _line_error(line, f"的 op 只能是 claim / confirm / reject，实际是 {op}")


def parse_claims(text: str) -> list[ClaimEvent]:
    return [
        parse_claim_line(trim_text(line), index)
        for index, line in enumerate(text.split("\n"), start=1)
        if trim_text(line)
    ]


def fold_claims(events: list[ClaimEvent]) -> list[ClaimState]:
    states: dict[str, ClaimState] = {}
    for event in events:
        if isinstance(event, ClaimOp):
            if event.id in states:
                raise VibeError(f"claims 中 id 重复：{event.id}（改口请先 reject 再新增）")
            states[event.id] = ClaimState(
                id=event.id,
                kind=event.kind,
                subject=event.subject,
                value=event.value,
                source=event.source,
                evidence=list(event.evidence) if event.evidence is not None else [],
                history=[ClaimHistoryEntry(op="claim", at=event.at)],
            )
            continue
        state = states.get(event.id)
        if state is None:
            raise VibeError(f"claims 中对未声明的 id 执行 {event.op}：{event.id}")
        if isinstance(event, ConfirmOp):
            if state.status == "rejected":
                raise VibeError(f"id {event.id} 已被 reject，需重新 claim 后才能 confirm")
            state.status = "confirmed"
        else:
            state.status = "rejected"
        state.history.append(
            ClaimHistoryEntry(op=event.op, by=event.by, at=event.at, note=event.note)
        )
    return list(states.values())


def count_claims(states: list[ClaimState]) -> ClaimCounts:
    return ClaimCounts(
        total=len(states),
        unconfirmed=sum(state.status == "unconfirmed" for state in states),
        confirmed=sum(state.status == "confirmed" for state in states),
        rejected=sum(state.status == "rejected" for state in states),
    )


def serialize_claim_event(event: ClaimEvent) -> str:
    return json_document(event.to_dict())


def append_claim_event(file: str | Path, event: ClaimEvent) -> None:
    path = Path(file)
    existing = read_utf8(path) if path.exists() else ""
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    line = f"{prefix}{serialize_claim_event(event)}\n"
    fold_claims(parse_claims(existing + line))
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)
