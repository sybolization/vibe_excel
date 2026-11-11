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

## 多个 store

一个 store 是「一个控制平面的范围」，不是全机器。想在首页同时看到几个 store 的项目，
就把 `--store` 多给几次：

```powershell
python -m vibe_excel.book build --store C:\data\a --store C:\data\b --out .\site
```

- 每个项目会带上来历（`<store 目录名> · <会话名>`），否则两个同名项目分不清；
  只给一个 store 时不加这层前缀——没必要每行都重复一遍。
- 合并后按「最近活跃」重排，与单个 store 的规则一致。
- 页面文件名仍取项目 id 前 8 位；万一同名冲突会加序号，不会互相覆盖。
- 单页模式（`--out x.html`）只接一个 store：它只渲染一个项目，多了没意义。

## 服务模式：为什么需要它

`book build` 出来的是一份静态 HTML，双击就能看——但它写不了磁盘。
「导入文件」「移出输入」「重建批次」都要动 ground-truth 目录、清单名单和 store 里的归档，
所以这些能力只在服务模式下开：

```powershell
python -m vibe_excel.book serve --store <store> --port 8731 --open
```

- 页面**现场采集**：每次刷新重新读 `control.db` 和运行目录，导入/删除后马上能看到结果
- 只绑 `127.0.0.1`，不开局域网
- 静态导出仍然保留，只是导入/删除按钮会置灰、并在悬停提示里说明原因——
  能力缺失要看得见，这比让它消失更不容易误判

运行态由 `render_book(..., runtime=...)` 写进页面里的
`<script type="application/json" id="book-runtime">`，前端据此决定开不开这些功能。

## 诊断侧栏

诊断（缺页、追不到的原始文件这类）原本钉在顶栏右上，展开就盖住深浅色开关和「发布」。
现在收在左下角：

- 收起态是一个 44px 的方块，只露告警图标与条数，不占画布
- 展开向上浮起，不挤压底部面板；右边缘可拖动改宽度（260–560px）
- 开合与宽度记在浏览器（`localStorage` 的 `vibe-book-warn`）
- `?warn=open` / `?warn=shut` 可以压过记忆值，排查和截图都用得上
- 没有诊断时整个方块不渲染，不留空壳

## 构建期输入：导入 / 移出 / 撤回

侧边栏管的是「构建 flow 用的输入」（ground-truth）。
发布后喂给 flow 的运行数据是另一套界面，不在这里。

**导入**：文件写进清单 `sessions.inputs.dir` 指向的目录，同时把文件名登记进 `inputs.files`。
`setup` 只认这份名单，不登记等于没导入。清单只做「files 那几行」的定点改写，
注释、顺序、别的插件原样保留。

**移出**：不是真删。

```
<store>/.trash/<时间戳>/       ← 撤回窗口期
    orders.xlsx
    packet.json                ← 元信息 + 表清单 + 受影响链路 + 移出理由
<store>/.archive/<时间戳>/     ← 重建批次成功后转正，长期保留
<store>/input-events.jsonl     ← 追加式事件流水
```

"包"（`packet.json`）是给 agent 准备的：将来接上对话通道，它只要读这个包，
就知道「哪个文件被拿掉了、为什么、牵动了哪些表」，再决定脚本怎么改。
原始文件一直在归档里，随时能读回全文。

**撤回**：只撤最近一次未被撤回的移出（`undo`），文件和名单同时恢复。

**页面上的灰**：判定不靠"记住了哪些操作"，而是对比当前批次和 ground-truth——
批次里有、ground-truth 里已经没有的文件，它带出的整条链（文件 → sheet → 表 → 机器 → 产物）
一起变灰。刷新、换机器都成立。灰掉的节点仍然可以点开看历史，只是底栏会说
「来源已移出输入，重建批次前不可用」。

## 代码在哪

| 文件 | 职责 |
| --- | --- |
| `book/projects.py` | `collect_projects`：只读 `control.db`，出 `ProjectSummary` 列表 |
| `book/index_render.py` | `render_index`：复用 `app.css` 渲染索引页 |
| `book/render.py` | `render_book(view, fonts=, home=)`；`font_face_css` |
| `book/cli.py` | 单页 / 站点 / 服务三种模式 |
| `book/serve.py` | 本地服务：页面现场采集 + 输入编辑接口 |
| `book/inputs_edit.py` | ground-truth 加/删/撤回、归档与事件 |
| `book/collect.py` | `collect_book(program_id=)`：站点模式按项目选会话 |

索引页没有自己的样式表：它内联 `app.css`，索引专属布局写在页面里的一小段 `<style>`。
token、玻璃顶栏、字号阶、主题开关全部与账页同源——两页看起来是一套东西，是因为真的是。
