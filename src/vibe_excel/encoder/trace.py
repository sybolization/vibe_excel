"""轨迹（trace）文档的不可变 schema 与构建逻辑。

数据来源是运行目录里的 events.jsonl、author.json、verify.json、run.json 与产物；
输出一条 InteractionTrace，写入语料库后按 hash 去重、不可覆盖。
trace_hash 会剔除 recordedAt，保证同一事实内容得到同一个指纹。
"""

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
    """判断字符串是否是受支持的结果标签。"""
    return value in OUTCOME_LABELS


def is_failure_category(value: str) -> TypeGuard[FailureCategory]:
    """判断字符串是否是受支持的失败类别。"""
    return value in FAILURE_CATEGORIES


def _is_source_kind(value: str) -> TypeGuard[SourceKind]:
    """判断来源类型是 author/verify/run/unknown 之一。"""
    return value in ("author", "verify", "run", "unknown")


@dataclass(slots=True)
class TracePrompt:
    """记录 prompt 文件名与内容 hash，用于复现案例的输入。"""

    file: str
    sha256: str

    def to_dict(self) -> dict[str, object]:
        """序列化为 file/sha256 两个字段。"""
        return {"file": self.file, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: object, where: str) -> TracePrompt:
        """校验并重建 prompt 引用。"""
        record = require_object(value, where)
        return cls(
            file=require_text(record.get("file"), f"{where}.file", allow_empty=True),
            sha256=require_text(record.get("sha256"), f"{where}.sha256", allow_empty=True),
        )


@dataclass(slots=True)
class TracePolicy:
    """一次运行的执行环境：provider、模型、pi 版本、代码提交与 prompt。

    所有字段都可缺失，缺失时序列化会直接省略，不写 null。
    """

    provider: str | None = None
    model: str | None = None
    pi_version: str | None = None
    commit: str | None = None
    prompt: TracePrompt | None = None

    def to_dict(self) -> dict[str, object]:
        """只输出存在的字段，缺失项不写 null。"""
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
        """从 policy 文档重建执行环境；所有字段均可选。"""
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
    """运行目录里的一个产物文件及其相对路径、hash 与字节数。"""

    path: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, object]:
        """序列化为 path/sha256/bytes 三个字段。"""
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}

    @classmethod
    def from_dict(cls, value: object, where: str = "artifact") -> TraceArtifact:
        """校验 path/sha256/bytes 后重建产物记录。"""
        record = require_object(value, where)
        return cls(
            path=require_text(record.get("path"), f"{where}.path", allow_empty=True),
            sha256=require_text(record.get("sha256"), f"{where}.sha256", allow_empty=True),
            bytes=require_integer(record.get("bytes"), f"{where}.bytes"),
        )


@dataclass(slots=True)
class TraceEvents:
    """events.jsonl 的字节数与 hash，用来锚定抽取所依据的原始事实。"""

    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, object]:
        """序列化为 sha256/bytes 两个字段。"""
        return {"sha256": self.sha256, "bytes": self.bytes}

    @classmethod
    def from_dict(cls, value: object, where: str) -> TraceEvents:
        """校验 sha256/bytes 后重建 events 锚点。"""
        record = require_object(value, where)
        return cls(
            sha256=require_text(record.get("sha256"), f"{where}.sha256", allow_empty=True),
            bytes=require_integer(record.get("bytes"), f"{where}.bytes"),
        )


