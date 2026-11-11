# book/ 后端审查（serve / chat / chat_runner / jobs / projects / projects_edit / collect）

基线：`python` 分支 HEAD `cc9aeb6`，行号按当前工作区。只读审查，未改任何源码。
`cc9aeb6` 已清掉 `jobs._json_meta`、`serve._store_label`、`Runner.send(resume=)`、`_Turn.proc`（停止杀进程的修法），本报告不重复；`_Turn.thinking/calls` 是那一轮漏下的。
用 ruff `F401,F811,F841,ARG,B` 扫过 `book/`，全过；AST 扫过 return/raise 后语句，未发现不可达块。

## 死代码

- **旧的一问一答流式链路** — `serve.py:391-394`（`/api/chat/stream` 路由）、`serve.py:442-490`（`_chat_stream`）、`serve.py:25`（`from .chat import send_stream as chat_stream`）、`chat.py:613-662`（`send_stream`）。证据：页面只调 `/api/chat/events|send|stop`（`chat.js:387/532/562`），docs 与 tests 全都没有引用；`send_stream` 连测试都没用。另外 `chat_runner.py:291-298` 与 `chat.py:643-649` 的 reply 解析是同一段逻辑，删旧链路即消。 — 建议：整条删（约 100 行）；删前确认仓库外没有客户端在读这个 ndjson 接口 — 风险 低
- **`system_prompt` 的 `events` 形参 +「最近的输入变更」段** — `chat.py:303`（签名）、`chat.py:342-346`（拼装）。证据：活路径 `chat_runner.py:272` 固定传 `()`，只有旧链路（`send_stream`/`send`）和测试传真数据。删完旧链路后它只剩测试在用，而 prompt 里那句承诺在生产里根本没执行。 — 建议：把 events 并进 `_chat_context()` 的返回值（serve 侧已 import `events`），从这里删掉形参与拼装；或者整段删、测试同步删 — 风险 中（prompt 内容会变）
- **同步接口 `send()` 与 `ask_pi()`** — `chat.py:665-695`、`chat.py:393-419`。证据：`send` 只有 `tests/test_chat.py:130-221` 在用；活路径里 `stream_events` 在 `backend=="pi"` 时走流式（`chat.py:450-452`），不会进 `ask()`，所以 `ask_pi` 的 pi 分支也只有那个测试能到。 — 建议：删 `send`/`ask_pi`（`ask` 的 http 分支要留，见「建议不动」）；测试迁到 `stream_events` 或直接删 — 风险 中（4 条测试要重写，`ask_pi` 还挂着 `--no-tools` 的旧工具策略）
- **`_Turn.thinking` / `_Turn.calls`** — `chat_runner.py:47,49`。证据：`thinking` 只在 `:300` append、从没被读（最终 thinking 来自 reply 事件）；`calls` 全仓零引用。 — 建议：删两个字段，`_Turn` 只留 `box/stopped/text` — 风险 低
- **`resolve_store()`** — `serve.py:61-72`（导出见 `:45`）。证据：只有 `tests/test_projects_edit.py:348-356` 调用；生产路径全部走新的 `_pick_store()`。docstring 里「不知道要在哪个 store 里建项目」还是「新建要选库」时代的说法（8bac3d0 已去掉选库）。 — 建议：删函数、`__all__` 条目和那条测试 — 风险 低
- **`create_project()` + `_duplicate_name()`** — `projects_edit.py:140-153`、`projects_edit.py:84-113`（`__all__` 见 `:42`）。证据：只有 `tests/test_projects_edit.py` 用（6 处）；服务端新建走 `create_workspace()`，一个项目一个工作区之后「往已有 store 里塞第二个项目」这条路径已不存在，`_duplicate_name` 的防重/防归档逻辑没有活人消费。 — 建议：删两个函数；测试改用 `create_workspace()`（坏名字用例可以原位保留，`_clean_name` 还在用）— 风险 中（测试要改，函数在 `__all__` 里）
- **`collect_book` 的 dict 入参分支** — `collect.py:562-566`（签名 `:547-553`、类型 `types.py:406`）。证据：全仓没有调用方传 dict（cli/serve/tests 都是 `Path`/`str` + 关键字参数）。 — 建议：删分支与 `CollectOptions` 入参（`__init__` 导出一起改）；若确有外部脚本用旧写法，至少标 deprecated — 风险 中（公开 API）

