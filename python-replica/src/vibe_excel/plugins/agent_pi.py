"""插件：agent-pi —— 用 pi 作为「写程序」的那一半。

与 ADR-0001 的结论一致：不新增任何模型可调用的工具、子进程只拿到最小环境。
换 ACP / 换别的 agent 时替换这一个插件。
本文件只管子进程与 JSONL 协议，不决定提示词内容或流程语义；超时与凭证变量名来自清单配置。

两个已经踩过的坑写在这里：
  - 子进程环境必须是**替换**而不是合并，否则宿主的其它凭证会跟着泄漏；
  - `get_session_stats` 必须在 `stop()` 之前调用，否则拿到空对象。
"""

from __future__ import annotations

import codecs
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO, Literal

from vibe_excel.kernel import (
    AgentOutcome,
    AgentRequest,
    AskQuestion,
    EventSink,
    PluginContext,
    QuestionHandler,
    define_plugin,
)
from vibe_excel.process import minimal_environment
from vibe_excel.util import VibeError

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
DEFAULT_TIMEOUT_MS = 20 * 60 * 1000
DEFAULT_ASK_TIMEOUT_MS = 5 * 60 * 1000
DEFAULT_API_KEY_ENV = "OPENCODE_API_KEY"
# 单条 RPC 响应的等待上限，与会话整体超时分开：协议层卡住要尽快失败而不是干等。
RPC_RESPONSE_TIMEOUT_MS = 30 * 1000
EXPORT_TIMEOUT_MS = 120 * 1000

UiAnswer = Literal["handled", "ignored"]


@dataclass(frozen=True)
class AgentPiConfig:
    """agent-pi 插件配置；凭证的值只在内存里用，不落盘、不打印。

    这里只存凭证的「变量名」`api_key_env`，真正的值在运行时从宿主环境读取。
    """

    cli: str
    provider: str
    model: str
    api_key_env: str
    timeout_ms: int
    ask_timeout_ms: int


def _required_text(raw: Mapping[str, object], field: str) -> str:
    """取必填字符串字段；缺失或全空白都报 VibeError。"""

    value = raw.get(field)
    if not isinstance(value, str) or value.strip() == "":
        raise VibeError(f"agent-pi 插件需要 config.{field}")
    return value.strip()


def _duration(raw: Mapping[str, object], field: str, default: int) -> int:
    """解析毫秒时长；接受数字或数字字符串，拒绝 bool 与 <=0。"""

    value = raw.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise VibeError(f"agent-pi 插件 config.{field} 必须是正数（毫秒）")
    try:
        number = float(value)
    except ValueError as error:
        raise VibeError(f"agent-pi 插件 config.{field} 必须是正数（毫秒）") from error
    if number <= 0:
        raise VibeError(f"agent-pi 插件 config.{field} 必须是正数（毫秒）")
    return int(number)


def _parse_config(raw: object) -> AgentPiConfig:
    """校验配置；缺 provider/model 时在加载期就报错，不悄悄用默认值。"""

    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise VibeError("agent-pi 插件配置必须是字符串键的对象")
    provider = _required_text(raw, "provider")
    model = _required_text(raw, "model")
    cli_raw = raw.get("cli", "")
    if not isinstance(cli_raw, str):
        raise VibeError("agent-pi 插件 config.cli 必须是字符串")
    api_key_raw = raw.get("apiKeyEnv")
    api_key_env = (
        api_key_raw
        if isinstance(api_key_raw, str) and api_key_raw.strip() != ""
        else DEFAULT_API_KEY_ENV
    )
    return AgentPiConfig(
        cli=cli_raw.strip(),
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        timeout_ms=_duration(raw, "timeoutMs", DEFAULT_TIMEOUT_MS),
        ask_timeout_ms=_duration(raw, "askTimeoutMs", DEFAULT_ASK_TIMEOUT_MS),
    )


def _command_for(cli_path: Path) -> list[str]:
    """按文件类型选启动方式：JS 走 node，脚本走当前解释器，其余直接执行。"""

    suffix = cli_path.suffix.lower()
    if suffix in (".js", ".mjs", ".cjs"):
        return ["node", str(cli_path)]
    if suffix == ".py":
        return [sys.executable, str(cli_path)]
    if suffix in (".cmd", ".bat"):
        return ["cmd", "/c", str(cli_path)]
    return [str(cli_path)]


