# 引擎层审查（sqlmap / encoder / session / cli / util / 根模块 / engines / tools）

审查对象：`C:\project\vibe_excel-python`（分支 `python`，HEAD `7dd4af3`）。

两条上下文：

- 引擎层（`sqlmap/`、`encoder/`、`session.py`、`flow*.py`、`runtime.py`、`workflow.py`、`engines/`、`util.py`）自成一个提交 `0adbe27 feat(python): 独立的 python 分支——Python 版直接铺在仓库根上`，之后 8 个提交全在 `book/chat`。也就是说这部分是**一次性从 TS 直译过来、再没被维护过**的，下面很多问题都是直译残留。
- 初筛命令 `ruff check --select F401,F811,F841,ARG,B` 在范围内只报 `tools/stub_gateway.py:58-59 ARG002`（覆盖父类 `log_message` 的形参，属噪音），没有未使用 import、没有未使用变量、没有不可达语句的静态告警。所以下面全是 ruff 看不到的「跨文件没人调用」和「语义重复」。

范围说明：`book/` 与 `assets/` 按要求没看；为覆盖「真正干活的引擎」，顺带核了 `engines/` 以及被根模块引用的 `plugins/`，但结论只落在引擎侧。

## 死代码

- `src/vibe_excel/sqlmap/` 的 TS 兼容面（`__init__.py:7-19,52-59`、`build.py:33-38,118-124,223-226`、`lineage.py:324`、`render.py:86-87`）— 7 个 camelCase 别名（`buildLineage`/`readDatabase`/`readCsv`/`splitCsvLine`/`parseLineage`/`renderHtml`/`escapeForScript`）只在「定义处 + `__init__` 再导出」出现，全仓（src/tests/tools/docs）没有任何 import 或调用；`BuildInput` TypedDict 从未作注解或实例化，`build_lineage` 的 camelCase 入参分支要求 `doc` 带 `dbPath`，而仓内 4 处调用（`sqlmap/cli.py:68`、`tests/test_runtime_session_sqlmap.py:185/201/213`）都显式传 `db_path`，分支永远走不到；`__init__.py:42-49` 的 `run_cli` 惰性转发也没有调用者（控制台入口是 `pyproject.toml:18` 的 `vibe_excel.sqlmap.cli:main`）。唯一提到这些别名的是 `docs/visualizations.md:66` 的「API 承诺」。建议：整套删掉只留 snake_case；如果确实要给旧 TS 脚本留，就补一条真正调用它们的测试——现在删掉没有任何测试会红。风险 低（仓内零引用；外部脚本不可见）
- `src/vibe_excel/flow_code.py:153-159` — `nodes_of_code_flow` 只是 `return list(flow.nodes)`，再挂一个 `nodesOfCodeFlow` 别名；两个名字除这两行外全仓零出现，注释自己写着「保留函数名与 TS 的 nodesOfCodeFlow 对齐」。建议：删，调用方直接取 `CodeFlow.nodes`。风险 低
- `src/vibe_excel/workflow.py:123-156` — `Executor` 类全仓无实例化（`grep "Executor("` 只命中 `plugins/agent_pi.py:244` 的 `ThreadPoolExecutor`）；同职责已由 `plugins/exec_plugin.py:64-101` 的 `ExecService.run` 实际承担（同样是 `clean_rerun(...) -> 结果`）。建议：删 `Executor`，只留 ExecService 一条执行路径。风险 低
- `src/vibe_excel/session.py:399-406` — `SessionStore.archive_session` 全仓无调用者、tests 未覆盖（项目级归档在 `book/projects_edit.py`，不在我范围）。建议：删，或补一条「归档后 `get_session` 仍可读、工作树已移除」的测试再留。风险 低
- `src/vibe_excel/runtime.py:84-86`、`218-224`、`416-426` — `run_directory`、`with_session_lock`、`diff_run_files` 三个公开函数全仓零调用（`tests/test_encoder.py:132` 的 `_run_directory` 是测试自己的 helper，不是它）；锁的真实路径是 `plugins/workspace.py:52 -> with_lock_sync`；`diff_run_files` 还与 `workflow.diff_rounds:315-324` 是同一类差异计算。建议：三个都删。风险 低
- 无人引用的声明（同一批 TS 对齐残留）：`src/vibe_excel/git.py:72-74` `current_branch`（git 状态由 `workspace_facts.py:359-364` 自己跑 `rev-parse`）；`src/vibe_excel/kernel.py:26-34` `SERVICE_NAMES`（内容与 22-24 行 `ServiceName` Literal 逐字重复）；`src/vibe_excel/kernel.py:301-320` `SessionsService` Protocol（`plugins/sessions.py` 是真实实现，但从未声明符合它，也无任何类型注解引用）；`src/vibe_excel/contracts.py:41` `DEFAULT_EXEC_LIMITS`（`process.py:97` 直接 `ExecLimits()`）；`src/vibe_excel/host.py:32-34` `FIBER_STATES`（只被 41 行 docstring 提到，且缺 host 实际会产出的 `DISABLED`，见 `host.py:148`）。建议：整批删；`FIBER_STATES` 若要留就补 `DISABLED` 并让 `PluginReport.state` 改用 Literal 校验。风险 低
- 两处不可达／从未执行的分支：`src/vibe_excel/host.py:111-124` 的「非 `PluginShape` 的鸭子类型插件」转换分支——仓内 7 个插件全部 `plugin = define_plugin(...)`，测试也只覆盖这个形状；`src/vibe_excel/manifest.py:147` 的 `raise AssertionError("unreachable")` 位于 `fail()`（标了 `NoReturn`）之后，永远到不了。建议：host 分支二选一（删掉并改文档「只接受 PluginShape」，或补一条文件型插件测试）；manifest 的 raise 直接删。风险 中（host 分支是留给外部插件的扩展点，删前确认没人用）

