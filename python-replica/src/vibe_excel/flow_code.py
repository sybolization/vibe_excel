"""“代码即流程”的桥接层：把入口文件的 main 交给内置解析器，换回节点列表。

声明式流程（flow.json 里逐条写 nodes）与代码即流程（只写入口文件、节点从 main 推导）在这里
合一：load_flow_at_commit 先读 flow.json，若没有 nodes 再静态解析 entry。解析器在子进程里
运行，本模块只消费它输出的 JSON，不 import 目标文件。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from vibe_excel.flow import (
    DEFAULT_FLOW_PATH,
    FlowNode,
    FlowSpec,
    FlowStep,
    FlowWarning,
    parse_flow_json,
    table_list,
)
from vibe_excel.git import read_file_at_commit
from vibe_excel.process import minimal_environment
from vibe_excel.util import VibeError, relative_path, require_list, require_object, require_text


@dataclass(frozen=True)
class CodeFlow:
    """代码即流程的解析结果：已解析的入口路径、节点与结构警告。"""

    entry: str
    nodes: list[FlowNode]
    warnings: list[FlowWarning]


def bundled_script(name: str) -> Path:
    """定位随包分发的内置引擎脚本；缺失时说明这是安装问题。"""
    path = Path(__file__).resolve().parent / "engines" / name
    if not path.is_file():
        raise VibeError(f"缺少内置 Python 引擎: {path}")
    return path


def parse_code_flow(
    source: str, entry: str = "pipeline.py", *, python: str = sys.executable
) -> CodeFlow:
    """在子进程中静态解析入口源码，返回节点与警告。

    用子进程而不是 import：避免目标文件的副作用污染当前进程；60 秒超时只防解析器卡死，
    解析器本身只做 AST 分析。空输出或非 JSON 输出都按契约破坏处理，并带上退出码与 stderr。
    """
    path = relative_path(entry, "流程入口", suffix=".py")
    try:
        result = subprocess.run(
            [python, str(bundled_script("flow_parse.py")), path], input=source, text=True,
            encoding="utf-8", capture_output=True, timeout=60, check=False,
            env=minimal_environment(),  # 最小环境：解析结果不应随调用者的环境变量变化
        )
    except subprocess.TimeoutExpired as error:
        raise VibeError(
            f"解析 {path} 超时（60 秒）：解析器只做静态分析，不该卡住，检查解释器 {python}"
        ) from error
    except OSError as error:
        raise VibeError(f"无法启动解析器（{python}）：{error}") from error
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if not stdout:
        detail = f"\n{stderr}" if stderr else ""
        raise VibeError(
            f"解析 {path} 没有输出（退出码 {result.returncode}）：解析器应当总是返回 JSON。{detail}"
        )
    try:
        payload: object = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise VibeError(
            f"解析 {path} 的返回不是 JSON（退出码 {result.returncode}）："
            f"\n{stdout[:800]}{f'{chr(10)}{stderr[:400]}' if stderr else ''}"
        ) from error
    body = require_object(payload, "代码解析器结果")
    if body.get("ok") is not True:
        failure = require_object(body.get("error"), "代码解析器错误")
        line = failure.get("line")
        location = path + (f":{line}" if isinstance(line, int) else "")
        raise VibeError(f"{location} [{failure.get('code')}] {failure.get('message')}")
    if result.returncode != 0:
        raise VibeError(f"代码解析器执行失败，退出码 {result.returncode}: {result.stderr}")
    nodes: list[FlowNode] = []
    for item in require_list(body.get("nodes"), "代码解析器的 nodes"):
        raw = require_object(item, "代码解析器节点")
        name = require_text(raw.get("func"), "node.func")
        steps: list[FlowStep] = []
        for step_raw in require_list(raw.get("steps", []), "node.steps"):
            step = require_object(step_raw, "节点步骤")
            line = step.get("line")
            if isinstance(line, bool) or not isinstance(line, int):
                raise VibeError("节点步骤行号必须是整数")
            kind: Literal["block", "statement"]
            match step.get("kind"):
                case "block" | "statement" as value:
                    kind = value
                case _:
                    raise VibeError("节点步骤 kind 必须是 block 或 statement")
            steps.append(
                FlowStep(line, kind, require_text(step.get("of"), "step.of"),
                         require_text(step.get("label"), "step.label", allow_empty=True))
            )
        nodes.append(
            FlowNode(
                name, f"{path}:{name}", table_list(raw.get("reads"), "node.reads"),
                table_list(raw.get("writes"), "node.writes"),
                title=require_text(raw.get("title"), "node.title"),
                steps=[item for item in steps if item.label],
                name=(require_text(raw.get("name"), "node.name") if "name" in raw else None),
            )
        )
    if not nodes:
        raise VibeError(f"{path} 的 main 未产生任何流程节点")
    warnings: list[FlowWarning] = []
    for item in require_list(body.get("warnings", []), "代码解析器警告"):
        warning = require_object(item, "解析器警告")
        line = warning.get("line")
        warnings.append(
            FlowWarning(
                require_text(warning.get("code"), "warning.code"),
                require_text(warning.get("message"), "warning.message"),
                line if isinstance(line, int) and not isinstance(line, bool) else None,
            )
        )
    return CodeFlow(path, nodes, warnings)


def load_flow_at_commit(
    repo_path: str | Path, commit: str, path: str = DEFAULT_FLOW_PATH, *,
    python: str = sys.executable,
) -> FlowSpec:
    """从指定提交读取 flow.json；没有声明节点时改从 entry 解析代码推导，返回合并后的规格。

    只读提交里的内容，因此计划与工作区里未提交的改动无关；flow.json 与入口文件都取自同一提交。
    """
    file = relative_path(path, "流程声明")
    spec = parse_flow_json(read_file_at_commit(repo_path, commit, file))
    if spec.nodes:
        return spec
    entry = spec.entry or "pipeline.py"
    code = parse_code_flow(read_file_at_commit(repo_path, commit, entry), entry, python=python)
    return replace(spec, entry=code.entry, nodes=code.nodes, warnings=code.warnings)


def nodes_of_code_flow(flow: CodeFlow) -> list[FlowNode]:
    """代码解析结果已经直接持有 FlowNode；保留函数名与 TS 的 nodesOfCodeFlow 对齐。"""

    return list(flow.nodes)


nodesOfCodeFlow = nodes_of_code_flow  # 与 TypeScript 侧的驼峰命名保持对齐
