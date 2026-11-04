# ADR-0008：代码即流程——图从代码解析，不从声明读

- 状态：已实现（`py/flow_parse.py` 解析 + `src/flow-code.ts` 翻译 + `node_runner.py` 输入注入）
- 日期：2026-11-04
- 修订：[ADR-0007](0007-flow-as-unit.md) §2（图的三种元素）与 §7（提示词是契约）

## 背景

ADR-0007 把工作流定成 `flow.json` + 手写 `nodes` 数组。它的残留问题是**同一件事写了两遍**：

```json
{ "id": "build_population", "call": "pipeline.py:build_population",
  "reads": ["employees", "available_positions"], "writes": ["population"] }
```

函数里的 `SELECT` / `to_sql` 已经说明了它读谁写谁。声明的 `reads` / `writes` 是抄一遍，
而且**抄错了没人拦**：加载器只校验形状（环、重复生产者、悬空读），不校验这张表是不是真的被读了。
于是出现一种很坏的失败——图和代码各说各话，图还是绿的。

更重要的是负担：模型要从零写出整张图，等于在写代码之前先猜一遍流程，然后让代码去对。
真实表有 85 个中文列，猜错的概率不低，而错一次要整轮重来。

## 决策

### 1. 脚本本来就是数据流，让代码说，不让模型抄

模型只写 Python。平台从 `main` 往下读：**一次调用 = 一个节点**，函数的参数/返回值标注 = 它的边。
不再有手写的 `nodes`。新增一步就是多写一个函数、在 `main` 里调一次——流会自己长出来。

「什么算一个节点」在实现时被钉成了三条（`py/flow_parse.py`）：

- **`main` 顶层语句里对「本文件模块级 `def`」的裸名调用**才算节点，按出现顺序；
- `Attribute` 调用（`pd.read_sql`、`conn.close`、`sqlite3.connect`）不算——few-shot 的 `main`
  里就有这些流水账，把它们报警只会训练人忽略警告；代价是「躲在下划线后面的跨文件节点」静态上
  发现不了，这条记在下面的已知缺口里；
- 裸名既不是本文件函数、也不是 builtin → **直接拒绝**：那是拼错函数名，或者跨文件调用，
  而跨文件会安静地少一个节点，正是这一节开头说的那种坏失败。

### 2. 代码契约（五条）

1. **`main` 是唯一入口，必须线性**：一行一次调用，不写 `if` / `for` / `try`，不做动态分发。
   需要分支就写进被调函数的内部——那里本来就是图之外的世界。
2. **签名上的 `"t:表名"` 表示这个参数或返回值承载哪张表**
   （`def clean(db, raw: "t:raw_employees") -> "t:employees"`）。
3. **每个函数体第一句是 docstring**，它就是流程图上那个节点的标题。
4. **函数内部的块也可以写 `"""…"""`**：`if` / `for` / `while` 的第一句是那个块的说明；
   裸语句前面写也可以、不写也行（写了当这条语句的说明）。
5. **表名必须是字面量**，`f"t:{x}"` 拒绝。

选择字符串字面量当做一个「规范块说明」，而不是 `#` 注释，理由是**解析器只需要一个**：

- Python 的 `ast` **不保留注释**（注释在词法阶段就丢了），读注释必须再用 `tokenize` 按行号猜归属，
  而「这行注释属于哪条语句」没有唯一答案；
- 块首字符串是 AST 的真实子节点（`Expr(Constant)`），归属确定。`if` / `for` / `while` 里写一句字符串
  完全合法、照常运行（实测：`ast.get_docstring()` 取不到它，取 `body[0].value.value` 即可）；
- 代价只有一条：linter 会报「无用字符串语句」（ruff B018 / pylint W0105），配置里关掉。

示例见 [prompts/author-flow.md](../../prompts/author-flow.md)，它同时是模型的说明书和回归用例：
`test/prompt-contract.test.ts` 会把那段 Python 抽出来，丢给**生产用的** `flow_parse.py` 跑一遍，
逐个核对解析出来的节点、边和标题。示例错了测试先红。

### 3. 边只来自签名，`main` 里不做变量类型传播

因为**节点之间传的是表，不是对象**：`raw_employees` 是物化出来的表，`employees` 是节点写的表，
函数拿到的只是承载它的 DataFrame。`main` 只负责顺序。

配套约定：**每个节点自己开连接、自己 `to_sql` 写自己的产出表**（写库才是它的事实，返回值只是
往下传的管道）。所以「这个节点写了什么」不需要推断，也不需要在 `main` 里追变量。

