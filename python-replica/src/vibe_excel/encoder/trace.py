from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeGuard

from vibe_excel.util import VibeError, now, require_list, require_object, require_text

from ._json import (
    Number,
    as_number,
    as_record,
    loads_json,
    locale_key,
    optional_number,
    optional_text,
    read_utf8,
    require_integer,
    trim_text,
)
from .claims import ClaimCounts, ClaimState, count_claims
from .events import (
    ExtractedRun,
    MessageCounts,
    StepSummary,
    UsageTotals,
    extract_run,
    sha256_text,
    stable_stringify,
)

TRACE_VERSION: Literal["0.1"] = "0.1"
type OutcomeLabel = Literal["pass", "partial", "fail"]
type FailureCategory = Literal[
    "schema-misread",
    "alias-ambiguity",
    "join-wrong",
    "type-coercion",
    "unit-confusion",
    "filter-rule",
    "tooling-error",
    "incomplete",
    "other",
]
type SourceKind = Literal["author", "verify", "run", "unknown"]

OUTCOME_LABELS: tuple[OutcomeLabel, ...] = ("pass", "partial", "fail")
FAILURE_CATEGORIES: tuple[FailureCategory, ...] = (
    "schema-misread",
    "alias-ambiguity",
    "join-wrong",
    "type-coercion",
    "unit-confusion",
    "filter-rule",
    "tooling-error",
    "incomplete",
    "other",
)


def is_outcome_label(value: str) -> TypeGuard[OutcomeLabel]:
    return value in OUTCOME_LABELS


def is_failure_category(value: str) -> TypeGuard[FailureCategory]:
    return value in FAILURE_CATEGORIES


def _is_source_kind(value: str) -> TypeGuard[SourceKind]:
    return value in ("author", "verify", "run", "unknown")


@dataclass(slots=True)
class TracePrompt:
    file: str
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {"file": self.file, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: object, where: str) -> TracePrompt:
        record = require_object(value, where)
        return cls(
            file=require_text(record.get("file"), f"{where}.file", allow_empty=True),
            sha256=require_text(record.get("sha256"), f"{where}.sha256", allow_empty=True),
        )


@dataclass(slots=True)
class TracePolicy:
    provider: str | None = None
    model: str | None = None
    pi_version: str | None = None
    commit: str | None = None
    prompt: TracePrompt | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.provider is not None:
            result["provider"] = self.provider
        if self.model is not None:
            result["model"] = self.model
        if self.pi_version is not None:
            result["piVersion"] = self.pi_version
        if self.commit is not None:
            result["commit"] = self.commit
        if self.prompt is not None:
            result["prompt"] = self.prompt.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: object, where: str = "policy") -> TracePolicy:
        record = require_object(value, where)
        prompt = record.get("prompt")
        return cls(
            provider=optional_text(record, "provider", where),
            model=optional_text(record, "model", where),
            pi_version=optional_text(record, "piVersion", where),
            commit=optional_text(record, "commit", where),
            prompt=TracePrompt.from_dict(prompt, f"{where}.prompt") if prompt is not None else None,
        )


@dataclass(slots=True)
class TraceArtifact:
    path: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}

    @classmethod
    def from_dict(cls, value: object, where: str = "artifact") -> TraceArtifact:
        record = require_object(value, where)
        return cls(
            path=require_text(record.get("path"), f"{where}.path", allow_empty=True),
            sha256=require_text(record.get("sha256"), f"{where}.sha256", allow_empty=True),
            bytes=require_integer(record.get("bytes"), f"{where}.bytes"),
        )


@dataclass(slots=True)
class TraceEvents:
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, object]:
        return {"sha256": self.sha256, "bytes": self.bytes}

    @classmethod
    def from_dict(cls, value: object, where: str) -> TraceEvents:
        record = require_object(value, where)
        return cls(
            sha256=require_text(record.get("sha256"), f"{where}.sha256", allow_empty=True),
            bytes=require_integer(record.get("bytes"), f"{where}.bytes"),
        )


