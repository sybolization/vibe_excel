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
    entry: str
    nodes: list[FlowNode]
    warnings: list[FlowWarning]


def bundled_script(name: str) -> Path:
    path = Path(__file__).resolve().parent / "engines" / name
    if not path.is_file():
        raise VibeError(f"Bundled Python engine is missing: {path}")
    return path


def parse_code_flow(
    source: str, entry: str = "pipeline.py", *, python: str = sys.executable
) -> CodeFlow:
    path = relative_path(entry, "flow entry", suffix=".py")
    try:
        result = subprocess.run(
            [python, str(bundled_script("flow_parse.py")), path], input=source, text=True,
            encoding="utf-8", capture_output=True, timeout=60, check=False,
            env=minimal_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VibeError(f"Cannot run the parser for {path}; check the uv interpreter: {error}") from error
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise VibeError(f"Parser returned invalid JSON for {path}: {result.stderr or result.stdout[:800]}") from error
    body = require_object(payload, "code parser result")
    if body.get("ok") is not True:
        failure = require_object(body.get("error"), "code parser error")
        line = failure.get("line")
        location = path + (f":{line}" if isinstance(line, int) else "")
        raise VibeError(f"{location} [{failure.get('code')}] {failure.get('message')}")
    if result.returncode != 0:
        raise VibeError(f"Code parser failed with exit {result.returncode}: {result.stderr}")
    nodes: list[FlowNode] = []
    for item in require_list(body.get("nodes"), "code parser nodes"):
        raw = require_object(item, "code parser node")
        name = require_text(raw.get("func"), "node.func")
        steps: list[FlowStep] = []
        for step_raw in require_list(raw.get("steps", []), "node.steps"):
            step = require_object(step_raw, "node step")
            line = step.get("line")
            if isinstance(line, bool) or not isinstance(line, int):
                raise VibeError("Node step line must be an integer")
            kind: Literal["block", "statement"]
            match step.get("kind"):
                case "block" | "statement" as value:
                    kind = value
                case _:
                    raise VibeError("Node step kind must be block or statement")
            steps.append(
                FlowStep(line, kind, require_text(step.get("of"), "step.of"),
                         require_text(step.get("label"), "step.label"))
            )
        nodes.append(
            FlowNode(
                name, f"{path}:{name}", table_list(raw.get("reads"), "node.reads"),
                table_list(raw.get("writes"), "node.writes"),
                title=require_text(raw.get("title"), "node.title"), steps=steps,
            )
        )
    if not nodes:
        raise VibeError(f"{path} main did not produce any workflow nodes")
    warnings: list[FlowWarning] = []
    for item in require_list(body.get("warnings", []), "code parser warnings"):
        warning = require_object(item, "parser warning")
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
    file = relative_path(path, "flow declaration")
    spec = parse_flow_json(read_file_at_commit(repo_path, commit, file))
    if spec.nodes:
        return spec
    entry = spec.entry or "pipeline.py"
    code = parse_code_flow(read_file_at_commit(repo_path, commit, entry), entry, python=python)
    return replace(spec, entry=code.entry, nodes=code.nodes, warnings=code.warnings)
