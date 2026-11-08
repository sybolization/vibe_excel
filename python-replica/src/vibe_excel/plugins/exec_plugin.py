"""插件：exec —— 在干净的 detached worktree 里执行一段入口契约。

这里刻意不知道任何业务语义：不知道 import/analyze，不知道 hr.db。
它只回答「用哪个 commit、跑哪个可执行文件、带什么参数、跑在哪个干净目录、退出码与耗时多少」。
换容器或远程执行器时，替换的是这一个插件。
worktree 的创建与清理不在这里，由 runtime.clean_rerun 负责。
"""

from __future__ import annotations

import time
from pathlib import Path

from vibe_excel.kernel import (
    ExecOutcome,
    ExecRequest,
    PluginContext,
    define_plugin,
)
from vibe_excel.runtime import CleanRunEntry, CleanRunOptions, clean_rerun
from vibe_excel.util import VibeError


class ExecConfig:
    """exec 插件配置：解释器绝对路径与默认超时。"""

    def __init__(self, python: str, timeout_ms: int) -> None:
        self.python = python
        self.timeout_ms = timeout_ms


def _parse_config(raw: object) -> ExecConfig:
    """校验配置；解释器路径在加载期就绝对化并确认存在，避免运行期才发现失效。"""

    if not isinstance(raw, dict):
        raise VibeError("exec 插件配置必须是对象")
    python_value = raw.get("python")
    if not isinstance(python_value, str) or not python_value.strip():
        raise VibeError("exec 插件需要 config.python（解释器绝对路径）")
    # 干净执行发生在 detached worktree 里，cwd 不是仓库根，相对路径必须在加载期解析。
    python = Path(python_value).expanduser()
    python = python if python.is_absolute() else Path.cwd() / python
    python = python.resolve()
    if not python.exists():
        raise VibeError(f"exec.python 不存在：{python}")
    timeout_raw = raw.get("timeoutMs", 15 * 60 * 1000)
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
        raise VibeError("exec.timeoutMs 必须是正数")
    timeout_ms = int(timeout_raw)
    if timeout_ms <= 0:
        raise VibeError("exec.timeoutMs 必须是正数")
    return ExecConfig(str(python), timeout_ms)


class ExecService:
    """干净重跑服务。

    无状态：每次 run 都在新的 detached worktree 里执行，超时即被杀掉。
    """

    def __init__(self, config: ExecConfig) -> None:
        self._config = config

    def run(self, request: ExecRequest) -> ExecOutcome:
        """把请求翻译成 clean_rerun 的选项并计时。

        `outputs=()` 表示不按声明收集产物：产物由工作流自己写进运行目录，
        这里只关心退出码与输出。
        """

        started = time.monotonic()
        result = clean_rerun(
            CleanRunOptions(
                repo_path=request.repo_path,
                commit=request.commit,
                run_root=request.run_root,
                # 解释器来自配置，入口脚本名由调用方给：exec 不知道 materialize / node_runner 是什么。
                entry=CleanRunEntry(
                    self._config.python, [request.entry.entry, *request.entry.args]
                ),
                outputs=(),
                timeout_ms=(
                    self._config.timeout_ms if request.timeout_ms is None else request.timeout_ms
                ),
                as_of_date=request.as_of,
            )
        )
        return ExecOutcome(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout=result.stdout,
            stderr=result.stderr,
            manifest_path=result.manifest_path,
            work_dir=result.work_dir,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def interpreter(self) -> str:
        """返回配置里的解释器绝对路径；sessions 用它解析流程与探测环境。"""

        return self._config.python


def _apply(ctx: PluginContext, config: ExecConfig) -> None:
    """把已校验的配置包成 ExecService 并注册为 exec 服务。"""

    ctx.provide("exec", ExecService(config))


plugin = define_plugin(name="exec", provide="exec", config=_parse_config, apply=_apply)
