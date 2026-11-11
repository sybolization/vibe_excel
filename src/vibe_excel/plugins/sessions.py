"""插件：sessions —— 会话生命周期编排，注册 8 条命令：

    setup / run / flow / publish / optimize / status / author / db

它把四件事串起来，但每件事都只通过服务调用，不认识具体实现：
写程序 → agent，执行 → exec，记录 → store，落盘 → workspace。

这一层是「产品语义」所在：草稿提交、数据版本、发布授权都在这里。
它不实现 git / sqlite / 子进程细节，那些分别由 session / runtime / git / process 提供；
命令函数只解析参数并把异常转成退出码。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from vibe_excel import git as git_module
from vibe_excel.contracts import RunId
from vibe_excel.flow import describe_flow, format_flow_warning, plan_flow, suggest_optimizations
from vibe_excel.flow_code import bundled_script, load_flow_at_commit
from vibe_excel.kernel import (
    AgentRequest,
    AgentService,
    AskQuestion,
    AuthorResult,
    CommandContext,
    CommandSpec,
    CommandsService,
    EntrySpec,
    ExecOutcome,
    ExecRequest,
    ExecService,
    OptimizeResult,
    PluginContext,
    RunResult,
    SetupResult,
    StatusView,
    StoreService,
    TelemetryService,
    WorkspacePaths,
    WorkspaceService,
    define_plugin,
    service,
)
from vibe_excel.optimize import (
    assert_same_flow,
    build_apply_prompt,
    build_review_prompt,
    parse_proposal_file,
    select_proposal,
)
from vibe_excel.process import ProcessResult
from vibe_excel.runtime import acquire_lock, restore_database, snapshot_database
from vibe_excel.session import ArtifactInput, Program, Session, SessionStore
from vibe_excel.util import (
    Digest,
    TableDigest,
    VibeError,
    batch_id_of,
    database_digest,
    dir_digests,
    fill_template,
    now,
    sha256_file,
)
from vibe_excel.workflow import (
    FlowRoundOptions,
    FlowRoundResult,
    MaterializeInput,
    PythonNodeInput,
    RoundDiff,
    diff_rounds,
    run_flow_round,
    write_materialize_spec,
)
from vibe_excel.workspace_facts import (
    ProbeDeps,
    WorkspaceFacts,
    probe_workspace,
    render_workspace_facts,
    write_agent_facts_file,
)


@dataclass(frozen=True)
class InputConfig:
    """输入批次：从哪个目录取哪些文件。"""

    dir: str
    files: list[str]


@dataclass(frozen=True)
class SessionsConfig:
    """sessions 插件配置；业务日期、库名、输出目录与输入批次都在加载期定死。"""

    program: str
    # 业务库文件名，落在 <work>/ 下；两轮运行各拿一份拷贝，不共享同一个文件。
    db_file: str
    # 业务日期 YYYY-MM-DD，必须显式钉住；不要把系统日期当业务日期。
    as_of: str
    # 输出目录名，相对 <work>/；双跑一致后会整体替换它。
    out_dir: str
    # 流程声明文件，相对代码仓根。
    flow_path: str
    # 是否允许模型向人提问；默认开，只有显式 false 才关闭。
    ask_enabled: bool
    # 单次 optimize（review 或 apply）允许模型运行多久（毫秒）。
    optimize_timeout_ms: int
    inputs: InputConfig | None = None


@dataclass(frozen=True)
class Resolved:
    """一次命令执行所需的三个坐标；`paths` 已经过 ensure，目录确定存在。"""

    program: Program
    session: Session
    paths: WorkspacePaths


@dataclass(frozen=True)
class Batch:
    """输入批次：内容哈希 id、批次目录，以及其中拷贝好的 files 子目录。"""

    id: str
    dir: Path
    files_dir: Path


def _stamp() -> str:
    """生成可排序的运行目录名；不依赖系统时区的准确性。"""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(":", "-").replace(".", "-")


def _fail(message: str) -> NoReturn:
    """统一失败出口：抛 VibeError，由命令边界转成退出码。"""

    raise VibeError(message)


def _record_of(value: object) -> object:
    """把 dataclass / 普通对象转成可 JSON 序列化的结构。"""

    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return value
    attributes = getattr(value, "__dict__", None)
    return attributes if isinstance(attributes, dict) else str(value)


def _parse_config(raw: object) -> SessionsConfig:
    """校验 sessions 配置；asOf 必须是 YYYY-MM-DD，配置了 inputs 时目录必须真的存在。"""

    if not isinstance(raw, dict):
        raise VibeError("sessions 插件配置必须是对象")
    program = raw.get("program")
    if not isinstance(program, str) or not program.strip():
        raise VibeError("sessions 插件需要 config.program")
    as_of = raw.get("asOf")
    if not isinstance(as_of, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        raise VibeError("sessions 插件需要 config.asOf（YYYY-MM-DD，业务日期必须显式钉住）")

    def text_or(key: str, fallback: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) and value else fallback

    timeout_raw = raw.get("optimizeTimeoutMs", 10 * 60 * 1000)
    # 非法或非正的超时退回 10 分钟，不让一个数字把 optimize 整体禁用。
    timeout = (
        int(timeout_raw)
        if isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool)
        and float(timeout_raw) > 0
        else 10 * 60 * 1000
    )
    inputs: InputConfig | None = None
    if "inputs" in raw:
        block = raw.get("inputs")
        if not isinstance(block, dict):
            raise VibeError("sessions.inputs 必须是对象")
        directory = block.get("dir")
        files = block.get("files")
        if not isinstance(directory, str) or not directory.strip():
            raise VibeError("sessions.inputs.dir 必须是非空字符串")
        if (
            not isinstance(files, list)
            or not files
            or any(not isinstance(item, str) for item in files)
        ):
            raise VibeError("sessions.inputs.files 必须是非空字符串数组")
        absolute = Path(directory).expanduser()
        # 输入目录在加载期绝对化并确认存在：路径之后会写进批次清单，越早失败越好。
        absolute = absolute if absolute.is_absolute() else Path.cwd() / absolute
        if not absolute.exists():
            raise VibeError(f"sessions.inputs.dir 不存在：{absolute}")
        inputs = InputConfig(str(absolute.resolve()), list(files))
    return SessionsConfig(
        program=program,
        db_file=text_or("dbFile", "hr.db"),
        as_of=as_of,
        out_dir=text_or("outDir", "out"),
        flow_path=text_or("flowPath", "flow.json"),
        # askEnabled 默认开：只有显式 false 才关闭，避免配置漏写导致模型无法提问。
        ask_enabled=raw.get("askEnabled") is not False,
        optimize_timeout_ms=timeout,
        inputs=inputs,
    )


def current_batch(inputs_root: Path) -> Batch:
    """输入批次目录必须恰好一个；多了说明 setup 被跑乱了。"""

    if not inputs_root.exists():
        _fail(f"输入批次目录不存在：{inputs_root}（先跑 setup）")
    dirs = sorted(
        (entry for entry in inputs_root.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name,
    )
    # 批次目录名是内容哈希，排序稳定；多于一个说明 setup 被反复跑过或有人手工塞了目录。
    if len(dirs) != 1:
        _fail(f"输入批次目录应恰好 1 个，实际 {len(dirs)} 个：先跑 setup")
    return Batch(dirs[0].name, dirs[0], dirs[0] / "files")


def latest_proposal_path(runs_root: Path) -> Path:
    """最近一次 optimize 评审留下的建议文件（apply 默认读它）。"""

    if not runs_root.exists():
        _fail(f"还没有跑过 optimize：{runs_root} 不存在")
    names = sorted(
        entry.name
        for entry in runs_root.iterdir()
        if entry.is_dir() and entry.name.startswith("optimize-review-")
    )
    # 目录名带毫秒时间戳，字符串排序后最后一个就是最近一次评审。
    if not names:
        _fail(f"还没有跑过 optimize：{runs_root} 下没有 optimize-review-* 目录")
    return runs_root / names[-1] / "optimize-proposal.json"


def _process_of(outcome: ExecOutcome) -> ProcessResult:
    """把 kernel 的 ExecOutcome 收敛成 workflow 需要的 ProcessResult（去掉路径类字段）。"""

    return ProcessResult(
        outcome.exit_code, outcome.timed_out, outcome.stdout, outcome.stderr, outcome.elapsed_ms
    )


def _digest_dict(item: Digest) -> dict[str, object]:
    """把文件摘要渲染成 run.json 的字段形状。"""

    return {"path": item.path, "sha256": item.sha256, "bytes": item.bytes}


def _table_dict(item: TableDigest) -> dict[str, object]:
    """把表摘要渲染成 run.json 的字段形状。"""

    return {"rows": item.rows, "sha256": item.sha256}


def _round_record(result: FlowRoundResult | None) -> dict[str, object] | None:
    """把运行结果写成 run.json 里的 camelCase 形状（前端与历史记录都认这个契约）。"""

    if result is None:
        return None
    # run.json 的键名是前端与历史记录的契约，必须 camelCase；源 dataclass 是 snake_case。
    materialized = result.materialize
    entries = [
        {
            "entry": record.entry,
            "produced": record.produced,
            "digest": None if record.digest is None else _table_dict(record.digest),
        }
        for record in materialized.entries
    ]
    nodes = [
        {
            "id": node.id,
            "call": node.call,
            "file": node.file,
            "func": node.func,
            "kwargs": node.kwargs,
            "exitCode": node.exit_code,
            "timedOut": node.timed_out,
            "elapsedMs": node.elapsed_ms,
            "stdout": node.stdout,
            "stderr": node.stderr,
            "changed": node.changed,
            "unchanged": node.unchanged,
            "missingReads": node.missing_reads,
            "truncated": node.truncated,
            "cancelled": node.cancelled,
        }
        for node in result.nodes
    ]
    return {
        "ok": result.ok,
        "materialize": {
            "exitCode": materialized.exit_code,
            "timedOut": materialized.timed_out,
            "elapsedMs": materialized.elapsed_ms,
            "stdout": materialized.stdout,
            "stderr": materialized.stderr,
            "entries": entries,
            "truncated": materialized.truncated,
            "cancelled": materialized.cancelled,
        },
        "nodes": nodes,
        "outputs": {name: _table_dict(item) for name, item in result.outputs.items()},
        "files": [_digest_dict(item) for item in result.files],
        "warnings": list(result.warnings),
    }


def _diff_record(diff: RoundDiff) -> dict[str, object]:
    """把双跑差异渲染成 run.json 的 diff 段。"""

    return {"tables": diff.tables, "files": diff.files, "identical": diff.identical}


class SessionsFeature:
    """会话编排服务与命令注册。

    持有各服务引用，不自己实现 git / sqlite；author / run / optimize 的 apply 模式会加会话锁，
    setup / status / publish 与 optimize 的 review 模式不加。所有方法都假设 setup 至少跑过一次。
    """

    def __init__(
        self,
        config: SessionsConfig,
        store: StoreService,
        workspace: WorkspaceService,
        exec_service: ExecService,
        agent: AgentService,
        telemetry: TelemetryService,
        commands: CommandsService,
    ) -> None:
        """依赖全部由宿主注入；本类不做服务定位，也不延迟初始化。"""

        self._config = config
        self._store = store
        self._workspace = workspace
        self._exec = exec_service
        self._agent = agent
        self._telemetry = telemetry
        self._commands = commands

    # ---- 基础 ----

    def _db(self) -> SessionStore:
        """取控制面库；store 服务的 db() 可能是懒打开的，这里只做类型收敛。"""

        return cast(SessionStore, self._store.db())

    def _db_path(self, paths: WorkspacePaths) -> Path:
        """业务库固定在 <work>/<dbFile>；每一轮运行各拿一份拷贝，不共享。"""

        return Path(paths.work) / self._config.db_file

    def resolve_context(self) -> Resolved:
        """找同名程序与最新活跃会话，并确保其工作区目录存在；两者缺一都直接失败。"""

        db = self._db()
        program = next(
            (candidate for candidate in db.list_programs() if candidate.name == self._config.program),
            None,
        )
        if program is None:
            _fail(f"程序仓库不存在：{self._config.program}（先跑 setup）")
        active = sorted(
            (
                candidate
                for candidate in db.list_sessions(program.id)
                if candidate.status == "active"
            ),
            key=lambda candidate: candidate.created_at,
        )
        if not active:
            _fail("没有活跃会话：先跑 setup")
        # 活跃会话可能不止一个（历史遗留）：取创建时间最晚的那个。
        session = active[-1]
        return Resolved(program, session, self._workspace.ensure(str(session.id)))

    def _facts(self, program: Program, session: Session, paths: WorkspacePaths) -> WorkspaceFacts:
        """组装探测坐标并调用 probe_workspace；输入批次还没建时退化为 inputs 根目录。"""

        try:
            inputs_dir = str(current_batch(Path(paths.work) / "inputs").files_dir)
        except VibeError:
            inputs_dir = str(Path(paths.work) / "inputs")
        return probe_workspace(
            paths.root,
            ProbeDeps(
                program=program.name,
                session_id=str(session.id),
                work=paths.work,
                code=paths.code,
                agent=paths.agent,
                python=self._exec.interpreter(),
                db_path=self._db_path(paths),
                flow_path=Path(paths.code) / self._config.flow_path,
                inputs_dir=inputs_dir,
                out_dir=Path(paths.work) / self._config.out_dir,
                as_of=self._config.as_of,
            ),
        )

    def _make_ask_user(self, trace_dir: Path) -> Callable[[AskQuestion], str | None]:
        """构造提问回调：打印到终端并追加写到 questions.jsonl，返回 None 表示未确认。"""

        questions_path = trace_dir / "questions.jsonl"

        def handle(question: AskQuestion) -> str | None:
            print(f"\n[模型提问] {question.question}")
            for index, option in enumerate(question.options):
                print(f"  {index + 1}) {option}")

            def record(answer: str | None, reason: str | None = None) -> None:
                entry: dict[str, object] = {
                    "at": now(),
                    "id": question.id,
                    "question": question.question,
                    "options": list(question.options),
                    "answer": answer,
                }
                if reason is not None:
                    entry["reason"] = reason
                with questions_path.open("a", encoding="utf-8", newline="\n") as output:
                    output.write(json.dumps(entry, ensure_ascii=False) + "\n")

            if not sys.stdin.isatty():
                # 非交互（CI / 管道）下不读 stdin：input() 会立刻 EOF，记录原因比抛异常有用。
                print("  非交互环境（stdin 不是终端）：记为未确认，模型按保守口径继续")
                record(None, "non-interactive")
                return None
            try:
                answer = input("你的回答 > ").strip()
            except (EOFError, KeyboardInterrupt):
                record(None, "cancelled")
                return None
            record(answer if answer else None)
            return answer if answer else None

        return handle

    # ---- 运行 ----

    def run_flow(self, as_of: str | None = None) -> RunResult:
        """加会话锁后执行双跑；锁默认不等待，被占用时立刻失败。"""

        resolved = self.resolve_context()
        return cast(
            RunResult,
            self._workspace.with_lock(
                str(resolved.session.id),
                lambda: self._run_flow_locked(
                    resolved, self._config.as_of if as_of is None else as_of
                ),
            ),
        )

    def _run_flow_locked(self, resolved: Resolved, as_of: str) -> RunResult:
        """双跑主流程：定输入 → 两轮独立目录 → 比对 → 只有一致才提升产物与登记数据版本。"""

        program, session, paths = resolved.program, resolved.session, resolved.paths
        db = self._db()
        batch = current_batch(Path(paths.work) / "inputs")
        revisions = db.list_data_revisions(session.id)
        if not revisions:
            _fail("没有数据版本：先跑 setup")
        # 数据版本按创建时间倒序，[0] 就是最近一次，也就是本轮输入。
        input_revision = revisions[0]
        commit = db.draft_head(session.id)
        if commit is None:
            _fail("草稿分支没有提交：先跑 author")
        # 先钉住 commit：后续读取都基于这一版字节，避免中途草稿变化造成「假一致」。
        flow = load_flow_at_commit(
            program.repo_path, str(commit), self._config.flow_path, python=self._exec.interpreter()
        )
        plan = plan_flow(flow)
        run = db.begin_run(
            session.id, input_batch_id=batch.id, data_revision_id=input_revision.id
        )
        run_dir = Path(paths.runs) / str(run.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.now(UTC)
        print(
            f"[flow {flow.flow}@{plan.digest}] code={str(commit)[:8]} "
            f"data={str(input_revision.id)[:8]} batch={batch.id}"
        )
        for line in describe_flow(plan):
            print(f"  {line}")
        for hint in suggest_optimizations(plan):
            print(f"  ~ 可优化（不影响本次运行）：{hint.note}")

        results: dict[str, FlowRoundResult] = {}
        # 双跑：a / b 两个独立目录各拿一份库拷贝；共享任何可变状态都会让「一致」变成假证据。
        for label in ("a", "b"):
            directory = run_dir / label
            directory.mkdir(parents=True, exist_ok=True)
            # 业务库文件名由流程声明（flow.db）给出，不是模块常量。
            db_path = directory / flow.db
            shutil.copyfile(input_revision.path, db_path)
            out_dir = directory / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            print(f"  ── 第 {label} 轮 ──")

            # 用默认参数把本轮目录钉在定义时：闭包变量下一轮会被覆盖，不能靠闭包。
            def run_materialize(
                request: MaterializeInput,
                *,
                target: Path = db_path,
                root: Path = directory,
            ) -> ProcessResult:
                return self._materialize(request, target, program, str(commit), root, as_of)

            def run_python(
                request: PythonNodeInput,
                *,
                root: Path = directory,
            ) -> ProcessResult:
                return self._run_node(request, program, str(commit), root, as_of)

            results[label] = run_flow_round(
                FlowRoundOptions(
                    plan=plan,
                    db_path=db_path,
                    inputs_dir=batch.files_dir,
                    out_dir=out_dir,
                    as_of=as_of,
                    run_materialize=run_materialize,
                    run_python=run_python,
                    read_file=lambda path: git_module.read_file_at_commit(
                        program.repo_path, str(commit), path
                    ),
                    log=print,
                )
            )
            if not results[label].ok:
                print(f"  第 {label} 轮失败，不再跑第二轮（在失败的结果上做比较没有意义）")
                break

        first = results.get("a")
        second = results.get("b")
        # 只有两轮都完整跑完才有可比性；缺一轮直接判不一致。
        diff = (
            diff_rounds(first, second)
            if first is not None and second is not None
            else RoundDiff([], [], False)
        )
        succeeded = (
            first is not None
            and second is not None
            and first.ok
            and second.ok
            and diff.identical
        )
        elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

        out_revision_id: str | None = None
        outputs: list[Digest] = []
        if succeeded and first is not None:
            # 整体替换输出目录：先删再拷，避免上一轮的残留文件混进新产物。
            out_target = Path(paths.work) / self._config.out_dir
            shutil.rmtree(out_target, ignore_errors=True)
            shutil.copytree(run_dir / "a" / "out", out_target)
            # 用 a 轮的库覆盖工作区业务库：b 轮只是证据，产物以 a 为准。
            restore_database(run_dir / "a" / flow.db, self._db_path(paths))
            outputs = dir_digests(out_target)
            # 业务库也登记为产物：只看 out/ 目录无法证明表内容一致。
            outputs.append(
                Digest(flow.db, sha256_file(self._db_path(paths)), self._db_path(paths).stat().st_size)
            )
            # 快照并登记新数据版本：下一次 run 会把它当输入，形成版本链。
            snapshot = snapshot_database(self._db_path(paths), paths.data_revisions)
            revision = db.register_data_revision(
                program_id=program.id,
                session_id=session.id,
                sha256=snapshot.sha256,
                path=snapshot.path,
                bytes=snapshot.bytes,
                parent_id=input_revision.id,
                note=f"{flow.flow}@{plan.digest} 产出",
            )
            out_revision_id = str(revision.id)
            print(f"  数据版本 {out_revision_id[:8]}（snapshot {snapshot.sha256[:10]}）")
        else:
            print("  两次重跑不一致或失败：不提升产物、不登记数据版本（这是事实，不要掩盖）")
            if diff.tables:
                print(f"    不一致的表：{', '.join(diff.tables)}")
            if diff.files:
                print(f"    不一致的文件：{', '.join(diff.files)}")

        status = "succeeded" if succeeded else "failed"
        # 不论成败都要 finish_run：停在 running 的 run 会让 status / 恢复视图失真。
        db.finish_run(
            run.id,
            status=cast(Any, status),
            exit_code=0 if succeeded else 1,
            note=f"flow={flow.flow}@{plan.digest} identical={str(diff.identical).lower()}",
        )
        for output in outputs:
            db.add_artifact(
                run.id,
                ArtifactInput(kind="flow", path=output.path, sha256=output.sha256, bytes=output.bytes),
            )

        record: dict[str, object] = {
            "kind": "flow",
            "flow": flow.flow,
            "flowDigest": plan.digest,
            "codeCommit": str(commit),
            "graph": {
                "source": "code" if flow.entry is not None else "json",
                "entry": flow.entry,
                "warnings": [
                    {
                        "code": warning.code,
                        "message": warning.message,
                        "line": warning.line,
                    }
                    for warning in flow.warnings
                ],
                "nodes": [
                    {
                        "id": node.id,
                        "name": node.name,
                        "call": node.call,
                        "reads": node.reads,
                        "writes": node.writes,
                        "args": node.args,
                        "timeoutMs": node.timeout_ms,
                        "title": node.title,
                        "steps": [
                            {"line": step.line, "kind": step.kind, "of": step.of, "label": step.label}
                            for step in node.steps
                        ],
                    }
                    for node in plan.spec.nodes
                ],
            },
            "batchId": batch.id,
            "inputRevisionId": str(input_revision.id),
            "asOf": as_of,
            "status": status,
            "identical": diff.identical,
            "diff": _diff_record(diff),
            "rounds": {
                "a": _round_record(results.get("a")),
                "b": _round_record(results.get("b")),
            },
            "outRevisionId": out_revision_id,
            "outputs": [_digest_dict(item) for item in outputs],
            "elapsedMs": elapsed_ms,
        }
        # publish 以 run.json 为主证据；只有记录里缺 codeCommit 时才退回数据库里的 run 记录。
        (run_dir / "run.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"  运行记录 {run_dir / 'run.json'}")
        return RunResult(
            run_id=str(run.id),
            status=status,
            exit_code=0 if succeeded else 1,
            identical=diff.identical,
            flow_digest=plan.digest,
            outputs=[_digest_dict(item) for item in outputs],
            out_revision_id=out_revision_id,
            run_dir=str(run_dir),
        )

    def _materialize(
        self,
        request: MaterializeInput,
        db_path: Path,
        program: Program,
        commit: str,
        directory: Path,
        as_of: str,
    ) -> ProcessResult:
        """把物化请求写成 spec，再在 detached worktree 里跑内置的 materialize.py。"""

        # spec 落在运行目录而不是 worktree：worktree 会被清理，spec 要留着可复查。
        spec_path = write_materialize_spec(directory / "materialize", request.entries)
        outcome = self._exec.run(
            ExecRequest(
                repo_path=program.repo_path,
                commit=commit,
                run_root=str(directory / "clean-materialize"),
                entry=EntrySpec(
                    entry=str(bundled_script("materialize.py")),
                    args=[
                        "--db", str(db_path),
                        "--inputs", str(request.inputs_dir),
                        "--spec", str(spec_path),
                        "--repo", ".",
                    ],
                ),
                as_of=as_of,
            )
        )
        return _process_of(outcome)

    def _run_node(
        self,
        request: PythonNodeInput,
        program: Program,
        commit: str,
        directory: Path,
        as_of: str,
    ) -> ProcessResult:
        """在 detached worktree 里跑 node_runner.py：file:func 与 kwargs 由流程声明，超时由节点给。"""

        arguments = [
            request.file, request.func,
            *[
                item
                for key, value in request.kwargs.items()
                # kwargs 的键在 CLI 上用连字符形式传（如 as_of → --as-of）。
                for item in (f"--{key.replace('_', '-')}", value)
            ],
        ]
        outcome = self._exec.run(
            ExecRequest(
                repo_path=program.repo_path,
                commit=commit,
                run_root=str(directory / "clean" / request.node.id),
                entry=EntrySpec(entry=str(bundled_script("node_runner.py")), args=arguments),
                timeout_ms=request.timeout_ms,
                as_of=as_of,
            )
        )
        return _process_of(outcome)

    # ---- 发布 ----

    def publish(self, run_id: str | None = None) -> dict[str, object]:
        """发布前重算三道闸：run.json 存在且合法、双跑一致、证据 commit 等于当前草稿 HEAD。"""

        resolved = self.resolve_context()
        db = self._db()
        runs = db.list_runs(resolved.session.id)
        run = db.get_run(RunId(run_id)) if run_id else (runs[0] if runs else None)
        # 边界：get_run 是全局查询，不校验归属；跨会话传入的 run_id 通常会在下一步
        # 因为当前会话的 runs 目录里找不到 run.json 而失败。
        if run is None:
            _fail("没有可发布的运行记录：先跑 run")
        record_path = Path(resolved.paths.runs) / str(run.id) / "run.json"
        # 以磁盘上的 run.json 为准：数据库里的 run 状态只是索引，不足以证明双跑发生过。
        if not record_path.exists():
            _fail(f"运行记录缺失：{record_path}（不能发布一条没有记录在案的运行）")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            _fail(f"运行记录不是合法 JSON：{record_path}：{error}")
        if not isinstance(record, dict):
            _fail(f"运行记录不是对象：{record_path}")
        print(
            f"运行 {str(run.id)[:8]}  flow@{record.get('flowDigest', '?')}  "
            f"两次一致={record.get('identical')}"
        )
        if record.get("status") != "succeeded" or record.get("identical") is not True:
            _fail(
                f"这次运行没有「两次一致」的证据（status={record.get('status', '?')} "
                f"identical={record.get('identical')}）：拒绝发布"
            )
        head = db.draft_head(resolved.session.id)
        if head is None:
            _fail("草稿分支没有提交：没有可发布的内容")
        evidence_commit = record.get("codeCommit") or run.code_commit
        if not evidence_commit:
            _fail("这次运行没有记下代码版本（codeCommit）：无法确认发布的是被验证过的字节，拒绝发布")
        if str(evidence_commit) != str(head):
            # 证据必须绑定当前 HEAD：草稿又提交过，这次双跑就不再代表现在的代码。
            _fail(
                f"证据已经过期：运行验证的是 {str(evidence_commit)[:8]}，"
                f"草稿现在是 {str(head)[:8]}。{str(head)[:8]} 上从没跑过双跑，"
                "拒绝发布（先在这个提交上重新 run）。"
            )
        result = db.publish(
            resolved.session.id,
            # 结论显式传入；db.publish 自己还会再校验分支领先、基线祖先与主工作树干净。
            clean_run_passed=record.get("identical") is True and str(evidence_commit) == str(head),
            evidence=(
                f"flow@{record.get('flowDigest')} run={run.id} identical=true "
                f"code={str(evidence_commit)[:8]}"
            ),
            expected_commit=str(head),
        )
        return {"commit": str(result.commit), "note": result.record.note}

    # ---- 状态 ----

    def status(self) -> StatusView:
        """取恢复视图并拼成 CLI 可打印的形状；键名沿用 camelCase 的对外契约。"""

        resolved = self.resolve_context()
        db = self._db()
        view = db.recover_session(resolved.session.id)
        node = view.restorable_node
        return StatusView(
            program=view.program,
            session=view.session,
            runs=cast(list[object], view.runs),
            restorable=None
            if node is None
            else {
                "codeCommit": str(node.code_commit),
                "dataRevisionId": str(node.data_revision_id),
                "inputBatchId": node.input_batch_id,
                "runId": str(node.run_id),
            },
            publishes=cast(
                list[dict[str, object]],
                [
                    {"commitSha": item.commit_sha, "at": item.at, "note": item.note}
                    for item in db.list_publishes(resolved.session.id)
                ],
            ),
            revisions=cast(
                list[dict[str, object]],
                [
                    {
                        "id": str(item.id),
                        "sha256": item.sha256,
                        "bytes": item.bytes,
                        "note": item.note,
                    }
                    for item in view.data_revisions
                ],
            ),
        )

    # ---- 授权与优化 ----

    def author(self, prompt_file: str, label: str, timeout_ms: int | None = None) -> AuthorResult:
        """让模型在可写工作区写程序：刷事实文件 → 渲染提示词 → 起事件流 → 加锁跑 agent → 提交草稿。

        解析不出流程时只打印诊断，不拦流程：真正的判定在 run 的双跑。
        """

        resolved = self.resolve_context()
        program, session, paths = resolved.program, resolved.session, resolved.paths
        prompt_path = Path(prompt_file).expanduser()
        # 相对提示词路径按 cwd 解析：--prompt 是调用者给的，不是清单里的配置。
        prompt_path = prompt_path if prompt_path.is_absolute() else Path.cwd() / prompt_path
        if not prompt_path.exists():
            _fail(f"提示词文件不存在：{prompt_path}")
        batch = current_batch(Path(paths.work) / "inputs")
        facts = self._facts(program, session, paths)
        environment = render_workspace_facts(facts)
        # 每次 author 前重写事实文件：路径失效的旧事实比没有事实更危险。
        agents_path = write_agent_facts_file(paths.agent, facts)
        # 模板变量同时给大小写两套（{{PYTHON}} 与 {{code}}）：历史提示词两种写法都有。
        variables = {
            "PYTHON": self._exec.interpreter().replace("\\", "/"),
            "code": str(paths.code),
            "inputs": str(batch.files_dir),
            "db": str(self._db_path(paths)),
            "out": str(Path(paths.work) / self._config.out_dir),
            "asOf": self._config.as_of,
            "WORKSPACE": environment,
            "CODE": str(paths.code),
            "INPUTS": str(batch.files_dir),
            "DB": str(self._db_path(paths)),
            "OUT": str(Path(paths.work) / self._config.out_dir),
            "ASOF": self._config.as_of,
            "FLOW": self._config.flow_path,
        }
        prompt = fill_template(prompt_path.read_text(encoding="utf-8"), variables)
        trace_dir = Path(paths.runs) / f"author-{label}-{_stamp()}"
        sink = self._telemetry.begin(str(trace_dir))
        ask_user = self._make_ask_user(trace_dir)
        # 用 acquire_lock 而不是 with_lock：要在 finally 里同时释放锁与事件流。
        lock = acquire_lock(self._workspace.lock_path(str(session.id)))
        try:
            print(f"[author:{label}] cwd={paths.code}")
            print(f"[author:{label}] 工作区事实写进 {agents_path}")
            outcome = self._agent.run(
                AgentRequest(
                    cwd=str(paths.code),
                    agent_dir=str(paths.agent),
                    session_dir=str(paths.pi_sessions),
                    prompt=prompt,
                    timeout_ms=timeout_ms,
                    on_question=ask_user if self._config.ask_enabled else None,
                    on_event=sink.event,
                )
            )
            for session_file in outcome.session_files:
                name = Path(session_file).stem
                self._agent.export_html(session_file, str(trace_dir / f"{name}.html"))
            # 即使模型会话失败也提交：写出的半成品要留痕，能不能跑由 run 判定。
            commit = self._db().commit_draft(session.id, f"author({label}): 由模型写出的程序")
            diagnostic: str | None = None
            if commit is not None:
                try:
                    parsed = load_flow_at_commit(
                        program.repo_path,
                        str(commit),
                        self._config.flow_path,
                        python=self._exec.interpreter(),
                    )
                    plan = plan_flow(parsed)
                    source = f"代码:{parsed.entry}" if parsed.entry is not None else "flow.json"
                    print(
                        f"  流程 flow@{plan.digest}  节点 {len(plan.spec.nodes)}  图={source}"
                    )
                    for warning in parsed.warnings:
                        print(f"  ! {format_flow_warning(parsed.entry, warning)}")
                except VibeError as error:
                    diagnostic = str(error)
                    print(f"  ⚠ 解析不出流程：{diagnostic}")
                    print("    这是提示，不是通过：run 会再失败一次。")
            (trace_dir / "author.json").write_text(
                json.dumps(
                    {
                        # author.json 记下这次会话的全部事实（含失败），供事后复盘。
                        "label": label,
                        "promptFile": str(prompt_path),
                        "flowDiagnostic": diagnostic,
                        "ok": outcome.ok,
                        "error": outcome.error,
                        "elapsedMs": outcome.elapsed_ms,
                        "eventCount": outcome.event_count,
                        "toolNames": outcome.tool_names,
                        "toolCallCount": outcome.tool_call_count,
                        "sessionFiles": outcome.session_files,
                        "stats": outcome.stats,
                        "commit": None if commit is None else str(commit),
                        "traceDir": str(trace_dir),
                    },
                    ensure_ascii=False, indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(
                f"耗时 {outcome.elapsed_ms / 1000:.1f}s，事件 {outcome.event_count} 条，"
                f"工具 {','.join(outcome.tool_names) or '无'}"
            )
            if commit is None:
                print("草稿无改动：模型没有写出任何文件（这是事实，不是成功）")
            else:
                print(f"草稿提交 {commit}")
            if not outcome.ok:
                print(f"模型会话失败：{outcome.error or '未知原因'}")
            return AuthorResult(
                commit=None if commit is None else str(commit),
                elapsed_ms=outcome.elapsed_ms,
                event_count=outcome.event_count,
                tool_names=list(outcome.tool_names),
                trace_dir=str(trace_dir),
            )
        finally:
            # 先关 sink（落盘 timeline.txt）再放锁：锁一放，另一个进程就可能开始用同一会话。
            sink.close()
            lock.release()

    def optimize(
        self, apply_id: str | None = None, source: str | None = None,
        timeout_ms: int | None = None,
    ) -> OptimizeResult:
        """两条路径：apply 只实现被人批准的那一条；review 在只读 worktree 里产出建议文件。"""

        resolved = self.resolve_context()
        program, session, paths = resolved.program, resolved.session, resolved.paths
        db = self._db()
        commit = db.draft_head(session.id)
        if commit is None:
            _fail("草稿分支没有提交：先跑 author")
        flow = load_flow_at_commit(
            program.repo_path, str(commit), self._config.flow_path, python=self._exec.interpreter()
        )
        plan = plan_flow(flow)
        flow_json = git_module.read_file_at_commit(program.repo_path, str(commit), self._config.flow_path)
        plan_lines = describe_flow(plan, steps=True)
        facts = self._facts(program, session, paths)
        environment = render_workspace_facts(facts)
        write_agent_facts_file(paths.agent, facts)

        if apply_id:
            # 优先级：绝对路径 > cwd 相对路径 > 最近一次 review 留下的建议文件。
            proposal_path = (
                Path(source).expanduser()
                if source and Path(source).is_absolute()
                else (Path.cwd() / source if source else latest_proposal_path(Path(paths.runs)))
            )
            if not proposal_path.exists():
                _fail(f"找不到建议文件：{proposal_path}（先跑一次 optimize）")
            proposal_file = parse_proposal_file(proposal_path.read_text(encoding="utf-8"))
            assert_same_flow(proposal_file, plan.digest)
            proposal = select_proposal(proposal_file, apply_id)
            print(f"批准的建议：{proposal.id} {proposal.title}")
            print(f"  风险：{proposal.risk}")
            print(f"  验证：{proposal.verify}")
            trace_dir = Path(paths.runs) / f"optimize-apply-{_stamp()}"
            sink = self._telemetry.begin(str(trace_dir))
            ask_user = self._make_ask_user(trace_dir)
            lock = acquire_lock(self._workspace.lock_path(str(session.id)))
            try:
                prompt = build_apply_prompt(
                    proposal=proposal,
                    approved_at=now(),
                    code_root=str(paths.code),
                    flow_json=flow_json,
                    plan_lines=plan_lines,
                    environment=environment,
                )
                outcome = self._agent.run(
                    AgentRequest(
                        cwd=str(paths.code),
                        agent_dir=str(paths.agent),
                        session_dir=str(paths.pi_sessions),
                        prompt=prompt,
                        timeout_ms=timeout_ms or self._config.optimize_timeout_ms,
                        on_question=ask_user if self._config.ask_enabled else None,
                        on_event=sink.event,
                    )
                )
                commit_after = self._db().commit_draft(
                    session.id, f"optimize: 应用已批准的建议 {proposal.id}"
                )
                # 模型没改动时 commit 为 None；不要把它当成 apply 成功。
                (trace_dir / "optimize.json").write_text(
                    json.dumps(
                        {
                            "mode": "apply",
                            "proposal": _record_of(proposal),
                            "proposalPath": str(proposal_path),
                            "outcome": _record_of(outcome),
                            "commit": None if commit_after is None else str(commit_after),
                            "traceDir": str(trace_dir),
                        },
                        ensure_ascii=False, indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                if commit_after is None:
                    print("没有改动：模型没有实现这条建议（这是事实，不是成功）")
                else:
                    print(f"草稿提交 {commit_after}")
                return OptimizeResult(
                    mode="apply",
                    trace_dir=str(trace_dir),
                    proposal_path=str(proposal_path),
                    proposals=[proposal],
                    commit=None if commit_after is None else str(commit_after),
                )
            finally:
                sink.close()
                lock.release()

        trace_dir = Path(paths.runs) / f"optimize-review-{_stamp()}"
        proposal_path = trace_dir / "optimize-proposal.json"
        trace_dir.mkdir(parents=True, exist_ok=True)
        worktree = trace_dir / "clean"
        sink = self._telemetry.begin(str(trace_dir))
        ask_user = self._make_ask_user(trace_dir)
        print(f"[optimize] flow@{plan.digest}  只读评审，不碰草稿")
        for hint in suggest_optimizations(plan):
            print(f"  ~ {hint.note}")
        try:
            git_module.git(
                program.repo_path, ["worktree", "add", "--detach", str(worktree), str(commit)]
            )
            # 评审用 detached worktree：只读，模型改坏了也污染不到草稿。
            prompt = build_review_prompt(
                flow_json=flow_json,
                flow_digest=plan.digest,
                plan_lines=plan_lines,
                hints=cast(list[object], suggest_optimizations(plan)),
                proposal_path=str(proposal_path),
                code_root=str(worktree),
                environment=environment,
            )
            outcome = self._agent.run(
                AgentRequest(
                    cwd=str(worktree),
                    agent_dir=str(paths.agent),
                    session_dir=str(paths.pi_sessions),
                    prompt=prompt,
                    timeout_ms=timeout_ms or self._config.optimize_timeout_ms,
                    on_question=ask_user if self._config.ask_enabled else None,
                    on_event=sink.event,
                )
            )
            if not proposal_path.exists():
                # 没写建议文件就是失败：不能把「没写」当成「没有可优化项」。
                _fail(f"模型没有写建议文件（这是事实，不是「没有可优化的地方」）：{proposal_path}")
            # 模型可能改动了 flow 字节；建议必须绑定当前 digest 才有效。
            proposal_file = parse_proposal_file(proposal_path.read_text(encoding="utf-8"))
            assert_same_flow(proposal_file, plan.digest)
            (trace_dir / "optimize.json").write_text(
                json.dumps(
                    {
                        "mode": "review",
                        "flowDigest": plan.digest,
                        "outcome": _record_of(outcome),
                        "file": _record_of(proposal_file),
                        "traceDir": str(trace_dir),
                    },
                    ensure_ascii=False, indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"建议 {len(proposal_file.proposals)} 条（{proposal_path}）：")
            for item in proposal_file.proposals:
                print(f"  {item.id}  {item.title}")
                print(f"      省：节点 {item.saves.nodes} / 表 {item.saves.tables}   风险：{item.risk}")
                print(f"      验证：{item.verify}")
            if proposal_file.proposals:
                print("要改哪一条，请显式批准：optimize --apply=<id>")
            else:
                # 防御性分支：parse_proposal_file 会先拒绝空数组，正常流程走不到这里。
                print("模型认为没有更短的等价写法（这也是结论）。")
            return OptimizeResult(
                mode="review",
                trace_dir=str(trace_dir),
                proposal_path=str(proposal_path),
                proposals=list(proposal_file.proposals),
                commit=None,
            )
        finally:
            sink.close()
            try:
                git_module.remove_worktree(program.repo_path, worktree)
            except VibeError:
                # 清理失败不掩盖主错误：忽略异常，worktree 留给下次手工清理。
                pass

    # ---- 初始化 ----

    def setup(self) -> SetupResult:
        """幂等初始化：程序 / 会话复用，输入批次按内容哈希去重，空库基线登记为数据版本。"""

        db = self._db()
        program = next(
            (candidate for candidate in db.list_programs() if candidate.name == self._config.program),
            None,
        )
        program_reused = program is not None
        if program is None:
            program = db.create_program(self._config.program)
        session = next(
            (
                candidate
                for candidate in db.list_sessions(program.id)
                if candidate.status == "active"
            ),
            None,
        )
        session_reused = session is not None
        if session is None:
            session = db.create_session(program.id, name=f"real-data-{self._config.as_of}")
        paths = self._workspace.ensure(str(session.id))

        if self._config.inputs is None:
            _fail("sessions 插件没有配置 inputs（把要录的文件名写进清单）")
        entries: list[dict[str, object]] = []
        for name in self._config.inputs.files:
            source = Path(self._config.inputs.dir) / name
            if not source.exists():
                _fail(f"缺少输入文件：{source}")
            entries.append(
                {"name": name, "sha256": sha256_file(source), "bytes": source.stat().st_size}
            )
        entries.sort(key=lambda item: str(item["name"]))
        # 批次 id 由文件内容哈希得出：同样的输入重复 setup 不会产生新批次。
        batch_id = batch_id_of(entries)
        batch_dir = Path(paths.work) / "inputs" / batch_id
        if not (batch_dir / "manifest.json").exists():
            files_dir = batch_dir / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            for entry in entries:
                shutil.copyfile(
                    Path(self._config.inputs.dir) / str(entry["name"]),
                    files_dir / str(entry["name"]),
                )
            (batch_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "batchId": batch_id,
                        "asOf": self._config.as_of,
                        "capturedAt": now(),
                        "files": entries,
                    },
                    ensure_ascii=False, indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        db_path = self._db_path(paths)
        if not db_path.exists():
            # 只建空库基线：表结构由物化节点建，setup 不猜业务 schema。
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(
                    "PRAGMA journal_mode=WAL; "
                    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("created_at", now()),
                )
                connection.commit()
            finally:
                connection.close()
        snapshot = snapshot_database(db_path, paths.data_revisions)
        # 每次都登记一条基线 revision：重复 setup 会产生多条同 sha 的记录，幂等只到
        # program / session / batch 这一级。
        db.register_data_revision(
            program_id=program.id,
            session_id=session.id,
            sha256=snapshot.sha256,
            path=snapshot.path,
            bytes=snapshot.bytes,
            note="空库基线",
        )
        return SetupResult(
            program_id=str(program.id),
            session_id=str(session.id),
            batch_id=batch_id,
            files=len(entries),
            reused=program_reused and session_reused,
        )

    # ---- 命令 ----

    def register_commands(self) -> None:
        """把 8 条命令装进 commands 服务；每个命令自己把异常转成退出码（1 业务失败、2 用法错误）。"""

        def fail_exit(error: Exception) -> int:
            # 命令边界统一兜异常：否则会带堆栈直接崩到 CLI 顶层，输出不可控。
            print(f"失败：{error}", file=sys.stderr)
            return 1

        def run_command(context: CommandContext) -> int:
            try:
                result = self.run_flow(context.flags.get("as-of"))
                context.print(
                    f"run {result.run_id[:8]} {result.status}  "
                    f"两次一致={result.identical}  flow@{result.flow_digest}"
                )
                return 0 if result.status == "succeeded" else 1
            except Exception as error:  # noqa: BLE001 - 命令边界统一转退出码
                return fail_exit(error)

        def flow_command(context: CommandContext) -> int:
            try:
                resolved = self.resolve_context()
                commit = self._db().draft_head(resolved.session.id)
                if commit is None:
                    _fail("草稿分支没有提交：先跑 author")
                plan = plan_flow(
                    load_flow_at_commit(
                        resolved.program.repo_path,
                        str(commit),
                        self._config.flow_path,
                        python=self._exec.interpreter(),
                    )
                )
                for line in describe_flow(plan, steps=True):
                    context.print(line)
                hints = suggest_optimizations(plan)
                for hint in hints:
                    context.print(f"  ~ {hint.note}")
                if hints:
                    context.print("  这些只是建议：要不要更短由你决定，模型不会自己改。")
                return 0
            except Exception as error:  # noqa: BLE001
                return fail_exit(error)

        def publish_command(context: CommandContext) -> int:
            try:
                result = self.publish(context.flags.get("run"))
                context.print(f"已合入 main：{result['commit']}")
                return 0
            except Exception as error:  # noqa: BLE001
                return fail_exit(error)

        def optimize_command(context: CommandContext) -> int:
            try:
                timeout = context.flags.get("timeout-ms")
                result = self.optimize(
                    context.flags.get("apply"),
                    context.flags.get("from"),
                    int(timeout) if timeout else None,
                )
                if result.mode == "review":
                    context.print(f"建议文件：{result.proposal_path}")
                return 0
            except Exception as error:  # noqa: BLE001
                return fail_exit(error)

        def status_command(context: CommandContext) -> int:
            try:
                view = self.status()
                program = cast(Any, view.program)
                session = cast(Any, view.session)
                context.print(
                    f"program {program.name} ({str(program.id)[:8]})  repo {program.repo_path}"
                )
                baseline = "-" if session.base_commit is None else str(session.base_commit)[:8]
                context.print(
                    f"session {str(session.id)[:8]}  草稿 {session.draft_branch}  基线 {baseline}"
                )
                restorable = (
                    "(无成功运行)"
                    if view.restorable is None
                    else json.dumps(view.restorable, ensure_ascii=False)
                )
                context.print(f"可恢复节点：{restorable}")
                context.print("数据版本：")
                for revision in view.revisions:
                    context.print(
                        f"  {str(revision['id'])[:8]}  {str(revision['sha256'])[:10]}  "
                        f"{int(str(revision['bytes'])):>9} B  {revision.get('note') or ''}"
                    )
                context.print("运行记录：")
                for run in view.runs:
                    item = cast(Any, run)
                    code = "-" if item.code_commit is None else str(item.code_commit)[:8]
                    context.print(
                        f"  {str(item.id)[:8]}  {item.status:<9} code={code} "
                        f"data={str(item.data_revision_id)[:8]}  {item.note or ''}"
                    )
                context.print(f"发布记录 {len(view.publishes)} 条")
                for record in view.publishes:
                    context.print(
                        f"  {str(record['commitSha'])[:8]}  {record['at']}  {record.get('note') or ''}"
                    )
                return 0
            except Exception as error:  # noqa: BLE001
                return fail_exit(error)

        def author_command(context: CommandContext) -> int:
            prompt_file = context.flags.get("prompt")
            if not prompt_file:
                context.print("author 需要 --prompt=<文件>")
                return 2
            timeout = context.flags.get("timeout-ms")
            try:
                result = self.author(
                    prompt_file,
                    context.flags.get("label", "run"),
                    int(timeout) if timeout else None,
                )
                # 没有产生提交就返回 1：模型没写出文件不算成功。
                return 0 if result.commit else 1
            except Exception as error:  # noqa: BLE001
                return fail_exit(error)

        def setup_command(context: CommandContext) -> int:
            try:
                result = self.setup()
                context.print(
                    f"program   {result.program_id[:8]}{'（复用）' if result.reused else '（新建）'}"
                )
                context.print(f"session   {result.session_id[:8]}")
                context.print(f"输入批次   {result.batch_id}（{result.files} 个文件）")
                return 0
            except Exception as error:  # noqa: BLE001
                return fail_exit(error)

        def db_command(context: CommandContext) -> int:
            try:
                resolved = self.resolve_context()
                digest = database_digest(self._db_path(resolved.paths))
                context.print(
                    json.dumps(
                        {name: _table_dict(item) for name, item in digest.items()},
                        ensure_ascii=False, indent=2,
                    )
                )
                return 0
            except Exception as error:  # noqa: BLE001
                return fail_exit(error)

        for spec in (
            # 这里的顺序只是源码可读性：help 输出按名字排序（见 CommandRegistry.all）。
            CommandSpec("run", "跑整条工作流两遍（物化 → 节点），比对声明的产出", run_command),
            CommandSpec("flow", "打印草稿里的工作流：物化 / 节点 / 边 / 可优化项", flow_command),
            CommandSpec("publish", "只发布带「两次一致」证据的运行", publish_command),
            CommandSpec("optimize", "模型自检「能不能更短」：默认只读评审，--apply=<id> 才动代码", optimize_command),
            CommandSpec("status", "恢复视图：代码版本 + 数据版本 + 输入批次 + 制品", status_command),
            CommandSpec("author", "让模型写程序并提交到草稿分支", author_command),
            CommandSpec("setup", "建程序仓库 / 会话 / 输入批次 / 空库基线（幂等）", setup_command),
            CommandSpec("db", "打印业务库的语义摘要（每表行数 + 内容哈希）", db_command),
        ):
            self._commands.register(spec)


def _apply(ctx: PluginContext, config: SessionsConfig) -> None:
    """把依赖服务组装成 SessionsFeature 并注册命令；任何 service() 缺失都会在这里立刻失败。"""

    feature = SessionsFeature(
        config,
        service(ctx, "store"),
        service(ctx, "workspace"),
        service(ctx, "exec"),
        service(ctx, "agent"),
        service(ctx, "telemetry"),
        service(ctx, "commands"),
    )
    ctx.provide("sessions", feature)
    feature.register_commands()


plugin = define_plugin(
    name="sessions",
    inject=("store", "workspace", "exec", "agent", "telemetry", "commands"),
    provide="sessions",
    config=_parse_config,
    apply=_apply,
)
