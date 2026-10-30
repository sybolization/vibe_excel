# 项目状态报告

> 生成时间：2026-10-30（晚于 S1/S3 交付，含真实数据第一次跑通）· 口径：只写**已跑过、有测试或产物可查**的事；没验证的一律归入「没做什么」或「缺口」。

## 一句话

**M0 完成**（pi 能跑通纵向闭环），**S1/S3 的基础设施已落地并有测试覆盖**，
**真实 HR 数据第一次端到端跑通**（5 个文件 75.9 MiB → 117,776 行入库 → 两个程序 → 独立核对 23/23 → 合入 main）。
**M1 仍未开始**：产品层面的纵向链路（探查 → 询问 → 确认 → 页面 → 发布 → 复用）一段都还没有。

---

## 一、做了什么

### 1. 工程基线

Git 仓库（`main`）、pnpm workspace（5 个 project）、pi 全家族钉 **1.4.0**、TypeScript 严格模式 + `erasableSyntaxOnly`（Node 直接执行 `.ts`，无构建步骤）。数据与制品一律不进 Git。

### 2. 数据运行时

uv 管理 **CPython 3.12.14**；pandas 2.3.3 / openpyxl 3.1.5 / SQLite 3.53.1，依赖锁在 [uv.lock](../runtime-env/uv.lock)。[smoke.py](../runtime-env/smoke.py) 6 项断言固化了一个真实陷阱：`pd.to_datetime` 对混合格式只从首值推断，会把合法的 `2026-03-05` 静默变成 `NaT`。

### 3. S1：Session 与版本（有测试）

[packages/session](../packages/session/src/index.ts)，设计见 [ADR-0002](adr/0002-control-plane-and-versions.md) / [ADR-0004](adr/0004-git-draft-and-publish.md)。

- 控制面 SQLite（`<store>/control.db`，schema v2）存 Program / Session / Run / DataRevision / Artifact / Publish；
- 每个会话一个草稿分支 `draft/<id>` + 独立 `git worktree`；
- run 开始时固定代码版本，之后的提交不属于该 run；
- 终态不可回退；同内容数据修订只登记一次；
- **发布三重校验**：干净重跑通过 + 草稿领先 + main 基线未被非授权改写，然后 `merge --no-ff` 并留发布记录；
- `recoverSession()` 给出只读恢复视图（代码版本 + 数据版本 + 输入批次 + 制品）。

### 4. S3：Runtime 与数据边界（有测试）

[packages/runtime](../packages/runtime/src/index.ts)，设计见 [ADR-0003](adr/0003-serialization-snapshot-cleanrun.md)。

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
- ADR：[0001 接入形态](adr/0001-pi-integration.md) · [0002 控制面与版本](adr/0002-control-plane-and-versions.md) · [0003 串行化/快照/重跑](adr/0003-serialization-snapshot-cleanrun.md) · [0004 草稿与发布](adr/0004-git-draft-and-publish.md) · [0005 执行单元](adr/0005-execution-unit.md)。

### 7. 真实数据第一次跑通（2026-10-30）

用 `ground_true_excel/wenxin` 的真实 HR 导出跑了一遍完整生命周期，脚手架在 [spikes/demo-real-data](../spikes/demo-real-data/src/main.ts)，
结果见 [RESULTS.md](../spikes/demo-real-data/RESULTS.md)，设计见 [ADR-0005](adr/0005-execution-unit.md)。

| 事实 | 数字 |
|---|---|
| 输入 | 5 个文件 / 75.9 MiB，内容寻址为批次 `734c98ef4d92` |
| 模型成本 | 2 轮 author、54 轮、61 次工具调用、1,626,450 tokens、**$0.056** |
| 录入 | 117,776 行入 6 张表，quarantine **0**，`hr.db` 74 MB |
| 从代码复现 | 模型自跑的库 vs 提交后干净 worktree 跑的库，7 张表内容哈希**相同** |
| 确定性 | `import` / `analyze` 各连跑两次，内容哈希一致 |
| 独立核对 | **23/23**（第二实现从原始 Excel 重算） |
| 有标签的部分 | PN 看板 18 行：**17 行对上**，1 行是缺输入（该组合在两份源表里 0 行） |
| 快照恢复 | 删掉一半 employees（60,248 → 30,124）→ 恢复回 60,248，内容哈希不变 |
| 发布 | `merge --no-ff` → main `77750ae8`，发布记录带证据路径 |