## 重复

- **`serve.py` 里「遍历工作区 × 项目」写了三遍，`programId → 项目` 又两遍** — `:243-245`（索引页）、`:259-261`（项目页）、`:492-497`（`_store_of`）；`/api/chat/send`（`:364-378`）与 `_chat_stream`（`:446-472`）各自在 `_store_of()` 之后又扫一遍 `collect_projects()` 找同一个项目。 — 建议：加 `_project(program_id) -> tuple[Path, ProjectSummary]`（内部一次扫描），三处共用；页面侧再用一个 `_projects()` 生成器 — 风险 低
- **三个 inputs 接口绕过 `_payload()` 裸解析 JSON** — `serve.py:546`、`:563`、`:568`。`_payload()`（`:499-507`）已经把「坏 JSON / 非对象」翻成人话并转成 `VibeError`（400），这三处直接 `json.loads(body or b"{}")`；`do_POST` 只捕 `VibeError`/`OSError`，坏请求会抛 `JSONDecodeError`，连接被直接断开，页面拿不到错误。 — 建议：统一走 `self._payload(body)` — 风险 低（顺手修一个真实故障）
- **归档回滚样板写了三遍** — `projects_edit.py:375-381`、`:396-402`、`:408-414`。都是「`_restore_moves` → 全成功就 `_discard_archive` → 有残留就带上残留原因再抛」。 — 建议：抽 `_rollback(moves, archive, error, prefix)`，三处只传不同的前缀/异常 — 风险 低
- **jsonl 写入有两条路** — `chat.py:184-188`（`append_record`）与 `chat.py:229-245`（`append_message`）。`append_message` 自己 `mkdir` + 开文件 + `json.dumps` + 写一行，`append_record` 做的是同一件事。 — 建议：`append_message` 组好 record 后直接 `append_record(path, record)`，锁与换行只留一份 — 风险 低
- **`identical=true` 的判定写了两份** — `projects.py:35-42`（`_identical`）与 `collect.py:663`（内联三元式）。同一条「note 里有 `identical=true` 才算 True，没有就留 None」的规则，注释还互相点名。 — 建议：收进一个共享函数，collect 侧调用；保留「未知不是 False」的语义 — 风险 低
- **「archived_projects 表在不在」检查写了三遍** — `projects.py:54-61`、`projects_edit.py:99-106`、`projects_edit.py:190-196`。三份 SQL 与「旧 store 缺表 = 没有归档」的约定。 — 建议：在 `projects_edit` 暴露 `archived_ids(db)` 之类的函数，`collect_projects` 反过来 import；语义只写一次 — 风险 低

## 过度抽象

- **`resume` 的粒度是「整条 drain」，但队列里混着两种消息** — `chat_runner.py:161`（用户消息起 drain 传 `False`）、`:196/205`（drain 级参数）、`:262/266/274`（`_turn` 用它决定落不落盘）、`:246-254`（唤醒文本进同一队列）。两个真实后果：① 一轮在跑时 job 结束 → `_wake` 只入队，正在跑的 `False` drain 接着把唤醒文本当用户消息 `append_message(..., "user", ...)` 落盘；② 唤醒起的 `True` drain 里进来的用户消息不落盘，只被当成一次性的 user 消息塞进当前轮，`context` 也会丢（`:253` 传的是 `None`）。 — 建议：给队列项打标（`pending: list[tuple[str, bool]]`，或在 `_Turn` 上加 `persist_user`），`resume` 从 drain 级参数降为每项属性；补「跑着时 job 结束」「唤醒中用户插话」两条测试。顺手：`_drain:208-211` 与循环开头 `:198-201` 完全重复、`send:151-163` 与 `_wake:246-254` 的入队块也重复，改这块时一并收掉 — 风险 中（行为修复，不是重构）
- **`new` 形参没有独立效果，「开始一段新对话」实际是续写** — `serve.py:377` 传 `new=payload.get("new") is True`，`chat_runner.py:124` → `pick_conversation(directory, None if new else conversation_id)`（`chat.py:164-181`）。`new=True` 与「不给 id」走同一条路，都返回最近那个 jsonl；而 `conversationId=""` + `new=true` 正是前端新对话按钮的固定请求（`chat.js:538-539`，草稿 id 是 `"draft"`），所以新对话会并进旧文件。 — 建议：给 `pick_conversation` 加显式 `new` 语义（`new=True` 时永远生成新时间戳文件），或在 `_ensure` 里分支；补一条「`new=True` 后文件数 +1」的测试 — 风险 中（修好后前端行为变化，先和 UI 对一下预期）

