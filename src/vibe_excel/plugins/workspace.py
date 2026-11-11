"""插件：workspace —— 每会话的目录布局与排他锁。

依赖 store 只为了拿 storeRoot（服务依赖驱动的注入，不是 import 依赖）。
目录命名与锁的实现都在 runtime.py；这里只做服务适配，不自己拼路径。
"""

from __future__ import annotations

from collections.abc import Callable

from vibe_excel.contracts import SessionId
from vibe_excel.kernel import PluginContext, StoreService, define_plugin, service
from vibe_excel.runtime import (
    WorkspacePaths,
    ensure_workspace,
    session_lock_path,
    with_lock_sync,
    workspace_paths,
)


class WorkspaceService:
    """把 runtime 的路径与锁函数包装成服务。

    无状态：每次调用都从 store 现取 storeRoot，所以配置变化立刻生效。
    """

    def __init__(self, store: StoreService) -> None:
        self._store = store

    def _root(self) -> str:
        return self._store.store_root()

    def paths(self, session_id: str) -> WorkspacePaths:
        """只计算路径，不建目录。"""

        return workspace_paths(self._root(), SessionId(session_id))

    def ensure(self, session_id: str) -> WorkspacePaths:
        """计算并创建本会话的各级目录（幂等）。"""

        return ensure_workspace(self._root(), SessionId(session_id))

    def lock_path(self, session_id: str) -> str:
        """会话锁文件的绝对路径。"""

        return str(session_lock_path(self._root(), SessionId(session_id)))

    def with_lock(self, session_id: str, action: Callable[[], object]) -> object:
        """在会话锁内执行 action；默认不等待，锁被占用时直接抛 VibeError。"""

        return with_lock_sync(self.lock_path(session_id), action)


def _apply(ctx: PluginContext, config: object) -> None:
    """用 store 的制品根构造服务；store 未就绪时 service() 会立刻报错。"""

    store = service(ctx, "store")
    ctx.provide("workspace", WorkspaceService(store))


plugin = define_plugin(
    name="workspace", inject=("store",), provide="workspace", apply=_apply
)