这一轮同时暴露了产品侧最硬的缺口：模型自己提出了 **7 个待确认项**（口径未覆盖的取值、`remaining` 是否净额等），
但它们**只存在于一份 JSON 里，没有任何人能点确认**。

---

## 二、没做什么

### 1. 目录对账

| 目录 | 状态 |
|---|---|
| `packages/contracts` | 有（仅类型） |
| `packages/session` | **有**（S1） |
| `packages/runtime` | **有**（S3 基础部分） |
| `packages/harness` | **无**——pi 接入只有 `spikes/m0-pi`（验证）与 `spikes/demo-real-data`（演示脚手架） |
| `packages/eval` | **无** |
| `apps/web` | **无** |
| `packages/growth` | 无（指南要求延后） |

### 2. 里程碑

- **M0：完成**（13 条缺口见 ADR-0001 §5）
- **S1 / S3 基础：完成并有测试**
- **M1：未开始**
- M2 / M3：未开始

### 3. 仍然缺的能力

1. **没有 Harness**：会话创建/恢复、事件收集、运行协调都还没有常驻层；`spikes/m0-pi` 与 `spikes/demo-real-data` 都是脚手架，不是产品代码。
2. **没有用户确认流程**：真实数据这一轮里模型自己提出了 7 个待确认项，但没有地方能接住它们——语义确认、待确认清单、决定如何影响下次运行，一个都没有。
3. **没有界面**：外壳、业务画布、待确认清单、下载入口全无。
4. **没有评测**：零合成任务、零独立 verifier、零 baseline（真实数据那次用的 `verify_outputs.py` 是一次性核对脚本，不是任务族）。
5. **没有工作区写入约束**（已决定推迟；调研结论见 ADR-0001 缺口 13）。
6. **业务正确性未验证**：真实数据跑通了，但除了 PN 看板 18 行（17 行可推导的都对上）之外没有任何标签；
   用户流程第 2 步的 supervisor → Position 映射更是**结构上不可直接 join**（工号-中文名 vs 英文名），没解。
7. **发布前没有钩子**：`cleanRunPassed` 由调用方提供，只防误用、不防谎报（ADR-0004 缺口）。
8. **快照是整库全量**：74 MB 的库一次快照要整份复制，尚无增量或轮转策略（ADR-0003 限制）。
9. **取消只验证了直接子进程**，后代进程回收未验证；author 阶段模型自己的试跑完全不在 run 记录里（ADR-0005 缺口 1）。

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
| 执行单元 | 入口契约由编排器定；输入批次内容寻址；run ≡ 干净执行 | ADR-0005 |
| 工作区写入约束 | 推迟（Windows 无可用的 OS 级沙箱） | ADR-0001 缺口 13 |

---

## 四、最该先补的三件事

1. **S2 Harness**：把两个 spike 里验证过的能力搬成常驻层——创建/恢复 pi 会话、收事件、按入口契约执行、
   把「已取消」这类事实自己记下来（取消时 pi 不产生任何事件）。照 ADR-0005 的六条决策写，别重新发明。
   顺带修掉 ADR-0005 缺口 2：author 一次 7 分钟，必须自己推进度，不能靠 stdout。
2. **用户确认流程**：这是产品与「一个会写脚本的 agent」的分界线。真实数据那一轮模型自己提了 7 个待确认项
   （口径未覆盖的取值、`remaining` 是否净额、PayRate 覆盖还是保留），**没有地方接住**。
   PN 看板那种「18 行真人答案」已经证明：有标签才能区分"跑通了"和"算对了"。
3. **S4 最小任务族**：M1 的输入是合成 Excel，属于 S4。先交 3–5 个够用的任务，含至少一个语义不确定案例，
   并把真实数据这次用的「第二实现核对」固化成 verifier。
