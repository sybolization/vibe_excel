# book 前端审查（render / index_render / transcript / types / app.css / app.js / chat.js / bg.py）

基线：`python` 分支 `e5c325d`（审查期间 HEAD 依次前进到 `cc9aeb6`、`e5c325d`；`bg.py`/`jobs.py` 的编码与监督进程改动在 `e5c325d` 落盘，本报告 `bg.py` 一节按该版本），写报告时工作区干净；行号按 `e5c325d`，只读审查、未改源码。
方法：把 `app.css` 的 136 个类逐个做了「JS/Python 里是否有词边界命中」的扫描（真正零引用的只有 `.ask-r`、`.bp-hint`、`.tbt`；`.txt` 只出现在 `LICENSE.txt` 注释里），复合选择器、元素选择器和 JS 声明再人工核对。

## 死代码

1. `src/vibe_excel/assets/book/app.css:370-373` — `.tbt` 四条规则没有任何元素使用：JS 里写的是 `tbtn`（`app.js:492` 的「撤回 / 重建输入批次」、`app.js:992` 的「移出」确认框）。反过来，真正在用的 `.tbtn` 在 app.css 里一条规则都没有，所以账页这两组按钮现在是裸按钮；`index_render.py:58-64` 的注释「app.css 目前还没给 .tbtn 定义，索引页先补上」就是在打补丁。 — 建议：把 370-373 改名 `.tbtn`（含 `.tbt.go` → `.tbtn.go`），并删掉索引页那段 scoped 补丁、统一 `.go` 的底色（这里用 `--accent-soft`，索引页 `index_render.py:62` 用 `--accent`，合并时二选一）。 — 风险：低（外观会变回设计意图，测试只认结构不认类名）。

2. `app.css:196-200、216、327、394-396` — 几组零引用/被覆盖的遗留：底栏页脚的输入框样式 `.bp-ft .box`、`.bp-ft input`、`::placeholder`（页脚只剩一个按钮，从初版移植起就没渲染过 input）、`.bp-hint`（`ba6ec98` 删掉「对话在右边」那句后留下，CSS 没删）、`.note.mono`（全仓没有 `class="note mono"`）、`.ask-r`（`a1b7d29` 把「为什么移出」的 textarea 去掉后留下），以及 `.chat-stop[hidden]`（被 283 行的全局 `[hidden]{display:none !important}` 覆盖）。 — 建议：只删这些规则；`.bp-ft`、`.chat-stop`、`.note`、`.mono` 本身都还在用，别连带删。 — 风险：低。

3. `src/vibe_excel/book/index_render.py:75,78` — `.ix-newdlg select` 两处选择器对应的 `<select>` 在 `8bac3d0`「新建不再选库」时已移除（同提交删掉了 `.ix-store`，`tests/test_projects_edit.py:371` 还在钉它不存在），现在只有同一条规则里的 `input` 有用。 — 建议：两条规则里去掉 `select` 部分。 — 风险：低。

4. `src/vibe_excel/assets/book/app.js:635-648` — `closure(fileName)` 定义之后全仓零调用（现役的是 `chainOf`，`app.js:652-673`；`downs/ups` 仍被 chainOf 使用，只删函数本体）。 — 建议：删。 — 风险：低。

5. `app.js:387,396` — 拖动画布时 `cv.classList.add("drag")` / `remove("drag")`，但 app.css 没有任何 `.drag` 规则（136 个类扫过），这两行只在 JS 里空转；拖动过程中光标仍是 `grab` 而不是 `grabbing`。 — 建议：要么删两行，要么补一条 `.cv.drag{cursor:grabbing}`（补规则更像本意）。 — 风险：低。

6. `chat.js:594-598` — `window.__CHAT.open()` 全仓零调用（`unread` 有 `?unread` 用，`refresh` 有 `app.js:912-914` 在调）；同一段代码还有一件半死的事：`handle()`（`chat.js:471-526`）没有 `type === "start"` 分支，而 `chat_runner.py:280` 每轮都发 `{"type":"start"}`，`docs/book.md:100` 的事件清单里也没有它，页面静默丢弃。 — 建议：删 `open()`；`start` 要么在页面里用起来（它带了这一轮的用户原文，可以当一行的来源），要么从后端停发，别让它一直空转。 — 风险：低。

