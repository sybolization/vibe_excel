"""插件：commands —— CLI 子命令注册表。

命令本身也是插件注册进来的，所以「加一个子命令」不需要改 CLI 入口，
只要多一个插件（或某个插件多注册一条）。
它不解析 argv、不打印 help：参数解析在 cli.py，排版输出由调用方负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vibe_excel.kernel import CommandSpec, PluginContext, define_plugin
from vibe_excel.util import VibeError


@dataclass(slots=True)
class CommandRegistry:
    """命令注册表；重复名字立即报错，按名字排序返回。"""

    _items: dict[str, CommandSpec] = field(default_factory=dict)

    def register(self, spec: CommandSpec) -> None:
        if spec.name in self._items:
            raise VibeError(f"命令重复注册：{spec.name}")
        self._items[spec.name] = spec

    def all(self) -> list[CommandSpec]:
        return [self._items[name] for name in sorted(self._items)]

    def find(self, name: str) -> CommandSpec | None:
        return self._items.get(name)


def _apply(ctx: PluginContext, config: object) -> None:
    """把空注册表注册为 commands 服务；命令由 sessions 插件在 apply 时填入。"""

    ctx.provide("commands", CommandRegistry())


plugin = define_plugin(name="commands", provide="commands", apply=_apply)