## 重复

- 会话/程序目录布局在三处各拼一遍：`runtime.py:59-66` 的 `workspace_paths` 定义 `sessions/<id>/{work,code,sessions,agent,runs}` 与 `data-revisions/`；`session.py:346` 又用 `self.store_root / "sessions" / id / "code"` 拼一次；`session.py:370-371` 的 `create_session` 再拼一次 root。旁证：`book/projects_edit.py:349`、`book/jobs.py:181` 也在拼 `program-repos/<id>`（book 不在我范围，只作为证据）。建议：`session.py` 改调 `runtime.workspace_paths(...).code`（runtime 不 import session，无循环依赖），`program-repos` 布局只留在 `session.program_repo_path:308-310` 一处。风险 中（拼错会让已有 store 的会话找不到工作树）
- SQLite 元数据读取写了四份：`util.py:232-261` 的 `database_digest`（列用户表 + 逐表 hash）、`sqlmap/build.py:41-71` 的 `read_database`（table+view + 行数 + `pragma_table_info`）、`workflow.py:167-177` 的 `table_names`（列用户表）、`workspace_facts.py:210-240` 的 `_default_tables`（列用户表 + 行数）。后两者还各自复制了 util 的底层：`workspace_facts.py:215` 手写 `?mode=ro` URI（对应 `util.readonly_database:148-160`），`workspace_facts.py:229` 手写标识符转义（对应 `util.quote_identifier:143-145`）。建议：`util` 加一个 `list_user_tables(path, include_views=False) -> list[str]`，`workflow`/`workspace_facts` 直接复用，`sqlmap` 在其上补列查询；`workspace_facts` 改用 `readonly_database` + `quote_identifier`。风险 中（三者语义有细微差异：sqlmap 把 view 也算表、workspace_facts 对 0 字节库返回空，合并时要逐条保留）
- `sha256_text` 两份同名实现：`util.py:129-131`（普通 UTF-8）与 `encoder/events.py:219-224`（先把 UTF-16 代理项按 Node 规则 replace，再 UTF-8）。`encoder/corpus.py:19` 从 events 导，`sqlmap/render.py:10` 从 util 导。两者语义不同，不能直接合成一份；建议把 encoder 那份改名（如 `sha256_text_js`）或挪进 `encoder/_json.py`，在 docstring 写明「JS 语义，与 util 的那份不同」。风险 中（哈希值被 `tests/test_encoder.py` 固定，改名安全、换实现不安全）
- `optimize.py` 的手写校验助手与 util 重复：`_as_text`（`optimize.py:75-83`）≈ `util.require_text`（`util.py:45-52`），`_as_mapping`（`optimize.py:100-105`）≈ `util.require_object`（`util.py:31-35`）。建议：换成 util 的两个函数；注意错误文案会变，`tests/test_harness_support.py:131` 断言了「应该是一个对象」，要同步改。风险 低
- 物化条目的序列化写了两份：`flow.MaterializeEntry.to_spec`（`flow.py:48-57`）按字段拼 dict，`flow.digest_of`（`flow.py:299-308`）又手拼一遍同样的 `file/table/sheet/columns/load`。建议：`digest_of` 用 `entry.to_spec()` 再对 columns 排序，同一份形状只维护一处。风险 低

## 过度抽象

