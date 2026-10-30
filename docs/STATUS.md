# 项目状态报告

> 生成时间：2026-10-30 · 口径：只写**已跑过、有测试或产物可查**的事；没验证的一律归入「没做什么」或「缺口」。

## 一句话

**M0 完成**（pi 能跑通纵向闭环），**S1/S3 的基础设施已落地并有测试覆盖**。
**M1 仍未开始**：产品层面的纵向链路（探查 → 询问 → 确认 → 页面 → 发布 → 复用）一段都还没有。

---

## 一、做了什么

### 1. 工程基线

Git 仓库（`main`）、pnpm workspace（5 个 project）、pi 全家族钉 **1.4.0**、TypeScript 严格模式 + `erasableSyntaxOnly`（Node 直接执行 `.ts`，无构建步骤）。数据与制品一律不进 Git。

### 2. 数据运行时

uv 管理 **CPython 3.12.14**；pandas 2.3.3 / openpyxl 3.1.5 / SQLite 3.53.1，依赖锁在 [uv.lock](runtime-env/uv.lock)。[smoke.py](runtime-env/smoke.py) 6 项断言固化了一个真实陷阱：`pd.to_datetime` 对混合格式只从首值推断，会把合法的 `2026-03-05` 静默变成 `NaT`。

### 3. S1：Session 与版本（有测试）

[packages/session](packages/session/src/index.ts)，设计见 [ADR-0002](docs/adr/0002-control-plane-and-versions.md) / [ADR-0004](docs/adr/0004-git-draft-and-publish.md)。

- 控制面 SQLite（`<store>/control.db`，schema v2）存 Program / Session / Run / DataRevision / Artifact / Publish；
- 每个会话一个草稿分支 `draft/<id>` + 独立 `git worktree`；
- run 开始时固定代码版本，之后的提交不属于该 run；
- 终态不可回退；同内容数据修订只登记一次；
- **发布三重校验**：干净重跑通过 + 草稿领先 + main 基线未被非授权改写，然后 `merge --no-ff` 并留发布记录；
- `recoverSession()` 给出只读恢复视图（代码版本 + 数据版本 + 输入批次 + 制品）。

### 4. S3：Runtime 与数据边界（有测试）

[packages/runtime](packages/runtime/src/index.ts)，设计见 [ADR-0003](docs/adr/0003-serialization-snapshot-cleanrun.md)。

| 能力 | 做法 | 关键点 |
|---|---|---|
| 串行化 | 每会话独立 `work/` + 会话级排他锁（`O_EXCL`，带陈旧锁回收） | 跨会话并发天然安全，因为不共用数据库文件 |
| 快照 | `VACUUM INTO` + 内容寻址 | 不复制活跃库文件；WAL 里已提交的数据也会被包含 |
| 恢复 | 暂存 → `quick_check` → 清 `-wal`/`-shm` → 原子 rename | 坏快照拒绝恢复，绝不用坏文件覆盖现有数据 |
| 干净重跑 | `git worktree --detach` + 最小环境 | 全新目录、内容确定、不带模型凭证 |
| 输出比较 | 文件内容哈希 + `diffRunFiles` | 只给差异，业务等价由调用方判断 |

### 5. 测试

`pnpm test`：**23 项全绿**（session 11 / runtime 12）。覆盖跨进程恢复（用另一个进程直接读 `control.db` 验证落盘）、只读恢复视图、main 非授权改写被拒、WAL 快照自洽、陈旧锁回收、确定性重跑一致性。

### 6. M0 与文档

- M0 五项检查仍可重跑（`node spikes/m0-pi/src/main.ts --all`），追踪产物四件套齐全；
- ADR：[0001 接入形态](docs/adr/0001-pi-integration.md) · [0002 控制面与版本](docs/adr/0002-control-plane-and-versions.md) · [0003 串行化/快照/重跑](docs/adr/0003-serialization-snapshot-cleanrun.md) · [0004 草稿与发布](docs/adr/0004-git-draft-and-publish.md)。

---

## 二、没做什么

### 1. 目录对账

| 目录 | 状态 |
|---|---|
| `packages/contracts` | 有（仅类型） |
| `packages/session` | **有**（S1） |
| `packages/runtime` | **有**（S3 基础部分） |
| `packages/harness` | **无**——pi 接入仍只在 `spikes/m0-pi` |
| `packages/eval` | **无** |
| `apps/web` | **无** |
| `packages/growth` | 无（指南要求延后） |

### 2. 里程碑

- **M0：完成**（13 条缺口见 ADR-0001 §5）
- **S1 / S3 基础：完成并有测试**
- **M1：未开始**
- M2 / M3：未开始

### 3. 仍然缺的能力

1. **没有 Harness**：会话创建/恢复、事件收集、运行协调都还没有常驻层；`spikes/m0-pi` 是验证脚本，不是产品代码。
2. **没有用户确认流程**：语义确认、待确认清单、决定如何影响下次运行——一个都没有。
3. **没有界面**：外壳、业务画布、待确认清单、下载入口全无。
4. **没有评测**：零合成任务、零独立 verifier、零 baseline。
5. **没有工作区写入约束**（已决定推迟；调研结论见 ADR-0001 缺口 13）。
6. **没有接触真实 HR 数据**（符合约束，但也意味着真实文件的脏法未被验证）。
7. **发布前没有钩子**：`cleanRunPassed` 由调用方提供，只防误用、不防谎报（ADR-0004 缺口）。
8. **快照是整库全量**：大库会阻塞写入，尚无增量或轮转策略（ADR-0003 限制）。
9. **取消只验证了直接子进程**，后代进程回收未验证。

---

## 三、已经拍板的决定

| 决定 | 结论 | 记录在 |
|---|---|---|
| 接入形态 | RPC 子进程 + 包内 `RpcClient` | ADR-0001 |
| 模型路线 | `opencode-go` / `deepseek-v4.1-flash`（长期） | ADR-0001 |
| 控制面存储 | 独立 SQLite + 内容寻址数据修订 | ADR-0002 |
| 串行化 | 目录隔离为主，会话锁为显式约束 | ADR-0003 |
| 快照 | `VACUUM INTO`，不复制活跃库 | ADR-0003 |
| 草稿与发布 | 一会话一 worktree，发布三重校验 | ADR-0004 |
| 工作区写入约束 | 推迟（Windows 无可用的 OS 级沙箱） | ADR-0001 缺口 13 |

---

## 四、最该先补的三件事

1. **S2 Harness**：把 `spikes/m0-pi` 里验证过的能力搬成常驻层——创建/恢复 pi 会话、收事件、把「已取消」这类事实自己记下来（因为取消时 pi 不产生任何事件），并在这里调用 session/runtime 的 API。
2. **S4 最小任务族**：M1 的输入是合成 Excel，属于 S4。先交 3–5 个够用的任务，含至少一个语义不确定案例。
3. **用户确认流程**：这是产品与「一个会写脚本的 agent」的分界线。同一输入四次运行两种判定已经证明：语义判断必须落到用户确认与标注上，而不是让模型每次即兴决定。