def resolve_pi_cli(config: AgentPiConfig) -> Path:
    """解析 pi CLI；空配置时只从 `VIBE_EXCEL_PI` 找，并给出可诊断的错误。"""

    configured = config.cli.strip()
    if configured != "":
        candidate = Path(configured).expanduser()
        source = "config.cli"
    else:
        from_env = os.environ.get("VIBE_EXCEL_PI", "").strip()
        if from_env == "":
            raise VibeError(
                "找不到 pi CLI：config.cli 为空，环境变量 VIBE_EXCEL_PI 也未设置。"
                "请在 vibe.plugins.yml 的 cli 填绝对路径，或设置 VIBE_EXCEL_PI 指向 pi 的 cli.js"
            )
        candidate = Path(from_env).expanduser()
        source = "环境变量 VIBE_EXCEL_PI"
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise VibeError(f"pi CLI 不存在：{candidate}（来源：{source}）")
    return candidate


def resolve_ask_user_extension() -> Path:
    """我们自己的 pi 扩展：`ask_user`；缺失时在启动前报错。"""

    path = Path(__file__).resolve().parent.parent / "ext" / "ask-user.ts"
    if not path.is_file():
        raise VibeError(f"pi 扩展缺失：{path}")
    return path


def _read_chunk(stream: BinaryIO) -> bytes:
    """优先用 `read1`：普通 `read` 会等满缓冲区，长连接下会把事件流卡住。"""

    reader = getattr(stream, "read1", None)
    if callable(reader):
        data = reader(4096)
        return data if isinstance(data, bytes) else b""
    return stream.read(1024)