- `src/vibe_excel/process.py:85-89` 的 `on_event` / `cancel` / `run_id` 形参 — 唯一生产调用 `runtime.py:373` 一个都没传；`ExecEvent`（`contracts.py:45-53`）只在这里被构造，tests 也没有覆盖。`run_process` 因此带着整套事件序列化（`process.py:133-143`）、取消检测（`process.py:152-168`）和 `cancelled` 结果字段，属于「为将来预留」。建议：删掉这三个形参与事件层，或补一个真正用到它们的调用方/测试；不要保持「代码在、没人走」的状态。风险 中（如果流式展示/随时停止是近期计划，删了要重写）
- `src/vibe_excel/runtime.py:227-232` 的 `with_lock_sync` — 只是 `with acquire_lock(...): return function()`，全仓唯一调用点是 `plugins/workspace.py:52`。建议：`WorkspaceService.with_lock` 直接用 `with acquire_lock(...)`，删掉这层转发。风险 低
- encoder 里两个单次包装：`encoder/_json.py:134-136` 的 `json_string` 只被 `encoder/events.py:201,211` 使用（实现就是 `_well_formed_json(json_text(...))`）；`encoder/events.py:227` 的 `_one_line(..., limit=PREVIEW_CHARS)`，全部 5 处调用只传 1 个参数，`limit` 从未被覆盖。建议：`limit` 形参删掉改常量；`json_string` 可以保留（名字说明了 JS 语义），但如果要删就内联到 events。风险 低

## 建议不动

- `engines/*.py` 自带的表名校验/路径校验/引号逻辑（`materialize.py:44,171-180,232-241`、`flow_parse.py:33,91-102,105-113`、`node_runner.py:99`）与包内 `flow.table_name:151`、`util.relative_path:163`、`util.quote_identifier:143` 重复 — 这些脚本由 `flow_code.bundled_script:40-45` 从包目录**当子进程直接执行**、经 stdin/stdout 交换 JSON，不能 import 包（否则会耦合到被执行的代码版本，破坏「干净重跑」语义）。建议保留重复，只加一行注释「故意自包含」。顺带一个可单独修的点：`node_runner.py:99` 用 `'"%s"' % table` 没转义双引号，表名带 `"` 会拼坏 SQL；在那个文件内补一个私有 quote 即可，不需要引包。
- 三套手写 CLI 解析（`cli.py:50-62 parse_argv`、`sqlmap/cli.py:20-40 _arguments`、`encoder/cli.py:66-97 _parse_args`）— 语义确实不同：全局 flag、固定 flag 子命令、可重复 flag。合并会把三种规则塞进一个函数，牺牲可读性（用户明确说过可读性 > 简洁性）。建议不动，最多在注释里互相指一下。
- `materialize.py` 的「两阶段读表」：plan 阶段只读表头（`plan_mapping_entry:284-301`），执行阶段再整表读（`read_sheet:384-396`）。看起来同一个 workbook 被打开两次，但这是「先校验全部条目、一条错都不写库」的原子性设计（`plan_all:495-538`）。建议不动。
- `session.py:84-99` 的 schema 版本检查与 `base_commit` 补列 — Python 版与 TS 版共用同一个 store 根，旧 `control.db` 是真实存在的（最近几个 book/chat 提交就在处理同一批 store）。这不属于「注释说兼容旧版但旧版已不存在」；删了会把老库当新库、丢失发布基线。建议不动。
- `session.py:288-291` 的 `SessionStore.open` 一行转发 — 它被 book、tests、`plugins/store.py:56` 十余处调用，是公开构造入口；删掉只增加改动面积、不增加可读性。同理，`encoder/trace.py` 里大量 `to_dict/from_dict` 逐字段展开虽然啰嗦，但字段与外部 JSON 契约一一对应，属于「可读性优先」的写法，建议不动。

## 如果只做三件事

1. **纯删除一批死代码**：除 sqlmap 兼容面之外的那 6 组（`flow_code` 包装、`workflow.Executor`、`archive_session`、`runtime` 三函数、`git.current_branch`＋kernel/contracts/host 的五个未引用声明、host 鸭子分支＋manifest 不可达 raise）。不改行为、不需要动测试，收益是去掉「读代码的人以为它有用」的成本。
2. **统一会话目录布局**：让 `session.py:346,370` 复用 `runtime.workspace_paths`，`program-repos` 只留一个出处。这是唯一一处「重复会导致真实事故」的地方（路径拼错 = 老会话的工作树找不到）。
3. **对「兼容面」做一次决定**：sqlmap 的 camelCase/BuildInput/`__init__.run_cli`（死代码第 1 条）和 `process.run_process` 的 on_event/cancel 层（过度抽象第 1 条）现在都是「文档/类型上像 API，仓内没人用」。要么补一条真正调用它们的测试（把承诺变成契约），要么删掉。不要继续留着两套等价入口。
