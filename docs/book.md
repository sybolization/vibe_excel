# book 站点：索引页与项目页

`book build` 有两种输出，入口同一条命令，靠 `--out` 区分：

| `--out` | 产物 | 用在哪 |
| --- | --- | --- |
| `x.html` | 一个项目的账页 | 单个项目、要发给别人一份文件 |
| 目录 | `index.html` + `p-<id前8>.html` + `fonts/` | 日常：先选项目，再进去看 |

## 为什么是两张页面

账页面向「这一个项目」：画布、来源、这一次运行走的每一步。
但打开 store 时先要回答的是另一个问题——看哪个项目。这两个问题混在一张页面上，
画布还没出现就得先挑项目，属于把选择成本塞进了主界面。

所以索引页只做一件事：列出项目，点进去。它是静态多页，不做前端路由：
`file://` 双击就能开，整个目录拷走后链接仍然有效。

## 索引页显示什么

每个项目一行，只用 `control.db` 里的聚合，不解析 `run.json`——首页要秒开。

| 位置 | 内容 | 为什么在 |
| --- | --- | --- |
| 左 | 项目名 / 会话名 | 认出是哪个项目、哪一轮 |
| 中 | `N 次运行 · N 次发布` | 判断这个项目有没有活过 |
| 右 | 状态 chip + 时间 | 最近一次是否成功、两次是否一致 |
| 末 | `进入 →` | 唯一动作 |

状态取自 `runs.status` 与 `runs.note` 里的 `identical=true` 标记：
没有标记是**未知**，不写成「不一致」，那是两件事。

采不到页面的项目仍然列出来，灰掉、不给箭头——比列一个点了会 404 的行诚实。

## 导航

- 索引 → `p-<id前8>.html`（相对链接）
- 项目页顶栏最左的 `←` 回索引。这个入口由 `<body data-home="index.html">` 驱动：
  单页模式下没有这个属性，页面行为与以前完全一致。
- 主题选择写在 `localStorage` 的 `vibe-book-theme`，两张页面共用一个值。

## 字体

字体文件在 `src/vibe_excel/assets/book/fonts/`，`book build` 原样复制到站点目录的 `fonts/`。

**为什么侧车而不是 base64 内联**：内联让每页 +21 MB，而一个站点有 N 个项目页，成本乘 N；
许可第 2 条允许「embed, bundle, redistribute … unmodified copies … with any software」，
随软件目录一起分发属于被允许的形态。

**为什么不做子集化**：许可第 2 条禁止对字体做任何修改，子集化就是修改。只带 Regular / Medium，
600 由 Medium 合成。许可与版权声明随字体一起复制（`fonts/LICENSE.txt`），声明本身放在 README，
不往界面里塞——页面上每多一行说明，都在挤占读图的注意力。

字体加载路径由 `font_face_css(prefix)` 决定：站点模式用 `fonts/`，单页模式不注入
`@font-face`，回退到本机字体。缺字体文件时站点照常生成，只是页面回落到系统字体。

## 代码在哪

| 文件 | 职责 |
| --- | --- |
| `book/projects.py` | `collect_projects`：只读 `control.db`，出 `ProjectSummary` 列表 |
| `book/index_render.py` | `render_index`：复用 `app.css` 渲染索引页 |
| `book/render.py` | `render_book(view, fonts=, home=)`；`font_face_css` |
| `book/cli.py` | 单页 / 站点两种模式，字体复制 |
| `book/collect.py` | `collect_book(program_id=)`：站点模式按项目选会话 |

索引页没有自己的样式表：它内联 `app.css`，索引专属布局写在页面里的一小段 `<style>`。
token、玻璃顶栏、字号阶、主题开关全部与账页同源——两页看起来是一套东西，是因为真的是。
