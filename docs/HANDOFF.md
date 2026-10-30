# 交接（2026-10-30 15:30）

> 给"重启 DSH 之后新开的那个会话"用。读完这一页就能接上，**不需要读历史会话**。
> 全部内容都有落盘依据；有疑问时以 `docs/` 里的文件和测试为准，不以这份文档为准。

## 一句话现状

M0 完成；S1/S3 有测试；**真实 HR 数据端到端跑通**（117,776 行入库、独立核对 23/23、合入 main）；
**S2 harness 已插件化**（cordis 宿主 + 9 个插件，29 项测试全绿）。**M1（产品链路）未开始。**

## 已经提交的七次

| 提交 | 内容 |
|---|---|
| `828ecfc` | M0：pi 1.4.0 RPC 接入验证，建立工程基线 |
| `e44d477` | M0：补齐追踪产物，修正两处会制造假结论的判据 |
| `ddf4c82` | 项目状态报告，划清「已完成 / 未开始」 |
| `c140769` | S1+S3：持久化与恢复、草稿与发布、`VACUUM INTO` 快照、会话锁（23 项测试） |
| `9803be0` | 真实数据演示 + ADR-0005（执行单元：入口契约 / 输入批次 / run ≡ 干净执行） |
| `313168c` | 插件化 harness + ADR-0006（cordis、清单、9 个插件） |
| `fd2f3dd` | `docs/ARCHITECTURE.md`（当前架构：三层、服务图、版本模型、时序） |

工作区干净，`pnpm typecheck` 通过，`pnpm test` 29/29。

## 必读的三份文件

1. [docs/ARCHITECTURE.md](ARCHITECTURE.md) —— 现在是什么样（三层代码、服务图、五件套版本模型、一次 run 的时序）
2. [docs/STATUS.md](STATUS.md) —— 做了什么 / 没做什么 / 下一步
3. [spikes/demo-real-data/RESULTS.md](../spikes/demo-real-data/RESULTS.md) —— 真实数据那轮的全部数字与 7 个待确认项

## 五个必须记住的坑（都是踩过的）

1. **cordis 的 `await fiber.await()` 不代表插件起来了。** 依赖未满足时它照样 resolve，必须读 `fiber.state`（0 PENDING / 1 LOADING / 2 ACTIVE / 3 FAILED）并等"没有 LOADING"才算稳定。`host.ts` 的 `settle()` 就是为这个写的。
2. **`data_revision_id` 记的是"输入"，不是"产出"。** 产出走 `artifacts`；改了库的阶段靠 `StageSpec.mutatesDatabase: true` 在跑完后快照出新版本给下一次用。
3. **`getSessionStats()` 必须在 `client.stop()` 之前调用**，否则拿到空对象。事后只能从 `events.jsonl` 的 `turn_end.message.usage` 聚合。
4. **`RpcClient` 的 env 是合并不是替换**（`{...process.env, ...options.env}`），真白名单只能临时换 `process.env`——所以它不能被并发调用（ADR-0001 缺口 2）。
5. **PN 余量那个"剩余空号"是净额**，等于「可用岗位」计数，**不能再减「本批已发」**。我第一版核对脚本就是在这里算错的，模型独立发现了同一件事。

## 现在最该做的三件事（按价值排序）

1. **确认流程**（`confirmations` 服务 + 第 10 个插件）。这是产品与"会写脚本的 agent"的分界线：真实数据那轮模型提了 7 个待确认项，现在只躺在一份 JSON 里，没人能点。
   顺带能验证服务切分——它会是 `StoreService.db()` 这个"门钥匙"债务的第一个真实消费者。
2. **S4 最小任务族**：3–5 个合成 Excel 任务 + 独立 verifier + baseline。真实数据那轮的 `tools/verify_outputs.py` 可以直接演化成 verifier。
3. **harness 余下三件**：热重载 / client 半边（为 `apps/web` 铺路）/ 分发包。见 ADR-0006 §8。

## 可选的小重构（30 分钟级，低风险）

- `SessionStore` → `class Sessions extends Service`，对外只留 `create/get/list`，把 DB 藏进去（修 `StoreService.db()` 暴露 23 个方法的债务）。
- `recoverSession()` 从方法变成可注册的投影。
- **不建议**现在做全量事件溯源（DSH 那种日志当事实源）：数据量只有 3 条 run、2 个数据版本，等确认流程落地再一起做。

## 命令速查

```powershell
pnpm typecheck ; pnpm test
node packages/harness/src/main.ts plugins        # 9 个插件与加载状态
node packages/harness/src/main.ts status         # 恢复视图（代码/数据/批次/制品）
node packages/harness/src/main.ts run --stage=analyze
node spikes/demo-real-data/src/main.ts status    # 旧脚手架（同一份 control.db）

uv run --project runtime-env python spikes/demo-real-data/tools/verify_outputs.py `
  --db C:/project/vibe_excel-store/sessions/23e16fce-bcf3-4d2a-9dac-fca5e4952db1/work/hr.db `
  --out C:/project/vibe_excel-store/sessions/23e16fce-bcf3-4d2a-9dac-fca5e4952db1/work/out/analyze
```

凭证：`OPENCODE_API_KEY`（从 `~/.dsh-cline/.credentials.yaml` 的 `OPENCODE_GO_API_KEY` 取，**不要打印**）。
只有 `author` 需要它；`run` / `verify` 不带任何模型凭证。

## 制品根里现在有什么（别误删）

`C:\project\vibe_excel-store`
- `control.db`：1 个 program、1 个 session、3 条 run、2 个数据版本、1 条发布记录
- `data-revisions/`：两个快照（空库基线 12 KB、import 产出 74 MB）
- `program-repos/6d1e7f79-…`：main 在 `77750ae8`（两次 author 的提交已合入）
- `sessions/23e16fce-…/`：`code/` 草稿 worktree、`work/hr.db` 74 MB、`work/out/analyze/` 四个产出、`runs/` 全部轨迹