@dataclass(slots=True)
class TraceVerification:
    """复核阶段的信息：阶段名、批次 id 与是否与结果一致。"""

    stage: str | None = None
    batch_id: str | None = None
    identical: bool | None = None

    def to_dict(self) -> dict[str, object]:
        """只输出存在的 stage/batchId/identical。"""
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
        """校验 identical 必须是布尔值后重建复核信息。"""
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
    """案例的来源运行目录。

    kind 按 author > verify > run > unknown 归类；events/verify 为可选附加证据。
    """

    run_dir: str
    run_dir_name: str
    kind: SourceKind
    events: TraceEvents | None = None
    verify: TraceVerification | None = None

    def to_dict(self) -> dict[str, object]:
        """输出三个必填字段以及可选的 events/verify。"""
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
        """校验 kind 并重建来源记录。"""
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
    """案例的判定结果。

    score 限定在 0..1；failureCategory 只在 fail/partial 时有意义。
    """

    label: OutcomeLabel
    score: Number | None = None
    failure_category: FailureCategory | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        """校验 label、score 与 failureCategory 的取值。"""
        if not is_outcome_label(self.label):
            raise VibeError(f"outcome.label 只能是 {' / '.join(OUTCOME_LABELS)}")
        if self.score is not None:
            score = as_number(self.score)
            if score is None or not 0 <= score <= 1:
                raise VibeError("outcome.score 必须在 0..1")
        if self.failure_category is not None and not is_failure_category(self.failure_category):
            raise VibeError(f"outcome.failureCategory 只能是 {' / '.join(FAILURE_CATEGORIES)}")

    def to_dict(self) -> dict[str, object]:
        """只输出非空字段，避免写入 null。"""
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
        """校验 label/category 后重建判定结果。"""
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
    """案例的量化指标：轮次、工具调用/失败数、用量与坏行数。"""

    turns: int
    tool_calls: int
    tool_errors: int
    usage: UsageTotals
    messages: MessageCounts
    bad_lines: int
    elapsed_ms: Number | None = None

    def to_dict(self) -> dict[str, object]:
        """输出必填指标；elapsedMs 缺失时省略。"""
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
        """要求计数为整数并重建指标。"""
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
    """一条完整的、不可变的案例轨迹。

    claimCounts 是 claims 的折叠统计，必须与 claims 内容一致；
    traceVersion 固定为 0.1，读取时会校验。
    """

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
        """输出 traceVersion 与全部子文档。"""
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
        """校验 traceVersion 后重建整条轨迹。"""
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
    """构建轨迹的入参。

    claims 与 outcome 会被深拷贝，避免调用方之后修改影响已构建对象。
    """

    run_dir: str | Path
    id: str
    family: str
    question: str
    outcome: TraceOutcome
    claims: list[ClaimState] | None = None
    recorded_at: str | None = None


def _read_optional_json(path: Path) -> dict[str, object] | None:
    """读取可选描述文件；格式错误时返回 None。

    这样格式错误的描述文件不能作为来源类型的证据。
    I/O 错误仍然向外抛出，而不会被误认为元数据缺失。
    """
    if not path.exists():
        return None
    try:
        return as_record(loads_json(read_utf8(path)))
    except json.JSONDecodeError:
        return None


def _string_field(record: dict[str, object] | None, key: str) -> str | None:
    """从可选记录里取字符串字段，其他类型视为缺失。"""
    value = record.get(key) if record is not None else None
    return value if isinstance(value, str) else None


def _describe_source(run_dir: str | Path) -> tuple[TraceSource, TracePolicy, Number | None]:
    """描述一次运行的来源与执行环境。

    来源类型按 author > verify > run 的顺序取第一个存在的描述文件；
    返回值还包含 author.json 记录的耗时。
    """
    directory = Path(run_dir)
    author = _read_optional_json(directory / "author.json")
    verify = _read_optional_json(directory / "verify.json")
    run = _read_optional_json(directory / "run.json")
    policy = TracePolicy()
    # provider/model/piVersion 只从 author.json 读取。
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
            # 空字符串视为“有该字段但无提交”，直接停止，不再看后续描述文件。
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
        # 直接对原始字节哈希，避免行尾或编码差异影响指纹。
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
    """列出运行目录里的产物并计算 hash。

    跳过符号链接，避免把目录外内容计入案例；文件按 locale 顺序排列。
    """
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
    """从运行目录构建一条完整的案例轨迹。

    路径不存在或不是目录时直接报错；没有 events.jsonl 时仍可构建，
    只是抽取事实为空。claims 与 outcome 都做深拷贝，避免调用方后续修改。
    """
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
    """计算轨迹的稳定指纹。

    先剔除 recordedAt，再按稳定 JSON 序列化后哈希，因此同一内容重复记录得到相同值。
    """
    payload = trace.to_dict() if isinstance(trace, InteractionTrace) else dict(trace)
    payload.pop("recordedAt", None)
    return sha256_text(stable_stringify(payload))
