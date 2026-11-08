"""`optimize` 的纯逻辑：建议文件的形状、按 id 挑选、版本对齐与两段提示词。

为什么不做成自动折叠：折叠会改变口径（合并两张中间表，等于删掉一个可以单独检查的
中间结果）。这是人的决定，不是模型的。所以这里只做两件事：

1. 评审（review）：只读地看代码和 flow，把建议写成一份 JSON；
2. 应用（apply）：拿着**被批准的那一条**去改代码和 flow，改完提交到草稿。

本模块是纯的：只做校验、拼提示词、挑建议，不碰文件系统和子进程。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, cast

from vibe_excel.util import VibeError

ProposalKind = Literal[
    "fold-nodes", "drop-node", "drop-materialize", "rewrite-node", "other"
]

KINDS: tuple[ProposalKind, ...] = (
    "fold-nodes",
    "drop-node",
    "drop-materialize",
    "rewrite-node",
    "other",
)


class ProposalSaves(dict[str, int]):
    """一条建议省下的节点数与表数；键名与建议文件 JSON 完全一致。

    继承 dict 是为了序列化出去时形状不变；`nodes` / `tables` 属性只是读取方便。
    """

    def __init__(self, nodes: int = 0, tables: int = 0) -> None:
        super().__init__(nodes=nodes, tables=tables)

    @property
    def nodes(self) -> int:
        return self["nodes"]

    @property
    def tables(self) -> int:
        return self["tables"]


@dataclass(frozen=True)
class FlowProposal:
    """模型提出的单条优化建议。

    字段与建议文件 JSON 一一对应；`verify` 只做非空校验，是否真的可执行由人评审。
    """

    id: str
    title: str
    kind: ProposalKind
    nodes: list[str]
    saves: ProposalSaves
    risk: str
    verify: str


@dataclass(frozen=True)
class ProposalFile:
    """建议文件：绑定到某一版流程，流程变过之后旧建议自动失效。"""

    flow_digest: str
    proposals: list[FlowProposal]


def _as_text(value: object, where: str) -> str:
    """要求非空字符串（去空白后）；用于解析模型写的 JSON。"""

    if not isinstance(value, str):
        raise VibeError(f"{where} 应该是字符串")
    text = value.strip()
    if text == "":
        raise VibeError(f"{where} 不能为空")
    return text


def _as_count(value: object, where: str) -> int:
    """对齐 TS 的 `Number(value)` + `Number.isInteger` 判定。"""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise VibeError(f"{where} 应该是非负整数，实际是 {value}")
    try:
        number = float(value)
    except ValueError as error:
        raise VibeError(f"{where} 应该是非负整数，实际是 {value}") from error
    if not number.is_integer() or number < 0:
        raise VibeError(f"{where} 应该是非负整数，实际是 {value}")
    return int(number)


def _as_mapping(value: object, where: str) -> dict[str, object]:
    """要求字符串键的对象；不是 dict 或键不是字符串都立即拒绝。"""

    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise VibeError(f"{where} 应该是一个对象")
    return {key: item for key, item in value.items()}


def parse_proposal_file(text: str, where: str = "建议文件") -> ProposalFile:
    """解析模型写出来的建议文件；形状不对就整份拒绝，不做部分接受。"""

    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise VibeError(f"{where}：建议文件不是合法 JSON：{error.msg}") from error
    root = _as_mapping(raw, where)

    flow_digest = _as_text(root.get("flowDigest"), f"{where}.flowDigest")
    proposals_raw = root.get("proposals")
    if not isinstance(proposals_raw, list):
        raise VibeError(f"{where}.proposals 应该是数组")
    if not proposals_raw:
        # 空数组在这里是错误：调用方无法区分「模型没写」与「真的没有可优化项」。
        raise VibeError(
            f"{where}.proposals 是空的：模型没有提出任何建议（这本身也是结论，但别写成空文件）"
        )

    seen: set[str] = set()
    proposals: list[FlowProposal] = []
    for index, item in enumerate(proposals_raw):
        item_where = f"{where}.proposals[{index}]"
        proposal = _as_mapping(item, item_where)
        identifier = _as_text(proposal.get("id"), f"{item_where}.id")
        if identifier in seen:
            raise VibeError(f"{item_where}.id 重复：{identifier}")
        seen.add(identifier)

        kind = _as_text(proposal.get("kind"), f"{item_where}.kind")
        if kind not in KINDS:
            raise VibeError(
                f"{item_where}.kind 不认识：{kind}（可选：{' / '.join(KINDS)}）"
            )

        nodes_raw = proposal.get("nodes")
        if not isinstance(nodes_raw, list) or any(
            not isinstance(node, str) for node in nodes_raw
        ):
            raise VibeError(f"{item_where}.nodes 应该是字符串数组")
        nodes = [node.strip() for node in nodes_raw if node.strip() != ""]

        saves_raw = proposal.get("saves")
        # saves 缺字段按 0：模型常省略「不省的项」，这不是错误。
        saves = saves_raw if isinstance(saves_raw, dict) else {}
        proposals.append(
            FlowProposal(
                id=identifier,
                title=_as_text(proposal.get("title"), f"{item_where}.title"),
                kind=cast(ProposalKind, kind),
                nodes=nodes,
                saves=ProposalSaves(
                    nodes=_as_count(saves.get("nodes", 0), f"{item_where}.saves.nodes"),
                    tables=_as_count(saves.get("tables", 0), f"{item_where}.saves.tables"),
                ),
                risk=_as_text(proposal.get("risk"), f"{item_where}.risk"),
                verify=_as_text(proposal.get("verify"), f"{item_where}.verify"),
            )
        )

    return ProposalFile(flow_digest=flow_digest, proposals=proposals)


def select_proposal(file: ProposalFile, identifier: str) -> FlowProposal:
    """按 id 取一条建议；取不到时把可用的 id 列出来。"""

    for proposal in file.proposals:
        if proposal.id == identifier:
            return proposal
    available = ", ".join(proposal.id for proposal in file.proposals)
    raise VibeError(f"没有这条建议：{identifier}（这份建议里有：{available}）")


def assert_same_flow(file: ProposalFile, digest: str) -> None:
    """建议必须基于**当前这版**流程：流程改过了，旧建议就可能已经指错节点。"""

    if file.flow_digest != digest:
        raise VibeError(
            f"这份建议是基于流程 {file.flow_digest} 写的，现在的草稿是 {digest}："
            "流程变过了，重新跑 optimize"
        )


def _hint_line(hint: object) -> str:
    """把一条机器优化提示渲染成单行；hint 可能是 dataclass 也可能是 dict。"""

    kind_raw = getattr(hint, "kind", None)
    note_raw = getattr(hint, "note", None)
    if isinstance(hint, dict):
        kind_raw = hint.get("kind")
        note_raw = hint.get("note")
    kind = kind_raw if isinstance(kind_raw, str) else "?"
    note = note_raw if isinstance(note_raw, str) else str(hint)
    return f"- [{kind}] {note}"


def build_review_prompt(
    *,
    flow_json: str,
    flow_digest: str,
    plan_lines: list[str],
    hints: list[object],
    proposal_path: str,
    code_root: str,
    environment: str = "",
) -> str:
    """评审提示词：只读、只写建议文件。"""

    if hints:
        hints_text = "\n".join(_hint_line(hint) for hint in hints)
    else:
        # 没有机器提示时明确写「只能从语义里找」：否则模型会编造形式冗余。
        hints_text = (
            "（机器没有发现形式冗余——死节点 / 单消费者 / 空物化都没有。"
            "所以你只能从代码的语义里找。）"
        )
    environment_block = f"\n{environment}\n" if environment else ""
    plan_text = "\n".join(plan_lines)

    return f"""# 任务：评审这条工作流能不能更短（**只读**，不要改任何文件）