7. `src/vibe_excel/book/types.py:344-361` — `BookView` 里 `generatedAt`(344)、`store`(345)、`publishes`(353)、`transcript`(357)、`focusEpisode`(360)、`warnings`(361) 六个字段页面从未读：`app.js` 只读 program/session/revisions/runs/currentRevision/lastRunRevision/graph/inputs/diagram，`chat.js:17` 只读 conversations。其中 `transcript`/`focusEpisode` 是旧「对话摘要列表」的残留（现在对话区只吃 conversations），但 episodes 仍随每次序列化进每张页面的 `<script id="book-data">`。 — 建议：先确认仓库外没有 API 消费者，再决定是停止把 transcript/focusEpisode 序列化进页面，还是在 `types.py` 里明确注释「只供 collect API，页面不用」。 — 风险：中（`collect_book` 是公开函数，这些字段可能是对外契约）。

## 重复

8. `app.css:204` 与 `:275` 同一 `.selh .nm` 写了两遍；`app.css:195` 与 `:471` `.bp-ft` 写了两遍（471 只补 `justify-content:flex-end`，是 `ba6ec98` 追加在文件末尾的）。 — 建议：各自合并成一条（275 的 `max-width:60%` 并进 204；471 并进 195，注释一起搬），少一次「改上面忘下面」。 — 风险：低。

9. `app.css:284-286` 与 `:304-306` — `.chat` / `.chat-rail` 的 `position/z-index/right/top/bottom` 加 `background/border-left/box-shadow` 逐字相同；`app.css:406-410` 的 `.toast` 与 `index_render.py:53-57` 的 `.ix-note` 又是同一套「墨底 + 阴影 + 透明度过渡 + max-width」写两份。 — 建议：前两者合成 `.chat,.chat-rail{…}`（各自保留 width/flex）；toast 与 ix-note 至少抽一个公共视觉类给索引页用。 — 风险：低。

10. `app.js:64` 与 `chat.js:37` — `esc()` 逐字相同（同样的 `[&<>"]` 映射），两个 IIFE 各存一份。 — 建议：放到 `app.js` 已经公开的 `window.__BOOK` 上（chat.js 本来就在用 `__BOOK.setGutter`），或由 `render.py` 在两个 IIFE 之前多内联一小段公共工具。 — 风险：低。

11. `src/vibe_excel/book/transcript.py:201,215,226,238,247` — 同一份 block 字面量写 5 遍（event/user/assistant/thinking/tool 各一遍，`name/brief/ok/ms/result` 恒为 `""/""/True/None/""`）；给 `ChatBlock` 加字段要改 5 处。 — 建议：`_block(kind, at, text, *, name="", brief="")` 之类的工厂，5 处调用；解析逻辑一行不动。 — 风险：低。

12. `app.js:530,553,578,597` — `<div class="pane" style="width:330px;flex:none">` 在文件面板、sheet、机器、表四个面板里写了四遍。 — 建议：加 `.pane.fix{width:330px;flex:none}`，四处改 class；内联样式只留给画布上必须按布局算的 left/top/width/height。 — 风险：低。

13. `src/vibe_excel/assets/book/bg.py:21` 的 `DONE = "__DONE__"` 改成内嵌监督脚本后已经没人引用（真正的写者在 `bg.py:31`/`:48` 的模板字符串里）；同一个哨兵又写死在 `jobs.py:24` 的 `DONE_MARK`，目录/名字约定也是两份：`bg.py:3,71,75` 的 `_chat/bg.py`、`jobs/` 对 `jobs.py:21-22` 的 `JOBS_DIR`/`HELPER`。 — 建议：删 21 行的死常量；哨兵与目录只留一份（例如让 bg.py 以 `--supervise` 自举，省掉内嵌脚本、重复 imports 和重复常量；或至少让模板引用同一个常量）；再补一条真正跑一遍 `bg.py` 再 `harvest()` 的端到端测试，把两处约定钉住（现在 `tests/test_jobs.py:40` 只验了它打印用法）。 — 风险：中（跨文件协议，改错会让「完成通知」永久静默，页面表现为 job 永远不回来）。

14. `app.js:892-901` 的 `postJson` 与 `index_render.py:172-184`、`:270-281` 的 fetch + `response.json()` + `data.ok !== true` 三份；「接口返回的不是 JSON（HTTP …）」这句在 `index_render.py:182` 与 `:279` 各写一遍。 — 建议：索引页两段内联脚本本来就同页，先合并出一个 `postJson` 供归档/新建共用（账页那份保持），错误文案只留一处。 — 风险：低。

## 过度抽象

