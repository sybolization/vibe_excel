"""插件：store —— 控制面（Program / Session / Run / DataRevision / Artifact / Publish）。

只是把 session 模块适配成服务：库本身不认识插件，可以被单测直接调用。
懒打开：只跑 `plugins` / `help` 这类命令时不该创建数据库。
不负责建表与迁移：那是 session.SessionStore.open 的事。
"""

from __future__ import annotations

from pathlib import Path

from vibe_excel.kernel import PluginContext, define_plugin
from vibe_excel.session import SessionStore
from vibe_excel.util import VibeError


class StoreConfig:
    """store 插件配置。

    `store_root` 必须是绝对路径：它会被写进 control.db，之后所有进程据此定位同一处制品。
    """

    def __init__(self, store_root: str) -> None:
        self.store_root = store_root


def _parse_config(raw: object) -> StoreConfig:
    """校验并绝对化 storeRoot；相对路径按 cwd 解析，解析结果后续会被持久化。"""

    if not isinstance(raw, dict):
        raise VibeError("store 插件配置必须是对象")
    value = raw.get("storeRoot")
    if not isinstance(value, str) or not value.strip():
        raise VibeError("store 插件需要 config.storeRoot（或用 ${VIBE_EXCEL_STORE} 注入）")
    # 制品根一律用绝对路径：它会出现在 control.db 里，之后所有进程都要能定位到同一处。
    path = Path(value).expanduser()
    path = path if path.is_absolute() else Path.cwd() / path
    return StoreConfig(str(path.resolve()))


class StoreService:
    """懒打开的会话存储服务。

    第一次 `db()` 才真正打开 control.db；`close()` 由宿主 dispose 调用，
    关闭后再调 `db()` 会重新打开（但宿主已 dispose 时不应再调用）。
    """

    def __init__(self, config: StoreConfig) -> None:
        self._config = config
        self._opened: SessionStore | None = None

    def db(self) -> SessionStore:
        """取控制面连接；首次调用会建库并打开。"""

        if self._opened is None:
            self._opened = SessionStore.open(self._config.store_root)
        return self._opened

    def store_root(self) -> str:
        """返回已绝对化的制品根；不会触发打开数据库。"""

        return self._config.store_root

    def close(self) -> None:
        """关闭已有连接；可重复调用，未打开时是空操作。"""

        if self._opened is not None:
            self._opened.close()
            self._opened = None


def _apply(ctx: PluginContext, config: StoreConfig) -> None:
    """注册服务，并把关闭数据库登记为 dispose 清理动作。"""

    store = StoreService(config)
    ctx.provide("store", store)
    ctx.effect(lambda: store.close)


plugin = define_plugin(
    name="store", provide="store", config=_parse_config, apply=_apply
)
