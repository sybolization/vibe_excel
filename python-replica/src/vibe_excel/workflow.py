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
    entries: list[MaterializeEntry]
    db_path: Path
    inputs_dir: Path


@dataclass(frozen=True)
class PythonNodeInput:
    node: FlowNode
    file: str
    func: str
    kwargs: dict[str, str]
    timeout_ms: int | None


@dataclass(frozen=True)
class MaterializeRecord:
    entry: dict[str, object]
    produced: bool
    digest: TableDigest | None


@dataclass(frozen=True)
class MaterializeReport:
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
    ok: bool
    materialize: MaterializeReport
    nodes: list[NodeRunRecord]
    outputs: dict[str, TableDigest]
    files: list[Digest]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FlowRoundOptions:
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
    def __init__(
        self, python: str = sys.executable, timeout_ms: int = 900_000,
        max_output_bytes: int = 8 * 1024 * 1024, kill_grace_ms: int = 5000,
    ) -> None:
        self.python = python
        self.timeout_ms = timeout_ms
        self.max_output_bytes = max_output_bytes
        self.kill_grace_ms = kill_grace_ms

    def interpreter(self) -> str:
        return self.python

    def run(
        self, *, repo_path: str | Path, commit: str, run_root: str | Path, entry: str | Path,
        args: list[str], as_of: str, timeout_ms: int | None = None,
    ) -> ProcessResult:
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
    return {
        key: re.sub(r"\{(\w+)\}", lambda match: variables.get(match[1], match[0]), value)
        for key, value in values.items()
    }


def table_names(path: str | Path) -> list[str]:
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
    started = time.monotonic()
    if not Path(path).exists():
        return ProcessResult(1, False, "", f"Database does not exist: {path}", 0)
    connection = sqlite3.connect(path)
    try:
        connection.executescript("BEGIN;\n" + sql + "\n;COMMIT;")
        return ProcessResult(0, False, "", "", round((time.monotonic() - started) * 1000))
    except sqlite3.Error as error:
        connection.rollback()
        return ProcessResult(1, False, "", str(error), round((time.monotonic() - started) * 1000))
    finally:
        connection.close()


def run_flow_round(options: FlowRoundOptions) -> FlowRoundResult:
    plan = options.plan
    options.out_dir.mkdir(parents=True, exist_ok=True)
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
            warn("Captured output was truncated at the configured byte limit")

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
            warn(f"Materialization did not produce {entry.entry['table']}")
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
                raise VibeError(f"Unknown node in plan: {identifier}")
            file, func = parse_call(node.call, f"nodes.{node.id}.call")
            existing = set(table_names(options.db_path))
            missing = [table for table in node.reads if table not in existing]
            kwargs = {
                "db": variables["db"], "inputs": variables["inputs"], "out": variables["out"],
                "as_of": options.as_of, **fill_kwargs(node.args or {}, variables),
            }
            options.log(f"  [{node.id}] {node.call}")
            before = database_digest(options.db_path, node.writes)
            if missing:
                execution = ProcessResult(1, False, "", f"Input tables do not exist: {', '.join(missing)}", 0)
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
                warn(f"Declared writes are unchanged (not automatically a failure): {', '.join(unchanged)}")
            if execution.exit_code != 0 or execution.timed_out or execution.cancelled or missing:
                ok = False
                break
    outputs = database_digest(options.db_path, plan.spec.outputs)
    if ok:
        missing_outputs = [table for table in plan.spec.outputs if table not in outputs]
        if not plan.spec.outputs:
            warn("flow.outputs is empty: two empty result sets are not successful verification")
            ok = False
        elif missing_outputs:
            warn(f"Declared output tables did not appear: {', '.join(missing_outputs)}")
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
    tables: list[str]
    files: list[str]
    identical: bool


def diff_rounds(a: FlowRoundResult, b: FlowRoundResult) -> RoundDiff:
    tables = sorted(
        name for name in a.outputs.keys() | b.outputs.keys()
        if a.outputs.get(name) != b.outputs.get(name)
    )
    left = {f"{item.path}:{item.sha256}" for item in a.files}
    right = {f"{item.path}:{item.sha256}" for item in b.files}
    files = sorted(left ^ right)
    return RoundDiff(tables, files, not tables and not files)


def write_materialize_spec(directory: str | Path, entries: list[MaterializeEntry]) -> Path:
    path = Path(directory) / "materialize.json"
    write_json(path, {"entries": [entry.to_spec() for entry in entries]})
    return path.resolve()
