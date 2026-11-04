# ADR-0008：代码即流程——图从代码解析，不从声明读

- 状态：已采纳（**解析器未实现**，见「缺口」）
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

示例见 [prompts/author-flow.md](../../prompts/author-flow.md)，它同时是模型的说明书和回归用例
（`test/prompt-contract.test.ts` 会把那段 Python 抽出来丢给 `ast` 跑一遍）。

### 3. 边只来自签名，`main` 里不做变量类型传播

因为**节点之间传的是表，不是对象**：`raw_employees` 是物化出来的表，`employees` 是节点写的表，
函数拿到的只是承载它的 DataFrame。`main` 只负责顺序。

配套约定：**每个节点自己开连接、自己 `to_sql` 写自己的产出表**（写库才是它的事实，返回值只是
往下传的管道）。所以「这个节点写了什么」不需要推断，也不需要在 `main` 里追变量。

### 4. 逐节点执行，图不进版本库

- 执行仍然是 harness 按顺序逐个调用节点（不是整脚本跑一遍）：每节点有耗时、有读写行数、失败能定位到具体函数。
  这套在 ADR-0007 时代已经实现（`flow-run.ts` 的 `runFlowRound`），换成解析出来的节点列表即可复用。
- **图不进版本库**：脚本即事实，图是它的投影。派生出来的东西一旦提交，就必然和代码漂移。
  图只写进每次运行的 `run.json`，作为那次运行的证据。

### 5. `flow.json` 只剩两件事

`materialize`（文件→表的映射，没有代码可解析，只能是声明）和 `outputs`（这次要交付的表）。
节点和边都不再写。

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

1. **解析器没实现**。`src/flow.ts:211` 仍然要求 `flow.json` 里有非空 `nodes`。也就是说
   `prompts/author-flow.md` 描述的是**目标契约**：现在拿它跑 `author`，模型会写出没有 `nodes` 的
   `flow.json`，`flow` 命令会直接拒绝。**在解析器落地前不要用它跑真实 author。**
2. **运行时观测校验没实现**（见上）。
3. **C 规则的自动检查没实现**：`main` 里有控制流、标注缺失、表名是 f-string，现在都只在提示词里说了，
   代码层面没有拦截。
4. **没有用真实模型验证过**这一版契约。

## 后果

- 正面：流程不再写两遍，代码和流向同一个方向漂移；新增一步的成本降到「写一个函数 + 调一次」；
  工作区事实自动跟进环境变化，路径猜错这类低级失败从「排查半小时」变成「提示词里直接写着」。
- 负面：`main` 线性约束是为了让解析器简单可靠，代价是顶层不能有分支，必须借道函数。
- 迁移：程序仓现有的 `import_hr.py` / `analyze.py` 还没改成这个形状。