你在一个**只读的干净工作目录**里：`{code_root}`。这个目录是当前草稿提交的一份拷贝，
你在这里只能读。不要执行 git 命令，不要修改任何文件（改了也不会被采用）。
{environment_block}
## 当前工作流（flow@{flow_digest}）

```json
{flow_json}
```

拓扑与依赖：

```
{plan_text}
```

机器能找到的形式冗余：

{hints_text}

## 你要做的事

读代码，判断有没有**更短且等价**的做法。允许的思路：

- 两个节点能不能合成一个（中间表只被一个下游读、而且没有别的用途）；
- 某个节点是不是空转（声明的写表跟上游一样）；
- 某个中间表能不能不落盘，直接在下一个节点里算；
- 物化了却没人用的表。

**不要**为了好看而合并：合并会删掉一个可以单独检查的中间结果。每条建议都必须能回答
「省了什么」和「什么情况下这条建议会伤到业务」。

## 输出

把建议写成一份 JSON，**写到这个绝对路径**：`{proposal_path}`

```json
{{
  "flowDigest": "{flow_digest}",
  "proposals": [
    {{
      "id": "p1",
      "title": "一句话说清改什么",
      "kind": "fold-nodes | drop-node | drop-materialize | rewrite-node | other",
      "nodes": ["涉及", "的节点"],
      "saves": {{ "nodes": 1, "tables": 1 }},
      "risk": "什么情况下这条建议会改变业务结果或删掉有用的检查点",
      "verify": "改完之后怎么证明等价（具体的表、行数或哈希对比）"
    }}
  ]
}}
```

