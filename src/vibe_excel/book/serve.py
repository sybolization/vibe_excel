"""本地服务：现场采集页面 + 构建期输入的增删接口。

- 页面不落盘：每次请求重新读 ``control.db`` 和运行目录，刷新即最新
- 写接口只有 ``/api/inputs/*``、``/api/projects/create`` 与 ``/api/projects/archive``：
  输入接口写 ground-truth 目录、清单名单与 ``input-events.jsonl``；新建接口只在
  control.db 里加一行 programs 和一个空仓库；归档接口把项目目录移进
  ``.archive/projects``、写 ``project-events.jsonl`` 并在 control.db 记一行归档
- 只绑 127.0.0.1，默认端口 8731
"""

from __future__ import annotations

import io
import json
import queue
import threading
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from ..util import VibeError
from .chat_runner import Runner
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
from .projects_edit import archive_project, create_workspace, start_session
from .render import _ASSETS, render_book, render_empty_book
from .types import ProjectSummary

__all__ = ["resolve_store", "serve"]

_ASSET_TYPES = {
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

_LOCK = threading.Lock()


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def resolve_store(stores: list[Path], raw: object) -> Path:
    """把页面带回来的 store 路径对回已知列表，认不出就报错。

    ``store`` 来自 HTTP 请求体，不能拿它直接建目录；只接受这个服务打开的那些。
    """
    value = "" if raw is None else str(raw).strip()
    if not value:
        raise VibeError("缺少 store：不知道要在哪个 store 里建项目")
    for store in stores:
        if str(store) == value:
            return store
    raise VibeError(f"未知的 store：{value}")


def impact_from_view(
    view: Mapping[str, Any], file_name: str
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """算「移出这个文件」牵动了什么：它物化出的表、下游每一步、最终产物。

    页面上的灰链和包里写给 agent 的链路必须是同一份数据，否则两边会各说各话。
    纯函数：给一份视图就能算，不依赖 store，方便单独验。
    """
    empty: tuple[list[dict[str, object]], dict[str, object]] = ([], {})
    inputs = view.get("inputs") or {}
    target = next((item for item in inputs.get("files") or [] if item.get("name") == file_name), None)
    if target is None:
        return empty
    tables: list[dict[str, object]] = []
    seeds: list[str] = []
    for row in list(target.get("direct") or []) + list(target.get("derived") or []):
        tables.append({"table": row.get("name"), "sheet": row.get("sheet")})
        if row.get("name"):
            seeds.append(str(row["name"]))
    if not seeds:
        return empty
    graph = view.get("graph") or {}
    nodes = list(graph.get("nodes") or [])
    touched = set(seeds)
    steps: list[dict[str, object]] = []
    # 按「谁读了受影响的表」一层层推进：一步只算一次，读到就算牵连
    alive = list(nodes)
    while True:
        moved = False
        rest: list[Any] = []
        for node in alive:
            reads = [str(item) for item in (node.get("reads") or [])]
            if any(item in touched for item in reads):
                writes = [str(item) for item in (node.get("writes") or [])]
                touched.update(writes)
                steps.append({"fn": node.get("fn"), "reads": reads, "writes": writes})
                moved = True
                continue
            rest.append(node)
        if not moved:
            break
        alive = rest
    chain: dict[str, object] = {"tables": seeds, "steps": steps}
    outputs = [str(item) for item in (graph.get("outputs") or []) if str(item) in touched]
    if outputs:
        chain["outputs"] = outputs
    return tables, chain


def _impact(store: Path, program_id: str, file_name: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    """从 store 里采集一份视图再算影响面；采不到就当作没有影响面。"""
    if not program_id or not file_name:
        return [], {}
    try:
        view = collect_book(store, program_id=program_id)
    except VibeError:
        return [], {}
    return impact_from_view(view, file_name)


def _chat_context(store: Path, program_id: str) -> dict[str, object]:
    """交给模型的项目事实：输入文件、步骤、结果表。"""
    try:
        view = collect_book(store, program_id=program_id)
    except VibeError:
        return {}
    inputs = cast("Mapping[str, Any]", view.get("inputs") or {})
    graph = cast("Mapping[str, Any]", view.get("graph") or {})
    files = [
        str(item.get("name"))
        for item in inputs.get("files") or []
        if item.get("inBatch") is True
    ]
    steps = [
        {"fn": node.get("fn"), "reads": node.get("reads") or [], "writes": node.get("writes") or []}
        for node in graph.get("nodes") or []
    ]
    diagram = cast("Mapping[str, Any]", view.get("diagram") or {})
    # 页面上有的表，模型也该看得到——不然它只能要求去查库
    tables = [
        {
            "name": node.get("label"),
            "rows": node.get("rows"),
            "columns": node.get("columns"),
        }
        for node in diagram.get("nodes") or []
        if isinstance(node, dict) and node.get("kind") == "table"
    ]
    return {"files": files, "steps": steps, "tables": tables, "outputs": graph.get("outputs") or []}


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
    root: Path | None = None
    runner: Runner
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

    def _workspaces(self) -> list[Path]:
        """工作区 = 一个库。启动时给的，加上 root 下新出现的。

        新建项目会在 root 下开一个新工作区，服务不重启也要能看见它。
        """
        found = list(self.stores)
        seen = {str(item) for item in found}
        root = getattr(self, "root", None)
        if root is not None and root.is_dir():
            for child in sorted(root.iterdir()):
                if not child.is_dir() or str(child) in seen:
                    continue
                if (child / "control.db").is_file():
                    found.append(child)
                    seen.add(str(child))
        return found

    def _index_html(self) -> str:
        pairs: list[tuple[Path, ProjectSummary]] = []
        for store in self._workspaces():
            for project in collect_projects(store):
                pairs.append((store, project))
        pages = {str(project["id"]): page_name(str(project["id"])) for _, project in pairs}
        root = getattr(self, "root", None)
        hint = "" if root is None else f"新建在 {root} 下，一个项目一个工作区。"
        return render_index(
            [project for _, project in pairs],
            store_label="",
            pages=pages,
            can_archive=True,
            can_create=root is not None,
            workspace_hint=hint,
        )

    def _project_html(self, name: str) -> str | None:
        for store in self._workspaces():
            for project in collect_projects(store):
                if page_name(str(project["id"])) != name:
                    continue
                if project["activeSessionId"] is None:
                    # 没有会话就没有草稿分支和工作目录：导入、对话、运行都没有落脚点，
                    # 这一页原本只能是一张说明书。补一个会话，让它就是那张空白画布。
                    # 新建路径已经会开会话，这里兜的是早于那次改动建出来的项目。
                    try:
                        start_session(store, str(project["id"]))
                        project = next(
                            (
                                item
                                for item in collect_projects(store)
                                if str(item["id"]) == str(project["id"])
                            ),
                            project,
                        )
                    except Exception:  # noqa: BLE001 - 补不上就退回说明书，不再往上报
                        return render_empty_book(
                            program=str(project["name"]), fonts="fonts/", home="index.html"
                        )
                view = collect_book(
                    store,
                    program_id=str(project["id"]),
                    session_id=project["activeSessionId"],
                )
                state = self._state(store)
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

    def _pick_store(self, raw: object) -> Path:
        """页面把 store 路径带回来：多 store 时状态必须跟着项目所属的 store 走。"""
        workspaces = self._workspaces()
        value = str(raw or "")
        if value:
            for store in workspaces:
                if str(store) == value:
                    return store
        return workspaces[0]

    def _state(self, store: Path | None = None) -> dict[str, Any]:
        workspaces = self._workspaces()
        target = workspaces[0] if store is None else store
        payload: dict[str, Any] = {
            "mode": "live",
            "store": str(target),
            "stores": [str(item) for item in workspaces],
        }
        try:
            ground: GroundTruth = resolve_ground_truth()
        except VibeError as error:
            return {**payload, "editable": False, "error": str(error)}
        pending = last_removal(target)
        return {
            **payload,
            "editable": True,
            "manifest": str(ground.manifest),
            "groundTruth": str(ground.directory),
            "files": list_files(ground),
            "events": events(target)[-20:],
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
            if path == "/api/chat/events":
                self._chat_events(parse_qs(urlparse(self.path).query))
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
            if path == "/api/chat/send":
                # 只排队、不等模型：回答由 /api/chat/events 那条流推回页面
                payload = self._payload(body)
                program_id = str(payload.get("programId") or "").strip()
                if not program_id:
                    raise VibeError("缺少 programId")
                owner = self._store_of(program_id)
                project = next(
                    (item for item in collect_projects(owner) if str(item["id"]) == program_id), None
                )
                session_id = "" if project is None else str(project.get("activeSessionId") or "")
                if not session_id:
                    raise VibeError("这个项目还没有可写对话的会话")
                result = self.runner.send(
                    owner,
                    session_id,
                    str(project.get("name") or "") if project else "",
                    str(payload.get("text") or ""),
                    conversation_id=str(payload.get("conversationId") or "") or None,
                    new=payload.get("new") is True,
                    context=_chat_context(owner, program_id),
                )
                self._api({**result, "state": self.runner.state(result["conversationId"])})
                return
            if path == "/api/chat/stop":
                payload = self._payload(body)
                conversation_id = str(payload.get("conversationId") or "").strip()
                if not conversation_id:
                    raise VibeError("缺少 conversationId")
                self._api(
                    {**self.runner.stop(conversation_id), "state": self.runner.state(conversation_id)}
                )
                return
            with _LOCK:
                self._mutate(path, body)
        except VibeError as error:
            self._api({"ok": False, "error": str(error)}, status=400)
        except OSError as error:
            self._api({"ok": False, "error": str(error)}, status=500)

    def _chat_events(self, query: dict[str, list[str]]) -> None:
        """SSE：把这一轮的事件推给页面。一段对话一条连接。"""
        conversation_id = (query.get("conversationId") or [""])[0].strip()
        if not conversation_id:
            self._api({"ok": False, "error": "缺少 conversationId"}, status=400)
            return
        subscription = self.runner.subscribe(conversation_id)
        incoming: queue.Queue[dict[str, Any]]
        backlog: list[dict[str, Any]] = []
        if subscription is None:
            incoming = queue.Queue()
        else:
            incoming, backlog = subscription
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True
        try:
            for pending in backlog:
                self.wfile.write(f"data: {json.dumps(pending, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    event = incoming.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
                if event.get("type") in ("done", "error", "interrupted"):
                    return
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.runner.unsubscribe(conversation_id, incoming)

    def _store_of(self, program_id: str) -> Path:
        """按项目 id 找到它所在的工作区（站点可能同时挂着好几个）。"""
        for store in self._workspaces():
            if any(str(project["id"]) == program_id for project in collect_projects(store)):
                return store
        raise VibeError(f"项目不存在或已归档：{program_id}")

    def _payload(self, body: bytes) -> dict[str, Any]:
        """写接口的 JSON 入参；坏请求也要回一句人话，而不是断开连接。"""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as error:
            raise VibeError(f"请求体不是合法 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise VibeError("请求体必须是 JSON 对象")
        return payload

    def _mutate(self, path: str, body: bytes) -> None:
        if path == "/api/projects/create":
            payload = self._payload(body)
            # 不再问「放进哪个库」：一个项目就是一个工作区，新的一律建在 root 下。
            root = getattr(self, "root", None)
            if root is None:
                raise VibeError("这个服务没有可用的工作区根目录，建不了项目")
            workspace, program = create_workspace(root, str(payload.get("name") or ""))
            self._api(
                {
                    "ok": True,
                    "id": str(program.id),
                    "name": str(program.name),
                    "workspace": str(workspace),
                }
            )
            return
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
            # store 路径同理（工作区名就是项目名，可能是中文）
            wanted = self._pick_store(unquote(self.headers.get("X-Store") or ""))
            ground = resolve_ground_truth()
            result = add_file(ground, name, body)
            self._api({"ok": True, "file": result, "state": self._state(wanted)})
            return
        if path == "/api/inputs/delete":
            payload = self._payload(body)
            ground = resolve_ground_truth()
            name = str(payload.get("name") or "")
            program_id = str(payload.get("programId") or "")
            # 多 store 时按项目找到它自己的那个：事件、.trash、状态都跟项目走
            owner = self._pick_store(payload.get("store"))
            if program_id:
                owner = self._store_of(program_id)
            # 影响面由服务端从同一份视图里算：页面只说是哪个文件。
            # 链路要写给将来的 agent 看，不能靠前端传上来一份可能对不上的。
            tables, chain = _impact(owner, program_id, name)
            event = remove_file(ground, owner, name, tables=tables, chain=chain)
            self._api({"ok": True, "event": event, "state": self._state(owner)})
            return
        if path == "/api/inputs/undo":
            ground = resolve_ground_truth()
            # 撤回的是这个项目所在 store 的最近一次移出，不是第一个 store 的
            owner = self._pick_store(self._payload(body).get("store"))
            event = undo_removal(ground, owner)
            self._api({"ok": True, "event": event, "state": self._state(owner)})
            return
        if path == "/api/inputs/rebuild":
            owner = self._pick_store(self._payload(body).get("store"))
            result = _rebuild_batch()
            archived = promote_trash(owner) if result["code"] == 0 else []
            self._api(
                {"ok": result["code"] == 0, **result, "archived": archived, "state": self._state(owner)}
            )
            return
        self._api({"ok": False, "error": f"未知接口：{path}"}, status=404)


def serve(
    stores: list[Path],
    *,
    port: int = 8731,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    root: Path | None = None,
) -> int:
    """起服务并阻塞。Ctrl+C 结束。"""
    for store in stores:
        if not (store / "control.db").is_file():
            raise VibeError(f"不是 store（缺 control.db）：{store}")
    # 工作区根目录：新建项目落在它下面。不给就用第一个库的上一级。
    workspace_root = root if root is not None else stores[0].parent
    runner = Runner()
    handler = type(
        "BoundHandler",
        (_Handler,),
        {"stores": stores, "root": workspace_root, "runner": runner},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    # 后台 job 靠这个线程唤醒对话：服务活着它就活着
    runner.start_watcher()
    url = f"http://{host}:{port}/"
    print(f"服务已启动：{url}")
    print(f"  工作区 {', '.join(str(item) for item in stores)}")
    print(f"  新建到 {workspace_root}")
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
