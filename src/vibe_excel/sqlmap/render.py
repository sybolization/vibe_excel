"""内嵌原样的两级 SQL 映射应用，不依赖外部资源。

只拼接 assets/sqlmap 下的 app.css/app.js 与 JSON 序列化后的 BuiltLineage；
视图模型嵌入 <script> 前必须转义，避免 </script> 提前闭合脚本标签。
"""

from html import escape
from pathlib import Path

from ..util import VibeError, escape_for_script, json_text
from .lineage import BuiltLineage

_ASSETS = Path(__file__).resolve().parents[1] / "assets" / "sqlmap"


def _asset(name: str) -> str:
    """读取随包分发的静态资源；缺失时提示打包不完整。"""
    try:
        return (_ASSETS / name).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VibeError(f"缺少内置的 sqlmap 资源 {name}：{error}") from error


def render_html(built: BuiltLineage) -> str:
    """把校验后的血缘渲染成单文件 HTML。

    诊断只来自 built["warnings"]，以折叠块呈现；不额外读取任何文件。
    """
    css, js = _asset("app.css"), _asset("app.js")
    # 内联 JSON 需转义 < 与行分隔符，否则数据里的 </script> 会提前关闭脚本标签。
    payload = escape_for_script(json_text(built))
    title = escape(f"{built['case']['title']} · SQL 表血缘")
    diagnostics = ""
    if built["warnings"]:
        items = "".join(f"<li>{escape(warning)}</li>" for warning in built["warnings"])
        diagnostics = (
            f'<details id="lineage-warnings"><summary>诊断 · {len(built["warnings"])} 条</summary>'
            f"<ul>{items}</ul></details>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{css}
#lineage-warnings{{position:fixed;top:12px;right:12px;z-index:1000;max-width:min(640px,90vw);max-height:60vh;overflow:auto;background:var(--panel,#1b1f24);border:1px solid var(--amber,#b58900);border-radius:6px;padding:8px 12px;color:var(--ink,#e6e6e6)}}
#lineage-warnings summary{{cursor:pointer;color:var(--amber,#b58900)}}
#lineage-warnings li{{margin:6px 0;overflow-wrap:anywhere}}
</style>
</head>
<body>
<div class="page">
  <div class="topbar">
    <h1>SQL 表血缘</h1>
    <span class="meta" id="case"></span>
  </div>
  {diagnostics}
  <div id="view1">
    <div class="stage" id="stage1">
      <div class="lanes" id="lanes"></div>
      <svg class="wires" id="wires1"></svg>
    </div>
  </div>
  <div id="view2" hidden>
    <div class="crumbs"><button type="button" class="ghost" id="back">← 返回总览</button></div>
    <div class="detail-head" id="detail-head"></div>
    <div class="stage" id="stage2">
      <div class="detail" id="detail"></div>
      <svg class="wires" id="wires2"></svg>
    </div>
    <div class="filters" id="filters" hidden></div>
  </div>
  <div class="panel" id="panel" hidden></div>
</div>
<script type="application/json" id="lineage">{payload}</script>
<script>
{js}
</script>
</body>
</html>
"""


renderHtml = render_html
escapeForScript = escape_for_script
