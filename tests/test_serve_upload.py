"""上传接口的跨层约定：路径与文件名里有中文时，HTTP 头只能放百分号编码。

浏览器不允许 header 值出现 ISO-8859-1 之外的字符——工作区名就是项目名，中文很常见。
这条测试守住两件事：前端发的编码服务端解得回来，文件按原名落盘。
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from vibe_excel.book.inputs_edit import GroundTruth
from vibe_excel.book.serve import _Handler
from vibe_excel.session import SessionStore

PROGRAM = "计算医疗期"


@pytest.fixture()
def server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path, Path]:
    """一个只挂了一个中文名 store 的服务。"""
    store = tmp_path / PROGRAM
    store.mkdir()
    SessionStore.open(store).close()
    inputs = tmp_path / "输入"
    inputs.mkdir()
    # 输入目录来自清单，与 store 无关：这里把它指到临时目录，别碰仓库里的配置。
    # add_file 会把文件名登记进清单，所以清单本身得存在。
    manifest = tmp_path / "vibe.plugins.yml"
    # 名单写成块状：书写器按行插「- 文件名」，行内 [] 它认不出来
    manifest.write_text(
        "sessions:\n  inputs:\n    dir: inputs\n    files:\n      - 旧的.xlsx\n", encoding="utf-8"
    )
    ground = GroundTruth(manifest, inputs, [])
    monkeypatch.setattr("vibe_excel.book.serve.resolve_ground_truth", lambda: ground)

    handler = type("TestHandler", (_Handler,), {"stores": [store], "root": tmp_path})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", store, inputs
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_upload_accepts_a_chinese_store_path(server: tuple[str, Path, Path]) -> None:
    base, store, inputs = server
    request = Request(
        f"{base}/api/inputs/upload",
        data=b"excel-bytes",
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            # 和 app.js 一致：文件名与 store 都先百分号编码再进 header
            "X-Filename": quote("刘智所有请假信息_202610211157.XLS", safe=""),
            "X-Store": quote(str(store), safe=""),
        },
    )
    payload = json.loads(urlopen(request, timeout=10).read())

    assert payload["ok"] is True
    assert payload["file"]["name"] == "刘智所有请假信息_202610211157.XLS"
    assert (inputs / "刘智所有请假信息_202610211157.XLS").read_bytes() == b"excel-bytes"
    # 状态回到的是同一个库：不是默认的第一个 store 就算过
    assert payload["state"]["store"] == str(store)