## 建议不动

- **`ask()` 的 http 分支、`probe()`、`_flatten()`** — `chat.py:578-610`、`:116-130`、`:350-361`。pi 没装时 backend 自动落到 `http`（`ChatConfig.from_env` 的 `:95`），这是唯一退路；删 `send`/`ask_pi` 时别顺手动 `ask`。 — 留
- **`read_messages` 的双重预算** — `chat.py:248-300`（常量 `:137-139`）。条数 12 + 字数 6000/2000 解决的是不同问题（慢/贵 vs 丢最新一句），注释已解释清楚；也别和 `transcript.parse_conversation` 合并——一个为模型上下文只取 user/assistant 文本，一个为页面保留 thinking/工具块。 — 留
- **`ensure_helper()` / `helper_path()`** — `jobs.py:44-56`、`:40-41`。把 `bg.py` 当真实文件拷进去、内容相同就不碰，注释里写着「拼字符串在转义上栽过」；`helper_path` 是命名的布局约定，测试也用它断言，不要为省一行内联。 — 留
- **`Watcher` 轮询 + `.claimed` 标记 + 硬线常量** — `jobs.py:143-176`、`:95-133`、`:26-29`；`chat_runner.py:31` 的 `_MAX_QUEUED_EVENTS` 与 `:101` 的 200 条 backlog。`harvest` 先写 `.claimed` 再交出去，保证同一 job 只唤醒一次；daemon 线程 + 2 秒轮询够用，换回调/异步只会多一层；400/200 是「宁可丢事件也不卡住 agent」的取舍，别改回无限队列。 — 留
- **camelCase 别名与小私有 helper** — `projects.py:144-146`、`collect.py:718`；`projects.py:25-27` 的 `_rows` 与 `collect.py:92-96` 的 `_query`；`serve.py:402-440` 手写 SSE 帧。别名是对外兼容层，删了不提升可读性；`_rows`/`_query` 各自一行且各有 docstring，抽公共只多一条跨模块依赖；SSE 的 backlog → 循环 → ping → 按 done/error/interrupted 收敛都在一个函数里最好读，不建议抽通用 responder。 — 留

## 如果只做三件事

1. **删旧问答链路**（`serve.py` 路由/`_chat_stream` + `chat.py` 的 `send_stream`/`send`/`ask_pi`，约 170 行）。收益：只剩 `send → 队列 → SSE` 一条路，后来人不会照着旧代码接功能；风险低，前提是确认仓库外没有 ndjson 客户端（仓库内 JS/文档/测试都没调）。
2. **修 `new` 与 `resume` 两个形参**（见「过度抽象」两条）。收益：修好一个用户可见的「新对话点了没反应」，以及两个会写错 jsonl 的唤醒场景；风险中，各补一条回归测试，`new` 的修法先和前端确认预期。
3. **收敛 `serve.py` 的项目查找 + 统一 `_payload`**（见「重复」前两条，另把 `_Turn.thinking/calls`、`resolve_store` 与 `_drain` 尾部顺手删）。收益：每次请求少两遍全库扫描，坏 JSON 从「断连接」变成 400 人话；风险低。