@dataclass(slots=True)
class TraceVerification:
    stage: str | None = None
    batch_id: str | None = None
    identical: bool | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.stage is not None:
            result["stage"] = self.stage
        if self.batch_id is not None:
            result["batchId"] = self.batch_id
        if self.identical is not None:
            result["identical"] = self.identical
        return result

    @classmethod
    def from_dict(cls, value: object, where: str = "source.verify") -> TraceVerification:
        record = require_object(value, where)
        identical = record.get("identical")
        if identical is not None and not isinstance(identical, bool):
            raise VibeError(f"{where}.identical 必须是布尔值")
        return cls(
            stage=optional_text(record, "stage", where),
            batch_id=optional_text(record, "batchId", where),
            identical=identical,
        )


@dataclass(slots=True)
class TraceSource:
    run_dir: str
    run_dir_name: str
    kind: SourceKind
    events: TraceEvents | None = None
    verify: TraceVerification | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "runDir": self.run_dir,
            "runDirName": self.run_dir_name,
            "kind": self.kind,
        }
        if self.events is not None:
            result["events"] = self.events.to_dict()
        if self.verify is not None:
            result["verify"] = self.verify.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: object, where: str = "source") -> TraceSource:
        record = require_object(value, where)
        kind = require_text(record.get("kind"), f"{where}.kind")
        if not _is_source_kind(kind):
            raise VibeError(f"{where}.kind 非法：{kind}")
        events, verify = record.get("events"), record.get("verify")
        return cls(
            run_dir=require_text(record.get("runDir"), f"{where}.runDir", allow_empty=True),
            run_dir_name=require_text(
                record.get("runDirName"), f"{where}.runDirName", allow_empty=True
            ),
            kind=kind,
            events=TraceEvents.from_dict(events, f"{where}.events") if events is not None else None,
            verify=TraceVerification.from_dict(verify, f"{where}.verify")
            if verify is not None
            else None,
        )


@dataclass(slots=True)
class TraceOutcome:
    label: OutcomeLabel
    score: Number | None = None
    failure_category: FailureCategory | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if not is_outcome_label(self.label):
            raise VibeError(f"outcome.label 只能是 {' / '.join(OUTCOME_LABELS)}")
        if self.score is not None:
            score = as_number(self.score)
            if score is None or not 0 <= score <= 1:
                raise VibeError("outcome.score 必须在 0..1")
        if self.failure_category is not None and not is_failure_category(self.failure_category):
            raise VibeError(f"outcome.failureCategory 只能是 {' / '.join(FAILURE_CATEGORIES)}")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"label": self.label}
        if self.score is not None:
            result["score"] = self.score
        if self.failure_category is not None:
            result["failureCategory"] = self.failure_category
        if self.notes is not None:
            result["notes"] = self.notes
        return result

    @classmethod
    def from_dict(cls, value: object, where: str = "outcome") -> TraceOutcome:
        record = require_object(value, where)
        label = require_text(record.get("label"), f"{where}.label")
        category = optional_text(record, "failureCategory", where)
        if not is_outcome_label(label):
            raise VibeError(f"{where}.label 只能是 {' / '.join(OUTCOME_LABELS)}")
        if category is not None and not is_failure_category(category):
            raise VibeError(f"{where}.failureCategory 非法：{category}")
        return cls(
            label=label,
            score=optional_number(record, "score", where),
            failure_category=category,
            notes=optional_text(record, "notes", where),
        )