### 3b. 输入表由平台注入（实现时补的细节，原文只写了"函数拿到的只是 DataFrame"）

**读**和**写**是不对称的，这一点原稿没有写透，实现时定死：

- **读**：标了 `"t:表名"` 的参数，由 `node_runner.py` 从 `--db` 读出那张表、读成 DataFrame 递进去。
  节点里**不该**再 `read_sql` 自己声明过的表。这样「签名即依赖」是机制而不是注释——想要一张表，
  就得在签名上声明它，声明了就会拿到。
- **写**：仍然是节点自己的事（自己开连接、自己 `to_sql`）。`runFlowRound` 按声明的写表前后
  取行内容摘要，**声明的写表没变化就报警**（空转）。

推论：`main` 平台从不执行，只被解析。它的函数体只为「`python pipeline.py` 能单跑」存在，
里面的 `pd.read_sql`、`conn.close` 都是给单跑用的流水账，不参与图。
所以节点的返回值内容完全自由（返回 df 方便对照、返回计数也行），平台只认标注。

另一个实现细节：目标文件写了 `from __future__ import annotations` 时，`inspect.signature`
拿到的是**带引号的源码文本**（`"'t:raw'"`），`node_runner` 要剥一层引号——`ast` 解析不受影响，
受影响的是运行时。

### 4. 逐节点执行，图不进版本库

- 执行仍然是 harness 按顺序逐个调用节点（不是整脚本跑一遍）：每节点有耗时、有读写行数、失败能定位到具体函数。
  这套在 ADR-0007 时代已经实现（`flow-run.ts` 的 `runFlowRound`），**换成解析出来的节点列表后一行没改**——
  这就是当初「节点 = `file.py:func`」这条设计的回报。
- **图不进版本库**：脚本即事实，图是它的投影。派生出来的东西一旦提交，就必然和代码漂移。
  图只写进每次运行的 `run.json`（`graph: {source, entry, nodes, warnings}`），作为那次运行的证据。
- `flow.json` 里手写的 `nodes` 仍然支持（老路）。两条路在 `loadFlowAtCommit` 汇合，之后走同一套
  `validateFlow` / `planFlow`，不因为来路不同就宽严不一。

### 5. `flow.json` 只剩两件事（外加一个可选的 `entry`）

`materialize`（文件→表的映射，没有代码可解析，只能是声明）和 `outputs`（这次要交付的表）。
节点和边都不再写。

实现时加了一个可选字段 `entry`（默认 `pipeline.py`）：平台得知道从哪个文件开始读 `main`。
不写就是约定名，写了就覆盖——`flow.json` 引用它引用的 Python 文件，和提示词里的说法一致。
`entry` 不进摘要（它已经体现在每个节点的 `call` 里）。

### 6. 工作区事实是探测出来的，不是写死的

模型是一个在工作区里行动的 coding agent，它必须先知道库在哪、解释器在哪、venv 里装了什么。
这些写死在提示词里会**安静地过期**（换机器、目录搬家、venv 被 uv 重建），所以每次 author / optimize
之前探测一次（`src/workspace-facts.ts`）：python 版本、venv 是否 uv 建的（读 `pyvenv.cfg` 的 `uv =` 行）、
已装的包（只查 dist-info）、库大小与表行数、输入批次文件、输出目录、`flow.json` 是否存在。

两个出口，一份数据：

- 渲染成 `{{WORKSPACE}}` 填进提示词；
- 写成 `<session>/agent/AGENTS.md`，pi 会当 context file 自动读（实测 pi 的
  `loadProjectContextFiles({cwd, agentDir})` 确实返回它），所以模型在整个会话里随时能查。
  放 agent 目录而不是程序仓，是因为它在 git 之外，不会每次 author 都留一个 diff。

探测不到的项必须给**可执行的下一步**（`uv pip install xlrd` / `vibectl setup`），不能只写 missing。

## 备选方案的实测与否决

### 运行时观测（`sqlite3.Connection.set_authorizer`）

不解析代码，跑一遍，让 SQLite 自己说执行了什么。实测结果：

| 模型写的代码 | authorizer 观测到的 |
|---|---|
| `pd.DataFrame(...).to_sql("built", conn, if_exists="replace")` | `CREATE_TABLE built` + `INSERT built` |
| `pd.read_sql("select s.a,d.v from src s join dim d on s.a=d.k")` | `READ src.a`、`READ src.b`、`READ dim.v`、`READ dim.k` |
| `for i in range(3): cur.execute("update built set b=? ...")` | 3 条 `UPDATE built` |
| `cur.executescript("create table sc(x); insert into sc select a from src")` | `CREATE_TABLE sc`、`READ src.a`、`INSERT sc` |

