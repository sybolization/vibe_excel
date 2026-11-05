"""Render the original book frontend as one fully offline HTML document."""

from html import escape
from pathlib import Path

from ..util import VibeError, escape_for_script, json_text
from .types import BookView

_ASSETS = Path(__file__).resolve().parents[1] / "assets" / "book"


def _asset(name: str) -> str:
    try:
        return (_ASSETS / name).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VibeError(f"Missing bundled book asset {name}: {error}") from error


def render_book(view: BookView) -> str:
    css, js = _asset("app.css"), _asset("app.js")
    payload = escape_for_script(json_text(view))
    program = view["program"]["name"] if view["program"] is not None else "book"
    warnings = list(view.get("warnings", []))
    if view["graph"] is not None:
        warnings.extend(view["graph"]["warnings"])
    if view["inputs"] is not None:
        warnings.extend(view["inputs"].get("warnings", []))
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
