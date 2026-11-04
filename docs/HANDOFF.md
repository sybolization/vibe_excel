# 交接（2026-11-04）

> 给"重启之后新开的那个会话"用。读完这一页就能接上，**不需要读历史会话**。
> 有疑问时以 `docs/` 里的文件和测试为准，这份文档只做索引。

## 一句话现状

控制面、运行时、插件化 harness 都落地并有测试（**13 个测试文件 / 143 项全绿**）；
工作流按 [ADR-0007](adr/0007-flow-as-unit.md) 管版本（`flow.json` + 双跑 + 证据发布）；
[ADR-0008](adr/0008-code-as-flow.md) 定了"代码即流程"的契约，**但解析器还没写**。

## ⚠️ 先看这条

`prompts/author-flow.md` 已经改成**目标契约**（不写 `nodes`，图从代码解析）。而 `src/flow.ts` 仍然要求
`flow.json` 里有非空 `nodes`。所以：

**在解析器（ADR-0008 缺口 1）落地之前，不要拿这份提示词跑真实 `author`**——模型会写出没有 `nodes` 的
`flow.json`，`flow` / `run` 会直接拒绝，白烧一轮模型调用。

要么先实现解析器，要么先用 `git show <此提交>:prompts/author-flow.md` 取回旧版提示词。

## 必读的四份文件

1. [docs/ARCHITECTURE.md](ARCHITECTURE.md) —— 现在是什么样（三层、服务图、版本模型、一次 run 的时序、落盘布局）
2. [docs/STATUS.md](STATUS.md) —— 2026-10-30 的快照（**历史**，数字已过时，读它的结论不读它的计数）
3. [ADR-0007](adr/0007-flow-as-unit.md) —— 工作流是版本单位：三种元素、双跑、证据发布、两段式 optimize、ask_user
4. [ADR-0008](adr/0008-code-as-flow.md) —— 代码即流程：五条代码契约、为什么不用运行时观测当主路径、缺口清单

## 五个必须记住的坑（都是踩过的）

1. **cordis 的 `await fiber.await()` 不代表插件起来了。** 依赖未满足时它照样 resolve，必须读 `fiber.state`
   （0 PENDING / 1 LOADING / 2 ACTIVE / 3 FAILED）并等"没有 LOADING"才算稳定。`host.ts` 的 `settle()` 就是为这个写的。
2. **`RpcClient` 的 env 是合并不是替换**（`{...process.env, ...options.env}`），真白名单只能临时换 `process.env`，
   所以它不能被并发调用。凭证只在 `start()` 期间存在于 `process.env` 上。
3. **`RunId` 是 `randomUUID()`，目录名里没有时间。** 任何"取最后一次 run"都不能按名字排序——集成测试里踩过，
   表现为「exit 1，但读到的 run.json 是上一条成功的」。按 `run.json` 的 mtime 取。
4. **pi 的 `RpcClient` 没有暴露回话口**，`ask_user` 的回答是直接写子进程 stdin 的（协议是公开的）。
   等不到回答按 `cancelled` 回，模型收到「未确认」，**不是**「用户同意」。
5. **提示词里的 few-shot 会被模型照抄。** `test/prompt-contract.test.ts` 会把那段 Python 抽出来丢给 `ast`
   跑一遍（`main` 不许有控制流、节点的表标注必须齐全），示例错了测试先红。

## 现在最该做的三件事（按价值排序）

1. **写解析器**（ADR-0008 缺口 1）：`packages/harness/py/flow_parse.py`，用 Python 的 `ast` 从 `main` 读出节点、
   从 `"t:表名"` 标注读出边、从 docstring 读出标签，输出与现有 `FlowSpec` 同构的 JSON。`flow-run.ts` 不用改——
   它本来就是按节点列表逐个执行。做完之后从 `flow.ts` 删掉 `nodes` 必填校验。
2. **运行时观测校验**（ADR-0008 缺口 2）：`sqlite3.Connection.set_authorizer` 实测能给出表级 + 列级读写
   （`READ` 带列名），跑完一轮拿它和签名标注对照，不一致就报警。**只做校验，不做主路径**。
3. **产品链路**：确认流程（`confirmations` 服务 + 插件）。这是「一个会写脚本的 agent」和产品的分界线。

## 命令速查

```powershell
pnpm typecheck ; pnpm test

node packages/harness/src/main.ts plugins     # 7 个插件与加载状态
node packages/harness/src/main.ts status      # 恢复视图（代码/数据/批次/制品）
node packages/harness/src/main.ts flow         # 从提交里读草稿的工作流（物化 / 节点 / 边 / 优化提示）
node packages/harness/src/main.ts run          # 双跑：物化 → 节点 → 比声明的产出
node packages/harness/src/main.ts publish      # 只发布带「两次一致」证据的运行
node packages/harness/src/main.ts optimize     # 只读评审，产出建议 JSON（人批准后 --apply=<id>）
```

凭证：`OPENCODE_API_KEY`（从 `~/.dsh-cline/.credentials.yaml` 的 `OPENCODE_GO_API_KEY` 取，**不要打印**）。
只有 `author` / `optimize` 需要它；`run` / `publish` 不带任何模型凭证。

## 制品根里现在有什么（别误删）

`C:\project\vibe_excel-store`
- `control.db`：1 个 program（molex-prep）、1 个 session（`23e16fce-…`）、若干 run 与数据版本
- `data-revisions/`：内容寻址的快照（空库基线 12 KB、import 产出 70 MB 级）
- `program-repos/`：程序仓库（main + `draft/<sid>`）
- `sessions/23e16fce-…/`：`code/` 草稿 worktree（**还没有 `flow.json`**，仍是 `import_hr.py` + `analyze.py` 的旧形状）、
  `work/hr.db`（70.8 MB / 8 张表 / employees 60,248 行）、`work/out/`、`runs/` 全部轨迹、
  `agent/AGENTS.md`（每次 author/optimize 前由 `workspace-facts.ts` 重写）
