# tests / tools / pyproject 审查

审查对象：分支 `python`，HEAD `cc9aeb6`（审查过程中 HEAD 从 `7dd4af3` 前进到 `cc9aeb6`，下文行号按 `cc9aeb6`）。
范围：`tests/` 全部、`tools/` 全部、`pyproject.toml` 的测试/打包配置。只读，未改动任何源码。

结论摘要：

- **`tests/test_jobs.py::test_helper_is_written_once_and_runnable` 现在是红的**：HEAD `cc9aeb6` 上 362 条用例跑出 `1 failed, 361 passed`（75 s），cp936 环境下稳定复现，加 `PYTHONUTF8=1` 即绿。提交 `cc9aeb6` 自称"362 passed"，那只在 UTF-8 模式下成立。
- `tests/` 里**没有 `conftest.py`**（0 个），测试脚手架全部按文件各写一份。
- 新代码里最大的两个空洞：`projects_edit.create_workspace`（0 条用例）与 `book/jobs.py` 的日志/盯梢路径。
- `cc9aeb6` 顺手修掉的 `proc_box` 传错、并补了 `test_stop_terminates_the_running_process`（`tests/test_chat_runner.py:247`），这条**已经覆盖，不必再报**。

## 覆盖空洞

- `book/jobs.py:63 _read_log`（+ `MAX_LOG_BYTES` `:29`）— **日志编码没人管，中文会被自己吃掉，且 5 MB 截断零测试**。Windows 分支 `assets/book/bg.py:47-55` 用 `cmd /c ... >> out.log` 落日志：子进程 stdout 是文件、按 cp936 编码，`_read_log` 却 `decode("utf-8","replace")`。我实测过：`print('中文输出 ok')` 经 `bg.py` 跑完，`out.log` 里是 GBK 字节（`214,208,206,196,202,228,179,246`），`_read_log` 返回 `'__DONE__ 0 \r\n\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd ok'` —— 7 个替换符；而它就是 `wake_text()`（`:136`）交给 agent 的正文。建议 `test_read_log_keeps_chinese_from_a_cp936_log`、`test_read_log_truncates_at_the_5mb_line`（造 5 MB+1 字节，断言长度封顶且末尾标记被剥掉）。风险：中文输出在唤醒文本里全变 `�`，agent 拿到乱码还以为命令没输出；截断边界没人钉，改错了也不会红。这也是上面那条红测试的同一个根因。

- `book/jobs.py:143 Watcher` + `:95 harvest` — **"停止后不再回调"和"callback 抛异常"都没测**。`tests/test_jobs.py:106` 只在 `finally` 里 `stop()`，从没断言 stop 之后不再收；而 `_loop`（`:165-177`）的 `try` 只捕获 `OSError/VibeError`，callback 抛别的异常会**静默打死这条 daemon 线程**——之后所有对话再也收不到后台 job 结果，服务看起来却一切正常。`harvest` 只测了正常/未完成/已认领，坏日志全没测：`out.log` 是二进制、哨兵是 `__DONE__ abc`（`_marker` 返回 `-1`）、`out.log` 不存在、`.claimed` 是目录。建议 `test_watcher_stops_delivering_after_stop`、`test_watcher_survives_a_callback_that_raises`、`test_harvest_skips_jobs_without_a_readable_marker`。风险：唤醒链路断在一个没人看得见的地方，且是"永远"。

- `book/chat_runner.py:79 subscribe` / `:90 unsubscribe` — **订阅者、退订、backlog 回放、队列满丢事件全零覆盖**。全仓唯一调用在 `tests/test_chat_runner.py:131`：拿完返回值就丢，内容从没检查过；`unsubscribe` 没有任何测试调用；`subscribe` 对未知 conversationId 返回 `None` 没测；`_MAX_QUEUED_EVENTS`（`:31`）满了 `queue.Full` 直接丢（`:104-108`）也没测。建议 `test_subscriber_gets_the_backlog_then_live_events`（先 publish 再 subscribe，断言 backlog 内容与顺序，然后断言新事件从队列里拿到）、`test_unsubscribe_stops_delivery`。风险：页面刷新/重连丢事件（用户看到"卡住的回答"）；每条 SSE 连接加一个 Queue 且永不释放 → 订阅者泄漏。

