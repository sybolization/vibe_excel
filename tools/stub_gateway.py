"""本机假网关：OpenAI 兼容的 /v1/chat/completions，用来验证对话链路。

真模型还没接上时，它至少能证明「发出去 → 落盘 → 回答 → 刷新还在同一段对话」是通的。
把真网关起在同一个端口（23100）就换成了真回答，页面一行都不用改。
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 23100
LAST_QUESTION = ""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - 覆盖父类
        if self.path.rstrip("/").endswith("/models"):
            self._json({"data": [{"id": "stub-model", "object": "model"}]})
            return
        self._json({"ok": True}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - 覆盖父类
        global LAST_QUESTION
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        messages = list(payload.get("messages") or [])
        question = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                question = str(message.get("content") or "")
                break
        LAST_QUESTION = question
        rounds = sum(1 for m in messages if m.get("role") == "user")
        reply = (
            f"（本机假网关）收到第 {rounds} 句：{question[:60]}。"
            "真网关起在 127.0.0.1:23100 时，这里就是模型的回答。"
        )
        print(f"[stub] 收到 {len(messages)} 条上下文，第 {rounds} 句：{question[:40]}", flush=True)
        self._json(
            {
                "id": f"stub-{int(time.time())}",
                "object": "chat.completion",
                "model": payload.get("model") or "stub-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}}],
            }
        )

    def _json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 覆盖父类
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"假网关已启动：http://127.0.0.1:{PORT}/v1", flush=True)
    server.serve_forever()
