"""插件：telemetry —— 一次执行的事实流（events.jsonl + timeline.txt）。

只记录观测到的事实：事件类型、序号、时间、工具名、截断的载荷。
不做「进度百分比」之类的推断——那是展示层的事。
换 otel 或远程采集时，替换的是这一个插件。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from vibe_excel.kernel import PluginContext, TelemetrySink, define_plugin
from vibe_excel.util import VibeError


def truncate(value: str, limit: int) -> str:
    """截断长文本；limit 以字符计，超出时追加省略号。"""

    return value if len(value) <= limit else f"{value[:limit]}…"


def brief(raw: object) -> str:
    """从一条事件里抠出人能读的要点；取不到就退回截断的 JSON。"""

    if not isinstance(raw, dict):
        return truncate(str(raw), 140)
    parts: list[str] = []
    tool_name = raw.get("toolName")
    if tool_name is None and raw.get("type") == "tool_execution_start":
        tool_name = raw.get("name")
    if isinstance(tool_name, str):
        parts.append(f"tool={tool_name}")
    if isinstance(raw.get("isError"), bool):
        parts.append(f"isError={raw['isError']}")
    payload = raw.get("result", raw.get("output", raw.get("input", raw.get("text"))))
    if isinstance(payload, str) and payload.strip():
        parts.append(f"«{truncate(' '.join(payload.split()), 180)}»")
    elif payload is not None:
        parts.append(f"«{truncate(_json_text(payload), 180)}»")
    if not parts:
        parts.append(truncate(_json_text(raw), 160))
    return " ".join(parts)


def _json_text(value: object) -> str:
    """把任意对象尽力序列化成 JSON；不可序列化时退回 str()。"""

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _now_iso() -> str:
    """毫秒精度的 UTC ISO 时间戳，尾部用 Z 便于排序与肉眼比对。"""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonlSink:
    """把事件写成 JSONL，并同步维护一份可读时间线。

    events.jsonl 用追加模式：同一个 run_dir 重开不会覆盖旧事件。
    `close()` 之后不应再送事件（此时文件已关闭）；线程安全由调用方保证（当前是单读取线程）。
    """

    def __init__(self, run_dir: str, echo: bool) -> None:
        directory = Path(run_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.run_dir = str(directory)
        # 追加模式而非覆盖：trace 目录可能被同一进程复用（review 与 apply 共用 runs/）。
        self._stream = (directory / "events.jsonl").open("a", encoding="utf-8", newline="\n")
        self._echo = echo
        self._lines: list[str] = []
        self._types: dict[str, int] = {}
        self._total = 0
        self._skipped_deltas = 0
        self._seq = 0
        self._closed = False

    def event(self, raw: object) -> None:
        """记账并写一行 JSONL；流式增量只计数，不进时间线。"""

        event_type = (
            raw.get("type") if isinstance(raw, dict) and isinstance(raw.get("type"), str) else "unknown"
        )
        self._total += 1
        self._types[str(event_type)] = self._types.get(str(event_type), 0) + 1
        self._stream.write(_json_text(raw) + "\n")
        # 每条都 flush：进程可能被 kill，宁可慢一点也不能丢掉最后几条事件。
        self._stream.flush()
        if event_type == "message_update":
            # message_update 是逐 token 的流式增量，进时间线会把真正的事件淹掉。
            self._skipped_deltas += 1
            return
        line = f"{self._seq:04d}  {_now_iso()}  {str(event_type):<22}  {brief(raw)}"
        self._seq += 1
        self._lines.append(line)
        if self._echo:
            print(f"  {line}")

    def timeline(self) -> str:
        """渲染可读时间线；末尾附总数、类型分布与折叠的增量条数。"""

        distribution = " ".join(f"{name}={count}" for name, count in self._types.items())
        return "\n".join(
            [
                *self._lines,
                "",
                f"# 共 {self._total} 条事件，类型 {len(self._types)} 种；"
                f"折叠 {self._skipped_deltas} 条流式增量",
                f"# 类型分布：{distribution}",
            ]
        )

    def event_count(self) -> int:
        """返回收到的事件总数（含被折叠的流式增量）。"""

        return self._total

    def type_counts(self) -> dict[str, int]:
        """返回各事件类型的计数副本。"""

        return dict(self._types)

    def flush(self) -> None:
        """写 timeline.txt 并关闭事件文件；重复调用是空操作。"""

        if self._closed:
            return
        self._closed = True
        (Path(self.run_dir) / "timeline.txt").write_text(
            self.timeline() + "\n", encoding="utf-8"
        )
        self._stream.close()

    def close(self) -> None:
        """`close` 是 flush 的别名，用来满足 TelemetrySink 协议。"""

        self.flush()


class TelemetryConfig:
    """telemetry 配置：是否逐条回显到 stdout。"""

    def __init__(self, echo: bool) -> None:
        self.echo = echo


def _parse_config(raw: object) -> TelemetryConfig:
    """解析配置；缺省（None）表示不回显，其余非对象类型一律报错。"""

    if raw is None:
        return TelemetryConfig(False)
    if not isinstance(raw, dict):
        raise VibeError("telemetry 插件配置必须是对象")
    return TelemetryConfig(raw.get("echo") is True)


class TelemetryService:
    """为运行目录开事件流。

    只负责创建与登记 sink；落盘位置由调用方（sessions）决定。
    """

    def __init__(self, config: TelemetryConfig) -> None:
        self._config = config
        self._open: list[JsonlSink] = []

    def begin(self, run_dir: str) -> TelemetrySink:
        """为 run_dir 创建一个 sink 并登记，便于宿主退出时统一 flush。"""

        sink = JsonlSink(run_dir, self._config.echo)
        self._open.append(sink)
        return sink

    def close_all(self) -> None:
        """flush 所有仍开着的 sink；已关闭的重复 flush 是空操作。"""

        for sink in self._open:
            sink.flush()
        self._open.clear()


def _apply(ctx: PluginContext, config: TelemetryConfig) -> None:
    """注册服务，并把 flush 所有 sink 登记为 dispose 清理动作。"""

    service = TelemetryService(config)
    ctx.provide("telemetry", service)
    ctx.effect(lambda: service.close_all)


plugin = define_plugin(
    name="telemetry", provide="telemetry", config=_parse_config, apply=_apply
)