- `book/chat_runner.py:278-280` 每轮清 backlog + 六种事件契约 — `conversation.recent = []` 上方的注释写着"别把上一轮的回答再播一遍"，但没有一条测试验证它；`start`(`:280`)、`text`/`thinking`(`:301-304`)、`done`(`:313`)、`interrupted`(`:191`)、`error`(`:207`) 六种事件形状在 362 条用例里**一次都没被断言**，而页面 `assets/book/chat.js` 全靠这些 `type` 名。建议 `test_backlog_only_replays_the_current_turn`、`test_events_are_start_text_done`。风险：改一个 type 名或字段名，后端全绿、页面全哑——这是最难定位的一类回归。

- `book/chat_runner.py:204-207` 异常路径 — `stream_events` 抛 `VibeError`（模型后端挂了是最常见的失败）会走 `_publish({"type":"error"})`；**busy 会不会卡住没有任何测试证明**。我顺代码走了一遍：`_drain` 的 `except` 之后回到循环顶部，`pending` 空时会把 `busy` 置回 `False`，所以现在是对的——但正是"现在对、没人钉住"的那种对。建议 `test_a_failing_turn_clears_busy_and_reports_the_error`（fake stream 直接 `raise VibeError("模型后端没起")`，断言 `runner.state(...)["busy"] is False` 且从订阅队列里拿到 `{"type":"error"}`）。风险：VibeError 是常态路径，一旦 busy 卡死，这段对话永久不可用且页面没有任何提示。

- `book/serve.py:358 /api/chat/send`、`:382 /api/chat/stop`、`:402 /api/chat/events` — **整个 HTTP 层 0 条测试**。全仓没有一条用例起过 `_Handler`（唯一的 HTTP server 是 `tests/test_chat.py:56` 那个替模型的假网关）。`/api/chat/send` 的 programId→project→activeSessionId→context 映射、`new=True`、`conversationId` 透传、三条 400 分支（缺 programId `:375` / 项目不存在 `:509` / 没有会话 `:381`）全没测；`/api/chat/stop` 连"缺 conversationId"都没测。建议起一个 `ThreadingHTTPServer(("127.0.0.1", 0), _Handler)` 的 fixture（约 8 行，`type("H", (_Handler,), {...})` 塞 `stores`/`root`/`runner`），只测 send/stop 的 4 条。风险：错误分支返回 500 而不是 400、字段名拼错，前端只会显示"没问成：未知原因"，用户和我们都定位不到。

- `book/serve.py:402 /api/chat/events`（SSE）值不值得测——**不值得为它起 socket**。它做的三件事里，"backlog 先发"和"终态事件后关流"都是 `Runner.subscribe` + 事件契约的语义（就是上面第 3、4 条），剩下的只有三行 `wfile.write` 加 15 秒 ping。真要加一道，把 `data: {json}\n\n` 的拼帧抽成纯函数 `_sse_frame(event) -> bytes`，测一条"含中文不炸、以两个换行结尾"就够，成本 5 行。风险低。判断：先补订阅与事件契约，这条排最后。

- `book/transcript.py:191-207 role: "note" → event block` — **零覆盖**，而这是新流程的必经之路：每次后台 job 完成都会写 note（`chat_runner.py:232/234/243`），页面重载时靠它渲染成事件条（`assets/book/chat.js:236`）。三条分支都没测：`kind=interrupted` → 文案"你按了停止"（`:199`）、普通 note 文本原样成 event、空 note 跳过。建议 `test_conversation_renders_job_notes_as_events`、`test_conversation_skips_empty_notes`。风险：job 事件在页面上整条消失；或者更糟——把 note 当成人说的话塞进上下文。另注：同一句"你按了停止"在 `transcript.py:199` 与 `chat.js:513` 各硬编码一次，没有测试把两边钉在一起，改一处就会出现两种说法。

- `book/projects_edit.py:116 create_workspace` — **一条用例都没有**，而它现在是 `/api/projects/create` 的唯一路径（`serve.py:516`），也是 `8bac3d0` 的主产物（`create_project` 有 6 条，`create_workspace` 0 条）。该测而没测的四个行为：① 建出 `root/<名字>/control.db` 且里面恰好一个同名 program；② 同名工作区已存在（有 control.db）→ `已经有了同名的工作区`（`:124-126`）；③ 同名目录被别的东西占着、没有 control.db → `这个名字已被 … 下的其它内容占用`（`:127`）；④ `SessionStore.open` 失败要 `rmtree` 回滚、`root/<名字>` 不能留下半个目录（`:132-137`，用 monkeypatch 让 open 抛）。建议 `test_create_workspace_creates_db_and_program`、`test_create_workspace_rejects_an_existing_workspace`、`test_create_workspace_rejects_an_occupied_dir`、`test_create_workspace_rolls_back_when_the_db_cannot_open`。风险：新建失败后 `root/名字` 留一个没有 control.db 的目录，索引页扫到它 → 之后每次点进去都报错，用户只能手删。（顺手：`create_workspace` 没进 `__all__` `:33-43`。）

