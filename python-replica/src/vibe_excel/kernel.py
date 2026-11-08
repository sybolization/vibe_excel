"""内核契约：服务名、服务接口与插件形状。

这一层不依赖任何插件框架，只描述「什么样算一个服务」。业务库
（contracts/session/runtime）保持在框架之外，可以被单元测试直接调用。
它不解析清单、不导入插件、也不维护插件状态机——那些分别是 manifest.py 与 host.py 的职责。

与 TS 版 kernel.ts 对齐的三点：
1. 插件是一个带 `name` / `inject` / `provide` / `config` / `apply(ctx, config)` 的对象；
2. 服务通过名字注入，启动顺序由服务可用性决定，不由清单顺序决定；
3. 依赖未满足的插件保持 PENDING，不会静默执行。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

# 服务名就是插件之间的公共接口：改名等于改契约，注入方与提供方必须同时改。
ServiceName = Literal[
    "store", "workspace", "exec", "agent", "telemetry", "commands", "sessions"
]

SERVICE_NAMES: tuple[ServiceName, ...] = (
    "store",
    "workspace",
    "exec",
    "agent",
    "telemetry",
    "commands",
    "sessions",
)


@dataclass(frozen=True, slots=True)
class CommandContext:
    """一条命令拿到的参数与输出通道。

    `print` 由宿主注入：命令不要直接写 stdout，否则输出无法被测试捕获，
    也无法被未来的远程宿主转发。
    """

    argv: list[str]
    flags: dict[str, str]
    print: Callable[[str], None]


CommandRun = Callable[[CommandContext], int]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """命令注册表里的一行：名字用于查找，summary 用于 help，run 返回进程退出码。"""

    name: str
    summary: str
    run: CommandRun


class CommandsService(Protocol):
    """命令注册表。实现方必须拒绝重名，`all()` 按名字稳定排序，`find()` 找不到返回 None。"""

    def register(self, spec: CommandSpec) -> None: ...

    def all(self) -> list[CommandSpec]: ...

    def find(self, name: str) -> CommandSpec | None: ...


@dataclass(frozen=True, slots=True)
class EntrySpec:
    """相对干净 worktree 根的可执行文件名与参数。

    `args` 原样拼进命令行，不经过 shell；需要转义或引号的逻辑由入口脚本自己处理。
    """

    entry: str
    args: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExecRequest:
    """一次干净重跑请求。

    `commit` 决定实际执行的代码字节，`run_root` 决定临时 worktree 落点；
    `timeout_ms` 为 None 时用 exec 服务的默认值，`as_of` 会作为业务日期注入子进程环境。
    """

    repo_path: str
    commit: str
    run_root: str
    entry: EntrySpec
    timeout_ms: int | None = None
    as_of: str | None = None


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    """干净重跑的结果快照。

    `work_dir` 是执行用的 detached worktree（可能已被清理），`manifest_path`
    是这次执行的清单文件；判断成功要看 `exit_code` 与 `timed_out` 两者。
    """

    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    manifest_path: str
    work_dir: str
    elapsed_ms: int


class WorkspacePaths(Protocol):
    """工作区路径集合；字段名与 runtime.workspace_paths 保持一致。"""

    root: Path
    work: Path
    code: Path
    pi_sessions: Path
    agent: Path
    runs: Path
    data_revisions: Path


class StoreService(Protocol):
    """控制面数据库服务（程序 / 会话 / 运行 / 数据版本 / 制品 / 发布）。

    `db()` 可能是懒打开的；调用方不得在宿主 dispose 之后继续使用返回的连接。
    """

    def db(self) -> object: ...

    def store_root(self) -> str: ...


class WorkspaceService(Protocol):
    """每会话的目录布局与排他锁。

    `ensure()` 有建目录的副作用；`with_lock()` 在同一会话锁内执行 action，
    用于串行化 run / author / optimize 这类互斥操作。
    """

    def paths(self, session_id: str) -> WorkspacePaths: ...

    def ensure(self, session_id: str) -> WorkspacePaths: ...

    def lock_path(self, session_id: str) -> str: ...

    def with_lock(self, session_id: str, action: Callable[[], object]) -> object: ...


class ExecService(Protocol):
    """执行入口契约的服务；`interpreter()` 返回的解释器路径同时被 sessions 用来解析流程与探测环境。"""

    def run(self, request: ExecRequest) -> ExecOutcome: ...

    def interpreter(self) -> str: ...


@dataclass(frozen=True, slots=True)
class AskQuestion:
    """pi 侧提问；回答必须带同一个 id。"""

    id: str
    question: str
    options: list[str]


QuestionHandler = Callable[[AskQuestion], str | None]
EventSink = Callable[[object], None]


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """让 agent 写程序的一次请求。

    `cwd` 是可写工作区；`on_question` 与 `on_event` 可能从别的线程回调，
    实现方必须自己保证线程安全（当前实现是直接在工作线程里调用）。
    """

    cwd: str
    agent_dir: str
    session_dir: str
    prompt: str
    timeout_ms: int | None = None
    on_question: QuestionHandler | None = None
    on_event: EventSink | None = None


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """一次 agent 运行的结果。

    `ok` 只表示会话正常结束，不代表写出了文件，也不代表流程可解析——
    这两件事分别由草稿提交与 run 的双跑来判定。
    """

    ok: bool
    error: str | None
    elapsed_ms: int
    event_count: int
    tool_names: list[str]
    tool_call_count: int
    session_files: list[str]
    stats: object


class AgentService(Protocol):
    """agent 服务；`export_html` 是把会话导出成可读证据的旁路，失败不应阻断主流程。"""

    def run(self, request: AgentRequest) -> AgentOutcome: ...

    def export_html(self, session_file: str, out_file: str) -> bool: ...


class TelemetrySink(Protocol):
    """一次执行的事件流。

    `close()` 会 flush 并写时间线文件；关闭之后不应再送事件（当前实现会写已关闭的文件）。
    """

    def event(self, raw: object) -> None: ...

    def timeline(self) -> str: ...

    def event_count(self) -> int: ...

    def type_counts(self) -> dict[str, int]: ...

    def close(self) -> None: ...


class TelemetryService(Protocol):
    """为每个运行目录开一个事件流；宿主 dispose 时负责统一关闭，调用方不要自己关两次。"""

    def begin(self, run_dir: str) -> TelemetrySink: ...


@dataclass(frozen=True, slots=True)
class AuthorResult:
    """`author` 的结果；`commit` 为 None 表示模型没有产生任何改动（不是成功）。"""

    commit: str | None
    elapsed_ms: int
    event_count: int
    tool_names: list[str]
    trace_dir: str


@dataclass(frozen=True, slots=True)
class RunResult:
    """一次双跑的结果；`identical` 是两轮逐表 / 逐文件比对，只有它为真时 status 才是 succeeded。"""

    run_id: str
    status: str
    exit_code: int
    identical: bool
    flow_digest: str
    outputs: list[dict[str, object]]
    out_revision_id: str | None
    run_dir: str


@dataclass(frozen=True, slots=True)
class StatusView:
    """status 的恢复视图：最近一次成功运行的四元组，加上发布与数据版本的历史。"""

    program: object
    session: object
    runs: list[object]
    restorable: dict[str, str] | None
    publishes: list[dict[str, object]]
    revisions: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class SetupResult:
    """setup 的幂等结果；`reused` 为真表示程序与会话都是复用已有的，没有新建。"""

    program_id: str
    session_id: str
    batch_id: str
    files: int
    reused: bool


@dataclass(frozen=True, slots=True)
class OptimizeResult:
    """optimize 结果；review 只产出建议文件（commit 恒为 None），apply 才可能产生新提交。"""

    mode: Literal["review", "apply"]
    trace_dir: str
    proposal_path: str
    proposals: list[object]
    commit: str | None


class SessionsService(Protocol):
    """会话编排服务。

    `author` / `run_flow` / `optimize`（apply 模式）会走会话锁；`setup` / `status` /
    `publish` 与 `optimize` 的 review 模式不走。所有方法都假设 `setup` 至少跑过一次。
    """

    def setup(self) -> SetupResult: ...

    def author(self, prompt_file: str, label: str, timeout_ms: int | None = None) -> AuthorResult: ...

    def run_flow(self, as_of: str | None = None) -> RunResult: ...

    def publish(self, run_id: str | None = None) -> dict[str, object]: ...

    def optimize(
        self, apply_id: str | None = None, source: str | None = None, timeout_ms: int | None = None
    ) -> OptimizeResult: ...

    def status(self) -> StatusView: ...


ConfigValidator = Callable[[object], object]
PluginApply = Callable[["PluginContext", Any], None]


@dataclass(slots=True)
class PluginShape:
    """插件声明；`apply` 只在依赖服务全部就绪后执行。

    宿主调度只看 `inject`：`provide` 仅用于状态表展示，写错了不会被提前发现，
    所以 apply 内必须用 `service()` 取服务，让缺失立刻报错。
    """

    name: str
    apply: PluginApply
    inject: list[str] = field(default_factory=list)
    provide: list[str] = field(default_factory=list)
    config: ConfigValidator | None = None


@dataclass(slots=True)
class PluginContext:
    """宿主交给插件的最小上下文：取服务、提供服务、登记清理函数。

    清理函数由宿主在 dispose 时逆序执行；插件不要在 apply 之外再改 services。
    """

    services: dict[str, object] = field(default_factory=dict)
    # 按登记顺序入栈，Host.dispose 逆序弹出：依赖方先清理，提供方后清理。
    disposers: list[Callable[[], None]] = field(default_factory=list)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("vibe_excel"))

    def get(self, name: str) -> object | None:
        return self.services.get(name)

    def has(self, name: str) -> bool:
        return name in self.services

    def provide(self, name: str, value: object) -> Callable[[], None]:
        """注册服务并返回一个反注册函数；重名立即报错，避免静默覆盖。"""

        if name in self.services:
            raise ValueError(f"服务重复注册：{name}")
        self.services[name] = value

        def remove() -> None:
            self.services.pop(name, None)

        self.disposers.append(remove)
        return remove

    def effect(self, factory: Callable[[], Callable[[], None]]) -> Callable[[], None]:
        """立即执行工厂并把返回的清理函数登记到 dispose 栈；工厂抛错则不登记。"""

        cleanup = factory()
        self.disposers.append(cleanup)
        return cleanup


def service(ctx: PluginContext, name: ServiceName) -> Any:
    """从上下文取服务；缺失时立即报错，避免静默降级。"""

    value = ctx.get(name)
    if value is None:
        raise ValueError(f"服务 {name} 尚未就绪（插件 inject 里漏了？）")
    return value


def define_plugin(
    *,
    name: str,
    apply: PluginApply,
    inject: Sequence[str] = (),
    provide: str | Sequence[str] = (),
    config: ConfigValidator | None = None,
) -> PluginShape:
    """声明一个插件；与 TS 版 definePlugin 的行为一致。"""

    provides = [provide] if isinstance(provide, str) else list(provide)
    return PluginShape(
        name=name,
        apply=apply,
        inject=list(inject),
        provide=provides,
        config=config,
    )


def parse_config(plugin: PluginShape, raw: object) -> object:
    """调用插件自己的校验器；异常文本原样交给宿主汇报。"""

    if plugin.config is None:
        return raw
    return plugin.config(raw)
