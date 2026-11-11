"""encoder 的命令行：record / claims / list / digest 四个子命令。

输入是运行目录与 claims.jsonl，输出写入语料库（corpus）目录并打印摘要。
案例一旦落盘就不可变更：同 id 且同内容只返回 unchanged，不同内容直接报错。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from vibe_excel.util import VibeError, now

from ._json import read_utf8, trim_text
from .claims import (
    CLAIM_KINDS,
    ClaimOp,
    ConfirmOp,
    RejectOp,
    append_claim_event,
    fold_claims,
    is_claim_kind,
    is_claim_source,
    parse_claims,
)
from .corpus import corpus_digest, list_corpus, save_trace
from .trace import (
    FAILURE_CATEGORIES,
    OUTCOME_LABELS,
    BuildTraceOptions,
    TraceOutcome,
    build_trace,
    is_failure_category,
    is_outcome_label,
)

USAGE = f"""用法：
  record --run <dir> --id <id> --family <f> --question <q> --corpus <dir>
         [--claims <file>] [--outcome pass|partial|fail] [--score 0.8]
         [--category <{"|".join(FAILURE_CATEGORIES)}>] [--note <text>]

  claims show    --file <claims.jsonl>
  claims add     --file <f> --id <id> --kind <{"|".join(CLAIM_KINDS)}>
                 --subject <s> --value <v> [--source agent|human] [--evidence <e>]...
  claims confirm --file <f> --id <id> --by <who> [--note <text>]
  claims reject  --file <f> --id <id> --by <who> [--reason <text>]

  list           --corpus <dir>
  digest         --corpus <dir>"""


@dataclass(slots=True)
class _Parsed:
    """一次命令行解析的结果。

    flags 按名称保留全部出现值，positionals 保留下标顺序。
    """

    positionals: list[str] = field(default_factory=list)
    flags: dict[str, list[str]] = field(default_factory=dict)


def _parse_args(argv: list[str]) -> _Parsed:
    """把 argv 拆成位置参数与标志。

    标志可重复出现，后续由 _flag/_required 决定取哪一个；无值的标志记为空串。
    """
    args = _Parsed()
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--"):
            has_value = index + 1 < len(argv) and not argv[index + 1].startswith("--")
            value = argv[index + 1] if has_value else ""
            args.flags.setdefault(token[2:], []).append(value)
            index += 2 if has_value else 1
        else:
            args.positionals.append(token)
            index += 1
    return args


def _flag(args: _Parsed, name: str) -> str | None:
    """取标志的最后一个值；普通标志重复出现时以后者为准。"""
    values = args.flags.get(name)
    return values[-1] if values else None


def _required(args: _Parsed, name: str) -> str:
    """取必填标志；缺失或为空时附带用法说明报错。"""
    value = _flag(args, name)
    if value is None or value == "":
        raise VibeError(f"缺少参数 --{name}\n\n{USAGE}")
    return value


def _print_claims(file: str) -> None:
    """折叠并打印 claims 文件当前状态。"""
    path = Path(file)
    states = fold_claims(parse_claims(read_utf8(path) if path.exists() else ""))
    if not states:
        print("（还没有任何语义断言）")
        return
    for state in states:
        evidence = f"  evidence={','.join(state.evidence)}" if state.evidence else ""
        print(f"{state.id}\t{state.status}\t{state.kind}\t{state.subject}\t{state.value}{evidence}")
    print(
        f"\n共 {len(states)} 条，未确认 {sum(state.status == 'unconfirmed' for state in states)} 条"
    )


_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_RADIX = re.compile(r"0(?:[xX][0-9a-fA-F]+|[bB][01]+|[oO][0-7]+)\Z")


def _score(text: str) -> float:
    """解析 0..1 的分数。

    除常规十进制外也接受 0x/0b/0o 字面量，兼容 JS Number 的解析习惯；
    空串按 0 处理。
    """
    stripped = trim_text(text)
    if not stripped:
        return 0
    try:
        if _RADIX.fullmatch(stripped):
            score = float(int(stripped, 0))
        elif _DECIMAL.fullmatch(stripped):
            score = float(stripped)
        else:
            raise VibeError("--score 必须在 0..1")
    except (ValueError, OverflowError) as error:
        raise VibeError("--score 必须在 0..1") from error
    if not 0 <= score <= 1:
        raise VibeError("--score 必须在 0..1")
    return score


def _record(args: _Parsed) -> None:
    """构建并保存一个新案例。

    outcome 缺省为 partial；claims 文件存在时先折叠成状态再写入案例，
    最后打印摘要并提示未确认语义仍需人工判定。
    """
    run_dir = _required(args, "run")
    case_id = _required(args, "id")
    family = _required(args, "family")
    question = _required(args, "question")
    corpus_dir = _required(args, "corpus")
    label = _flag(args, "outcome")
    if label is None:
        label = "partial"
    if not is_outcome_label(label):
        raise VibeError(f"--outcome 只能是 {' / '.join(OUTCOME_LABELS)}")
    raw_category = _flag(args, "category")
    category = raw_category if raw_category else None
    if category is not None and not is_failure_category(category):
        raise VibeError(f"--category 只能是 {' / '.join(FAILURE_CATEGORIES)}")
    score_text = _flag(args, "score")
    outcome = TraceOutcome(
        label=label,
        score=_score(score_text) if score_text else None,
        failure_category=category,
        notes=_flag(args, "note") or None,
    )
    claims_file = _flag(args, "claims")
    claims = (
        fold_claims(parse_claims(read_utf8(claims_file)))
        if claims_file and Path(claims_file).exists()
        else []
    )
    trace = build_trace(
        BuildTraceOptions(
            run_dir=run_dir,
            id=case_id,
            family=family,
            question=question,
            outcome=outcome,
            claims=claims,
        )
    )
    result = save_trace(corpus_dir, trace)
    pending = trace.claim_counts.unconfirmed
    print(f"案例 {trace.id}（{result.status}）")
    print(f"  hash      {result.hash[:16]}")
    print(
        f"  工具调用  {trace.metrics.tool_calls}（失败 {trace.metrics.tool_errors}）· 轮次 {trace.metrics.turns}"
    )
    print(
        f"  成本      ${trace.metrics.usage.cost_usd:.4f} · tokens {trace.metrics.usage.total_tokens}"
    )
    print(f"  语义      {trace.claim_counts.total} 条，未确认 {pending} 条")
    category_display = (
        f"（{trace.outcome.failure_category}）" if trace.outcome.failure_category else ""
    )
    print(f"  结果      {trace.outcome.label}{category_display}")
    print(f"  落盘      {result.path}")
    if pending > 0:
        print(f'\n提示：{pending} 条语义还没人确认，这个案例在 RSI 里只能算"待判"。')


def _dispatch(args: _Parsed) -> None:
    """按第一个位置参数分发到 record/list/digest/claims 子命令。"""
    command = args.positionals[0] if args.positionals else None
    sub = args.positionals[1] if len(args.positionals) > 1 else None
    if command == "record":
        _record(args)
    elif command == "list":
        entries = list_corpus(_required(args, "corpus"))
        if not entries:
            print("（案例库为空）")
            return
        for entry in entries:
            print(
                f"{entry.id}\t{entry.family}\t{entry.outcome}\t语义 {entry.claims.confirmed}/{entry.claims.total}"
                f"\t{entry.hash[:12]}\t{entry.recorded_at}"
            )
        print(f"\n共 {len(entries)} 条；指纹 {corpus_digest(entries)[:16]}")
    elif command == "digest":
        print(corpus_digest(list_corpus(_required(args, "corpus"))))
    elif command == "claims":
        file = _required(args, "file")
        if sub is None or sub == "show":
            _print_claims(file)
        elif sub == "add":
            kind = _required(args, "kind")
            if not is_claim_kind(kind):
                raise VibeError(f"--kind 只能是 {' / '.join(CLAIM_KINDS)}")
            source = _flag(args, "source")
            if source is None:
                source = "agent"
            if not is_claim_source(source):
                raise VibeError("--source 只能是 agent 或 human")
            evidence = [item for item in args.flags.get("evidence", []) if item]
            append_claim_event(
                file,
                ClaimOp(
                    id=_required(args, "id"),
                    kind=kind,
                    subject=_required(args, "subject"),
                    value=_required(args, "value"),
                    source=source,
                    evidence=evidence or None,
                    at=now(),
                ),
            )
            _print_claims(file)
        elif sub == "confirm" or sub == "reject":
            operation = ConfirmOp if sub == "confirm" else RejectOp
            append_claim_event(
                file,
                operation(
                    id=_required(args, "id"),
                    by=_required(args, "by"),
                    note=_flag(args, "note" if sub == "confirm" else "reason") or None,
                    at=now(),
                ),
            )
            _print_claims(file)
        else:
            raise VibeError(f"未知子命令：claims {sub}\n\n{USAGE}")
    else:
        raise VibeError(USAGE)


def run_cli(argv: list[str]) -> int:
    """执行编码 CLI；已知错误统一转成返回码 1。"""
    try:
        _dispatch(_parse_args(argv))
    except (VibeError, OSError, json.JSONDecodeError, UnicodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def main() -> None:
    """供 `python -m vibe_excel.encoder` 调用的入口；失败时设置退出码。"""
    status = run_cli(sys.argv[1:])
    if status:
        raise SystemExit(status)


if __name__ == "__main__":
    main()