15. 主题的「一处定义」只做了一半：`render.py:54-63` 的 `THEME_ICONS` 和 `:65-80` 的 `THEME_SCRIPT` 被索引页（`index_render.py:479,485`）和空项目页（`render.py:129,141`）复用，但账页的 `app.js:36-62` 又把同一套存储键、`?t=` 优先级重写了一遍，`app.js:420-421` 还内联了一份逐字相同的 SVG；`<button class="iconbtn" …>` 这段 HTML 在 `render.py:129`、`index_render.py:479`、`app.js:419-422` 拼了三遍。为了复用样式，`index_render.py:14` 还 import 了 render 的私有 `_asset`。 — 建议：在 `render.py` 出一个公开的 `theme_button()`（图标 + 按钮）和 `theme_head()`（脚本），两个 Python 页改用它；账页要么也走同一段脚本、从 app.js 删掉那 27 行，要么在注释里写明「app.js 自己实现是刻意的」，别留下一个只是看起来统一的共享常量。 — 风险：中（主题是三页共用的唯一交互，改错会在某一页丢主题）。

16. `app.js:902-915` 把新的输入状态写回 `#book-runtime` 的 `textContent`，`chat.js` 再在 `eventsHtml()`（`chat.js:192-197`）和 `contextIds()`（`chat.js:353-358`）各 `JSON.parse` 一次——用 DOM 节点当两个 IIFE 之间的消息总线，而且 `paintThread()` 每画一次就重解析一次。 — 建议：`app.js` 已经有 `window.__BOOK`，加一个 `getInputs()` 直接读内存里的 `RT.inputs`；`chat.js` 加载时取一次缓存，`applyState` 后主动刷新。`#book-runtime` 继续保留给「刷新页面时重新读」就行。 — 风险：低。

## 建议不动

17. camelCase 别名：`render.py:238-240`、`transcript.py:122-123,288-289`、`index_render.py:492`（以及 `book/__init__.py` 的再导出）。`projects.py:144` 的注释写明这是给上层脚本的兼容面，删了不提升页面可读性，只会断外部调用。 — 留。

18. `transcript.py:47` / `:154` 两个解析器，以及 `collect.py:299-306`「读一次、解析两遍」。一份是 episode 摘要（关联 run、首页用），一份是逐条 block（对话区用），字段和语义本来就不同；合并会把两种视图塞进一个循环，正是「可读性大于简洁性」该停手的地方。 — 留。

19. 看起来像死代码、其实都被动态使用：`.blk.think`（`chat.js:187`）、`.tool .fail` 和 `.fd`（`chat.js:178`，由 `b.ok` 动态追加，字面量里搜不到）、`.evsec/.evhd`（`chat.js:211`）、`.imgref`（`chat.js:81`）、`.chat-stop`（`chat.js:128`）、`.btn.off` 两个占位按钮（`app.js:423` 的「发布」、`app.js:631` 的「复制」；`docs/book.md:93` 明说发布故意不代劳）。 — 留。

20. 调试/兜底钩子：`app.js:29-33` 的 `ground()` 兼容 `RT.files` 形状（现在 serve.py 只发 `inputs`，但注释说明误判会让整条链被错标成「已移出」，这个兜底值钱）；`?selftest`/`?sel`/`?node`/`?focus`（`app.js:766-884,1215-1220`）、`?send`/`?unread`/`?chat`（`chat.js:343,606,625`）、`__CHATSTATE`（`chat.js:614`）；CSS 里 `--cw,360px` 的 fallback（`app.css:142,184,219`）与 `chat.js:26` 的 360——首帧 JS 未跑时需要 fallback。 — 留，最好在 `docs/book.md` 补一句这些 query 是截图/排障入口，免得下次又被当成死代码删掉。

## 如果只做三件事

1. **`.tbt` → `.tbtn`**：把 `app.css:370-373` 改名，删掉 `index_render.py:58-64` 的补丁，统一 `.go` 底色。修一个真实可见的缺陷（账页两组按钮裸奔），并让 4 条从出生起就没匹配上的规则重新生效。
2. **CSS 死规则清扫 + 合并补丁**：删 `196-200`、`216`、`327`、`394-396`，合并 `204/275`、`195/471`。十几行，零行为变化；ruff 管不到 CSS，只能靠人。
3. **前端死钩子收口**：删 `closure()`、`.drag` 类开关、`__CHAT.open()`；对 `types.py` 的 6 个页面不读字段和 `start` 事件做个明确决定（用起来 / 停发 / 注释为 API 专用）。这些是「看着像功能、其实没接线」的地方，比多余样式更费下一个人的时间。
