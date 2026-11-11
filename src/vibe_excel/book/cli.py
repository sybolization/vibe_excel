"""命令行 book 导出：一个自包含 HTML，或者一整个可离线打开的站点。

输入是 store 目录（含 control.db、runs 与 sessions）。两种输出：

- `--out x.html`     一个项目的单页（旧的用法，行为不变）
- `--out <目录>`     站点：`index.html` 项目索引 + 每个项目一页 + `fonts/`

采集阶段全部只读，唯一写入目标是 --out；会拒绝把结果写回源数据库。
"""

import shutil
import sqlite3
import sys
from pathlib import Path

from ..util import VibeError
from .collect import collect_book
from .index_render import render_index
from .projects import collect_projects, page_name
from .render import _ASSETS, render_book
from .types import BookView

USAGE = (
    "build --store <store 目录> --out <html 路径 | 站点目录> [--session <会话 id>] [--run <运行 id>]\n"
    "        --out 以 .html/.htm 结尾出单页；否则当作站点目录，出 index.html + 每项目一页"
)


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


def _install_fonts(out: Path) -> None:
    """把随包的字体复制到站点目录。

    原样复制，不改文件名、不改内容——许可第 2 条禁止任何修改，
    第 4 条要求任何副本都带上版权声明与协议，所以 LICENSE 一起搬。
    """
    source = _ASSETS / "fonts"
    if not source.is_dir():
        print(f"注意：包内缺少字体目录 {source}，页面将回退到本机字体", file=sys.stderr)
        return
    shutil.copytree(source, out / "fonts", dirs_exist_ok=True)


def _build_site(store: Path, out: Path) -> int:
    """渲染整个站点：索引 + 每个项目一页 + 字体。

    单个项目采不到（没有会话、库被占用等）不拖垮整站：那一行还列着，只是不给链接。
    """
    projects = collect_projects(store)
    out.mkdir(parents=True, exist_ok=True)
    _install_fonts(out)

    pages: dict[str, str] = {}
    for project in projects:
        name = page_name(project["id"])
        try:
            view = collect_book(
                store, program_id=project["id"], session_id=project["activeSessionId"]
            )
        except (VibeError, sqlite3.Error, OSError) as error:
            print(f"  跳过 {project['name']}：{error}", file=sys.stderr)
            continue
        (out / name).write_text(
            render_book(view, fonts="fonts/", home="index.html"), encoding="utf-8"
        )
        pages[project["id"]] = name

    index = render_index(
        projects, store_label=store.name or str(store), pages=pages
    )
    (out / "index.html").write_text(index, encoding="utf-8")
    print(f"程序   {len(projects)} 个，已出页面 {len(pages)} 个")
    for project in projects:
        state = "已出页面" if project["id"] in pages else "未出页面"
        print(f"       {project['name']:<18} {project['runCount']} 次运行 {state}")
    print(f"\n落盘   {out}\\index.html（{round(len(index.encode('utf-8')) / 1024)} KB）")
    return 0


def run_cli(argv: list[str]) -> int:
    """执行一次导出：采集视图模型、渲染 HTML、落盘并打印摘要。

    已知的用户错误与 I/O 错误统一转成返回码 1，不向调用方抛出。
    """
    if argv in (["--help"], ["-h"], ["build", "--help"], ["build", "-h"]):
        print(f"用法：{USAGE}\n产出：一个自包含的 HTML 账页，或一个可离线打开的站点目录")
        return 0
    try:
        flags = _arguments(argv)
        store, out = Path(flags["store"]).resolve(), Path(flags["out"]).resolve()
        # 输出绝不能落在源库上，否则会直接破坏 control.db。
        if out == store / "control.db" or out.suffix.lower() == ".db":
            raise VibeError(f"不能把 HTML 导出到源数据库：{out}")
        if out.suffix.lower() not in (".html", ".htm"):
            # 目录模式：一次把整个 store 的入口和每个项目的账页都出掉。
            # 目录已存在是常态（反复重建站点），只有同名文件挡路才算错误。
            if out.exists() and not out.is_dir():
                raise VibeError(f"--out 是目录模式，但这里已有一个文件：{out}")
            if out == store:
                raise VibeError(f"站点目录不能就是 store 本身：{out}")
            return _build_site(store, out)
        # 单页模式下已有文件只允许是 HTML，避免误覆盖别的产物。
        if out.exists() and out.is_file() and out.suffix.lower() not in (".html", ".htm"):
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
