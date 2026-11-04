# 当前架构（加了 cordis 之后）

> 生成于 2026-10-30，对应提交 `313168c`。这份文档描述**现在是什么样**，不描述应该是什么样。
> 决策的"为什么"在 [ADR](adr/) 里，本文只讲"是什么、在哪、怎么连起来"。

## 1. 全景

三块东西，边界比类名重要：

```
┌─ 代码仓库 C:\project\vibe_excel ────────────────────────────────┐
│  packages/contracts   packages/session   packages/runtime        │
│  packages/harness（宿主 + 7 个插件）   vibe.plugins.yml（清单）   │
│  spikes/*（验证与演示，不是产品代码）   docs/*  runtime-env/*      │
└──────────────────────────────────────────────────────────────────┘
                    │ 只在运行时通过路径约定引用
                    ▼
┌─ 制品根 C:\project\vibe_excel-store（不进 Git）──────────────────┐
│  control.db            控制面：程序/会话/运行/数据版本/制品/发布   │
│  program-repos/<id>    程序的 git 仓库（长期资产）                 │
│  sessions/<id>/{work,code,agent,sessions,runs}                    │
│  data-revisions/<sha>.db   业务库的一致性快照（内容寻址）          │
└──────────────────────────────────────────────────────────────────┘
```

代码仓库和制品根**没有任何硬链接**：代码里只出现路径拼装（`store.storeRoot()`），所以换机器、换目录都不改代码。

## 2. 三层代码，依赖方向单一

```
清单 (vibe.plugins.yml)
   │ 组装
   ▼
插件层  packages/harness/src/plugins/*.ts   ← 唯一认识 cordis 的地方
   │ 适配（把库包成服务）
   ▼
库层    packages/{contracts,session,runtime}  ← 没有一行 cordis
```

- **库层**：`contracts` 只有类型；`session` 是控制面 + git 草稿/发布；`runtime` 是工作区、锁、快照、干净重跑。
  三者可以被 vitest 直接调用，不需要起宿主。
- **插件层**：一共 7 个源文件管宿主（`host.ts` / `manifest.ts` / `cli.ts` / `kernel.ts` / `main.ts` / `fs-util.ts` / `index.ts`）
  加 7 个插件文件（`store` / `workspace` / `exec` / `agent-pi` / `telemetry` / `sessions` / `commands`）。
  插件只做适配与编排，不实现存储或执行。
- **清单**：一行一个插件，谁依赖谁写在 `inject` / `provide` 里。

跨包 import 实测只有四条边：`session→contracts`、`runtime→contracts`、`harness→{contracts,session,runtime}`。没有环。

## 3. 服务图

宿主动词只有三个：`provide` / `inject` / `apply`。启动顺序**由服务可用性决定**，不由清单行序决定。

```
        ┌──────────┐   ┌───────────┐   ┌───────┐   ┌──────┐   ┌──────────┐
        │ commands │   │ telemetry │   │ store │   │ exec │   │ agent-pi │
        └────┬─────┘   └─────┬─────┘   └───┬───┘   └──┬───┘   └────┬─────┘
             │               │             │          │            │
             │               │        ┌────▼──────┐   │            │
             │               │        │ workspace │   │            │
             │               │        └────┬──────┘   │            │
             └───────────────┴─────────────┴──────────┴────────────┘
                                             │
                                       ┌─────▼──────┐
                                       │  sessions  │  ← 编排 + 注册 7 条命令
                                       └─────┬──────┘
                                             │ 按 commit 读 flow.json
                                       ┌─────▼──────┐   ┌──────────┐
                                       │  flow-run  │──▶│ 节点函数  │
                                       └────────────┘   └──────────┘
```

| 服务 | 提供者 | 依赖 | 一句话 |
|---|---|---|---|
| `commands` | commands | — | 子命令注册表；命令也来自插件 |
| `telemetry` | telemetry | — | `events.jsonl` + `timeline.txt` |
| `store` | store | — | `control.db`，懒打开 |
| `workspace` | workspace | store | 目录布局 + 会话排他锁 |
| `exec` | exec | — | 干净 worktree 里执行入口契约 |
| `agent` | agent-pi | — | pi 子进程（凭证只在这里进子进程） |
| `sessions` | sessions | 上列全部 | 生命周期编排 |