@dataclass(slots=True)
class TraceMetrics:
    turns: int
    tool_calls: int
    tool_errors: int
    usage: UsageTotals
    messages: MessageCounts
    bad_lines: int
    elapsed_ms: Number | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "turns": self.turns,
            "toolCalls": self.tool_calls,
            "toolErrors": self.tool_errors,
        }
        if self.elapsed_ms is not None:
            result["elapsedMs"] = self.elapsed_ms
        result.update(
            {
                "usage": self.usage.to_dict(),
                "messages": self.messages.to_dict(),
                "badLines": self.bad_lines,
            }
        )
        return result

    @classmethod
    def from_dict(cls, value: object, where: str = "metrics") -> TraceMetrics:
        record = require_object(value, where)
        return cls(
            turns=require_integer(record.get("turns"), f"{where}.turns"),
            tool_calls=require_integer(record.get("toolCalls"), f"{where}.toolCalls"),
            tool_errors=require_integer(record.get("toolErrors"), f"{where}.toolErrors"),
            usage=UsageTotals.from_dict(record.get("usage"), f"{where}.usage"),
            messages=MessageCounts.from_dict(record.get("messages"), f"{where}.messages"),
            bad_lines=require_integer(record.get("badLines"), f"{where}.badLines"),
            elapsed_ms=optional_number(record, "elapsedMs", where),
        )