class PiRpcClient:
    """pi RPC 子进程的最小同步客户端（严格 JSONL：只按 `\\n` 切帧）。

    生命周期：`start()` 之后才能调用其它方法；`stop()` 可以重复调用，
    未启动时是空操作；停止后所有挂起的 RPC 都会被拒绝。
    """

    def __init__(
        self,
        *,
        cli_path: Path,
        cwd: str,
        env: Mapping[str, str],
        provider: str,
        model: str,
        args: list[str],
        ask_timeout_ms: int,
        on_question: QuestionHandler | None,
        on_event: EventSink | None,
        log: Callable[[str], None],
    ) -> None:
        self._cli_path = cli_path
        self._cwd = cwd
        self._env = dict(env)
        self._provider = provider
        self._model = model
        self._args = list(args)
        self._ask_timeout_ms = ask_timeout_ms
        self._on_question = on_question
        self._on_event = on_event
        self._log = log

        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: IO[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._pending: dict[str, Future[dict[str, object]]] = {}
        self._pending_lock = threading.Lock()
        self._request_id = 0
        self._settled = threading.Event()
        self._exit_error: str | None = None
        self._stderr = ""
        self._stderr_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None

    def start(self) -> None:
        """启动子进程并起两条读取线程；启动即退出的进程会在这里被识别成错误。"""

        command = [
            *_command_for(self._cli_path),
            "--mode",
            "rpc",
            "--provider",
            self._provider,
            "--model",
            self._model,
            *self._args,
        ]
        self._process = subprocess.Popen(
            command,
            cwd=self._cwd,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=NO_WINDOW,
        )
        self._stdin = self._process.stdin
        stdout = self._process.stdout
        stderr = self._process.stderr
        if stdout is None or stderr is None:
            self.stop()
            raise VibeError("pi 子进程管道不可用")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pi-ui")
        self._threads = [
            threading.Thread(target=self._read_stdout, args=(stdout,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(stderr,), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

        time.sleep(0.1)
        # 先等一小会儿再 poll：把「启动即崩溃」变成同步错误，而不是让调用方等满超时。
        code = self._process.poll()
        if code is not None:
            time.sleep(0.2)
            raise VibeError(
                f"pi 子进程启动即退出（code={code}）：{self.stderr_tail()}"
            )

    def stop(self) -> None:
        """关 stdin → terminate → kill 逐级降级，最后拒绝所有挂起的 RPC；可重复调用。"""

        process = self._process
        if process is None:
            return
        stdin = self._stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
        try:
            process.terminate()
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for thread in self._threads:
            thread.join(timeout=1)
        self._threads = []
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._process = None
        self._stdin = None
        self._reject_pending(VibeError("pi 子进程已停止"))

    def write_ui_response(self, payload: dict[str, object]) -> bool:
        """按 RPC 协议往 stdin 写一行；子进程已经没了就返回 False。"""

        return self._write(payload)

    def prompt_and_wait(self, message: str, timeout_ms: int) -> None:
        """发送 prompt，等到 `agent_settled`；超时按失败处理。"""

        self._settled.clear()
        response = self._request(
            {"type": "prompt", "message": message}, RPC_RESPONSE_TIMEOUT_MS, "提交提示词"
        )
        self._ensure_success(response, "提交提示词")
        if not self._settled.wait(timeout_ms / 1000):
            raise VibeError(f"等待 pi 完成超时（{round(timeout_ms / 1000)}s）")
        if self._exit_error is not None:
            raise VibeError(self._exit_error)

    def session_stats(self) -> dict[str, object]:
        """必须在 stop 之前调用，否则子进程已经没了。"""

        response = self._request(
            {"type": "get_session_stats"}, RPC_RESPONSE_TIMEOUT_MS, "读取会话统计"
        )
        self._ensure_success(response, "读取会话统计")
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    def answer_ui_request(self, event: Mapping[str, object]) -> UiAnswer:
        """把 `extension_ui_request` 转给上层，并按同一个 id 写回答案。"""

        method = event.get("method")
        if method not in ("input", "select"):
            return "ignored"
        identifier = event.get("id")
        if not isinstance(identifier, str) or identifier == "":
            return "ignored"
        title = event.get("title") if isinstance(event.get("title"), str) else ""
        options_raw = event.get("options")
        options = (
            [str(item) for item in options_raw] if isinstance(options_raw, list) else []
        )

        answer: str | None = None
        if self._on_question is not None:
            try:
                answer = self._ask(AskQuestion(identifier, str(title), options))
            except Exception as error:  # noqa: BLE001 - 用户侧异常按未回答处理
                self._log(f"[ask_user] {error}：按未回答处理")
                answer = None

        if answer is None:
            payload: dict[str, object] = {
                "type": "extension_ui_response",
                "id": identifier,
                "cancelled": True,
            }
        else:
            payload = {
                "type": "extension_ui_response",
                "id": identifier,
                "value": answer,
            }
        if not self._write(payload):
            self._log("[ask_user] 子进程已退出，回答没有送达")
        return "handled"

    def stderr_tail(self) -> str:
        """取 stderr 最后三行；子进程没有任何输出时给出明确文案而不是空串。"""

        with self._stderr_lock:
            text = self._stderr.strip()
        if text == "":
            return "（pi 没有输出 stderr）"
        return " | ".join(text.splitlines()[-3:])

    def _ask(self, question: AskQuestion) -> str | None:
        """在独立线程里调用上层提问回调，并施加 askTimeoutMs；回调异常转成 VibeError。"""

        handler = self._on_question
        if handler is None:
            return None
        answers: list[str | None] = []
        failures: list[Exception] = []
        done = threading.Event()

        def invoke() -> None:
            try:
                answers.append(handler(question))
            except Exception as error:  # noqa: BLE001 - 用户侧异常按未回答处理
                failures.append(error)
            finally:
                done.set()

        threading.Thread(target=invoke, daemon=True, name="pi-ask").start()
        if not done.wait(self._ask_timeout_ms / 1000):
            raise VibeError(
                f"等待用户回答超时（{round(self._ask_timeout_ms / 1000)}s）"
            ) from None
        if failures:
            raise VibeError(str(failures[0]) or failures[0].__class__.__name__)
        return answers[0] if answers else None

    def _request(
        self, command: dict[str, object], timeout_ms: int, label: str
    ) -> dict[str, object]:
        """按 id 发送一条 RPC 并同步等响应；超时或写失败都会清理挂起项。"""

        if self._process is None or self._stdin is None:
            raise VibeError("pi 子进程未启动")
        if self._exit_error is not None:
            raise VibeError(self._exit_error)
        # 先登记 future 再写：响应可能在 write 返回之前就已经到达读取线程。
        with self._pending_lock:
            self._request_id += 1
            identifier = f"req_{self._request_id}"
            future: Future[dict[str, object]] = Future()
            self._pending[identifier] = future
        payload = {**command, "id": identifier}
        if not self._write(payload):
            with self._pending_lock:
                self._pending.pop(identifier, None)
            raise VibeError(f"{label} 失败：pi 子进程 stdin 不可写")
        try:
            return future.result(timeout=timeout_ms / 1000)
        except FutureTimeoutError:
            raise VibeError(f"{label} 超时（{round(timeout_ms / 1000)}s）") from None
        finally:
            with self._pending_lock:
                self._pending.pop(identifier, None)

    def _ensure_success(self, response: dict[str, object], label: str) -> None:
        """把 success != True 的响应转成 VibeError，并尽量带上服务端给的错误文本。"""

        if response.get("success") is not True:
            detail = response.get("error")
            reason = detail if isinstance(detail, str) else "未知原因"
            raise VibeError(f"{label} 失败：{reason}")

    def _write(self, payload: dict[str, object]) -> bool:
        """把一行 JSON 写进 stdin；子进程已退出或管道损坏时返回 False 而不是抛异常。"""

        stdin = self._stdin
        if stdin is None or stdin.closed:
            return False
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            stdin.write(line.encode("utf-8"))
            stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    def _read_stdout(self, stream: BinaryIO) -> None:
        """按 `\\n` 切帧（协议是严格 JSONL）；EOF 时把未完成的行也解析掉。"""

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        try:
            while True:
                chunk = _read_chunk(stream)
                if not chunk:
                    break
                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.endswith("\r"):
                        line = line[:-1]
                    if line.strip() != "":
                        self._handle_line(line)
            buffer += decoder.decode(b"", final=True)
            if buffer.strip() != "":
                self._handle_line(buffer.rstrip("\r"))
        except (OSError, ValueError) as error:
            self._exit_error = f"读取 pi stdout 失败：{error}"
        finally:
            if not self._settled.is_set():
                # stdout 关闭意味着子进程不会再产出：没 settled 就按失败标记，别让等待方挂死。
                process = self._process
                code = process.poll() if process is not None else None
                self._exit_error = (
                    f"pi 子进程在完成前退出（code={code}）：{self.stderr_tail()}"
                )
            self._settled.set()
            pending_error = self._exit_error
            if pending_error is not None:
                self._reject_pending(VibeError(pending_error))

    def _read_stderr(self, stream: BinaryIO) -> None:
        """持续收集 stderr；只用于出错时的诊断尾巴，不参与任何判定。"""

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = _read_chunk(stream)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    with self._stderr_lock:
                        self._stderr += text
        except (OSError, ValueError):
            return


    def _handle_line(self, line: str) -> None:
        """分发一行 JSON：response 唤醒对应 future，agent_settled 放行等待，ui 请求进线程池。"""

        try:
            decoded: object = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(decoded, dict):
            return
        data: dict[str, object] = {
            key: value for key, value in decoded.items() if isinstance(key, str)
        }
        if data.get("type") == "response":
            identifier = data.get("id")
            if isinstance(identifier, str):
                with self._pending_lock:
                    future = self._pending.pop(identifier, None)
                if future is not None:
                    if not future.done():
                        future.set_result(data)
                    return
        if data.get("type") == "agent_settled":
            self._settled.set()
        if data.get("type") == "extension_ui_request":
            executor = self._executor
            if executor is not None:
                # 用单线程池而不是在读取线程里直接调用：提问会阻塞，不能卡住 stdout 读取。
                executor.submit(self.answer_ui_request, data)
        if self._on_event is not None:
            self._on_event(data)

    def _reject_pending(self, error: VibeError) -> None:
        """把所有挂起的 future 以异常收尾，避免调用方永久等待。"""

        with self._pending_lock:
            futures = list(self._pending.values())
            self._pending.clear()
        for future in futures:
            if not future.done():
                future.set_exception(error)


class AgentPiService:
    """把 pi 子进程的生命周期包装成 kernel.AgentService。

    无状态：每次 run 都新起一个子进程，失败信息落进 outcome 而不是抛出。
    """

    def __init__(self, config: AgentPiConfig) -> None:
        self._config = config

    def run(self, request: AgentRequest) -> AgentOutcome:
        """一次完整会话：校验凭证 → 起子进程 → 发提示词 → 取统计 → 停进程 → 收集会话文件。"""

        cli_path = resolve_pi_cli(self._config)
        api_key = os.environ.get(self._config.api_key_env)
        if api_key is None or api_key == "":
            raise VibeError(f"缺少凭证环境变量 {self._config.api_key_env}")
        extension = resolve_ask_user_extension()

        started = time.monotonic()
        events = 0
        tools: set[str] = set()
        tool_calls = 0

        def handle_event(event: object) -> None:
            nonlocal events, tool_calls
            events += 1
            if isinstance(event, dict):
                event_type = event.get("type")
                direct = event.get("toolName")
                if direct is None and event_type == "tool_execution_start":
                    direct = event.get("name")
                if isinstance(direct, str):
                    tools.add(direct)
                    if event_type == "tool_execution_start":
                        tool_calls += 1
            if request.on_event is not None:
                request.on_event(event)

        args = [
            "--no-approve",
            "--offline",
            "--session-dir",
            request.session_dir,
            "--extension",
            str(extension),
        ]
        # 环境是「替换」而不是合并：只带凭证与 agent 目录，宿主的其它变量不进子进程。
        env = minimal_environment(
            {
                self._config.api_key_env: api_key,
                "PI_CODING_AGENT_DIR": request.agent_dir,
            }
        )
        client = PiRpcClient(
            cli_path=cli_path,
            cwd=request.cwd,
            env=env,
            provider=self._config.provider,
            model=self._config.model,
            args=args,
            ask_timeout_ms=self._config.ask_timeout_ms,
            on_question=request.on_question,
            on_event=handle_event,
            log=lambda line: print(line),
        )

        ok = False
        error: str | None = None
        try:
            client.start()
            client.prompt_and_wait(
                request.prompt,
                self._config.timeout_ms if request.timeout_ms is None else request.timeout_ms,
            )
            ok = True
        except Exception as caught:  # noqa: BLE001 - 失败要落进 outcome，不吞掉
            error = str(caught) or caught.__class__.__name__

        stats: dict[str, object] = {}
        try:
            # 必须在 stop 之前取：stop 会关掉子进程，统计接口随之失效。
            stats = client.session_stats()
        except Exception:  # noqa: BLE001 - 统计拿不到不影响主流程
            # 统计只进报告，不参与判定：拿不到也继续。
            stats = {}
        client.stop()

        session_files: list[str] = []
        session_dir = Path(request.session_dir)
        if session_dir.exists():
            try:
                session_files = sorted(
                    str(path)
                    for path in session_dir.iterdir()
                    if path.is_file() and path.name.endswith(".jsonl")
                )
            except OSError:
                session_files = []

        return AgentOutcome(
            ok=ok,
            error=error,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            event_count=events,
            tool_names=sorted(tools),
            tool_call_count=tool_calls,
            session_files=session_files,
            stats=stats,
        )

    def export_html(self, session_file: str, out_file: str) -> bool:
        """用 pi 自带的 `--export` 把会话文件导出成 HTML；失败不影响主流程。"""

        try:
            cli_path = resolve_pi_cli(self._config)
            result = subprocess.run(
                [*_command_for(cli_path), "--export", session_file, out_file],
                capture_output=True,
                check=False,
                timeout=EXPORT_TIMEOUT_MS / 1000,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError, VibeError):
            return False
        return result.returncode == 0


def _apply(ctx: PluginContext, config: object) -> None:
    """把已校验的配置包成 AgentPiService 并注册为 agent 服务。"""

    if not isinstance(config, AgentPiConfig):
        raise VibeError("agent-pi 插件配置未经校验：缺少 config 校验器？")
    ctx.provide("agent", AgentPiService(config))


plugin = define_plugin(
    name="agent-pi",
    provide="agent",
    config=_parse_config,
    apply=_apply,
)