规则：

- 一条一个 id（p1、p2…），一次最多 5 条；
- `risk` 写不出来就不要提这条；
- `verify` 必须是能实际执行的检查，不许写「人工确认」；
- 如果**没有**更短且等价的写法，就写 `"proposals": []` 并在最终回复里说明为什么——
  这是好答案，不是失败。

除了这份 JSON 文件，什么都不要写。"""


def _proposal_payload(proposal: FlowProposal) -> dict[str, object]:
    """把建议重新拼成 JSON 形状，供 apply 提示词内联展示。"""

    return {
        "id": proposal.id,
        "title": proposal.title,
        "kind": proposal.kind,
        "nodes": list(proposal.nodes),
        "saves": {"nodes": proposal.saves["nodes"], "tables": proposal.saves["tables"]},
        "risk": proposal.risk,
        "verify": proposal.verify,
    }


def build_apply_prompt(
    *,
    proposal: FlowProposal,
    approved_at: str,
    code_root: str,
    flow_json: str,
    plan_lines: list[str],
    environment: str = "",
) -> str:
    """应用提示词：只实现被批准的那一条，改完自查。"""

    environment_block = f"\n{environment}\n" if environment else ""
    plan_text = "\n".join(plan_lines)
    # 只把被批准的那一条塞进提示词：整份建议都放进去会诱导模型顺手改别的。
    payload = json.dumps(
        {"approvedAt": approved_at, "proposal": _proposal_payload(proposal)},
        ensure_ascii=False,
        indent=2,
    )

    return f"""# 任务：实现一条**已被人批准**的优化建议

用户逐条看过建议，批准了这一条。你的工作是把它实现出来，并证明行为等价。
除了这一条，不许做别的优化。
{environment_block}

```json
{payload}
```

当前工作流（flow@摘要见上），你在可写的工作目录 `{code_root}` 里：

```json
{flow_json}
```

```
{plan_text}
```

## 要求

1. 只改这条建议涉及的节点/表；其他行为保持不变。
2. 改完之后**必须**用下面这个检查证明等价（这是批准时写下的验证办法）：
   > {proposal.verify}
   把检查结果（行数、哈希、或你实际跑出来的对比）写进最终回复。
3. 如果实现过程中发现等价性不成立——**停下来**，不要硬改：把原因写清楚，保持工作区不变。
4. 不要执行 git 命令；提交由编排器负责。
5. 不要顺手修别的东西。"""