- `pyproject.toml` 两处 — ① `examples/pipeline.py` 被 force-include 进 wheel（`:39`），却 ruff 显式排除（`:50`）、mypy 不管（`:57`）、tests **一次都没引用**：发布包里带一个没人验证的文件（我用 `compile()` 过了一遍，现在能编译，所以这是"锁不住"而不是"已坏"），建议加 3 行 `test_examples_compile`，或别打进包；② mypy `files = ["src/vibe_excel"]`（`:57`）不覆盖 tests，`[tool.pytest.ini_options]`（`:42-45`）只有 `-ra`，没有 `filterwarnings`——测试里 279 个函数、5 千行的 `Any` 与签名漂移没人管。风险：前者是发布物完整性，后者是维护成本，都不致命。

## 重复脚手架

- **`tests/` 里没有 `conftest.py`（0 个），`store` 骨架在 4 个文件各写一份**：`tests/test_chat.py:74 _store`、`tests/test_chat_runner.py:23 _make_conversation`（这两份只 `mkdir`，很便宜）、`tests/test_projects_edit.py:34 _store` / `:71 _empty_store`、`tests/test_projects.py:28 _store_with_runs`（这三份走真 `SessionStore`）。全仓 `SessionStore.open` 共 9 处。我的判断：**只值得抽最底层那一层**——`conftest.py` 里给一个 `store_factory` fixture，负责 `SessionStore.open` + `create_program`（+ 可选 `create_session`），因为每个 program 都要真 `git init`（实测 `create_program` 0.774 s、`create_session` 0.372 s、`open` 0.036 s）。各文件里往里插 runs/publishes 的那几段**别动**：它们就是各自文件在讲的事，抽走之后读的人得在两个文件之间跳，得不偿失。

- **唯一我建议动手的一处：`tests/test_projects_edit.py:187 test_archive_rejects_bad_identifiers`** 是 7 个参数，每个都在用例开头 `root, _, _ = _store(tmp_path)` 建一个完整 store（含 git init，各 1.6–1.8 s），可 `archive_project` 在 `_clean_id`（`projects_edit.py:171-176`）就 raise 了、**根本没碰到 store**。该文件 35 条用例跑 27.5 s，这 7 条占约 11 s（全量套件的 ~15%），只为断言一个名字校验。改法可以完全不动用例体：把 `_store` 调用换成 `tmp_path.mkdir(exist_ok=True)`，或给它一个 module 级小 fixture。可读性不受影响，纯赚。

- **别抽的**：起本地 HTTP 服务的脚手架目前只有一份（`tests/test_chat.py:56-71` 的 `_Stub` + `stub` fixture），等真去补第 6 条那几个路由时再抽，现在抽只会多跳一层；`tests/test_book.py:29 _jsonl` 与手写 `json.dumps` 混用是文件内风格，统一它也提高不了可读性。

## 脆弱测试

1. **`tests/test_jobs.py:40 test_helper_is_written_once_and_runnable`——现在是红的，而且靠环境才绿**。`:48-56` 用 `subprocess.run([sys.executable, str(target)], capture_output=True, text=True, encoding="utf-8")` 跑 `bg.py`，`bg.py:34` 打印中文"用法"。子进程 stdout 是管道 → 按 cp936 编码，父进程按 UTF-8 解 → `'\ufffd\xf7\ufffd\ufffd\ufffdpython _chat/bg.py "<command>"'`，`assert "用法" in result.stdout` 必红。同仓已有两种正确写法：`tests/test_chat.py:174-179` 的注释直说"Windows 控制台默认 cp936，父进程按 UTF-8 读会崩"，`tests/test_encoder.py:1156-1158` 显式给子进程 `PYTHONUTF8=1`。修法一行：给 `subprocess.run` 传 `env={**os.environ, "PYTHONUTF8": "1"}`（或 `PYTHONIOENCODING=utf-8`）。我验证过：加上 `PYTHONUTF8=1` 后这条从红变绿。

2. **`tests/test_chat_runner.py:114 test_stop_marks_interrupted_and_does_not_fake_an_answer`——靠 `time.sleep(1.5)` 撑出"正在跑"的窗口**（`:120`）。`stop()` 必须落在这 1.5 s 内，晚到就变成"一轮正常跑完"，`assert assistant == []`（`:145-148`）直接红；机器忙、调试器断点、GC 都会把它推向失败。同文件新增的 `:247 test_stop_terminates_the_running_process` 用的是同样的 1.5 s 写法（`:272`）。正确做法这个文件里已经演示过：`:57` 用的是 `threading.Event` gate，`stop()` 之后再 `gate.set()`。顺手把 `:114` 改造成 gate 式，稳定性立刻上来。

