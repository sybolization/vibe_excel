"""插件包：由 `vibe.plugins.yml` 按 `vibe_excel.plugins:<属性名>` 引用。

这里只是再导出，不含初始化逻辑：每个 `plugin` 都是 define_plugin 的纯声明，
导入期不会打开数据库、不会起子进程；真正的副作用发生在宿主调用 apply 时。
"""

from __future__ import annotations

from vibe_excel.plugins.agent_pi import plugin as agent_pi
from vibe_excel.plugins.commands import plugin as commands
from vibe_excel.plugins.exec_plugin import plugin as exec_plugin
from vibe_excel.plugins.sessions import plugin as sessions
from vibe_excel.plugins.store import plugin as store
from vibe_excel.plugins.telemetry import plugin as telemetry
from vibe_excel.plugins.workspace import plugin as workspace

__all__ = [
    "agent_pi",
    "commands",
    "exec_plugin",
    "sessions",
    "store",
    "telemetry",
    "workspace",
]
