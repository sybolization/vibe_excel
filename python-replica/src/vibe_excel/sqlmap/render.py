"""Embed the unchanged two-level SQL-map application without external assets."""

from html import escape
from pathlib import Path

from ..util import VibeError, escape_for_script, json_text
from .lineage import BuiltLineage

_ASSETS = Path(__file__).resolve().parents[1] / "assets" / "sqlmap"


def _asset(name: str) -> str:
    try:
        return (_ASSETS / name).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VibeError(f"Missing bundled sqlmap asset {name}: {error}") from error


def render_html(built: BuiltLineage) -> str:
    css, js = _asset("app.css"), _asset("app.js")
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
