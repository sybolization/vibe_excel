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

## 一个项目就是一个工作区

新建项目不再问「放进哪个库」：它自己就是一个工作区。目录 `<root>/<项目名>/`，里面是

```
control.db          ← 项目、会话、运行、发布
program-repos/      ← 代码仓库（git）
sessions/           ← 会话 JSONL
data-revisions/  .archive/  .trash/  input-events.jsonl
_chat/              ← 对话用的东西：bg.py、jobs/
```

服务启动时给 `--store`（可多次）说的是「现在要看的那些工作区」，`--root` 说的是
「新建的落在哪儿」（不给就是第一个库的上一级）。root 下带 `control.db` 的目录会被自动发现，
新建完不用重启。

内部字段还叫 `store`：`--store` 多值、按项目找它所属的工作区都靠它。界面上没有这个词了。

## 对话面板

对话不是装饰：发出去的话会追加进项目的会话 JSONL，模型的回答也写进同一份文件。两件事一起做，
才有「接着说」——只显示在浏览器里的话，刷新就没了，而且下一次发送会另起一段。

会话文件的位置是 `sessions/<session-id>/sessions/*.jsonl`，与 `collect_book` 读的是同一处。
续写哪一段：带 `conversationId` 就续它，不带就取最近一段；只有按「新建对话」再发才会另起。

### 它是有工具的

对话里的 agent 跑在 `pi --mode json --tools read,bash,powershell,edit,write,grep,find,ls` 上，
cwd 就是那个工作区。所以它想看列名会自己去查 sqlite、想跑表会自己去跑 `vibectl`——
prompt 里塞的只是这个项目的事实（输入文件、每步读写了哪些表、结果表），不是它唯一的眼睛。

工具调用流进页面：`tool_execution_start` 一到就长出一行灰字（命令原文），
结束补上结果与耗时；失败挂红点徽标，不染整行。落库按 pi 的形状分开写
（思考 → 每个 toolCall + toolResult → 最后那段回答），刷新后顺序还是「先调用、再结果、后回答」。

`publish` 和「移出输入」不代劳：那是不可逆的两颗扣子，留在页面上。

### 排队、停止、后台 job

这些由 `book/chat_runner.py` 管：

- `POST /api/chat/send` 只排队，立刻返回；跑由后台线程来
- `GET /api/chat/events` 是 SSE，把 `thinking / text / call / result / done / error / interrupted`
  一条条推给页面；连上来先补发这一轮已经发生的事件
- 命令跑着的时候你再说话，排在后面接着跑，不打断当前回合
- `POST /api/chat/stop` 结束当前回合，并在会话里写一条 `note{"kind":"interrupted"}`；
  页面显示「你按了停止」，不伪装成谁说的话。agent 闲着时点停止什么都不发生

长命令不该干等。工作区里有 `_chat/bg.py`（包里的 `assets/book/bg.py` 照抄过去）：

```
python _chat/bg.py "<command>"
→ job 20261111-154903
→ log .../_chat/jobs/20261111-154903/out.log
```

命令在一个脱离进程里跑，输出进日志，末尾写一行 `__DONE__ <退出码>`。服务端的
`book/jobs.py::Watcher` 每 2 秒扫一遍，看到哨兵就认领并唤醒对话——那段话只进这一次的
上下文，不落盘（它不是用户说的）。人一说话唤醒计数清零，连续唤醒超过 3 次就先停手：
不然「唤醒 → 又起 job → 又唤醒」会自己转起来。

硬线：同时最多 20 个 job、单个最多 1 小时、日志超过 5 MB 截断。

每个回合开始前会给 `program-repos/` 提交一次快照（没有变化就跳过），改坏了能回退。

### 后端

- `pi`（默认）：`pi -p --no-session --tools … --provider … --model …`，鉴权、网关、
  Cloudflare 那一层都由 pi 负责。key 走 `OPENCODE_API_KEY`
- `http`：直连 OpenAI 兼容的 `/chat/completions`（本机网关不需要 key）

没有可用后端时如实报错，不伪造回复——比让人以为说上了话更接近事实。

## 代码在哪

| 文件 | 职责 |
| --- | --- |
| `book/projects.py` | `collect_projects`：只读 `control.db`，出 `ProjectSummary` 列表 |
| `book/index_render.py` | `render_index`：复用 `app.css` 渲染索引页 |
| `book/render.py` | `render_book(view, fonts=, home=)`；`font_face_css` |
| `book/cli.py` | 单页 / 站点 / 服务三种模式 |
| `book/serve.py` | 本地服务：页面现场采集 + 输入编辑接口 + 对话事件流 |
| `book/chat.py` | 调 pi（工具全开）、把 pi 的事件摊成页面事件 |
| `book/chat_runner.py` | 一段对话的执行器：排队、停止、被 job 唤醒 |
| `book/jobs.py` | 后台 job（哨兵、认领、上限）与回合前快照 |
| `book/inputs_edit.py` | ground-truth 加/删/撤回、归档与事件 |
| `book/collect.py` | `collect_book(program_id=)`：站点模式按项目选会话 |

索引页没有自己的样式表：它内联 `app.css`，索引专属布局写在页面里的一小段 `<style>`。
token、玻璃顶栏、字号阶、主题开关全部与账页同源——两页看起来是一套东西，是因为真的是。
