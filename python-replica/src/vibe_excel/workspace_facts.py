"""工作区事实：把「数据库在哪、Python 在哪、venv 里装了什么」变成一份**探测出来的**表。

为什么不是写死在提示词里：路径会变（换机器、目录搬家、venv 被 uv 重建）。写死的句子
在路径失效时会**安静地**变成谎言——模型拿着一条不存在的路径，提示词还理直气壮地说
「只能用这个」。所以这里每次运行前重新探测，探测不到的东西必须给出**可执行的下一步**，
而不是一句 missing。

本模块只回答「工作区长什么样」，不回答「代码要怎么写」——后者是提示词的事。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

DEFAULT_PACKAGES: tuple[str, ...] = ("pandas", "openpyxl", "xlrd")
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

VenvManager = Literal["uv", "venv", "unknown"]

# 只查 dist-info，不真的 import pandas（快一个数量级）。
PACKAGE_SCRIPT = "\n".join(
    [
        "import json,sys,importlib.metadata as m",
        "r={}",
        "for n in sys.argv[1:]:",
        "    try:r[n]=m.version(n)",
        "    except Exception:r[n]=None",
        "print(json.dumps(r))",
    ]
)


@dataclass(frozen=True)
class ProbeDeps:
    """探测时要知道的坐标；全部由 sessions 插件从会话上下文解析出来。

    这些字段是「坐标」而不是「事实」：路径是否存在、里面有什么，由探测函数实测填入。
    """

    program: str
    session_id: str
    work: str | Path
    code: str | Path
    agent: str | Path
    python: str
    db_path: str | Path
    flow_path: str | Path
    inputs_dir: str | Path
    out_dir: str | Path
    as_of: str


@dataclass(frozen=True)
class PackageState:
    """一个依赖包在本解释器里的版本；version 为 None 表示没装上或读不到元数据。"""

    name: str
    version: str | None


@dataclass(frozen=True)
class CodeFacts:
    """程序仓的代码规模；只数 .py 文件，不评估质量。"""

    path: str
    py_files: int


@dataclass(frozen=True)
class VenvFacts:
    """解释器所属 venv 及其创建者；`managed_by` 靠 pyvenv.cfg 判断，判不出时为 unknown。"""

    dir: str
    managed_by: VenvManager


@dataclass(frozen=True)
class PythonFacts:
    """解释器的存在性、版本、venv 归属与已装依赖；不存在时后面几项都退化为空。"""

    path: str
    exists: bool
    version: str | None
    venv: VenvFacts | None
    packages: list[PackageState]


@dataclass(frozen=True)
class UvFacts:
    """uv 的路径与版本；PATH 上没有 uv 时两者都是 None。"""

    path: str | None
    version: str | None


@dataclass(frozen=True)
class TableState:
    """业务库里一张表的行数；行数是探测快照，不是业务校验。"""

    name: str
    rows: int


@dataclass(frozen=True)
class DbFacts:
    """业务库文件的事实；tables 为空可能表示库不存在、是空库，或打不开。"""

    path: str
    exists: bool
    bytes: int
    mtime: str | None
    tables: list[TableState]


@dataclass(frozen=True)
class InputFile:
    """输入批次里的一个文件及其字节数。"""

    name: str
    bytes: int


@dataclass(frozen=True)
class InputsFacts:
    """输入批次目录与其中的文件清单（不递归）。"""

    dir: str
    exists: bool
    files: list[InputFile]


@dataclass(frozen=True)
class OutFacts:
    """输出目录是否存在与文件数（只数一层，不递归）。"""

    dir: str
    exists: bool
    count: int


@dataclass(frozen=True)
class FlowFacts:
    """流程声明文件（默认 flow.json）是否存在。"""

    path: str
    exists: bool


@dataclass(frozen=True)
class GitFacts:
    """程序仓的 Git 状态；不在仓库里时 available=False。"""

    available: bool = False
    repo: str | None = None
    branch: str | None = None
    head: str | None = None
    dirty: bool | None = None
    changes: int = 0


@dataclass(frozen=True)
class WorkspaceFacts:
    """一次探测的完整结果；`probe_at` 是探测时刻，不是业务日期（`as_of` 才是）。"""

    probe_at: str
    program: str
    session_id: str
    as_of: str
    root: str
    code: CodeFacts
    python: PythonFacts
    uv: UvFacts
    db: DbFacts
    inputs: InputsFacts
    out: OutFacts
    flow: FlowFacts
    git: GitFacts = field(default_factory=GitFacts)


def _default_run(file: str, args: list[str]) -> str | None:
    """同步执行并返回 stdout；失败（含超时/找不到）返回 None。"""

    try:
        # 8 秒硬超时：探测是前台操作，卡住的 git / uv 不该让命令无限等待。
        result = subprocess.run(
            [file, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _default_tables(db_path: str) -> list[TableState]:
    """表清单 + 行数；打不开返回空数组，探测不因库损坏而炸。"""

    try:
        # 只读 URI 打开：探测绝不能触发写盘、WAL 或锁，业务库不属于这个模块。
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables: list[TableState] = []
        for name in names:
            # 表名可能含引号或空格：先按 SQLite 规则转义再拼进 COUNT，避免语法错。
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                row = connection.execute(f"SELECT COUNT(*) AS n FROM {quoted}").fetchone()
                rows = int(row[0]) if row is not None else 0
            except sqlite3.Error:
                rows = 0
            tables.append(TableState(name=name, rows=rows))
        return tables
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _version_of(raw: str | None) -> str | None:
    """从版本输出里抓三段式版本号；抓不到返回 None（不猜）。"""

    if raw is None:
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", raw)
    return match.group(1) if match is not None else None


def _probe_packages(python: str, names: tuple[str, ...]) -> list[PackageState]:
    """在目标解释器里跑 PACKAGE_SCRIPT（只读 dist-info）；任何失败都退化成「版本未知」。"""

    raw = _default_run(python, ["-c", PACKAGE_SCRIPT, *names])
    if raw is None:
        return [PackageState(name=name, version=None) for name in names]
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return [PackageState(name=name, version=None) for name in names]
    if not isinstance(parsed, dict):
        return [PackageState(name=name, version=None) for name in names]
    versions = parsed
    return [
        PackageState(
            name=name,
            version=versions.get(name) if isinstance(versions.get(name), str) else None,
        )
        for name in names
    ]


def _walk_error(error: OSError) -> None:
    """遍历失败时静默跳过；探测不该因为一个坏目录整份失败。"""


def _count_py(directory: Path) -> int:
    """统计 .py 文件数；跳过 .git 与 node_modules，坏目录不中断遍历。"""

    if not directory.exists():
        return 0
    total = 0
    for _current, dirs, files in os.walk(directory, onerror=_walk_error):
        # 原地改 dirs 才能阻止 os.walk 继续下探；重新赋值给新列表是官方用法。
        dirs[:] = [name for name in dirs if name not in (".git", "node_modules")]
        total += sum(1 for name in files if name.endswith(".py"))
    return total


def _list_inputs(directory: Path) -> list[InputFile]:
    """列输入文件（不递归），按名字排序便于比对；读不了返回空数组。"""

    if not directory.exists():
        return []
    try:
        files = [
            InputFile(name=entry.name, bytes=entry.stat().st_size)
            for entry in directory.iterdir()
            if entry.is_file()
        ]
    except OSError:
        return []
    return sorted(files, key=lambda item: item.name)


def _venv_dir_of(python: str) -> Path | None:
    """`<venv>/Scripts/python.exe`（Windows）或 `<venv>/bin/python`（POSIX）→ `<venv>`。"""

    target = Path(python)
    tail = target.name.lower()
    parent = target.parent.name.lower()
    if (tail == "python.exe" and parent == "scripts") or (tail == "python" and parent == "bin"):
        return target.parent.parent
    return None


def _venv_manager_of(directory: Path) -> VenvManager:
    """uv 建 venv 时会在 pyvenv.cfg 里写一行 `uv = <版本>`，靠它区分来源。"""

    config = directory / "pyvenv.cfg"
    if not config.exists():
        return "unknown"
    try:
        content = config.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    return "uv" if any(re.match(r"^\s*uv\s*=", line) for line in content.splitlines()) else "venv"


def _find_uv() -> str | None:
    """在 PATH 上定位 uv；where/which 失败返回 None，找到但输出为空时退回裸名字 `uv`。"""

    raw = _default_run("where" if os.name == "nt" else "which", ["uv"])
    if raw is None:
        return None
    for line in raw.splitlines():
        trimmed = line.strip()
        if trimmed != "":
            return trimmed
    return "uv"


def _first_line(raw: str | None) -> str | None:
    """取第一行非空输出；用于把 git 的多行输出压成单行。"""

    if raw is None:
        return None
    for line in raw.splitlines():
        trimmed = line.strip()
        if trimmed != "":
            return trimmed
    return None


def _probe_git(code: Path) -> GitFacts:
    """用 git 读程序仓状态；以 `status --porcelain` 成功为准，其余字段失败时留 None。"""

    status = _default_run("git", ["-C", str(code), "status", "--porcelain"])
    if status is None:
        return GitFacts()
    repo = _first_line(_default_run("git", ["-C", str(code), "rev-parse", "--show-toplevel"]))
    branch = _first_line(_default_run("git", ["-C", str(code), "rev-parse", "--abbrev-ref", "HEAD"]))
    head = _first_line(_default_run("git", ["-C", str(code), "rev-parse", "--short=8", "HEAD"]))
    changes = sum(1 for line in status.splitlines() if line.strip() != "")
    return GitFacts(
        available=True,
        repo=repo,
        branch=branch,
        head=head,
        dirty=changes > 0,
        changes=changes,
    )


def _stamp_of(moment: datetime) -> str:
    """把时刻格式化成分钟级字符串，供事实表展示（不参与任何判定）。"""

    return moment.strftime("%Y-%m-%d %H:%M")


def probe_workspace(root: str | Path, deps: ProbeDeps) -> WorkspaceFacts:
    """每次都重新探测；缺项不抛错，交给渲染层写成下一步。"""

    code = Path(deps.code)
    python_path = deps.python
    python_exists = Path(python_path).exists()
    python_version = _version_of(_default_run(python_path, ["--version"])) if python_exists else None
    # 解释器不存在或版本都问不出来时，不再探测包：对着无效路径反复起子进程只会拖慢命令。
    if python_exists and python_version is not None:
        packages = _probe_packages(python_path, DEFAULT_PACKAGES)
    else:
        packages = [PackageState(name=name, version=None) for name in DEFAULT_PACKAGES]

    venv = _venv_dir_of(python_path)
    venv_facts = (
        VenvFacts(dir=str(venv), managed_by=_venv_manager_of(venv))
        if venv is not None
        else None
    )

    uv_version = _version_of(_default_run("uv", ["--version"]))
    uv_path = _find_uv() if uv_version is not None else None

    db_path = Path(deps.db_path)
    db_exists = db_path.exists()
    db_bytes = 0
    db_mtime: str | None = None
    if db_exists:
        try:
            # 只 stat 文件，不打开库：探测阶段不该触发 WAL 或拿业务库的锁。
            stat = db_path.stat()
            db_bytes = stat.st_size
            db_mtime = _stamp_of(datetime.fromtimestamp(stat.st_mtime))
        except (OSError, ValueError, OverflowError):
            db_bytes = 0
            db_mtime = None
    # 0 字节文件不当成库：把占位文件解释成「有库但没有表」会误导后续节点。
    tables = _default_tables(str(db_path)) if db_exists and db_bytes > 0 else []

    out_dir = Path(deps.out_dir)
    out_exists = out_dir.exists()
    out_count = 0
    if out_exists:
        try:
            out_count = len(list(out_dir.iterdir()))
        except OSError:
            out_count = 0

    inputs_dir = Path(deps.inputs_dir)
    flow_path = Path(deps.flow_path)

    return WorkspaceFacts(
        probe_at=_stamp_of(datetime.now()),
        program=deps.program,
        session_id=deps.session_id,
        as_of=deps.as_of,
        root=str(Path(root)),
        code=CodeFacts(path=str(code), py_files=_count_py(code)),
        python=PythonFacts(
            path=python_path,
            exists=python_exists,
            version=python_version,
            venv=venv_facts,
            packages=packages,
        ),
        uv=UvFacts(path=uv_path, version=uv_version),
        db=DbFacts(
            path=str(db_path),
            exists=db_exists,
            bytes=db_bytes,
            mtime=db_mtime,
            tables=tables,
        ),
        inputs=InputsFacts(
            dir=str(inputs_dir),
            exists=inputs_dir.exists(),
            files=_list_inputs(inputs_dir),
        ),
        out=OutFacts(dir=str(out_dir), exists=out_exists, count=out_count),
        flow=FlowFacts(path=str(flow_path), exists=flow_path.exists()),
        git=_probe_git(code),
    )


def fmt_bytes(n: int) -> str:
    """按 1024 进制渲染字节数；0 或负数统一显示 0 B。"""

    if n <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB")
    value = float(n)
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    precision = 0 if unit == 0 else 1
    return f"{value:.{precision}f} {units[unit]}"


def render_workspace_facts(facts: WorkspaceFacts) -> str:
    """渲染成 markdown；探测不到的东西一律落到「缺的东西」一节，写成下一步。"""

    lines: list[str] = []
    if facts.python.venv is None:
        venv_note = "不在 venv 里"
    elif facts.python.venv.managed_by == "uv":
        venv_note = "uv 建的 venv"
    elif facts.python.venv.managed_by == "venv":
        venv_note = "venv 建的（非 uv）"
    else:
        venv_note = "venv（来源不明）"

    lines.append(f"## 工作区事实（vibectl 探测于 {facts.probe_at}，本文件每次运行前重写，别手改）")
    lines.append("")
    lines.append(f"程序 `{facts.program}`　会话 `{facts.session_id}`　业务日期 `{facts.as_of}`")
    lines.append("")
    lines.append("| 项 | 路径 | 实测状态 |")
    lines.append("|---|---|---|")
    lines.append(f"| 程序仓 | `{facts.code.path}` | {facts.code.py_files} 个 .py 文件 |")
    if facts.python.exists:
        version = facts.python.version or "版本未知"
        lines.append(f"| Python | `{facts.python.path}` | {version}（{venv_note}） |")
    else:
        lines.append(f"| Python | `{facts.python.path}` | **不存在** |")
    if facts.uv.version is None:
        lines.append("| uv | — | **不在 PATH 上** |")
    else:
        lines.append(f"| uv | `{facts.uv.path or 'uv'}` | {facts.uv.version} |")
    if facts.db.exists:
        lines.append(
            f"| 业务库 | `{facts.db.path}` | {fmt_bytes(facts.db.bytes)}，"
            f"{len(facts.db.tables)} 张表，写于 {facts.db.mtime or '未知'} |"
        )
    else:
        lines.append(f"| 业务库 | `{facts.db.path}` | **不存在** |")
    if facts.inputs.exists:
        lines.append(f"| 输入批次 | `{facts.inputs.dir}` | {len(facts.inputs.files)} 个文件 |")
    else:
        lines.append(f"| 输入批次 | `{facts.inputs.dir}` | **目录不存在** |")
    if facts.out.exists:
        state = "空" if facts.out.count == 0 else f"{facts.out.count} 个文件"
    else:
        state = "**不存在**"
    lines.append(f"| 输出目录 | `{facts.out.dir}` | {state} |")
    lines.append(
        f"| 流程声明 | `{facts.flow.path}` | {'存在' if facts.flow.exists else '**不存在**'} |"
    )
    if facts.git.available:
        parts: list[str] = []
        if facts.git.branch is not None:
            parts.append(f"分支 `{facts.git.branch}`")
        if facts.git.head is not None:
            parts.append(f"HEAD `{facts.git.head}`")
        if facts.git.dirty:
            parts.append(f"{facts.git.changes} 个未提交改动")
        else:
            parts.append("工作区干净")
        lines.append(f"| Git | `{facts.git.repo or facts.code.path}` | {'，'.join(parts)} |")
    else:
        lines.append(f"| Git | `{facts.code.path}` | **不可用**（不在 Git 仓库里，或 git 不在 PATH） |")

    if facts.db.tables:
        # 只展示前 12 张：事实表是概览，完整清单请直接查业务库。
        shown = " / ".join(f"{table.name}({table.rows})" for table in facts.db.tables[:12])
        more = (
            f" …另 {len(facts.db.tables) - 12} 张" if len(facts.db.tables) > 12 else ""
        )
        lines.extend(["", f"业务库里的表：{shown}{more}"])

    if facts.inputs.files:
        shown = "、".join(
            f"{file.name} ({fmt_bytes(file.bytes)})" for file in facts.inputs.files[:6]
        )
        more = f" …另 {len(facts.inputs.files) - 6} 个" if len(facts.inputs.files) > 6 else ""
        lines.extend(["", f"输入文件：{shown}{more}"])

    installed = [pkg for pkg in facts.python.packages if pkg.version is not None]
    missing = [pkg for pkg in facts.python.packages if pkg.version is None]
    if facts.python.exists and facts.python.packages:
        if installed:
            rendered = "、".join(f"{pkg.name} {pkg.version}" for pkg in installed)
        else:
            rendered = "（一个都没装上）"
        lines.extend(["", f"Python 包：{rendered}"])

    todo: list[str] = []
    # 每一缺项都要配一条能直接执行的命令：只写「缺失」会让模型开始猜。
    if not facts.python.exists:
        todo.append(
            "解释器不存在：先跑 `vibectl setup`，或用 `uv venv` 建一个再把路径写进 "
            "`vibe.plugins.yml`"
        )
    if missing:
        names = "、".join(f"`{pkg.name}`" for pkg in missing)
        install = " ".join(pkg.name for pkg in missing)
        todo.append(f"没装 {names}：`uv pip install {install}`")
    if not facts.db.exists:
        todo.append("业务库还不存在：先跑 `vibectl setup`（或让导入节点先建表），不要假设里面有数据")
    if not facts.flow.exists:
        todo.append("流程声明还不存在：`flow` 命令会因此失败；先写 `flow.json` 再跑 run")
    if not facts.inputs.exists or not facts.inputs.files:
        todo.append("输入批次是空的：物化会读到 0 个文件，先确认 `inputs.dir` 指对了")
    if not facts.git.available:
        todo.append(
            f"Git 状态不可用：确认 `{facts.code.path}` 在 Git 仓库里，且 `git` 在 PATH 上"
        )
    if todo:
        lines.extend(["", "### 缺的东西（照这个做，别猜）", ""])
        lines.extend(f"- {item}" for item in todo)

    return "\n".join(lines)


def write_agent_facts_file(root: str | Path, facts: WorkspaceFacts) -> Path:
    """把事实写成 pi 的 context file（`<agent>/AGENTS.md`）。

    文件内容同时是「实测路径」与「使用约束」：模型读到后不应再自己找路径或写死别的路径。
    """

    directory = Path(root)
    # 目录可能还没被 workspace.ensure 建出来：写盘前自建，避免首次 author 就失败。
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "AGENTS.md"
    path.write_text(
        f"{render_workspace_facts(facts)}\n\n"
        "## 使用方式\n\n"
        "- 上面的路径是**实测的绝对路径**，直接用，不要再去找、不要在代码里写死别的路径。\n"
        "- 这份表是探测结果，不是配置；表过期了以实测为准，并且应当在回复里说明哪里对不上。\n"
        f"- 业务日期 `{facts.as_of}` 是钉住的，不要把当前系统日期当成业务日期。\n",
        encoding="utf-8",
    )
    return path
