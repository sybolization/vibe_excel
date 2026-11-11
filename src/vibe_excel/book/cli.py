"""命令行 book 导出：只写出请求的 HTML 文件。

输入是 store 目录（含 control.db、runs 与 sessions），输出是一个自包含 HTML。
采集阶段全部只读，唯一写入目标是 --out 指定的 HTML；会拒绝把结果写回源数据库
或覆盖非 HTML 的现有文件。
"""

import sqlite3
import sys
from pathlib import Path

from ..util import VibeError
from .collect import collect_book
from .render import render_book
from .types import BookView

USAGE = "build --store <store 目录> --out <html 路径> [--session <会话 id>] [--run <运行 id>]"


def _arguments(argv: list[str]) -> dict[str, str]:
    """解析 build 子命令的参数。

    只接受 --store/--out/--session/--run 四个标志，并强制要求前两个；
    未知或缺少值的标志直接抛出面向用户的用法错误。
    """
    if not argv or argv[0] != "build":
        raise VibeError(f"用法：{USAGE}")
    flags: dict[str, str] = {}
    index = 1
    while index < len(argv):
        token = argv[index]
        if token not in ("--store", "--out", "--session", "--run"):
            raise VibeError(f"未知参数 {token}\n用法：{USAGE}")
        if index + 1 == len(argv) or argv[index + 1].startswith("--"):
            raise VibeError(f"参数 {token} 缺少值\n用法：{USAGE}")
        flags[token[2:]] = argv[index + 1]
        index += 2
    for name in ("store", "out"):
        if not flags.get(name):
            raise VibeError(f"缺少参数 --{name}\n用法：{USAGE}")
    return flags


def _summarize(view: BookView) -> None:
    """打印人类可读的采集摘要。

    只影响终端输出，不改变视图模型；可选页面缺失时用占位符呈现。
    """
    program, session, graph, inputs = view["program"], view["session"], view["graph"], view["inputs"]
    print(f"程序   {program['name'] if program is not None else '—'}")
    print(f"会话   {session['name'] + '（' + session['id'][:8] + '）' if session is not None else '（无会话）'}")
    print(f"运行   {len(view['runs'])} 次，其中 {sum(run['hasTranscript'] for run in view['runs'])} 次有对话")
    print(f"数据   {len(view['revisions'])} 个版本，工作集 {(view['currentRevision'] or '—')[:8]}")
    if inputs is not None:
        print(f"输入   {len(inputs['files'])} 个文件 → {inputs['dbTables']} 张表")
        print(f"       当前流程读 {inputs['readTables']} 张 · 写 {inputs['writeTables']} 张")
    if graph is not None:
        steps = sum(len(node["steps"]) for node in graph["nodes"])
        print(f"流程   {graph['flow']} · {len(graph['nodes'])} 个节点 · {steps} 步 · 入口 {graph['entry']}")
        for node in graph["nodes"]:
            print(f"       {node['fn']:<18} {len(node['steps'])} 步  {'+'.join(node['reads']) or '—'} → {'+'.join(node['writes']) or '—'}")
    else:
        print("流程   没有可展示的 run.json 流程")
    for warning in view.get("warnings", []):
        print(f"注意   {warning}")


def run_cli(argv: list[str]) -> int:
    """执行一次导出：采集视图模型、渲染 HTML、落盘并打印摘要。

    已知的用户错误与 I/O 错误统一转成返回码 1，不向调用方抛出。
    """
    if argv in (["--help"], ["-h"], ["build", "--help"], ["build", "-h"]):
        print(f"用法：{USAGE}\n产出：一个自包含的 HTML 账页，无服务器或网络依赖")
        return 0
    try:
        flags = _arguments(argv)
        store, out = Path(flags["store"]).resolve(), Path(flags["out"]).resolve()
        # 输出绝不能落在源库上，否则会直接破坏 control.db。
        if out == store / "control.db" or out.suffix.lower() == ".db":
            raise VibeError(f"不能把 HTML 导出到源数据库：{out}")
        # 已有文件只允许是 HTML，避免误覆盖别的产物。
        if out.exists() and out.suffix.lower() not in (".html", ".htm"):
            raise VibeError(f"不能覆盖现有源文件：{out}")
        view = collect_book(store, session_id=flags.get("session"), run_id=flags.get("run"))
        html = render_book(view)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        _summarize(view)
        print(f"\n落盘   {out}（{round(len(html.encode('utf-8')) / 1024)} KB）")
        return 0
    except (VibeError, OSError, UnicodeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1


def main() -> int:
    """供 `python -m vibe_excel.book` 与控制台脚本调用的入口。"""
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