@dataclass(slots=True)
class InteractionTrace:
    trace_version: Literal["0.1"]
    id: str
    family: str
    question: str
    recorded_at: str
    policy: TracePolicy
    source: TraceSource
    steps: list[StepSummary]
    claims: list[ClaimState]
    claim_counts: ClaimCounts
    artifacts: list[TraceArtifact]
    metrics: TraceMetrics
    outcome: TraceOutcome

    def to_dict(self) -> dict[str, object]:
        return {
            "traceVersion": self.trace_version,
            "id": self.id,
            "family": self.family,
            "question": self.question,
            "recordedAt": self.recorded_at,
            "policy": self.policy.to_dict(),
            "source": self.source.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "claims": [claim.to_dict() for claim in self.claims],
            "claimCounts": self.claim_counts.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metrics": self.metrics.to_dict(),
            "outcome": self.outcome.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> InteractionTrace:
        record = require_object(value, "trace")
        if record.get("traceVersion") != TRACE_VERSION:
            raise VibeError(f"trace.traceVersion 必须是 {TRACE_VERSION}")
        return cls(
            trace_version=TRACE_VERSION,
            id=require_text(record.get("id"), "trace.id"),
            family=require_text(record.get("family"), "trace.family"),
            question=require_text(record.get("question"), "trace.question", allow_empty=True),
            recorded_at=require_text(
                record.get("recordedAt"), "trace.recordedAt", allow_empty=True
            ),
            policy=TracePolicy.from_dict(record.get("policy")),
            source=TraceSource.from_dict(record.get("source")),
            steps=[
                StepSummary.from_dict(item, f"trace.steps[{index}]")
                for index, item in enumerate(require_list(record.get("steps"), "trace.steps"))
            ],
            claims=[
                ClaimState.from_dict(item, f"trace.claims[{index}]")
                for index, item in enumerate(require_list(record.get("claims"), "trace.claims"))
            ],
            claim_counts=ClaimCounts.from_dict(record.get("claimCounts")),
            artifacts=[
                TraceArtifact.from_dict(item, f"trace.artifacts[{index}]")
                for index, item in enumerate(
                    require_list(record.get("artifacts"), "trace.artifacts")
                )
            ],
            metrics=TraceMetrics.from_dict(record.get("metrics")),
            outcome=TraceOutcome.from_dict(record.get("outcome")),
        )


@dataclass(slots=True)
class BuildTraceOptions:
    run_dir: str | Path
    id: str
    family: str
    question: str
    outcome: TraceOutcome
    claims: list[ClaimState] | None = None
    recorded_at: str | None = None


def _read_optional_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    # A malformed optional descriptor is not evidence of a source kind.
    # I/O errors still propagate instead of being mistaken for missing metadata.
    try:
        return as_record(loads_json(read_utf8(path)))
    except json.JSONDecodeError:
        return None


def _string_field(record: dict[str, object] | None, key: str) -> str | None:
    value = record.get(key) if record is not None else None
    return value if isinstance(value, str) else None


def _describe_source(run_dir: str | Path) -> tuple[TraceSource, TracePolicy, Number | None]:
    directory = Path(run_dir)
    author = _read_optional_json(directory / "author.json")
    verify = _read_optional_json(directory / "verify.json")
    run = _read_optional_json(directory / "run.json")
    policy = TracePolicy()
    for key, attribute in (
        ("provider", "provider"),
        ("model", "model"),
        ("piVersion", "pi_version"),
    ):
        value = _string_field(author, key)
        if value:
            setattr(policy, attribute, value)
    for descriptor in (author, verify, run):
        commit = _string_field(descriptor, "commit")
        if commit is not None:
            if commit:
                policy.commit = commit
            break
    prompt_file = _string_field(author, "promptFile")
    if prompt_file:
        prompt_path = Path(prompt_file)
        prompt_hash = sha256_text(read_utf8(prompt_path)) if prompt_path.exists() else ""
        policy.prompt = TracePrompt(file=prompt_path.name, sha256=prompt_hash)
    source = TraceSource(
        run_dir=str(run_dir),
        run_dir_name=directory.name,
        kind="author"
        if author is not None
        else "verify"
        if verify is not None
        else "run"
        if run is not None
        else "unknown",
    )
    events_path = directory / "events.jsonl"
    if events_path.exists():
        events = events_path.read_bytes()
        source.events = TraceEvents(sha256=hashlib.sha256(events).hexdigest(), bytes=len(events))
    if verify is not None:
        identical = verify.get("identical")
        source.verify = TraceVerification(
            stage=_string_field(verify, "stage") or None,
            batch_id=_string_field(verify, "batchId") or None,
            identical=identical if isinstance(identical, bool) else None,
        )
    elapsed_ms = as_number(author.get("elapsedMs")) if author is not None else None
    return source, policy, elapsed_ms


def _list_artifacts(run_dir: str | Path) -> list[TraceArtifact]:
    artifacts: list[TraceArtifact] = []
    for path in sorted(Path(run_dir).iterdir(), key=lambda entry: locale_key(entry.name)):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        artifacts.append(
            TraceArtifact(path=path.name, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        )
    return artifacts


def build_trace(options: BuildTraceOptions) -> InteractionTrace:
    if not trim_text(options.id):
        raise VibeError("trace.id 不能为空")
    if not trim_text(options.family):
        raise VibeError("trace.family 不能为空")
    directory = Path(options.run_dir)
    if not directory.exists():
        raise VibeError(f"run 目录不存在：{options.run_dir}")
    if not directory.is_dir():
        raise VibeError(f"run 路径不是目录：{options.run_dir}")
    events_path = directory / "events.jsonl"
    extracted = extract_run(events_path) if events_path.exists() else ExtractedRun()
    source, policy, elapsed_ms = _describe_source(options.run_dir)
    claims = copy.deepcopy(options.claims) if options.claims is not None else []
    return InteractionTrace(
        trace_version=TRACE_VERSION,
        id=options.id,
        family=options.family,
        question=options.question,
        recorded_at=options.recorded_at if options.recorded_at is not None else now(),
        policy=policy,
        source=source,
        steps=extracted.steps,
        claims=claims,
        claim_counts=count_claims(claims),
        artifacts=_list_artifacts(options.run_dir),
        metrics=TraceMetrics(
            turns=extracted.turns,
            tool_calls=len(extracted.steps),
            tool_errors=sum(not step.ok for step in extracted.steps),
            elapsed_ms=elapsed_ms,
            usage=extracted.usage,
            messages=extracted.messages,
            bad_lines=extracted.bad_lines,
        ),
        outcome=copy.deepcopy(options.outcome),
    )


def trace_hash(trace: InteractionTrace | dict[str, object]) -> str:
    payload = trace.to_dict() if isinstance(trace, InteractionTrace) else dict(trace)
    payload.pop("recordedAt", None)
    return sha256_text(stable_stringify(payload))
