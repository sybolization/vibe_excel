"""把原始 book 前端渲染成一个完全离线的 HTML 文档。

本模块只拼接内置的 app.css/app.js 与序列化后的视图模型，不访问网络或 store。
视图模型会嵌入 <script> 标签，因此序列化结果必须先转义再拼接。
"""

from html import escape
from pathlib import Path

from ..util import VibeError, escape_for_script, json_text
from .types import BookView

_ASSETS = Path(__file__).resolve().parents[1] / "assets" / "book"


def _asset(name: str) -> str:
    """读取随包分发的静态资源；缺失时提示打包不完整。"""
    try:
        return (_ASSETS / name).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VibeError(f"缺少内置的 book 资源 {name}：{error}") from error


def render_book(view: BookView) -> str:
    """把 book 视图模型渲染成单文件 HTML。

    诊断信息合并自 view、graph 与 inputs 三处并去重，以 details 折叠块呈现，
    保证可选页面缺失时仍能浏览其余内容。
    """
    css, js = _asset("app.css"), _asset("app.js")
    # 内联 JSON 中的 < 与行分隔符必须转义，否则 </script> 会提前闭合脚本标签。
    payload = escape_for_script(json_text(view))
    program = view["program"]["name"] if view["program"] is not None else "book"
    warnings = list(view.get("warnings", []))
    if view["graph"] is not None:
        warnings.extend(view["graph"]["warnings"])
    if view["inputs"] is not None:
        warnings.extend(view["inputs"].get("warnings", []))
    # 三处诊断可能重复，按首次出现顺序去重后再渲染。
    warnings = list(dict.fromkeys(warnings))
    diagnostics = ""
    if warnings:
        items = "".join(f"<li>{escape(warning)}</li>" for warning in warnings)
        diagnostics = (
            f'<details id="book-warnings"><summary>诊断 · {len(warnings)} 条（缺页仍可浏览）</summary>'
            f"<ul>{items}</ul></details>\n"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(program)} · book</title>
<style>
{css}
#book-warnings{{position:fixed;top:12px;right:12px;z-index:1000;max-width:min(640px,90vw);max-height:60vh;overflow:auto;background:var(--panel);border:1px solid var(--amber);border-radius:6px;padding:8px 12px;color:var(--ink)}}
#book-warnings summary{{cursor:pointer;color:var(--amber)}}
#book-warnings li{{margin:6px 0;overflow-wrap:anywhere}}
</style>
</head>
<body>
<div id="root"></div>
{diagnostics}<script type="application/json" id="book-data">{payload}</script>
<script>
{js}
</script>
</body>
</html>
"""


renderBook = render_book
escapeForScript = escape_for_script