3. **`tests/test_jobs.py:128 test_snapshot_commits_changes`——依赖真实 git**。它自己配了 `user.email/user.name`（这点做得对），但仍受全局配置左右：`commit.gpgsign=true`（很常见）会让 `git commit` 失败、`snapshot()` 返回 `""`，用例直接红；`core.autocrlf`、`core.hooksPath`、`init.defaultBranch` 也会改行为。更麻烦的是没有 git 的机器上它 `pytest.skip`（`:131-132`）——**回归会静默消失**。另外 `jobs.py:209` 取的是 `commit.stdout.splitlines()[0]`，git 一旦先打一行提示就取错（本机 `git add` 的 autocrlf 警告就在 stderr，侥幸没事）。建议：把 `snapshot` 的 git 调用收进一个可替换的小函数，正常路径用 fake 断言 argv 与"无改动就不提交"，真 git 那条单独打 marker 并保留 skip。

紧随其后的两条（各一句话）：`tests/test_chat_runner.py:74` 用 `assert elapsed < 1.0` 给"send 立即返回"计时，压测下容易擦线——用 gate 证明"没被卡住"比计时稳；`tests/test_jobs.py:97 test_wake_text_says_what_happened` 把写给模型的整句话拆成 `后台任务结束`/`退出码 9`/`没有输出` 三个字面量，这段文案是最可能被反复调的 prompt，建议只钉结构（job 名、退出码数字、空输出有占位）。

## 建议不动

- **`tools/` 整体别加测试**。`tools/stub_gateway.py` 是手工验页面的假网关（带 `LAST_QUESTION`/轮次状态），和 `tests/test_chat.py` 的 `_Stub` fixture 用途不同；`tools/pi/` 是第三方 npm 装配（`node_modules` 已 gitignore、无 lock）。给它们写测试等于上一个永远没人跑的维护面，README 里点一句用途就够。

- **`tests/test_book.py` 里的大块数据 fixture 留原地**（`_book_view`、`_INPUT_TABLES`、`_DIAGRAM_*`、`_jsonl`；该文件 11 个私有 helper）。它们就是"物化/派生/孤儿/分层"这些概念在该文件的说法，抽到 conftest 后读的人要在两个文件之间来回跳。用户说得对：可读性大于简洁性。

- **`tests/test_harness_support.py` 的 `_write_script` / `FAKE_PI_*` 子进程脚本别改写成 mock**。它用真的子进程 stdout 行通信，是唯一覆盖 `AgentPiService` 超时（`:717`）、问答回写（`:696`）、取消（`:733`）三条路径的方式；换成 mock 就失去"真的读一行 JSON"的验证，也正是这类测试能抓到 `bg.py`/`pi` 的编码问题。同理 `tests/test_encoder.py:1156 test_module_cli_is_independent_of_node` 留着——它是唯一证明"打包后 CLI 能独立跑"的东西。

- **别给 pytest 上 `-W error`/`filterwarnings = ["error"]`**。pandas/openpyxl 在这条链上会出 warning，一刀切会红成一片；真要收，范围写死到本项目的 `DeprecationWarning`。反过来，`tests/test_projects_edit.py:348 test_resolve_store_only_accepts_the_stores_the_service_opened` 别急着删：`resolve_store` 现在 src 里已经零调用（只剩 `serve.py:45` 的 `__all__` 和定义），要不要这个函数是另一个决定；先删测试会把"这个入口已经不在了"这件事藏起来，建议留着并单独问一句"这函数还留吗"。

## 如果只做三件事

1. **把红的修了，并顺手治根因**：`tests/test_jobs.py:48` 给子进程 `PYTHONUTF8=1`（一行），同时补 `test_read_log` 对 GBK 日志的用例——因为 `bg.py` 在 Windows 上确实写 GBK，现在 agent 拿到的唤醒正文就是 `�`。
2. **给 `create_workspace` 补 4 条（含失败回滚）**：它是"新建项目"的唯一路径，`create_project` 有 6 条、它有 0 条；回滚那条尤其值得写在测试里，它保护的是"用户点了新建，磁盘上别留一个坏目录"。
3. **给 `Runner` 补 3 条**：订阅/backlog 回放、异常路径清 busy、六种事件的形状。这三条正是"页面看起来正常"与"静默丢消息"的分界线，也是没有 HTTP 级测试时最划算的替代。
