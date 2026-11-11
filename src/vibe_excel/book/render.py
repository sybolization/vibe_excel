"""把原始 book 前端渲染成一个完全离线的 HTML 文档。

本模块只拼接内置的 app.css/app.js 与序列化后的视图模型，不访问网络或 store。
视图模型会嵌入 <script> 标签，因此序列化结果必须先转义再拼接。
"""

import re
from html import escape
from pathlib import Path

from ..util import VibeError, escape_for_script, json_text
from .types import BookView

_ASSETS = Path(__file__).resolve().parents[1] / "assets" / "book"

# 随包分发的字体：华为原版，未做任何修改（许可第 2 条禁止修改，子集化也算修改）。
# 只带 400 / 500 两个字重；面板标题的 600 由 Medium 合成。
FONT_WEIGHTS = {
    "HarmonyOS_Sans_SC_Regular.ttf": 400,
    "HarmonyOS_Sans_SC_Medium.ttf": 500,
}
FONT_FAMILY = "HarmonyOS Sans SC"


def _asset(name: str) -> str:
    """读取随包分发的静态资源；缺失时提示打包不完整。"""
    try:
        return (_ASSETS / name).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VibeError(f"缺少内置的 book 资源 {name}：{error}") from error


def font_face_css(prefix: str = "") -> str:
    """生成两段 @font-face，指向与页面同目录的 fonts/（prefix 由站点结构决定）。

    用侧车字体文件而不是 base64 内联：一个站点有 N 个项目页，内联会让体积乘以 N。
    许可第 2 条允许「embed, bundle, redistribute … unmodified copies … with any software」，
    侧车随软件一起分发属于被允许的形态；但不得把字体单独拎出去分发。
    """
    faces = []
    for name, weight in FONT_WEIGHTS.items():
        faces.append(
            f'@font-face{{font-family:"{FONT_FAMILY}";'
            f'src:url("{prefix}{name}") format("truetype");'
            f"font-weight:{weight};font-style:normal;font-display:swap}}"
        )
    return "\n".join(faces)


def inline(source: str) -> str:
    """内联脚本里若出现 </script，浏览器会提前收尾；统一挡掉。"""
    return re.sub(r"</script", r"<\\/script", source, flags=re.IGNORECASE)


def render_book(
    view: BookView, *, fonts: str | None = None, home: str | None = None
) -> str:
    """把 book 视图模型渲染成 HTML。

    诊断信息合并自 view、graph 与 inputs 三处并去重，以 details 折叠块呈现，
    保证可选页面缺失时仍能浏览其余内容。

    fonts：字体相对当前页面的路径前缀（站点模式给 ""，单页模式给 None 表示不注入）；
    home：上一级（项目索引页）的相对地址，给了才在顶栏出现返回入口。
    """
    css, js = _asset("app.css"), _asset("app.js")
    font_css = "" if fonts is None else font_face_css(fonts) + "\n"
    marked, chat = _asset("vendor/marked.min.js"), _asset("chat.js")
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
    # 页面把自己的位置告诉前端：有没有上一级。单页模式下不出现，行为与以前一致。
    body_attrs = "" if home is None else f' data-home="{escape(home, quote=True)}"'

    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(program)} · book</title>
<style>
{font_css}{css}
#book-warnings{{position:fixed;top:12px;right:calc(var(--cw, 360px) + 12px);z-index:1000;max-width:min(640px,90vw);max-height:60vh;overflow:auto;background:var(--panel);border:1px solid var(--amber);border-radius:6px;padding:8px 12px;color:var(--ink)}}
#book-warnings summary{{cursor:pointer;color:var(--amber)}}
#book-warnings li{{margin:6px 0;overflow-wrap:anywhere}}
</style>
</head>
<body{body_attrs}>
<div id="root"></div>
{diagnostics}<script type="application/json" id="book-data">{payload}</script>
<script>
{inline(marked)}
</script>
<script>
{inline(js)}
</script>
<script>
{inline(chat)}
</script>
</body>
</html>
"""


renderBook = render_book
escapeForScript = escape_for_script