业务流程**不再是一个插件**：它是程序仓库里的代码本身——`flow.json` 只声明「哪个 Excel 变成哪张表」
和「这次要交付哪几张表」，节点与边从 Python 的 `main` 和签名标注里解析出来，跟代码一起提交、一起发布。
见 [ADR-0007](adr/0007-flow-as-unit.md) 与 [ADR-0008](adr/0008-code-as-flow.md)（**解析器待实现**，在此之前
`flow.json` 仍需手写 `nodes`）。

## 4. 版本模型：五件事，各管各的

恢复一个节点需要把它们凑齐，缺一个就不算可恢复：

| 东西 | 存在哪 | 谁写的 | 用来回答 |
|---|---|---|---|
| **代码版本** `code_commit` | `runs.code_commit` + git | `beginRun` 时取 `draftHead` | 这次跑的是哪版程序 |
| **工作流版本** `flow@<12>` | 程序仓库里的 `flow.json`，摘要写进 `run.json` | 模型写、跟代码一起提交 | 这次跑的是哪条流（物化 + 产出表） |
| **数据版本** `data_revision_id` | `data_revisions` + `data-revisions/<sha>.db` | 两轮一致后才自动快照 | 这次读的是哪版业务库 |
| **输入批次** `input_batch_id` | `sessions/<id>/work/inputs/<batchId>/` | `setup` 内容寻址复制 | 这批原始文件是哪一份 |
| **制品** | `artifacts` 表 | run 结束按声明产出的哈希 | 输出了什么、多大、什么哈希 |
| **发布** | `publishes` 表 | `publish` 读 `run.json` 的两轮证据 | 谁在什么时候、凭哪份证据合进 main |

关键点：`data_revision_id` 记的是**输入**版本，不是产出。产出走 `artifacts`；一次成功的运行会再产生一个新的数据版本给下一次用。

## 5. 一次 `run` 的时序

```
CLI 进程                     cordis 宿主               子进程 / 磁盘
─────────────────────────────────────────────────────────────────────────────
run
  └─ commands 找到命令
      └─ sessions.runFlow
           ├─ workspace.withLock(会话锁)          ← 同一会话串行化的唯一入口
           ├─ git show <commit>:flow.json          ← 读的是提交，不是工作区
           ├─ planFlow(flow)                       ← 拓扑序 / 环 / 悬空读 校验
           ├─ store.beginRun(...)                  → runs 一行（status=running）
           ├─ 第 a 轮：从同一数据快照复制一份库
           │     ├─ materialize.py --spec …        ← 文件 → 表（只做映射）
           │     └─ 每个节点：node_runner.py file func --db …
           │           └─ git worktree --detach <commit> → 全新目录
           ├─ 第 b 轮：同样的输入、同样的业务日，重跑一次
           ├─ diffRounds(a, b)                     ← 比声明的产出（表 + 文件）
           ├─ 一致 → 提升第 a 轮产物，VACUUM INTO → 新数据版本
           │  不一致 → 运行失败，不提升、不登记
           ├─ store.finishRun(...)                 → 终态不可回退
           └─ 写 run.json（flow@摘要 + 两轮记录 + identical）
```

`author` 是另一条路：`sessions.author` → `agent.run`（起 pi 子进程、白名单环境、事件流落盘）→ `commitDraft`。
**写程序**和**跑程序**是分开的两步：模型写的时候在草稿 worktree 里调试，跑的时候一定在 detached worktree 里、一定对应某个 commit、一定不带模型凭证。

写程序之前先探测一次工作区（`workspace-facts.ts`）：库在哪、多大、有几张表，python 在哪、哪个版本、venv 是不是 uv 建的、装了哪些包，输入批次里有什么文件。
探测结果有**两个出口**：填进提示词的 `{{WORKSPACE}}`，以及写成 `<session>/agent/AGENTS.md`——pi 会把它当 context file 自动读，所以模型在整个会话里随时能查，而不只是提示词开头那一段。
**为什么必须探测而不是写死**：路径会变（换机器、目录搬家、venv 被 uv 重建），写死的句子在路径失效时会安静地变成谎言。探测不到的项一律落到「缺的东西」，给可执行的下一步（`uv pip install xlrd` / `vibectl setup`），而不是一句 missing。

