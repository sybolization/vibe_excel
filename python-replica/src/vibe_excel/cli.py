"""CLI：组合根。

它只做「读清单 → 起宿主 → 把子命令交给 commands 服务」。
子命令全部由插件注册（sessions 插件注册 setup/run/flow/publish/optimize/status/author/db），
所以新增一个命令不需要改这个文件。
它不实现任何命令细节，也不常驻：一次调用只构造一个宿主，退出前一定 dispose。
退出码约定：0 成功、1 命令自己的业务失败、2 用法或清单加载错误。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vibe_excel.host import format_reports, load_host
from vibe_excel.kernel import CommandContext, CommandSpec, CommandsService
from vibe_excel.util import VibeError


@dataclass(slots=True)
class CliOptions:
    """清单路径、补丁层、环境变量与输出函数。"""

    manifest: str | None = None
    # 补丁按顺序叠加：后面的补丁看到的是前面补丁改过的清单。
    patches: list[str] = field(default_factory=list)
    # env 只用于解析清单里的 ${VAR}，不会进入 exec / agent 的子进程环境。
    env: Mapping[str, str] | None = None
    out: Callable[[str], None] | None = None


def default_manifest() -> str:
    """按开发态与安装态的顺序定位默认清单。"""

    # 顺序：当前目录（开发态）→ 与包同目录（wheel 把 yml 塞进包里）→ 仓库根（源码直接跑）。
    candidates = [
        Path.cwd() / "vibe.plugins.yml",
        Path(__file__).resolve().parent / "vibe.plugins.yml",
        Path(__file__).resolve().parents[2] / "vibe.plugins.yml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise VibeError("找不到 vibe.plugins.yml：请在清单目录运行，或设置 VIBE_EXCEL_MANIFEST")


def parse_argv(argv: list[str]) -> tuple[str | None, list[str], dict[str, str]]:
    """`--key=value` / `--key` → flags；第一个非 flag 是命令名。"""

    rest: list[str] = []
    flags: dict[str, str] = {}
    for argument in argv:
        if not argument.startswith("--"):
            rest.append(argument)
            continue
        name, separator, value = argument[2:].partition("=")
        # 不带值的 flag 记为 "true"：命令自己决定它是不是布尔开关，未知 flag 不报错。
        flags[name] = value if separator else "true"
    return (rest[0] if rest else None, rest[1:], flags)


def print_help(commands: list[CommandSpec], out: Callable[[str], None]) -> None:
    """打印命令表；列宽按最长命令名对齐，宿主命令至少占 9 格。"""

    out("用法：vibectl <命令> [--flag=value]")
    out("")
    out("命令（由插件注册）：")
    width = max((len(command.name) for command in commands), default=6)
    for command in commands:
        out(f"  {command.name:<{width}}  {command.summary}")
    out("")
    out("宿主命令：")
    host_width = max(width, 9)
    out(f"  {'plugins':<{host_width}}  列出清单里的插件与加载状态")
    out(f"  {'help':<{host_width}}     显示这段说明")


def run_cli(argv: list[str], options: CliOptions | None = None) -> int:
    """执行一条 CLI 调用；不终止调用方进程，返回退出码。"""

    settings = options or CliOptions()
    out = settings.out or print
    manifest_path = (
        settings.manifest
        or os.environ.get("VIBE_EXCEL_MANIFEST")
        or default_manifest()
    )

    try:
        host = load_host(manifest_path, settings.patches, settings.env)
    except Exception as error:  # noqa: BLE001 - 清单加载失败必须转成退出码
        print(f"加载清单失败：{error}", file=sys.stderr)
        # 退出码 2 区分于命令自身返回的 1（业务失败）：2 表示根本没跑起来。
        return 2

    try:
        for report in host.reports():
            if report.state == "FAILED":
                # 单个插件失败只警告不中止：让 `plugins` / `help` 仍然可用，便于排错。
                print(
                    f"插件加载失败 {report.id}：{report.error or '未知错误'}",
                    file=sys.stderr,
                )

        commands_raw = host.ctx.get("commands")
        if commands_raw is None:
            print("commands 服务未就绪：检查清单里 commands 插件是否被禁用", file=sys.stderr)
            return 2
        commands: CommandsService = commands_raw  # type: ignore[assignment]

        command, rest, flags = parse_argv(argv)
        if not command or command in ("help", "--help"):
            print_help(commands.all(), out)
            return 0
        if command == "plugins":
            out(format_reports(host.reports()))
            return 0

        spec = commands.find(command)
        if spec is None:
            print(f"未知命令：{command}", file=sys.stderr)
            print_help(commands.all(), out)
            return 2
        context = CommandContext(argv=rest, flags=flags, print=out)
        return spec.run(context)
    finally:
        # 用 finally 保证 dispose：插件清理（数据库句柄、子进程）不能依赖调用方的纪律。
        host.dispose()


def main() -> None:
    """控制台入口。"""

    raise SystemExit(run_cli(sys.argv[1:]))
