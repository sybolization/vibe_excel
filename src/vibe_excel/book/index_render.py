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
.ix-row{display:flex;align-items:center;gap:10px;padding:14px 16px;margin-bottom:10px;
  border:1px solid var(--line2);border-radius:12px;background:var(--card);color:inherit}
.ix-row:hover{background:var(--panel);border-color:var(--line)}
/* 整行是「进入项目」的点击区；删除按钮留在链接外，点它不会跳页。 */
.ix-hit{display:flex;align-items:center;gap:18px;flex:1;min-width:0;color:inherit;
  text-decoration:none}
.ix-hit:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:8px}
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
.ix-del{flex:none;height:26px;padding:0 9px;border-radius:7px;font:11.5px var(--font);
  color:var(--muted);white-space:nowrap}
.ix-del:hover{background:var(--fill);color:var(--amber)}
.ix-del:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.ix-row.off{opacity:.55}
.ix-row.off:hover{background:var(--card);border-color:var(--line2)}
.chip.ok{background:var(--accent-soft);color:var(--accent)}
.chip.bad{background:color-mix(in srgb, var(--amber) 16%, transparent);color:var(--amber)}
.chip.idle{background:var(--fill);color:var(--muted)}
.ix-empty{padding:56px 0;text-align:center;color:var(--muted);font:12.5px/20px var(--font)}
.ix-empty b{display:block;margin-bottom:6px;font-weight:500;color:var(--ink2)}
/* 归档成功后的顶部提示：与账页的 toast 同一套材质，位置留给顶栏下方。 */
.ix-note{position:fixed;z-index:80;left:50%;top:68px;transform:translate(-50%,-8px);opacity:0;
  pointer-events:none;padding:8px 15px;border-radius:10px;background:var(--ink);color:var(--panel);
  font:12px/1.4 var(--font);box-shadow:var(--shadow);transition:opacity .16s,transform .16s;
  max-width:70vw}
.ix-note.on{opacity:1;transform:translate(-50%,0)}
/* ask 浮层里的按钮：与账页 .btn 同一档尺寸。app.css 目前还没给 .tbtn 定义，索引页先补上。 */
.ask .tbtn{height:26px;padding:0 11px;border-radius:7px;font:12px var(--font);
  color:var(--ink2);background:var(--fill)}
.ask .tbtn:hover{background:var(--hover);color:var(--ink)}
.ask .tbtn.go{background:var(--accent);color:var(--on-accent)}
.ask .tbtn.go:hover{background:var(--accent-fill)}
.ask .tbtn:disabled{opacity:.55;cursor:default}
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