**表级和列级血缘都能拿到，零解析、零约定**——连当初放弃的列血缘都免费。但没被采纳为主路径，因为：

1. **只看到跑过的分支**。代码里没执行的 `if` 分支，在观测里不存在，而评审要看的正是「它到底怎么设计的」；
2. 它要求每次都得跑，而图的价值之一是**写之前**就能看（模型先画、再跑）；
3. 归因依赖连接：模型自己 `sqlite3.connect(path)` 就漏了观测。

结论：静态解析是主路径，观测降级为**可选校验**（跑完后拿实际读写的表和签名标注对照，不一致就报警）。
这一层还没实现。

### 把文件索引进模型上下文（像 aider 的 repo-map）

查了 pi（`@earendil-works/pi-coding-agent@1.4.0`）整个包，含 3.5 MB 的 bundle：`repoMap|repo_map|repomap|tree-sitter|ts-morph`
**零命中**——它没有代码索引，靠 `read` / `glob` / `grep` 让模型懒惰探索。

pi 给的挂钩点足够自己实现：`context`（每次 LLM 调用前，可改消息）、`before_agent_start`（每次 run 开始，
`systemPromptOptions.sections` 可加段落）、`resources_discover`（贡献 skill / prompt 路径）、
`DefaultResourceLoaderOptions.agentsFilesOverride`（用代码提供 context file，不落盘）。

暂不做的理由：对单个 `.py` 文件喊一声「这是个文件」没有信息量，有用的是**节点表**（节点名 + 签名 + 一句话）。
而节点表的价值只在**多次调用 / 跨会话**时成立——现在的 `author` 是一次调用，模型刚写完的代码还在它自己
上下文里，再塞一张地图只是重复。等 author 拆成分步再谈。

## 缺口（写下来，免得被当成已经做了）

已经补上的：

- ~~解析器没实现~~ → `py/flow_parse.py`（36 条用例：`test/flow-code.test.ts`），
  提示词里的 few-shot 由**生产解析器**验证（`test/prompt-contract.test.ts`）。
- ~~C 规则的自动检查没实现~~ → 现在都在解析期拒绝，且报错带文件 + 行号，
  错误码 15 个（`MAIN_CONTROL_FLOW` / `NODE_PARAM_NOT_TABLE` / `NODE_TABLE_NAME_BAD` …）。

还没做的：

1. **运行时观测校验没实现**（见上）。静态解析只保证「代码写成这个形状」，不保证
   「节点真的只读写它声明的表」——那要靠 `set_authorizer` 跑完对账。
2. **没有用真实模型验证过**这一版契约（只验证了「人能写对的代码，解析器读得对」）。
3. **跨文件节点发现不了**：`import helpers; helpers.clean(db)` 不会被当成节点，
   也不会报错。契约要求节点在入口文件里，但这条只是**提示词里的约定**，解析器管不到。
   真要管，得把仓库文件清单也喂给解析器（现在只喂一个文件）。
4. **`main` 的参数传递不校验**：平台从不执行 `main`，所以 `build(db)` 少传一个参数
   不会被静态检查发现，只能靠模型自己 `python pipeline.py` 单跑。
5. **摘要不含 `title` / `steps`**（故意的，见 §后果）：改注释不会让摘要变，也不会
   让已批准的 `optimize` 建议失效。代价是「代码里改了 docstring」在摘要上不可见。

## 后果

- 正面：流程不再写两遍，代码和流向同一个方向漂移；新增一步的成本降到「写一个函数 + 调一次」；
  工作区事实自动跟进环境变化，路径猜错这类低级失败从「排查半小时」变成「提示词里直接写着」。
- 正面（等价性可验证）：同一条流「手写 `nodes`」和「从代码解析」**摘要必须相等**。这不是巧合而是
  判据——程序仓那条真实流程（`molex-prep`）两种写法都得到 `flow@0bfdfe7998dd`，为此还专门加了
  `test/flow-code.test.ts` 里的一条用例。
- 负面：`main` 线性约束是为了让解析器简单可靠，代价是顶层不能有分支，必须借道函数。
- 负面：输入表按 `SELECT *` 全列注入。真实花名册是 6 万行 × 85 列，`population` 节点其实只用 7 列，
  却要扛整个宽表（数百 MB 量级）。要收窄就在 `materialize.columns` 里投影，别在节点里再查一遍。
- 迁移：程序仓的 `analyze.py` / `import_hr.py` 仍是旧形状（不属于这条流）；`pipeline.py` 已按本 ADR
  重写，数字与手写那版逐表一致。
