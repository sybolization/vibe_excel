"""案例语料库：以 id 目录存放 trace.json，并在 index.jsonl 追加摘要。

数据来源是调用方给出的 InteractionTrace；语料库本身是 append-only 的。
同一 id 写入不同内容会被拒绝，索引只在新增案例时追加一行，已有行永不改写。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vibe_excel.util import VibeError, read_json, require_object, require_text

from ._json import json_document, loads_json, locale_key, read_utf8, trim_text
from .claims import ClaimCounts
from .events import sha256_text
from .trace import InteractionTrace, trace_hash

_CASE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")


@dataclass(slots=True)
class CorpusEntry:
    """index.jsonl 里的一行案例摘要。

    hash 是 trace 的稳定指纹，claims 是写入时的声明统计快照。
    """

    id: str
    family: str
    hash: str
    outcome: str
    claims: ClaimCounts
    recorded_at: str

    def to_dict(self) -> dict[str, object]:
        """按索引行格式序列化，claims 展开为四个计数。"""
        return {
            "id": self.id,
            "family": self.family,
            "hash": self.hash,
            "outcome": self.outcome,
            "claims": {
                "total": self.claims.total,
                "confirmed": self.claims.confirmed,
                "unconfirmed": self.claims.unconfirmed,
                "rejected": self.claims.rejected,
            },
            "recordedAt": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> CorpusEntry:
        """校验必填字段后重建索引条目。"""
        record = require_object(value, "entry")
        return cls(
            id=require_text(record.get("id"), "entry.id"),
            family=require_text(record.get("family"), "entry.family"),
            hash=require_text(record.get("hash"), "entry.hash", allow_empty=True),
            outcome=require_text(record.get("outcome"), "entry.outcome", allow_empty=True),
            claims=ClaimCounts.from_dict(record.get("claims"), "entry.claims"),
            recorded_at=require_text(
                record.get("recordedAt"), "entry.recordedAt", allow_empty=True
            ),
        )


@dataclass(slots=True)
class SaveResult:
    """一次保存的结果。

    status=added 表示新建案例；unchanged 表示同 id 同内容已存在，未改写任何文件。
    """

    status: Literal["added", "unchanged"]
    hash: str
    path: str

    def to_dict(self) -> dict[str, object]:
        """序列化为 status/hash/path 三字段。"""
        return {"status": self.status, "hash": self.hash, "path": self.path}


def _validate_id(case_id: str) -> None:
    """校验案例 id 可作为目录名。

    只允许字母数字与 ._-，且不能以符号开头，防止路径穿越与非法文件名。
    """
    if _CASE_ID.fullmatch(case_id) is None:
        raise VibeError(f"案例 id 只允许字母数字与 ._-，且不能以符号开头：{case_id}")


def _trace_path(corpus_dir: str | Path, case_id: str) -> Path:
    """案例 trace.json 的固定路径：cases/<id>/trace.json。"""
    return Path(corpus_dir) / "cases" / case_id / "trace.json"


def _existing_result(path: Path, trace: InteractionTrace, digest: str) -> SaveResult:
    """处理已存在案例。

    内容相同返回 unchanged；内容不同直接报错，因为案例不可变，新的运行必须换 id。
    """
    existing = require_object(read_json(path), "trace")
    existing_hash = trace_hash(existing)
    # 内容不同即拒绝：案例不可变，新的运行必须换 id。
    if existing_hash != digest:
        raise VibeError(
            f"案例 {trace.id} 已存在且内容不同（existing={existing_hash[:12]} new={digest[:12]}）。"
            "案例是不可变的：要记录新的运行，请换一个 id。"
        )
    return SaveResult(status="unchanged", hash=digest, path=str(path))


def save_trace(corpus_dir: str | Path, trace: InteractionTrace) -> SaveResult:
    """把轨迹写入语料库，并追加索引行。

    trace 文件用独占创建（x），避免并发覆盖；索引只追加，已有行永不改写。
    """
    _validate_id(trace.id)
    digest = trace_hash(trace)
    path = _trace_path(corpus_dir, trace.id)
    if path.exists():
        return _existing_result(path, trace, digest)
    entry = CorpusEntry(
        id=trace.id,
        family=trace.family,
        hash=digest,
        outcome=trace.outcome.label,
        claims=ClaimCounts(
            total=trace.claim_counts.total,
            confirmed=trace.claim_counts.confirmed,
            unconfirmed=trace.claim_counts.unconfirmed,
            rejected=trace.claim_counts.rejected,
        ),
        recorded_at=trace.recorded_at,
    )
    document = f"{json_document(trace.to_dict(), indent=2)}\n"
    index = Path(corpus_dir) / "index.jsonl"
    existing_index = read_utf8(index) if index.exists() else ""
    # 索引末尾缺换行时补一个前缀，避免新行粘到上一行。
    prefix = "\n" if existing_index and not existing_index.endswith("\n") else ""
    index_line = f"{prefix}{json_document(entry.to_dict())}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 独占创建：并发写同一案例时，后到者会退回 _existing_result 而不是覆盖。
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
    except FileExistsError:
        return _existing_result(path, trace, digest)
    # 只有成功新建案例才追加索引，已有案例不会产生重复行。
    with index.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(index_line)
    return SaveResult(status="added", hash=digest, path=str(path))


def read_trace(corpus_dir: str | Path, case_id: str) -> InteractionTrace:
    """读取并校验一个已存在的案例。"""
    _validate_id(case_id)
    path = _trace_path(corpus_dir, case_id)
    if not path.exists():
        raise VibeError(f"案例不存在：{case_id}")
    return InteractionTrace.from_dict(read_json(path))


def list_corpus(corpus_dir: str | Path) -> list[CorpusEntry]:
    """读取 index.jsonl 里的全部案例摘要。

    索引不存在时返回空列表；空行跳过，坏行直接报错，避免静默丢案例。
    """
    path = Path(corpus_dir) / "index.jsonl"
    if not path.exists():
        return []
    entries: list[CorpusEntry] = []
    for index, raw_line in enumerate(read_utf8(path).split("\n"), start=1):
        line = trim_text(raw_line)
        if not line:
            continue
        try:
            value = loads_json(line)
        except json.JSONDecodeError as error:
            raise VibeError(f"案例索引第 {index} 行不是合法 JSON") from error
        try:
            entries.append(CorpusEntry.from_dict(value))
        except VibeError as error:
            raise VibeError(f"案例索引第 {index} 行 {error}") from error
    return entries


def corpus_digest(entries: list[CorpusEntry]) -> str:
    """计算语料库指纹。

    先按 id 与 hash 拼成行、按 locale 顺序排序，再整体哈希，结果与索引行序无关。
    """
    canonical = "\n".join(sorted((f"{entry.id}:{entry.hash}" for entry in entries), key=locale_key))
    return sha256_text(canonical)
