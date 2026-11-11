"""项目索引页：一整页只做一件事——让你选进哪个项目。

样式复用 assets/book/app.css（同一套 token、同一个玻璃顶栏、同一个主题键），
索引页专属的布局写在页面里的一小段 <style>，不新增第二套设计。
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from .projects import page_name
from .render import _asset, font_face_css
from .types import ProjectSummary

__all__ = ["render_index", "renderIndex"]

_ICONS = (
    '<svg class="i-moon" viewBox="0 0 16 16" width="15" height="15" fill="none" '
    'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M13.4 9.8A5.7 5.7 0 0 1 6.2 2.6a5.7 5.7 0 1 0 7.2 7.2z"/></svg>'
    '<svg class="i-sun" viewBox="0 0 16 16" width="15" height="15" fill="none" '
    'stroke="currentColor" stroke-width="1.4" stroke-linecap="round">'
    '<circle cx="8" cy="8" r="3"/>'
    '<path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1'
    'M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1"/></svg>'
)

_INDEX_STYLE = """
/* 索引页专属：一列项目卡片。其余全部沿用 app.css 的 token 与组件。 */
body.ix{overflow-y:auto}
body.ix .fbar{right:12px}
.ix-list{max-width:880px;margin:0 auto;padding:80px 16px 24px}
.ix-row{display:flex;align-items:center;gap:18px;padding:14px 16px;margin-bottom:10px;
  border:1px solid var(--line2);border-radius:12px;background:var(--card);
  color:inherit;text-decoration:none}
.ix-row:hover{background:var(--panel);border-color:var(--line)}
.ix-main{min-width:0;flex:1}
.ix-name{font:500 13.5px/18px var(--font);color:var(--ink);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.ix-sub{margin-top:3px;font:11.5px/16px var(--font);color:var(--muted);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.ix-facts{flex:none;font:11.5px/16px var(--font);color:var(--ink2);
  font-variant-numeric:tabular-nums;text-align:right}
.ix-last{flex:none;display:flex;align-items:center;gap:8px;min-width:118px;justify-content:flex-end}
.ix-when{font:11.5px/16px var(--font);color:var(--muted);font-variant-numeric:tabular-nums}
.ix-go{flex:none;font:12px var(--font);color:var(--accent);white-space:nowrap}
.ix-row.off{opacity:.55}
.ix-row.off:hover{background:var(--card);border-color:var(--line2)}
.chip.ok{background:var(--accent-soft);color:var(--accent)}
.chip.bad{background:color-mix(in srgb, var(--amber) 16%, transparent);color:var(--amber)}
.chip.idle{background:var(--fill);color:var(--muted)}
.ix-empty{padding:56px 0;text-align:center;color:var(--muted);font:12.5px/20px var(--font)}
.ix-empty b{display:block;margin-bottom:6px;font-weight:500;color:var(--ink2)}
"""

_THEME_SCRIPT = """
(() => {
  /* 与账页同一套规则、同一个存储键：在索引页选的主题，进项目页照样是它。 */
  const KEY = "vibe-book-theme";
  const read = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
  const store = (value) => { try { localStorage.setItem(KEY, value); } catch { /* 存不下不影响本次 */ } };
  const apply = (value) => { document.documentElement.dataset.theme = value === "dark" ? "dark" : "light"; };
  const prefers = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  apply(new URLSearchParams(location.search).get("t") ?? read() ?? (prefers ? "dark" : "light"));
  document.querySelector(".iconbtn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    apply(next);
    store(next);
  });
})();
"""


def _when(stamp: str | None) -> str:
    """把 ISO 时间折成本地时刻。

    不写「3 分钟前」：页面是静态产物，相对时间一转身就是谎话。
    """
    if not stamp:
        return "—"
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return escape(stamp[:16].replace("T", " "))
    return moment.strftime("%m-%d %H:%M")


def _status(project: ProjectSummary) -> tuple[str, str]:
    """返回 (chip 的类名, 文案)。没有运行和运行失败必须分得开。"""
    run = project["lastRun"]
    if run is None:
        return "idle", "未运行"
    if run["status"] == "succeeded":
        if run["identical"] is True:
            return "ok", "一致"
        if run["identical"] is False:
            return "bad", "不一致"
        return "ok", "成功"
    if run["status"] == "failed":
        return "bad", "失败"
    if run["status"] in ("running", "pending"):
        return "idle", "运行中"
    return "idle", escape(run["status"])


def _row(project: ProjectSummary, href: str | None) -> str:
    sessions = project["sessions"]
    active = next(
        (item for item in sessions if item["id"] == project["activeSessionId"]), None
    )
    if active is None:
        sub = "没有会话"
    else:
        sub = escape(active["name"])
        if len(sessions) > 1:
            sub += f" · 共 {len(sessions)} 个会话"
    chip, text = _status(project)
    run = project["lastRun"]
    when = _when(None if run is None else run["at"])
    title = "" if run is None else f' title="{escape(run["id"])}"'
    # 采集失败的项目仍然列出来，但不给链接——比列一个点了会 404 的行诚实。
    if href is None:
        opening, closing = '<div class="ix-row off">', "</div>"
        go = ""
    else:
        opening, closing = f'<a class="ix-row" href="{escape(href)}">', "</a>"
        go = '<span class="ix-go">进入 →</span>'
    return f"""      {opening}
        <div class="ix-main"><div class="ix-name">{escape(project["name"])}</div><div class="ix-sub">{sub}</div></div>
        <div class="ix-facts">{project["runCount"]} 次运行 · {project["publishCount"]} 次发布</div>
        <div class="ix-last"{title}><span class="chip {chip}">{text}</span><span class="ix-when">{when}</span></div>
        {go}
      {closing}"""


def render_index(
    projects: list[ProjectSummary],
    *,
    store_label: str,
    title: str = "vibe excel",
    pages: dict[str, str] | None = None,
) -> str:
    """把项目列表渲染成索引页。

    页面是静态的：链接是相对路径，双击打开、整个目录搬走后仍然可用。

    pages：项目 id → 相对链接。给了就按它连，没给的项目渲染成不可点的行；
    不传时按默认命名连（测试与单项目预览用）。
    """
    css = _asset("app.css")
    count = len(projects)
    if count == 0:
        body = (
            '    <div class="ix-empty"><b>这个 store 里还没有项目</b>'
            '先在项目目录里跑一次 <span class="mono">vibectl setup</span>，再回来刷新这一页。</div>'
        )
    else:
        body = "\n".join(
            _row(project, pages.get(project["id"]) if pages is not None else page_name(project["id"]))
            for project in projects
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · 项目</title>
<style>
{font_face_css("fonts/")}
{css}
{_INDEX_STYLE}
</style>
</head>
<body class="ix">
<header class="glass fbar">
  <div class="brand"><b>{escape(title)}</b><span>项目</span></div>
  <span class="rev">{escape(store_label)}</span>
  <span class="grow"></span>
  <span class="rev">{count} 个项目</span>
  <button class="iconbtn" data-act="theme" title="切换深色 / 浅色" aria-label="切换深色 / 浅色">{_ICONS}</button>
</header>
<main class="ix-list">
{body}
</main>
<script>
{_THEME_SCRIPT}
</script>
</body>
</html>
"""


renderIndex = render_index
