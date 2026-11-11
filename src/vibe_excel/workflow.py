"""一轮流程执行：先物化，再按拓扑序跑节点，最后核对输出与文件差异。

这是执行层里“跑一遍”的编排器，只依赖注入的回调（run_materialize / run_python / read_file），
不自己决定子进程怎么起；干净重跑与真实运行由调用方（Executor / runtime）提供。成功判定
基于数据库内容摘要，而不是单独相信进程退出码。
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vibe_excel.flow import FlowNode, FlowPlan, MaterializeEntry, parse_call
from vibe_excel.process import ProcessResult
from vibe_excel.runtime import CleanRunEntry, CleanRunOptions, clean_rerun
from vibe_excel.util import (
    Digest,
    TableDigest,
    VibeError,
    database_digest,
    dir_digests,
    readonly_database,
    write_json,
)


@dataclass(frozen=True)
class MaterializeInput:
    """物化回调的入参：清单、目标库与批次目录。"""

    entries: list[MaterializeEntry]
    db_path: Path
    inputs_dir: Path


@dataclass(frozen=True)
class PythonNodeInput:
    """Python 节点的执行入参：节点、文件、函数、已填充的 kwargs 与超时。"""

    node: FlowNode
    file: str
    func: str
    kwargs: dict[str, str]
    timeout_ms: int | None


@dataclass(frozen=True)
class MaterializeRecord:
    """单个物化条目的结果：规格回显、是否产出、产出表摘要。"""

    entry: dict[str, object]
    produced: bool
    digest: TableDigest | None


@dataclass(frozen=True)
class MaterializeReport:
    """物化阶段的汇总：进程结果，以及每个条目是否落地。"""

    exit_code: int
    timed_out: bool
    elapsed_ms: int
    stdout: str
    stderr: str
    entries: list[MaterializeRecord]
    truncated: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class NodeRunRecord:
    """单个节点的执行记录：命令、退出码、输出，以及写表的 before/after 差异。"""

    id: str
    call: str
    file: str
    func: str | None
    kwargs: dict[str, str]
    exit_code: int
    timed_out: bool
    elapsed_ms: int
    stdout: str
    stderr: str
    changed: list[str]
    unchanged: list[str]
    missing_reads: list[str]
    truncated: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class FlowRoundResult:
    """一轮执行的完整结果：物化报告、节点记录、输出表摘要与 out 目录文件摘要。"""

    ok: bool
    materialize: MaterializeReport
    nodes: list[NodeRunRecord]
    outputs: dict[str, TableDigest]
    files: list[Digest]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FlowRoundOptions:
    """一轮执行的依赖注入包：计划、路径、固定变量与三个回调。"""

    plan: FlowPlan
    db_path: Path
    inputs_dir: Path
    out_dir: Path
    as_of: str
    run_materialize: Callable[[MaterializeInput], ProcessResult]
    run_python: Callable[[PythonNodeInput], ProcessResult]
    read_file: Callable[[str], str]
    log: Callable[[str], None] = print


class Executor:
    """把干净重跑包装成 ProcessResult 的默认执行器。"""

    def __init__(
        self, python: str = sys.executable, timeout_ms: int = 900_000,
        max_output_bytes: int = 8 * 1024 * 1024, kill_grace_ms: int = 5000,
    ) -> None:
        """保存解释器与默认的时长/输出上限；节点级 timeoutMs 会覆盖这里的默认值。"""
        self.python = python
        self.timeout_ms = timeout_ms
        self.max_output_bytes = max_output_bytes
        self.kill_grace_ms = kill_grace_ms

    def interpreter(self) -> str:
        """返回将要使用的 Python 解释器路径。"""
        return self.python

    def run(
        self, *, repo_path: str | Path, commit: str, run_root: str | Path, entry: str | Path,
        args: list[str], as_of: str, timeout_ms: int | None = None,
    ) -> ProcessResult:
        """在指定提交的独立工作树上干净重跑入口脚本，返回纯进程结果。"""
        result = clean_rerun(
            CleanRunOptions(
                repo_path, commit, run_root, CleanRunEntry(self.python, [str(entry), *args]),
                timeout_ms=self.timeout_ms if timeout_ms is None else timeout_ms,
                as_of_date=as_of, max_output_bytes=self.max_output_bytes,
                kill_grace_ms=self.kill_grace_ms,
            )
        )
        return ProcessResult(
            result.exit_code, result.timed_out, result.stdout, result.stderr, result.elapsed_ms,
            result.cancelled, result.truncated,
        )


def fill_kwargs(values: Mapping[str, str], variables: Mapping[str, str]) -> dict[str, str]:
    """用 variables 替换节点 args 里的 {名字} 占位符；未知占位符原样保留。"""
    return {
        key: re.sub(r"\{(\w+)\}", lambda match: variables.get(match[1], match[0]), value)
        for key, value in values.items()
    }


def table_names(path: str | Path) -> list[str]:
    """列出数据库里用户表的名称；库不存在时返回空表而不是抛错。"""
    if not Path(path).exists():
        return []
    with readonly_database(path) as connection:
        return [
            str(row["name"]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]


def execute_sql_node(path: str | Path, sql: str) -> ProcessResult:
    """在一个事务里执行节点 SQL：全部成功才提交，出错整体回滚。

    节点 SQL 不应自带 BEGIN / COMMIT 等事务控制语句，否则会提前提交或破坏原子性。
    """
    started = time.monotonic()
    if not Path(path).exists():
        # 库不存在时不建空库，直接给节点一个失败结果。
        return ProcessResult(1, False, "", f"数据库不存在: {path}", 0)
    connection = sqlite3.connect(path)
    try:
        # 显式包一层事务：executescript 自身不保证原子性，失败必须整体回滚。
        connection.executescript("BEGIN;\n" + sql + "\n;COMMIT;")
        return ProcessResult(0, False, "", "", round((time.monotonic() - started) * 1000))
    except sqlite3.Error as error:
        connection.rollback()
        return ProcessResult(1, False, "", str(error), round((time.monotonic() - started) * 1000))
    finally:
        connection.close()


def run_flow_round(options: FlowRoundOptions) -> FlowRoundResult:
    """按计划执行一轮：物化 -> 逐节点 -> 输出核对，返回结构化结果。

    任何阶段失败都会短路后续节点，但已发生的结果仍完整返回。节点声明的写表若内容未变
    只记警告（不视为失败）；outputs 为空则直接判失败，因为“两个空结果集”不构成校验。
    """
    plan = options.plan
    options.out_dir.mkdir(parents=True, exist_ok=True)
    # 固定变量；节点 args 里的占位符只能引用这些键。
    variables = {
        "db": str(options.db_path.resolve()), "inputs": str(options.inputs_dir.resolve()),
        "out": str(options.out_dir.resolve()), "asOf": options.as_of,
    }
    warnings: list[str] = []

    def warn(message: str) -> None:
        warnings.append(message)
        options.log(f"    ! {message}")

    def report(execution: ProcessResult) -> None:
        if execution.stdout.strip():
            options.log(execution.stdout.rstrip())
        if execution.stderr.strip():
            options.log(execution.stderr.rstrip())
        if execution.truncated:
            warn("捕获的输出在配置的字节上限处被截断")

    materialized = (
        options.run_materialize(MaterializeInput(plan.spec.materialize, options.db_path, options.inputs_dir))
        if plan.spec.materialize else ProcessResult(0, False, "", "", 0)
    )
    report(materialized)
    digest = database_digest(options.db_path, [entry.table for entry in plan.spec.materialize])
    entries = [
        MaterializeRecord(entry.to_spec(), entry.table in digest, digest.get(entry.table))
        for entry in plan.spec.materialize
    ]
    for entry in entries:
        if not entry.produced:
            warn(f"物化未生成 {entry.entry['table']}")
    ok = (
        materialized.exit_code == 0 and not materialized.timed_out and not materialized.cancelled
        and all(entry.produced for entry in entries)
    )
    nodes: list[NodeRunRecord] = []
    by_id = {node.id: node for node in plan.spec.nodes}
    if ok:
        for identifier in plan.order:
            node = by_id.get(identifier)
            if node is None:
                raise VibeError(f"计划中存在未知节点: {identifier}")
            file, func = parse_call(node.call, f"nodes.{node.id}.call")
            # 每轮都重新取表名：上游节点会新建表，不能缓存。
            existing = set(table_names(options.db_path))
            missing = [table for table in node.reads if table not in existing]
            kwargs = {
                "db": variables["db"], "inputs": variables["inputs"], "out": variables["out"],
                "as_of": options.as_of, **fill_kwargs(node.args or {}, variables),
            }
            options.log(f"  [{node.id}] {node.call}")
            # 前后各取一次摘要：内容真的变了才算写成功。
            before = database_digest(options.db_path, node.writes)
            if missing:
                execution = ProcessResult(1, False, "", f"输入表不存在: {', '.join(missing)}", 0)
            elif func is None:
                execution = execute_sql_node(options.db_path, options.read_file(file))
            else:
                execution = options.run_python(PythonNodeInput(node, file, func, kwargs, node.timeout_ms))
            after = database_digest(options.db_path, node.writes)
            changed = [table for table in node.writes if before.get(table) != after.get(table)]
            unchanged = [table for table in node.writes if table not in changed]
            nodes.append(
                NodeRunRecord(
                    node.id, node.call, file, func, kwargs, execution.exit_code, execution.timed_out,
                    execution.elapsed_ms, execution.stdout, execution.stderr, changed, unchanged,
                    missing, execution.truncated, execution.cancelled,
                )
            )
            report(execution)
            if unchanged and execution.exit_code == 0:
                # 退出码为 0 但内容没变：可能是幂等写入，只警告，不判失败。
                warn(f"声明的写入未发生变化（不自动视为失败）: {', '.join(unchanged)}")
            if execution.exit_code != 0 or execution.timed_out or execution.cancelled or missing:
                ok = False
                break
    outputs = database_digest(options.db_path, plan.spec.outputs)
    if ok:
        missing_outputs = [table for table in plan.spec.outputs if table not in outputs]
        if not plan.spec.outputs:
            # 空 outputs 无法区分“没输出”和“输出为空”，因此不能算成功。
            warn("flow.outputs 为空：两个空结果集不构成成功校验")
            ok = False
        elif missing_outputs:
            warn(f"声明的输出表未出现: {', '.join(missing_outputs)}")
            ok = False
    return FlowRoundResult(
        ok,
        MaterializeReport(
            materialized.exit_code, materialized.timed_out, materialized.elapsed_ms,
            materialized.stdout, materialized.stderr, entries, materialized.truncated, materialized.cancelled,
        ),
        nodes, outputs, dir_digests(options.out_dir), warnings,
    )


@dataclass(frozen=True)
class RoundDiff:
    """两轮结果的差异集：变化的表、变化的文件，以及是否完全一致。"""

    tables: list[str]
    files: list[str]
    identical: bool


def diff_rounds(a: FlowRoundResult, b: FlowRoundResult) -> RoundDiff:
    """比较两轮的表摘要与文件摘要，用于双跑一致性验证。"""
    tables = sorted(
        name for name in a.outputs.keys() | b.outputs.keys()
        if a.outputs.get(name) != b.outputs.get(name)
    )
    left = {f"{item.path}:{item.sha256}" for item in a.files}
    right = {f"{item.path}:{item.sha256}" for item in b.files}
    files = sorted(left ^ right)
    return RoundDiff(tables, files, not tables and not files)


def write_materialize_spec(directory: str | Path, entries: list[MaterializeEntry]) -> Path:
    """把物化清单写成 materialize.json，返回其绝对路径。"""
    path = Path(directory) / "materialize.json"
    write_json(path, {"entries": [entry.to_spec() for entry in entries]})
    return path.resolve()
