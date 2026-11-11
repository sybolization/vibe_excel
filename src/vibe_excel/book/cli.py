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
from .types import BookView, ProjectSummary

USAGE = (
    "serve --store <store 目录> [--store ...] [--port 8731] [--no-open]\n"
    "        起本地服务：现场采集页面，导入/删除/撤回/重建批次走接口\n"
    "build --store <store 目录> [--store ...] --out <html 路径 | 站点目录>"
    " [--session <会话 id>] [--run <运行 id>]\n"
    "        --store 可以给多次，多个 store 合成一个索引；\n"
    "        --out 以 .html/.htm 结尾出单页（只接一个 store），否则出站点"
)


def _arguments(argv: list[str]) -> tuple[list[Path], dict[str, str]]:
    """解析 build 子命令的参数，返回 (store 列表, 其余标志)。

    --store 可以重复给：多个 store 合成一个索引页。
    --session / --run 只对单页模式有意义。
    未知或缺少值的标志直接抛出面向用户的用法错误。
    """
    if not argv or argv[0] != "build":
        raise VibeError(f"用法：{USAGE}")
    stores: list[Path] = []
    flags: dict[str, str] = {}
    index = 1
    while index < len(argv):
        token = argv[index]
        if token not in ("--store", "--out", "--session", "--run"):
            raise VibeError(f"未知参数 {token}\n用法：{USAGE}")
        if index + 1 == len(argv) or argv[index + 1].startswith("--"):
            raise VibeError(f"参数 {token} 缺少值\n用法：{USAGE}")
        if token == "--store":
            stores.append(Path(argv[index + 1]).resolve())
        else:
            flags[token[2:]] = argv[index + 1]
        index += 2
    if not stores:
        raise VibeError(f"缺少参数 --store\n用法：{USAGE}")
    if not flags.get("out"):
        raise VibeError(f"缺少参数 --out\n用法：{USAGE}")
    return stores, flags


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


def _build_site(stores: list[Path], out: Path) -> int:
    """渲染整个站点：索引 + 每个项目一页 + 字体。

    多个 store 合成一张列表，每个项目记住自己来自哪个 store，索引页据此标出来源。
    单个项目采不到（没有会话、库被占用等）不拖垮整站：那一行还列着，只是不给链接。
    """
    multi = len(stores) > 1
    pairs: list[tuple[Path, ProjectSummary]] = []
    for store in stores:
        for project in collect_projects(store):
            if multi:
                # 「store」这种通用目录名单独看没有信息量，带上它的上一层更好认
                name = store.name or str(store)
                project["store"] = (
                    name if name.lower() != "store" else f"{store.parent.name}/{name}"
                )
            pairs.append((store, project))
    # 合并后重新排一次：最近活跃的在前，与单个 store 的规则保持一致。
    pairs.sort(
        key=lambda item: (
            (item[1]["lastRun"]["at"] if item[1]["lastRun"] is not None else None)
            or item[1]["createdAt"]
        ),
        reverse=True,
    )

    out.mkdir(parents=True, exist_ok=True)
    _install_fonts(out)

    pages: dict[str, str] = {}
    used: set[str] = set()
    for store, project in pairs:
        name = page_name(project["id"])
        if name in used:
            # 理论上 id 是 uuid 不会撞；真撞了也不能互相覆盖。
            name = f"p-{project['id'][:8]}-{len(used)}.html"
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
        used.add(name)

    label = stores[0].name if not multi else f"{len(stores)} 个 store"
    index = render_index([project for _, project in pairs], store_label=label, pages=pages)
    (out / "index.html").write_text(index, encoding="utf-8")
    print(f"程序   {len(pairs)} 个，已出页面 {len(pages)} 个")
    for store, project in pairs:
        where = f"{store.name}/" if multi else ""
        state = "已出页面" if project["id"] in pages else "未出页面"
        print(f"       {where}{project['name']:<18} {project['runCount']} 次运行 {state}")
    print(f"\n落盘   {out}\\index.html（{round(len(index.encode('utf-8')) / 1024)} KB）")
    return 0


def _run_serve(argv: list[str]) -> int:
    """serve 子命令：解析参数后起服务，阻塞到 Ctrl+C。"""
    stores: list[Path] = []
    port = 8731
    open_browser = True
    index = 1
    while index < len(argv):
        token = argv[index]
        if token not in ("--store", "--port", "--no-open"):
            raise VibeError(f"未知参数 {token}\n用法：{USAGE}")
        if token == "--no-open":
            open_browser = False
            index += 1
            continue
        if index + 1 == len(argv) or argv[index + 1].startswith("--"):
            raise VibeError(f"参数 {token} 缺少值\n用法：{USAGE}")
        value = argv[index + 1]
        if token == "--store":
            stores.append(Path(value).resolve())
        else:
            try:
                port = int(value)
            except ValueError as error:
                raise VibeError(f"--port 需要整数：{value}") from error
        index += 2
    if not stores:
        raise VibeError(f"缺少参数 --store\n用法：{USAGE}")
    from .serve import serve

    return serve(stores, port=port, open_browser=open_browser)


def run_cli(argv: list[str]) -> int:
    """执行一次导出：采集视图模型、渲染 HTML、落盘并打印摘要。

    已知的用户错误与 I/O 错误统一转成返回码 1，不向调用方抛出。
    """
    if argv and argv[0] == "serve" and argv not in (["serve", "--help"], ["serve", "-h"]):
        try:
            return _run_serve(argv)
        except (VibeError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 1
    if argv in (["--help"], ["-h"], ["build", "--help"], ["build", "-h"], ["serve", "--help"]):
        print(f"用法：{USAGE}\n产出：一个自包含的 HTML 账页，或一个可离线打开的站点目录")
        return 0
    try:
        stores, flags = _arguments(argv)
        store, out = stores[0], Path(flags["out"]).resolve()
        # 输出绝不能落在源库上，否则会直接破坏 control.db。
        if out == store / "control.db" or out.suffix.lower() == ".db":
            raise VibeError(f"不能把 HTML 导出到源数据库：{out}")
        if out.suffix.lower() not in (".html", ".htm"):
            # 目录模式：一次把整个 store 的入口和每个项目的账页都出掉。
            # 目录已存在是常态（反复重建站点），只有同名文件挡路才算错误。
            if out.exists() and not out.is_dir():
                raise VibeError(f"--out 是目录模式，但这里已有一个文件：{out}")
            if out in stores:
                raise VibeError(f"站点目录不能就是 store 本身：{out}")
            return _build_site(stores, out)
        # 单页模式下已有文件只允许是 HTML，避免误覆盖别的产物。
        if out.exists() and out.is_file() and out.suffix.lower() not in (".html", ".htm"):
            raise VibeError(f"不能覆盖现有源文件：{out}")

        if len(stores) > 1:
            raise VibeError("单页模式只接一个 store；要看多个 store 请用站点模式（--out 给目录）")
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
