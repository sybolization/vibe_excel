"""宿主：把清单变成可用的服务图。

这里只做四件事：解析清单 → 逐个导入插件模块 → 等依赖满足后注册 → 汇报每个插件的状态。
不做的事：不按清单顺序强行启动（顺序由服务依赖决定）、不吞掉加载错误（失败必须可见）。
调用方必须在使用完后调用 `Host.dispose`，否则插件登记的文件句柄与子进程不会释放。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from vibe_excel.kernel import (
    PluginContext,
    PluginShape,
    define_plugin,
    parse_config,
)
from vibe_excel.manifest import (
    Manifest,
    PluginRow,
    apply_patch,
    parse_manifest,
    resolve_module_specifier,
)
from vibe_excel.util import VibeError


@dataclass(frozen=True, slots=True)
class PluginReport:
    """一台插件的加载结果快照。

    `state` 取值见 FIBER_STATES，`error` 只在失败或依赖未满足时非 None。
    """

    id: str
    module: str
    state: str
    inject: list[str]
    provides: list[str]
    error: str | None


@dataclass(slots=True)
class Host:
    """加载完成后的宿主：服务容器、最终清单与状态表。

    生命周期：`load_host` 构造 → 调用方使用 `ctx` → `dispose()` 逆序回收。
    dispose 之后服务容器不可再用（`store` 等服务的清理函数已执行）。
    """

    ctx: PluginContext
    manifest: Manifest
    _reports: list[PluginReport] = field(default_factory=list)

    def reports(self) -> list[PluginReport]:
        """返回状态副本，避免调用方改到宿主内部记录。"""

        return [replace(report) for report in self._reports]

    def dispose(self) -> None:
        """逆序执行清理：依赖方先走，提供方后走。"""

        while self.ctx.disposers:
            cleanup = self.ctx.disposers.pop()
            try:
                cleanup()
            except Exception as error:  # noqa: BLE001 - 卸载失败不阻断其余清理
                sys.stderr.write(f"插件清理失败：{error}\n")


@dataclass(slots=True)
class _Pending:
    """待激活的插件：清单行 + 插件形状 + 已校验的配置。"""

    row: PluginRow
    plugin: PluginShape
    config: object


def _import_plugin(specifier: str) -> PluginShape:
    """支持 `module:attr`、模块名与文件路径三种模块说明符。"""

    path = Path(specifier)
    if path.is_absolute() or path.suffix == ".py":
        if not path.exists():
            raise VibeError(f"插件文件不存在：{path}")
        # 模块名只保证本进程内唯一，且不注册进 sys.modules：文件型插件不能做包内相对导入。
        module_name = f"vibe_excel_plugin_{abs(hash(specifier))}"
        module_spec = importlib.util.spec_from_file_location(module_name, path)
        if module_spec is None or module_spec.loader is None:
            raise VibeError(f"无法加载插件模块：{specifier}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        candidate: object = getattr(module, "plugin", None)
    else:
        module_name, _, attribute = specifier.partition(":")
        module = importlib.import_module(module_name)
        candidate = getattr(module, attribute) if attribute else getattr(module, "plugin", None)

    if candidate is None:
        raise VibeError(f"模块 {specifier} 没有导出插件（期望 plugin 或 module:attr）")
    if not isinstance(candidate, PluginShape):
        apply_method = getattr(candidate, "apply", None)
        if not callable(apply_method):
            raise VibeError(f"模块 {specifier} 的插件缺少 apply(ctx)")
        candidate = define_plugin(
            name=str(getattr(candidate, "name", specifier)),
            apply=apply_method,
            inject=list(getattr(candidate, "inject", []) or []),
            provide=list(getattr(candidate, "provide", []) or []),
            config=getattr(candidate, "config", None),
        )
    if not callable(candidate.apply):
        raise VibeError(f"模块 {specifier} 的插件缺少 apply(ctx)")
    return candidate


def load_host(
    manifest_path: str | Path,
    patches: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Host:
    """解析清单、加载插件并等待依赖逐个就绪。"""

    manifest = parse_manifest(manifest_path, env)
    for patch in patches or []:
        manifest = apply_patch(manifest, patch, env)

    ctx = PluginContext()
    reports: list[PluginReport] = []
    pending: list[_Pending] = []

    for row in manifest.rows:
        module = row.name or row.id
        specifier = resolve_module_specifier(manifest, module)
        if row.disabled:
            # 禁用的行也进状态表：`vibectl plugins` 要能看出它为什么没生效。
            reports.append(
                PluginReport(row.id, module, "DISABLED", [], [], None)
            )
            continue
        try:
            plugin = _import_plugin(specifier)
        except Exception as error:  # noqa: BLE001 - 加载失败必须汇报，不能吞
            reports.append(
                PluginReport(row.id, module, "FAILED", [], [], str(error))
            )
            continue
        raw_config: object = row.config or {}
        # 没有 config 的行按空对象处理：插件校验器可以自己从空配置给出默认值。
        try:
            config = parse_config(plugin, raw_config)
        except Exception as error:  # noqa: BLE001
            reports.append(
                PluginReport(
                    row.id, module, "FAILED", list(plugin.inject), list(plugin.provide),
                    f"配置校验失败：{error}",
                )
            )
            continue
        pending.append(_Pending(row, plugin, config))
        reports.append(
            PluginReport(
                row.id, module, "PENDING", list(plugin.inject), list(plugin.provide), None
            )
        )

    # 多轮激活：每轮把依赖已满足的插件跑起来，直到没有进展。
    # 上界是「每个插件各一轮」再加两轮余量；apply 失败也算进展，所以不会死循环。
    states = {entry.row.id: "PENDING" for entry in pending}
    for _ in range(len(pending) + 2):
        progressed = False
        for entry in pending:
            if states[entry.row.id] != "PENDING":
                continue
            if any(not ctx.has(name) for name in entry.plugin.inject):
                continue
            try:
                entry.plugin.apply(ctx, entry.config)
            except Exception as error:  # noqa: BLE001
                states[entry.row.id] = "FAILED"
                progressed = True
                _set_report(reports, entry.row.id, "FAILED", f"apply 失败：{error}")
                continue
            states[entry.row.id] = "ACTIVE"
            progressed = True
            # provide 只是声明；apply 实际注册的服务必须与之一致，否则依赖方会拿到空位。
            missing = [name for name in entry.plugin.provide if not ctx.has(name)]
            if missing:
                for name in entry.plugin.provide:
                    ctx.services.pop(name, None)
                states[entry.row.id] = "FAILED"
                _set_report(
                    reports,
                    entry.row.id,
                    "FAILED",
                    f"声明提供 {', '.join(missing)}，但 apply 没有注册",
                )
                continue
            _set_report(reports, entry.row.id, "ACTIVE", None)
        if not progressed:
            break

    for entry in pending:
        if states[entry.row.id] == "PENDING":
            # 没被激活的都是依赖缺失：把缺的注入名写进 error，改清单时不用再猜。
            missing = [name for name in entry.plugin.inject if not ctx.has(name)]
            _set_report(
                reports,
                entry.row.id,
                "PENDING",
                f"依赖服务未就绪：{', '.join(missing) or '未知'}",
            )

    return Host(ctx=ctx, manifest=manifest, _reports=reports)


def _set_report(
    reports: list[PluginReport], identifier: str, state: str, error: str | None
) -> None:
    """按 id 就地替换状态行；找不到 id 时静默忽略（状态只用于展示，不影响控制流）。"""

    for index, report in enumerate(reports):
        if report.id == identifier:
            reports[index] = replace(report, state=state, error=error)
            return


def format_reports(reports: list[PluginReport]) -> str:
    """打印一张插件表（`vibectl plugins` 用）。"""

    width = max((len(report.id) for report in reports), default=4)
    lines: list[str] = []
    for report in reports:
        services = f" provides={','.join(report.provides)}" if report.provides else ""
        inject = f" inject={','.join(report.inject)}" if report.inject else ""
        # 只印错误首行：完整堆栈/细节已经在加载期的 stderr 里出现过一次。
        error = f"  !! {report.error.splitlines()[0]}" if report.error else ""
        lines.append(f"{report.id:<{width}}  {report.state:<9}{services}{inject}{error}")
    return "\n".join(lines)