_ARCHIVE_SCRIPT = """
(() => {
  /* 删除 = 归档。二次确认贴着被点的那一行弹出，绝不替用户按下去。
     这段只出现在服务托管的索引页；build 出来的静态站点没有接口可调，不注入。 */
  const NOTE_MS = 4200;
  const note = document.getElementById("ix-note");
  const count = document.getElementById("ix-count");
  const main = document.querySelector(".ix-list");
  let asking = null;
  let timer = 0;

  const close = () => {
    if (asking !== null) { asking.remove(); asking = null; }
  };

  const flash = (text) => {
    if (note === null) return;
    note.textContent = text;
    note.className = "ix-note on";
    clearTimeout(timer);
    timer = setTimeout(() => { note.className = "ix-note"; }, NOTE_MS);
  };

  const place = (box, row) => {
    const anchor = row.getBoundingClientRect();
    const size = box.getBoundingClientRect();
    const gap = 10;
    const margin = 12;
    /* 优先贴在行右侧；右边放不下就翻到左侧。两侧都窄时改为与行右缘对齐，
       宁可压住这一行的右半段，也不要跑到屏幕另一头去盖别的行。 */
    let left = anchor.right + gap;
    if (left + size.width > window.innerWidth - margin) {
      const flipped = anchor.left - size.width - gap;
      left = flipped >= margin ? flipped : anchor.right - size.width;
    }
    left = Math.min(Math.max(margin, left), Math.max(margin, window.innerWidth - size.width - margin));
    /* 默认与行的上沿对齐；下方不够就翻到行的上方，再夹回视口。 */
    let top = anchor.top;
    if (top + size.height > window.innerHeight - margin) top = anchor.top - size.height - 6;
    if (top < margin) top = anchor.bottom + gap;
    top = Math.min(Math.max(margin, top), Math.max(margin, window.innerHeight - size.height - margin));
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
  };

  const showEmpty = () => {
    if (main === null) return;
    const empty = document.createElement("div");
    empty.className = "ix-empty";
    const head = document.createElement("b");
    head.textContent = "列表已空";
    empty.appendChild(head);
    empty.appendChild(document.createTextNode("归档的项目保存在 "));
    const path = document.createElement("span");
    path.className = "mono";
    path.textContent = ".archive/projects/";
    empty.appendChild(path);
    empty.appendChild(document.createTextNode("，需要时按 packet.json 找回。"));
    main.appendChild(empty);
  };

  const ask = (row) => {
    close();
    const box = document.createElement("div");
    box.className = "ask";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "删除项目");
    box.innerHTML = `
      <div class="ask-h">删除项目</div>
      <div class="ask-b"></div>
      <div class="ask-n"></div>
      <div class="ask-f"><button class="tbtn" type="button" data-ask="cancel">取消</button><button class="tbtn go" type="button" data-ask="ok">归档</button></div>
      <div class="ask-e"></div>`;
    box.querySelector(".ask-b").textContent = row.dataset.name || "";
    box.querySelector(".ask-n").textContent =
      `${row.dataset.sessions || "0"} 个会话 · ${row.dataset.runs || "0"} 次运行 · ${row.dataset.publishes || "0"} 次发布。删除后目录进归档区，索引里不再列出。`;
    document.body.appendChild(box);
    place(box, row);
    asking = box;
    box.querySelector('[data-ask="cancel"]').addEventListener("click", close);
    box.querySelector('[data-ask="ok"]').addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const errorLine = box.querySelector(".ask-e");
      const name = row.dataset.name || "";
      button.disabled = true;
      errorLine.textContent = "";
      try {
        const response = await fetch("/api/projects/archive", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          /* 删除不需要理由：接口仍接受可选的 reason，但界面不再向用户要。 */
          body: JSON.stringify({ programId: row.dataset.project }),
        });
        let data;
        try {
          data = await response.json();
        } catch {
          throw new Error(`接口返回的不是 JSON（HTTP ${response.status}）`);
        }
        if (data.ok !== true) throw new Error(data.error || `归档失败（HTTP ${response.status}）`);
        close();
        row.remove();
        if (main !== null && count !== null) {
          count.textContent = `${main.querySelectorAll(".ix-row").length} 个项目`;
          if (main.querySelectorAll(".ix-row").length === 0) showEmpty();
        }
        flash(`已归档 ${name}`);
      } catch (error) {
        errorLine.textContent = error instanceof Error ? error.message : String(error);
        button.disabled = false;
      }
    });
  };

  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest('[data-act="archive"]');
    if (button === null) return;
    const row = button.closest(".ix-row");
    if (row !== null) ask(row);
  });
  window.addEventListener("pointerdown", (event) => {
    if (asking !== null && !asking.contains(event.target)) close();
  }, true);
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });

  /* ?confirm=<项目 id> 只给截图与排查用：展开那一行的确认框，不会直接归档。 */
  const wanted = new URLSearchParams(location.search).get("confirm");
  if (wanted !== null && main !== null) {
    const row = Array.from(main.querySelectorAll(".ix-row")).find(
      (item) => item.dataset.project === wanted
    );
    if (row !== undefined) {
      row.scrollIntoView({ block: "center" });
      ask(row);
    }
  }
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


def _row(project: ProjectSummary, href: str | None, *, can_archive: bool = False) -> str:
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
    # 站点合并多个 store 时先亮出来源，否则两个同名项目分不清
    origin = project.get("store")
    if origin:
        sub = f"{escape(origin)} · {sub}"
    chip, text = _status(project)
    run = project["lastRun"]
    when = _when(None if run is None else run["at"])
    title = "" if run is None else f' title="{escape(run["id"])}"'
    identifier = escape(project["id"], quote=True)
    name = escape(project["name"])
    # data-* 是给二次确认浮层读的：把计数随行带上，浮层不用再去接口要一遍。
    attributes = (
        f' data-project="{identifier}" data-name="{escape(project["name"], quote=True)}"'
        f' data-sessions="{len(sessions)}" data-runs="{project["runCount"]}"'
        f' data-publishes="{project["publishCount"]}"'
    )
    inner = f"""
        <div class="ix-main"><div class="ix-name">{name}</div><div class="ix-sub">{sub}</div></div>
        <div class="ix-facts">{project["runCount"]} 次运行 · {project["publishCount"]} 次发布</div>
        <div class="ix-last"{title}><span class="chip {chip}">{text}</span><span class="ix-when">{when}</span></div>
        {'<span class="ix-go">进入 →</span>' if href is not None else ''}"""
    # 采集失败的项目仍然列出来，但不给链接——比列一个点了会 404 的行诚实。
    if href is None:
        row_class, hit = "ix-row off", f'<div class="ix-hit">{inner}</div>'
    else:
        row_class = "ix-row"
        hit = f'<a class="ix-hit" href="{escape(href)}">{inner}</a>'
    delete = ""
    if can_archive:
        delete = (
            f'<button class="ix-del" type="button" data-act="archive" title="删除项目"'
            f' aria-label="删除项目 {escape(project["name"], quote=True)}">删除</button>'
        )
    return f'      <div class="{row_class}"{attributes}>{hit}{delete}</div>'


def render_index(
    projects: list[ProjectSummary],
    *,
    store_label: str,
    title: str = "vibe excel",
    pages: dict[str, str] | None = None,
    can_archive: bool = False,
) -> str:
    """把项目列表渲染成索引页。

    页面是静态的：链接是相对路径，双击打开、整个目录搬走后仍然可用。

    pages：项目 id → 相对链接。给了就按它连，没给的项目渲染成不可点的行；
    不传时按默认命名连（测试与单项目预览用）。
    can_archive：由服务托管时传 True，每行才出现「删除」并注入归档脚本。
    build 出来的静态站点没有接口可调，给一个点了会报错的按钮不诚实。
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
            _row(
                project,
                pages.get(project["id"]) if pages is not None else page_name(project["id"]),
                can_archive=can_archive,
            )
            for project in projects
        )
    note = ""
    if can_archive:
        note = '<div class="ix-note" id="ix-note" role="status" aria-live="polite"></div>'
    archive_script = f"\n<script>\n{_ARCHIVE_SCRIPT}\n</script>" if can_archive else ""
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
  <span class="rev" id="ix-count">{count} 个项目</span>
  <button class="iconbtn" data-act="theme" title="切换深色 / 浅色" aria-label="切换深色 / 浅色">{_ICONS}</button>
</header>
{note}<main class="ix-list">
{body}
</main>
<script>
{_THEME_SCRIPT}
</script>{archive_script}
</body>
</html>
"""


renderIndex = render_index
