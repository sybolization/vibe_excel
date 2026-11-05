# 项目状态报告

> 生成时间：2026-10-30（晚于 S1/S3 交付，含真实数据第一次跑通）· 口径：只写**已跑过、有测试或产物可查**的事；没验证的一律归入「没做什么」或「缺口」。

## 一句话

**M0 完成**（pi 能跑通纵向闭环），**S1/S3 的基础设施已落地并有测试覆盖**，
**真实 HR 数据第一次端到端跑通**（5 个文件 75.9 MiB → 117,776 行入库 → 两个程序 → 独立核对 23/23 → 合入 main），
**S2 harness 已插件化落地**（cordis 宿主 + 9 个插件 + 29 项测试）。
**M1 仍未开始**：产品层面的纵向链路（探查 → 询问 → 确认 → 页面 → 发布 → 复用）一段都还没有。

---

> **本文是 2026-10-30 的状态快照，其中的计数与插件清单已过时**（当时 9 个插件、29 项测试、还有 `stages`）。
> 当前结构看 [ARCHITECTURE.md](ARCHITECTURE.md)；当前数据看 `node packages/harness/src/main.ts status`；
> 决策依据看 [ADR-0007](adr/0007-flow-as-unit.md) 与 [ADR-0008](adr/0008-code-as-flow.md)。
> 下面保留原文，是因为「为什么当时这么想」本身是记录。

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

### 8. S2：插件化 harness（有测试）

[packages/harness](../packages/harness/src/index.ts) + [vibe.plugins.yml](../vibe.plugins.yml)，设计见 [ADR-0006](adr/0006-plugin-architecture.md)。

宿主是 **cordis 4.0.5**（与 DSH 同一个框架、同一套插件契约），不是自制的类似物：

| 插件 | 提供 | 依赖 | 职责 |
|---|---|---|---|
| `commands` | commands | — | CLI 子命令注册表（命令由插件注册，加命令不改入口） |
| `telemetry` | telemetry | — | `events.jsonl` + `timeline.txt` |
| `store` | store | — | 控制面库（懒打开） |
| `workspace` | workspace | store | 每会话目录 + 排他锁 |
| `exec` | exec | — | 干净 worktree 里的入口契约执行 |
| `agent-pi` | agent | — | pi 子进程（凭证只在这里进子进程） |
| `sessions` | sessions | 上列全部 | 生命周期编排 + 7 条命令 |

业务流**不再是插件**：`flow.json` 跟代码一起版本化（ADR-0007），原来的 `stages` / `stage-molex` 两个插件已删除。

验证：7 个插件全部 ACTIVE；`run` 默认双跑，跑的是「物化 → 节点」整条流；
`publish` 只认 `run.json` 里的两次一致证据。测试 106 项。

---

## 二、没做什么

### 1. 目录对账

| 目录 | 状态 |
|---|---|
| `packages/contracts` | 有（仅类型） |
| `packages/session` | **有**（S1） |
| `packages/runtime` | **有**（S3 基础部分） |
| `packages/harness` | **有**（S2，插件化宿主 + 9 个插件） |
| `packages/eval` | **无** |
| `apps/web` | **无** |
| `packages/growth` | 无（指南要求延后） |

### 2. 里程碑

- **M0：完成**（13 条缺口见 ADR-0001 §5）
- **S1 / S3 基础：完成并有测试**
- **M1：未开始**
- M2 / M3：未开始

### 3. 仍然缺的能力

1. **Harness 还差三件事**：没有热重载（改插件要重启）、没有 client 半边（Web 还没开始）、没有分发包（一份清单，无 bundle/profile/版本化）。见 ADR-0006 缺口。
2. **没有用户确认流程**：真实数据这一轮里模型自己提出了 7 个待确认项，但没有地方能接住它们——语义确认、待确认清单、决定如何影响下次运行，一个都没有。
3. **没有界面**：外壳、业务画布、待确认清单、下载入口全无。
4. **没有评测**：零合成任务、零独立 verifier、零 baseline（真实数据那次用的 `verify_outputs.py` 是一次性核对脚本，不是任务族）。
5. **没有工作区写入约束**（已决定推迟；调研结论见 ADR-0001 缺口 13）。
6. **业务正确性未验证**：真实数据跑通了，但除了 PN 看板 18 行（17 行可推导的都对上）之外没有任何标签；
   用户流程第 2 步的 supervisor → Position 映射更是**结构上不可直接 join**（工号-中文名 vs 英文名），没解。
7. **发布前没有钩子**：`cleanRunPassed` 由调用方提供，只防误用、不防谎报（ADR-0004 缺口）。
8. **快照是整库全量**：74 MB 的库一次快照要整份复制，尚无增量或轮转策略（ADR-0003 限制）。
9. **取消只验证了直接子进程**，后代进程回收未验证；author 阶段模型自己的试跑完全不在 run 记录里（ADR-0005 缺口 1）。
10. **多会话并发未实测**：宿主是单进程；跨会话并发只在目录隔离层面推理过（ADR-0006 缺口 7）。

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
| 插件宿主 | 直接用 cordis 4.0.5（与 DSH 同框架），能力＝服务，业务包也是插件 | ADR-0006 |
| 工作区写入约束 | 推迟（Windows 无可用的 OS 级沙箱） | ADR-0001 缺口 13 |

---

## 四、最该先补的三件事

1. **用户确认流程**：这是产品与「一个会写脚本的 agent」的分界线。真实数据那一轮模型自己提了 7 个待确认项
   （口径未覆盖的取值、`remaining` 是否净额、PayRate 覆盖还是保留），**没有地方接住**。
   PN 看板那种「18 行真人答案」已经证明：有标签才能区分"跑通了"和"算对了"。
   现在 harness 有了 `commands` 与 `stages` 两个服务，确认流程可以作为 `confirmations` 服务 + 一个插件落地。
2. **S4 最小任务族**：M1 的输入是合成 Excel，属于 S4。先交 3–5 个够用的任务，含至少一个语义不确定案例，
   并把真实数据这次用的「第二实现核对」固化成 verifier（插件形态：`verifiers` 服务 + 一个包）。
3. **Harness 剩下的三件事**：热重载、client 半边、分发包——按 ADR-0006 缺口的顺序，先做前两个里
   与产品近期相关的一个（多半是 client 半边，因为它和 Web 一起动）。
