"""Command-line book export: only the requested HTML file is written."""

import sqlite3
import sys
from pathlib import Path

from ..util import VibeError
from .collect import collect_book
from .render import render_book
from .types import BookView

USAGE = "build --store <store 目录> --out <html 路径> [--session <会话 id>] [--run <运行 id>]"


def _arguments(argv: list[str]) -> dict[str, str]:
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
    if argv in (["--help"], ["-h"], ["build", "--help"], ["build", "-h"]):
        print(f"用法：{USAGE}\n产出：一个自包含的 HTML 账页，无服务器或网络依赖")
        return 0
    try:
        flags = _arguments(argv)
        store, out = Path(flags["store"]).resolve(), Path(flags["out"]).resolve()
        if out == store / "control.db" or out.suffix.lower() == ".db":
            raise VibeError(f"不能把 HTML 导出到源数据库：{out}")
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
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