## 6. 落盘布局

```
<store>/
├── control.db                     7 张表：programs / sessions / runs / data_revisions
│                                  artifacts / publishes / meta（+ user_version 迁移用）
├── program-repos/<programId>/     main + draft/<sid> 分支
├── data-revisions/<sha256>.db     内容寻址，同内容只存一份
├── m0/                            M0 验证脚本的产物（历史遗留）
└── sessions/<sessionId>/
    ├── code/                      草稿分支的 git worktree（模型在这里写程序）
    ├── work/
    │   ├── inputs/<batchId>/      内容寻址的输入副本 + manifest.json
    │   ├── hr.db                  业务库（WAL）
    │   └── out/<stage>/           阶段产出
    ├── agent/  sessions/          每会话独立的 pi 配置与会话文件
    │   └── AGENTS.md              每次 author/optimize 前重写的工作区事实（pi 自动读）
    └── runs/<runId>/
        ├── run.json               这次运行的全部事实（commit/版本/退出码/哈希）
        ├── clean/                 本次执行的 detached worktree 产物与 manifest
        ├── stdout.log stderr.log
        └── events.jsonl timeline.txt author.json *.html    （author 轨迹）
```

## 7. 边界

| 边界 | 事实 |
|---|---|
| 代码 vs 数据 | 数据、库、产出、轨迹一律在制品根，`.gitignore` 挡掉 `*.db` / `*.xlsx` / `ground_true_excel/` |
| 进程 | CLI（node）→ pi 子进程（node）→ python 子进程；`exec` 的执行发生在 python 子进程里，cwd 是 detached worktree |
| 环境 | pi 子进程只拿到白名单变量；干净执行只拿操作系统基础变量**和 `VIBE_EXCEL_AS_OF`**，模型凭证不进 |
| 锁 | 会话级排他锁（`O_EXCL` + 陈旧回收），只管同一台机器、同一个会话 |
| 仓库 vs 框架 | `session` / `runtime` 不认识 cordis；`harness` 是唯一宿主相关知识 |

## 8. 换实现时改哪里（这就是插件化的收益）

| 想换 | 换什么 | 上游改动 |
|---|---|---|
| 执行器（本地 → 容器/远程） | `exec` 插件 | 0 |
| agent（pi → ACP/其它） | `agent-pi` 插件 | 0 |
| 控制面（SQLite → Postgres） | `store` 插件 | 0 |
| 轨迹出口（文件 → OTel） | `telemetry` 插件 | 0 |
| 入口（CLI → HTTP） | `commands` 插件 + 一个 HTTP 宿主 | 0 |
| **业务流程**（molex → 周报） | 程序仓库里的代码 + `flow.json` | 0 |
| 宿主框架本身 | `host.ts` + `kernel.ts` 的 `PluginContext` | 插件写法不变（ADR-0006 决策 7） |

## 9. 不存在的（别以为有）

1. 没有 `confirmations` 服务：模型提的待确认项只落在一份 JSON 里；能实时问答的只有 `ask_user`
   （模型问、人答、答案写回并落进 `questions.jsonl`），没有「点确认」的界面。
2. 没有 `verifiers` 服务：双跑只证明"同一 commit 两次结果一致"，业务正确性仍靠外部脚本核对。
3. 没有「全库双跑比对」：现在只比声明的产出（`outputs` 表 + out 文件），因为 `meta.created_at` 这类非确定内容还没被禁掉。
4. 没有「自动优化」：`optimize` 只会**建议**（模型在只读 worktree 里评审），`--apply=<id>` 只实现被批准的那一条；
   机器自己不会改流程。
5. 没有 Web / client 半边：`apps/web` 空着，插件模型还没往 UI 延伸。
6. 没有评测：`packages/eval` 空着，零任务、零 baseline。
7. 没有热重载、没有分发包、没有插件级权限（ADR-0006 §8）。
6. 没有多会话并发的实测；锁不跨机器。
7. 没有工作区写入约束（Windows 上没有可用的 OS 级沙箱，ADR-0001 缺口 13）。
