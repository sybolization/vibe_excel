"""从 Python 源码里解析出工作流（ADR-0008「代码即流程」）。

平台不读手写的节点列表，只读代码。这个文件是**唯一的**解析器，做三件事：

  1. 从 `main` 的顶层语句里读出**节点顺序**（一次调用 = 一个节点）；
  2. 从被调函数的签名标注 `"t:表名"` 读出**边**（参数 = 读，返回 = 写）；
  3. 从 docstring 读出**标题**，从块首字符串读出**节点内部的步骤说明**。

为什么只用 `ast`：注释在词法阶段就丢了，块首字符串是 AST 的真实子节点，归属确定。
为什么不在 TS 里解析：写 Python 的 AST 就用 Python 自己，TS 侧只当薄壳（`src/flow-code.ts`）。

这个脚本是纯的：**不 import 目标文件、不连数据库、不写任何东西**，所以毫秒级、无副作用。
输出永远是 JSON（成功 / 契约违反 / 内部错误三种都是），调用方不需要解析 traceback。

退出码：0 成功，2 契约违反，3 解析器内部错误。
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import re
import sys
import tokenize
import traceback

# 平台固定递给节点的入参（`flow-run.ts` 的 kwargs）。它们不需要表标注。
RUNTIME_PARAMS = ("db", "as_of", "inputs", "out")

# 和 `src/flow.ts` 里的 IDENT 保持同一套：表名要能直接拼进 SQL 标识符。
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

TABLE_PREFIX = "t:"

# main 顶层出现这些就是「不线性」。
CONTROL_FLOW = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Match,
)


class ContractError(Exception):
    """代码违反了五条契约之一。带行号，调用方直接印出来。"""

    def __init__(self, code: str, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line


def _str_expr(stmt: ast.stmt) -> str | None:
    """语句是裸字符串字面量时返回它的内容（块说明），否则 None。"""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
        return stmt.value.value
    return None


def _table_annotation(annotation: ast.expr | None) -> str | None:
    """`"t:表名"` -> 表名；不是表标注 -> None。

    只看 AST 节点本身，不用 `ast.unparse`：源码里写的是带引号的字符串字面量，
    unparse 出来是 `'t:raw'`（还带一层引号），直接取 `Constant.value` 才是干净的。
    """
    if not (isinstance(annotation, ast.Constant) and isinstance(annotation.value, str)):
        return None
    text = annotation.value.strip()
    if not text.startswith(TABLE_PREFIX):
        return None
    return text[len(TABLE_PREFIX) :].strip()


def _annotation_text(annotation: ast.expr | None) -> str:
    """把注解还原成源码文本；失败时退回节点类型名，只为报错信息不中断。"""
    if annotation is None:
        return "(无)"
    try:
        return ast.unparse(annotation)
    except Exception:  # noqa: BLE001 - 报错信息不该因为格式化再抛一次
        return type(annotation).__name__


def _as_table(name: str, where: str, line: int, *, what: str) -> str:
    """表名必须是 ASCII 标识符：中文、带点、带空格的名字在这里就拒绝。"""
    if not name:
        raise ContractError("NODE_TABLE_NAME_BAD", f'{where} 的 t: 后面没有表名（写成 "t:表名"）', line)
    if not IDENT_RE.match(name):
        raise ContractError(
            "NODE_TABLE_NAME_BAD",
            f"{where} 的表名 {name!r} 不是 ASCII 标识符（{what}）："
            "表名要能直接拼进 SQL，用 [A-Za-z_][A-Za-z0-9_]*，中文/带点/带空格的都不行",
            line,
        )
    return name


def _node_name_ok(name: str, line: int) -> None:
    """节点名要能进日志与 run.json，必须是 ASCII 标识符。"""
    if not IDENT_RE.match(name):
        raise ContractError(
            "NODE_NAME_BAD",
            f"节点函数名 {name!r} 不是 ASCII 标识符：节点名会进日志、进 run.json、拼成 `file.py:函数名`，"
            "用 [A-Za-z_][A-Za-z0-9_]*",
            line,
        )


def _module_functions(tree: ast.Module) -> tuple[dict[str, ast.FunctionDef], set[str]]:
    """模块级同步函数表 + 异步函数名集合（异步的不能当节点，但要能说出原因）。"""
    sync: dict[str, ast.FunctionDef] = {}
    async_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            sync[node.name] = node
        elif isinstance(node, ast.AsyncFunctionDef):
            async_names.add(node.name)
    return sync, async_names


def _find_main(tree: ast.Module) -> ast.FunctionDef:
    """定位模块级 main；缺失、重复或 async 分别给出不同契约错误。"""
    mains = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"]
    if not mains:
        raise ContractError(
            "MAIN_MISSING",
            "代码里没有模块级的 `def main`：平台从 main 读流程，没有它就没有图。"
            "写一个 `def main(db, as_of)`，一行调用一个节点",
        )
    if len(mains) > 1:
        raise ContractError("MAIN_DUPLICATE", "定义了不止一个 main：只能有一个入口", mains[1].lineno)
    main = mains[0]
    if isinstance(main, ast.AsyncFunctionDef):
        raise ContractError("NODE_ASYNC", "main 是 async def：平台按同步函数调用节点，不支持协程", main.lineno)
    return main


def _check_main_is_linear(main: ast.FunctionDef) -> None:
    """拒绝 main 顶层的分支/循环/定义：main 必须一行一次节点调用。"""
    for stmt in main.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            raise ContractError(
                "MAIN_CONTROL_FLOW",
                f"main 顶层出现了{_kind_name(stmt)}：main 只能一行一次调用，"
                "定义/分支都要放进被调的函数体里",
                stmt.lineno,
            )
        if isinstance(stmt, CONTROL_FLOW):
            raise ContractError(
                "MAIN_CONTROL_FLOW",
                f"main 顶层有 {_kind_name(stmt)}：main 必须线性（一行一次调用）。"
                "`for f in [a, b]: f()` 这种解析不出顺序，把分支/循环写进被调函数内部",
                stmt.lineno,
            )


def _kind_name(stmt: ast.stmt) -> str:
    """把 AST 语句节点映射成给人看的种类名。"""
    names = {
        ast.If: "if",
        ast.For: "for",
        ast.AsyncFor: "async for",
        ast.While: "while",
        ast.With: "with",
        ast.AsyncWith: "async with",
        ast.Try: "try",
        ast.Match: "match",
        ast.FunctionDef: "函数定义",
        ast.AsyncFunctionDef: "async 函数定义",
        ast.ClassDef: "类定义",
    }
    for kind, label in names.items():
        if isinstance(stmt, kind):
            return label
    return type(stmt).__name__


def _check_main_params(main: ast.FunctionDef) -> None:
    """禁止在 main 的参数上写 t: 标注（main 不是节点）。"""
    for arg in _all_args(main):
        if _table_annotation(arg.annotation) is not None:
            raise ContractError(
                "MAIN_TABLE_PARAM",
                f"main 的参数 {arg.arg} 写了 t: 标注：main 不是节点，不参与图。"
                "要拿表就在函数体里自己查，或者交给节点（标注写在节点函数上，平台会把表递进去）",
                arg.lineno,
            )


def _all_args(fn: ast.FunctionDef) -> list[ast.arg]:
    """收集除 vararg/kwarg 外的全部具名参数（位置限定也算）。"""
    args = fn.args
    return [*args.posonlyargs, *args.args, *args.kwonlyargs]


def _calls_in(stmt: ast.stmt) -> list[ast.Call]:
    """语句里出现的全部 Call 节点（含嵌套），供 main 扫描节点调用。"""
    return [node for node in ast.walk(stmt) if isinstance(node, ast.Call)]


def _node_calls(
    main: ast.FunctionDef,
    sync_defs: dict[str, ast.FunctionDef],
    async_names: set[str],
) -> list[str]:
    """按出现顺序读出 main 里的节点调用。

    只有「裸名调用本文件的模块级函数」才算节点。`pd.read_sql` / `conn.close` 这类
    属性调用是 main 里的流水账（few-shot 里就有），**不是**节点，静默忽略——
    区分「pandas 管道」和「躲在下划线后面的一步」静态上做不到，与其刷噪音不如不管；
    跨文件放节点这件事靠提示词说清楚，并记在 ADR 的已知缺口里。
    """
    order: list[str] = []

    for stmt in main.body:
        local: list[ast.Call] = []
        for call in _calls_in(stmt):
            func = call.func
            if not isinstance(func, ast.Name):
                continue
            name = func.id
            if name in sync_defs:
                local.append(call)
            elif name in async_names:
                raise ContractError(
                    "NODE_ASYNC",
                    f"main 里调用了 async 函数 {name}：节点必须是同步函数（平台不会 await）",
                    call.lineno,
                )
            elif hasattr(builtins, name):
                continue  # print / len / range：main 里的记流水账，不是节点
            else:
                raise ContractError(
                    "MAIN_CALL_NOT_LOCAL",
                    f"main 里调用了 {name}，但本文件没有定义它：节点必须定义在同一个文件里"
                    "（跨文件 import 进来的函数在这里会安静地少一个节点，所以直接拒绝）",
                    call.lineno,
                )
        if len(local) > 1:
            names = " / ".join(call.func.id for call in local if isinstance(call.func, ast.Name))
            raise ContractError(
                "MAIN_NESTED_CALL",
                f"main 的一条语句里出现了多个节点调用（{names}）：一行只能调用一个节点，"
                "把中间步骤拆成单独的一行",
                stmt.lineno,
            )
        if local:
            assert isinstance(local[0].func, ast.Name)
            order.append(local[0].func.id)

    if not order:
        raise ContractError(
            "MAIN_NO_NODES",
            "main 里没有调用本文件定义的任何函数：这样解析出来是空图。"
            "main 的每一行应该是一次节点调用（import 进来的别的文件里的函数不会进图）",
            main.lineno,
        )
    return order


def _collect_steps(fn: ast.FunctionDef) -> list[dict[str, object]]:
    """节点内部的说明：块首字符串 = 那个分支/循环的说明，裸语句前的字符串 = 那条语句的说明。"""
    steps: list[dict[str, object]] = []

    def bodies_of(stmt: ast.stmt) -> list[list[ast.stmt]]:
        out: list[list[ast.stmt]] = []
        for field in ("body", "orelse", "finalbody"):
            value = getattr(stmt, field, None)
            if isinstance(value, list) and all(isinstance(item, ast.stmt) for item in value):
                out.append(value)
        for handler in getattr(stmt, "handlers", None) or ():
            out.append(handler.body)
        return out

    def visit(body: list[ast.stmt], skip_first: bool) -> None:
        pending: ast.Expr | None = None
        for index, stmt in enumerate(body):
            label = _str_expr(stmt)
            if label is not None:
                if skip_first and index == 0:
                    continue  # 函数/块的 docstring，标题或块说明已经单独取过
                pending = stmt
                continue
            if pending is not None:
                steps.append(
                    {
                        "line": pending.lineno,
                        "kind": "statement",
                        "of": _kind_name(stmt),
                        "label": _str_expr(pending),
                    }
                )
                pending = None
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # 内嵌定义有自己的 docstring，不往里走
            if isinstance(stmt, CONTROL_FLOW):
                for sub in bodies_of(stmt):
                    if not sub:
                        continue
                    head = _str_expr(sub[0])
                    if head is not None:
                        steps.append({"line": stmt.lineno, "kind": "block", "of": _kind_name(stmt), "label": head})
                        visit(sub[1:], False)
                    else:
                        visit(sub, True)

    visit(fn.body, True)
    return steps


def _describe_node(fn: ast.FunctionDef, name: str, names: dict[int, str]) -> dict[str, object]:
    """从函数签名与 docstring 生成节点规格：标题、读表、写表、步骤与展示名。"""
    line = fn.lineno
    # 只取第一行做标题：流程图上一行就够。
    title = ast.get_docstring(fn, clean=True)
    if title is None or title.strip() == "":
        raise ContractError(
            "NODE_NO_DOC",
            f"节点 {name} 的函数体第一句不是 docstring：那句话就是流程图上的节点标题，写一句「它算出什么」",
            line,
        )

    reads: list[str] = []
    if fn.args.posonlyargs:
        raise ContractError(
            "NODE_POSONLY",
            f"节点 {name} 有位置限定参数（`/`）：平台按关键字传参，去掉 `/`",
            line,
        )
    for arg in [*fn.args.args, *fn.args.kwonlyargs]:
        if arg.arg in RUNTIME_PARAMS:
            continue  # 固定入参，不需要标注
        if arg.arg == "report":
            raise ContractError(
                "NODE_PARAM_UNANNOTATED",
                f"节点 {name} 的参数 report 是历史遗留、平台还没实现："
                f"可用固定入参只有 {' / '.join(RUNTIME_PARAMS)}，要表就读 t: 标注",
                arg.lineno,
            )
        if arg.annotation is None:
            raise ContractError(
                "NODE_PARAM_UNANNOTATED",
                f"节点 {name} 的参数 {arg.arg} 没有标注："
                f'要么是固定入参（{" / ".join(RUNTIME_PARAMS)}），要么标成 "t:表名" 说明它吃哪张表',
                arg.lineno,
            )
        table = _table_annotation(arg.annotation)
        if table is None:
            raise ContractError(
                "NODE_PARAM_NOT_TABLE",
                f'节点 {name} 的参数 {arg.arg} 标注是 {_annotation_text(arg.annotation)}，不是 "t:表名"：'
                "表名必须是字符串字面量（f-string、变量、Table[...] 都读不出来）",
                arg.lineno,
            )
        table = _as_table(table, f"节点 {name} 的参数 {arg.arg}", arg.lineno, what="参数标注")
        if table not in reads:
            reads.append(table)

    for extra in (fn.args.vararg, fn.args.kwarg):
        if extra is not None and _table_annotation(extra.annotation) is not None:
            raise ContractError(
                "NODE_PARAM_NOT_TABLE",
                f"节点 {name} 的 *{extra.arg} 上不能标表名：表要么是具名参数（一个参数一张表），要么不标",
                extra.lineno,
            )

    writes: list[str] = []
    if fn.returns is None:
        raise ContractError(
            "NODE_RETURN_NOT_TABLE",
            f'节点 {name} 没有返回标注：它写哪张表就得说清楚——`-> "t:表名"`',
            line,
        )
    table = _table_annotation(fn.returns)
    if table is None:
        raise ContractError(
            "NODE_RETURN_NOT_TABLE",
            f'节点 {name} 的返回标注是 {_annotation_text(fn.returns)}，不是 "t:表名"：'
            "返回标注声明这个节点**写**哪张表（写成字符串字面量）",
            line,
        )
    # 签名只能声明一张写表：一个节点写多张表在代码即流程下表达不了。
    writes.append(_as_table(table, f"节点 {name} 的返回值", line, what="返回标注"))

    return {
        "func": name,
        "line": line,
        "title": title.strip().splitlines()[0].strip(),
        "reads": reads,
        "writes": writes,
        "steps": _collect_steps(fn),
        **({"name": names[fn.lineno - 1]} if (fn.lineno - 1) in names else {}),
    }


# `# name：中文` —— 节点在流程图上的名字。
# 只认紧挨着 `def` 上一行的这一行注释（不留空行），不认函数体里的：
# 一个名字写在声明旁边，改函数时顺手就会一起改。
NAME_COMMENT_RE = re.compile(r"#\s*name\s*[:：]\s*(\S[^\n]*?)\s*$", re.IGNORECASE)


def _collect_name_comments(source: str) -> dict[int, str]:
    """行号 → 中文名。解析不出来就返回空表：名字只影响展示，不该让整条流程失败。"""

    out: dict[int, str] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            matched = NAME_COMMENT_RE.search(token.string)
            if matched:
                out[token.start[0]] = matched.group(1).strip()
    except (tokenize.TokenError, IndentationError):
        return {}
    return out


def parse(source: str, entry: str) -> dict[str, object]:
    """解析入口源码，返回 {entry, nodes, warnings}；契约违反抛 ContractError。"""
    # BOM 会让 ast.parse 报「invalid non-printable character」，先剥掉。
    if source.startswith("\ufeff"):
        source = source[1:]

    try:
        tree = ast.parse(source, filename=entry)
    except SyntaxError as error:
        raise ContractError(
            "SYNTAX_ERROR",
            f"{entry} 不是合法 Python：{error.msg}",
            error.lineno,
        ) from error

    names = _collect_name_comments(source)
    sync_defs, async_names = _module_functions(tree)
    main = _find_main(tree)
    _check_main_params(main)
    _check_main_is_linear(main)
    order = _node_calls(main, sync_defs, async_names)
    warnings: list[dict[str, object]] = []

    nodes: list[dict[str, object]] = []
    seen: set[str] = set()
    for name in order:
        fn = sync_defs[name]
        _node_name_ok(name, fn.lineno)
        if name in seen:
            raise ContractError(
                "NODE_DUPLICATE",
                f"{name} 在 main 里被调用了两次：一个函数只能是一个节点（要跑两遍就写两个函数）",
                fn.lineno,
            )
        seen.add(name)
        nodes.append(_describe_node(fn, name, names))

    # 带表标注却没被 main 调用：多半是模型以为它已经进图了。
    for name, fn in sync_defs.items():
        if name == "main" or name in seen:
            continue
        annotated = _table_annotation(fn.returns) is not None or any(
            _table_annotation(arg.annotation) is not None for arg in _all_args(fn)
        )
        if annotated:
            warnings.append(
                {
                    "code": "UNUSED_TABLE_FUNC",
                    "message": f"{name} 带表标注但 main 没调用它：它不会成为节点，是死代码",
                    "line": fn.lineno,
                }
            )

    return {"entry": entry.replace("\\", "/"), "nodes": nodes, "warnings": warnings}


def _emit(payload: dict[str, object], code: int) -> int:
    """JSON 一律写 stdout 的 buffer：Windows 上不走 locale 编码，中文不会炸。"""
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    return code


def main(argv: list[str]) -> int:
    """命令行入口：从 stdin 读源码，只输出 JSON，退出码区分成功/契约违反/内部错误。"""
    entry = argv[0] if argv else "pipeline.py"
    try:
        source = sys.stdin.buffer.read().decode("utf-8")
        body = parse(source, entry)
    except ContractError as error:
        return _emit(
            {
                "ok": False,
                "error": {"code": error.code, "message": error.message, "line": error.line},
            },
            2,
        )
    except Exception:  # noqa: BLE001 - 内部错误也要是 JSON，调用方不该去解 traceback
        return _emit(
            {
                "ok": False,
                "error": {
                    "code": "INTERNAL",
                    "message": f"解析器内部错误：{traceback.format_exc(limit=3)}",
                    "line": None,
                },
            },
            3,
        )
    return _emit({"ok": True, **body}, 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
