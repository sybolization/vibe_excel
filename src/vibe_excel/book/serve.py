"""本地服务：现场采集页面 + 构建期输入的增删接口。

- 页面不落盘：每次请求重新读 ``control.db`` 和运行目录，刷新即最新
- 写接口只有 ``/api/inputs/*`` 与 ``/api/projects/archive``：前者写 ground-truth 目录、
  清单名单、``input-events.jsonl`` 与 ``.trash/.archive``；后者把项目目录移进
  ``.archive/projects``、写 ``project-events.jsonl`` 并在 control.db 记一行归档
- 只绑 127.0.0.1，默认端口 8731
"""

from __future__ import annotations

import io
import json
import threading
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..util import VibeError
from .collect import collect_book
from .index_render import render_index
from .inputs_edit import (
    GroundTruth,
    add_file,
    events,
    last_removal,
    list_files,
    promote_trash,
    remove_file,
    resolve_ground_truth,
    undo_removal,
)
from .projects import collect_projects, page_name
from .projects_edit import archive_project
from .render import _ASSETS, render_book
from .types import ProjectSummary

__all__ = ["serve"]

_ASSET_TYPES = {
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

_LOCK = threading.Lock()


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _rebuild_batch() -> dict[str, object]:
    """调 setup：把当前 ground-truth 快照成新批次，然后把 .trash 归档成 .archive。

    exec 插件要求 config.python 是解释器的绝对路径。服务本身就是用那个解释器起的，
    缺环境变量时按 sys.executable 兜底——否则会以「插件加载失败」收场，而用户看到的
    只是一句「未知命令 setup」，根本猜不到原因。
    """
    import os
    import sys

    from ..cli import run_cli  # 延迟导入：只有重建时才需要整个宿主

    os.environ.setdefault("VIBE_EXCEL_PYTHON", sys.executable)
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        code = run_cli(["setup"])
    output = buffer.getvalue()
    failure = ""
    if code != 0:
        # 宿主把真正的失败原因打在最前面（插件加载失败 / 配置校验），挑出来给页面看
        for line in output.splitlines():
            if "失败" in line or "错误" in line:
                failure = line.strip()
                break
    return {"code": code, "output": output, "failure": failure}


class _Handler(BaseHTTPRequestHandler):
    stores: list[Path] = []
    server_version = "vibe-book/1"

    # ---- 输出helpers ----

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _html(self, text: str, status: int = 200) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

    def _api(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send(status, _json(payload), "application/json; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 覆盖父类签名
        # 静默：终端只留启动和错误，不要每个请求刷一行
        pass

    # ---- 页面 ----

    def _index_html(self) -> str:
        multi = len(self.stores) > 1
        pairs: list[tuple[Path, ProjectSummary]] = []
        for store in self.stores:
            for project in collect_projects(store):
                if multi:
                    name = store.name
                    project["store"] = name if name.lower() != "store" else f"{store.parent.name}/{name}"
                pairs.append((store, project))
        label = self.stores[0].name if not multi else f"{len(self.stores)} 个 store"
        pages = {str(project["id"]): page_name(str(project["id"])) for _, project in pairs}
        return render_index(
            [project for _, project in pairs], store_label=label, pages=pages, can_archive=True
        )

    def _project_html(self, name: str) -> str | None:
        for store in self.stores:
            for project in collect_projects(store):
                if page_name(str(project["id"])) != name:
                    continue
                view = collect_book(
                    store,
                    program_id=str(project["id"]),
                    session_id=project["activeSessionId"],
                )
                state = self._state()
                # 项目页是服务现场渲染的：把输入运行态一起带上，导入/删除才不是死的。
                # 输入目录不可用时根开关跟着关掉，按钮灰着比点下去报错诚实。
                runtime = {
                    "mode": "live",
                    "editable": state.get("editable") is True,
                    "inputs": state,
                }
                return render_book(view, fonts="fonts/", home="index.html", runtime=runtime)
        return None

    # ---- 状态 ----

    def _state(self) -> dict[str, Any]:
        store = self.stores[0]
        payload: dict[str, Any] = {"mode": "live", "stores": [str(item) for item in self.stores]}
        try:
            ground: GroundTruth = resolve_ground_truth()
        except VibeError as error:
            return {**payload, "editable": False, "error": str(error)}
        pending = last_removal(store)
        return {
            **payload,
            "editable": True,
            "manifest": str(ground.manifest),
            "groundTruth": str(ground.directory),
            "files": list_files(ground),
            "events": events(store)[-20:],
            "canUndo": pending is not None,
            "undoFile": None if pending is None else pending.get("file"),
        }

    # ---- 请求分发 ----

    def do_GET(self) -> None:  # noqa: N802 - 覆盖父类
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._html(self._index_html())
                return
            if path == "/api/state":
                self._api(self._state())
                return
            if path.startswith("/fonts/"):
                target = _ASSETS / "fonts" / Path(path).name
                if not target.is_file():
                    self._send(404, b"not found", "text/plain")
                    return
                self._send(200, target.read_bytes(), _ASSET_TYPES.get(target.suffix, "application/octet-stream"))
                return
            if path.endswith(".html"):
                html = self._project_html(Path(path).name)
                if html is None:
                    self._send(404, b"not found", "text/plain")
                    return
                self._html(html)
                return
            self._send(404, b"not found", "text/plain")
        except (VibeError, OSError) as error:
            self._html(f"<pre>{error}</pre>", status=500)

    def do_POST(self) -> None:  # noqa: N802 - 覆盖父类
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            with _LOCK:
                self._mutate(path, body)
        except VibeError as error:
            self._api({"ok": False, "error": str(error)}, status=400)
        except OSError as error:
            self._api({"ok": False, "error": str(error)}, status=500)

    def _store_of(self, program_id: str) -> Path:
        """站点可能合并多个 store：按项目 id 找到它所在的那个。"""
        for store in self.stores:
            if any(str(project["id"]) == program_id for project in collect_projects(store)):
                return store
        raise VibeError(f"项目不存在或已归档：{program_id}")

    def _payload(self, body: bytes) -> dict[str, Any]:
        """归档接口的 JSON 入参；坏请求也要回一句人话，而不是断开连接。"""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as error:
            raise VibeError(f"请求体不是合法 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise VibeError("请求体必须是 JSON 对象")
        return payload

    def _mutate(self, path: str, body: bytes) -> None:
        store = self.stores[0]
        if path == "/api/projects/archive":
            payload = self._payload(body)
            program_id = str(payload.get("programId") or "").strip()
            if not program_id:
                raise VibeError("缺少 programId")
            target = self._store_of(program_id)
            event = archive_project(
                target, program_id, reason=str(payload.get("reason") or "")
            )
            self._api({"ok": True, "event": event, "state": self._state()})
            return
        if path == "/api/inputs/upload":
            # 前端把文件名 percent-encode 后放进 header：非 ASCII 直接塞 header 会被浏览器拒绝
            name = unquote(self.headers.get("X-Filename") or "")
            ground = resolve_ground_truth()
            result = add_file(ground, name, body)
            self._api({"ok": True, "file": result, "state": self._state()})
            return
        if path == "/api/inputs/delete":
            payload = json.loads(body or b"{}")
            ground = resolve_ground_truth()
            event = remove_file(
                ground,
                store,
                str(payload.get("name") or ""),
                tables=payload.get("tables") or [],
                chain=payload.get("chain") or {},
            )
            self._api({"ok": True, "event": event, "state": self._state()})
            return
        if path == "/api/inputs/undo":
            ground = resolve_ground_truth()
            event = undo_removal(ground, store)
            self._api({"ok": True, "event": event, "state": self._state()})
            return
        if path == "/api/inputs/rebuild":
            result = _rebuild_batch()
            archived = promote_trash(store) if result["code"] == 0 else []
            self._api({"ok": result["code"] == 0, **result, "archived": archived, "state": self._state()})
            return
        self._api({"ok": False, "error": f"未知接口：{path}"}, status=404)


def serve(
    stores: list[Path], *, port: int = 8731, host: str = "127.0.0.1", open_browser: bool = True
) -> int:
    """起服务并阻塞。Ctrl+C 结束。"""
    for store in stores:
        if not (store / "control.db").is_file():
            raise VibeError(f"不是 store（缺 control.db）：{store}")
    handler = type("BoundHandler", (_Handler,), {"stores": stores})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"服务已启动：{url}")
    print(f"  store  {', '.join(str(item) for item in stores)}")
    try:
        ground = resolve_ground_truth()
        print(f"  inputs {ground.directory}（{len(ground.files)} 个文件）")
    except VibeError as error:
        print(f"  注意   输入目录不可用，导入/删除会失败：{error}")
    if open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0
