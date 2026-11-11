# 代码审查：死代码与瘦身

四份审查报告（`backend.md` / `frontend.md` / `engine.md` / `tests.md`）是四个只读
subagent 各自扫一块地盘得出的，基线是 `ce4d8f8`。这里记的是「已经做了什么」「
还留着什么没动、为什么」——免得下一轮又把同样的东西重新发现一遍。

约定：可读性大于代码简洁性。合并函数、缩短名字、删注释来换行数，一律不做。

## 已经删掉的

- **旧的 `/api/chat/stream` 整条链路**（约 100 行）：路由、`_chat_stream`、
  只给它用的 `chat.send_stream`。页面早就走 `/api/chat/send` + `/api/chat/events`
- **sqlmap 的 camelCase 兼容面**：7 个别名、`BuildInput`、`run_cli` 转发，
  以及 `build_lineage` 里要求 `dbPath` 的那条入参分支（仓内调用全传 `db_path`，走不到）
- **一批零引用符号**：`flow_code.nodes_of_code_flow`、`workflow.Executor`、
  `SessionStore.archive_session`、`runtime` 的 `run_directory`/`with_session_lock`/`diff_run_files`、
  `git.current_branch`、`kernel.SERVICE_NAMES`/`SessionsService`、
  `contracts.DEFAULT_EXEC_LIMITS`、`host.FIBER_STATES`
- **book 包的 10 个 camelCase 别名**：API 一律 snake_case，JSON 视图键仍是 camelCase
  （那是数据形状，不是命名）
- **前端遗留规则**：`.bp-ft .box` / `.bp-ft input` / `.bp-hint` / `.note.mono` /
  `.ask-r` / `.chat-stop[hidden]`、`.ix-newdlg select`、`app.js` 的 `closure()`、
  `chat.js` 的 `__CHAT.open()`
- **两处真 bug**：`.tbt` 是 `tbtn` 的笔误（账页按钮从引入起就没样式）；
  `chat_runner` 的停止接口拿不到子进程（把 `[turn]` 当成盒子传了）

## 留着没动的，和为什么

| 东西 | 为什么留 |
| --- | --- |
| `chat.send` / `ask_pi` / `ask` | 产品侧已经不走它，但 `tests/test_chat.py` 里四条用例（对话连续性、后端挂掉时问题留档、pi 链路、缺 key 报错）挂在它上面，是 http 兜底路径唯一能测的入口。要删得先把这四条迁到 `Runner`——那是一次独立的改动，别混在清理里 |
| `types.BookView` 的 `generatedAt`/`store`/`publishes`/`transcript`/`focusEpisode`/`warnings` | 页面一个都不读，但 `collect_book` 是公开函数，这些字段可能是仓库外的契约。删之前得先确认没有外部消费者；`transcript`/`focusEpisode` 是旧「对话摘要列表」的残留，最可疑 |
| `host.py` 的鸭子类型插件分支 | 仓内 7 个插件都是 `define_plugin(...)`，这条分支永远走不到；但它是留给外部插件的扩展点。要删就得改文档说「只接受 PluginShape」 |
| `serve.resolve_store` / `projects_edit.create_project` | 现在只有测试在用（产品走 `_pick_store` / `create_workspace`）。它们钉的是「未知 store 要报错」这条语义，删之前请把语义搬到新入口上 |
| `plugins` 里未用的 `config` 形参 | 签名由 `PluginShape.apply` 规定，删了就装不上 |
| `jobs.DONE_MARK` 与 `bg.py` 里的 `DONE` 各写一份 | `bg.py` 要被原样拷进每个工作区、独立运行，不能 import 包内常量 |
| 三处「跑一轮」里剩下的两处（`chat.send` 内部） | 与上面 `chat.send` 同一条：等测试迁走了一起删 |

## 下一轮如果要继续

按收益/风险排：

1. 把 `tests/test_chat.py` 那四条迁到 `Runner`，然后删 `chat.send`（连同它内部那份「跑一轮」）
2. 确认 `BookView` 那几个字段没有外部消费者，然后停止把它们序列化进页面
3. `serve` 里「项目 → 所属工作区」有五种写法，`_store_of` 每次还会重扫一遍
   `collect_projects`（每次都查 control.db）；收成一个 `_find_project()`
4. `jobs.snapshot` 手搓了 `git status/add/commit`，而仓库本来有 `git.py` 的薄封装
   （还带 `--no-pager` 和 timeout）；换成它，顺手补一行日志——现在快照成没成没人知道
