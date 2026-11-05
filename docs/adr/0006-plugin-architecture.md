# ADR-0006：插件化架构——与 DSH 共用同一套插件模型

- 状态：已接受
- 日期：2026-10-30
- 相关：指南 §3.1、§3.4、§12 S2；ADR-0001（pi 接入）、ADR-0005（执行单元）

## 1. 问题

S2 之前，所有能力都堆在 `spikes/demo-real-data/src/main.ts` 里：起会话、跑 pi、执行程序、写事件、解析命令行。
「加一个能力」＝改这个文件；「换一个实现」＝改这个文件。而接下来要加的每一件东西——确认流程、评测任务族、
Web 界面、沙箱化执行器——都会继续往这个文件里塞。

维护成本不是随能力线性上升，是随**交叉引用**上升。所以问题不是"要不要插件化"，而是
"插件化到什么程度、和谁保持一致"。

## 2. DSH 的事实（读的是本机 runtime 里的真实代码，不是复述文档）

| 事实 | 证据位置 |
|---|---|
| 宿主是 **cordis 4.0.5**（`@deepseek-ai/cordis`，DSH 仓库的 `vendor/cordis`，公开发布在 npm） | `node_modules/@deepseek-ai/cordis/package.json` |
| 一个能力＝一个 npm 包，导出 cordis 插件：`{ name, inject, provide, Config, apply(ctx, config) }` | `lib/types/registry.d.ts` 的 `Plugin.Function/Constructor/Object` |
| 组合＝**清单行** `- id / name / disabled / config`，多补丁层按 id 覆盖 | `profiles/web/cordis.patch.yml`、`dsh-base/cordis.patch.yml`（528 行） |
| 补丁**整体替换**目标行的 `config`，不深合并 | `dsh-base/cordis.patch.yml` 文件头注释 |
| 启动顺序**不由行序决定**，由服务可用性决定 | 同上注释：「Row order carries no load semantics (activation is service-availability driven)」 |
| profile ＝ `package.json` 的 `dsh.profile.bundles` + `cordis.patch.yml`；用户层还有全局 patch | `profiles/web/package.json`、`~/.dsh-cline/cordis.patch.yml` |
| 连 Web 半边也是插件（`dsh.client.inject` + `platform`） | `@dsh-cline/host-services/package.json` 的 `dsh.client` |

也就是说，DSH 的「一切皆插件」不是设计口号，是**一个 500+ 行的清单加一套服务注入**：
没有"框架代码"和"业务代码"的分界，只有服务和消费者。

## 3. 实测：cordis 的确切语义（四条，两条与直觉相反）

写探针跑过（`packages/harness` 的 `_probe.ts`，验证完即删）：

| 现象 | 结论 |
|---|---|
| 先注册消费者、后注册提供者 | 消费者 PENDING → 提供者一就绪，消费者**自动加载** |
| 提供者被 dispose | 依赖方 cleanup 执行、退回 PENDING，其提供的服务从 `ctx.get` 消失 |
| `Config` 校验失败 | 抛 `invalid config:`，`apply` **不会**执行 |
| 依赖永远缺失 | fiber 停在 PENDING，`apply` 不执行，但 `await fiber.await()` **仍然 resolve** |

最后一条有个直接后果：**不能用 `await` 判断"插件起来了"，必须读 `fiber.state`**。
宿主因此要等"没有 LOADING/UNLOADING"才算稳定（`host.ts` 的 `settle()`）——这一步在第一次跑的时候
把 `consumer` 读成了 `LOADING`，是实测抓出来的。

## 4. 决策

### 决策 1：直接用 cordis，不自建"长相类似"的内核

一致性有两种做法：用它，或者照它写一个。后者更省依赖，但它是**模仿**：语义会漂，生态用不了，
以后想搬进 DSH 还得重写一遍。既然目标是"维护方便"，少写一个内核才是省。

代价是引入一个 RC 阶段的外部依赖（`4.0.5`，锁定版本，见缺口 4）。

### 决策 2：能力＝服务；扩展点也是服务，不是钩子表

每个插件要么 `provide` 一个服务，要么 `inject` 别人的服务，要么两者都做。没有"注册回调到某个列表"的第二套机制。
这与 DSH 的 `dsh-tools`（工具插件往 `ctx.tools` 里注册）同构。

所以"支持多种执行器"不需要定义 `ExecutorPlugin` 接口加一个注册表，只需要：**换掉 `exec` 这个服务**。

### 决策 3：清单 + 补丁层，`config` 整体替换

`vibe.plugins.yml` 一行一个插件，`id` 是行的身份。补丁层按 id 覆盖或 `insert:` 追加；写错 id 直接报错。

不深合并，因为深合并会让"这一行最终是什么"无法从文件里读出来——DSH 已经为此吃过一次设计，
把结论写在了补丁文件头。

### 决策 4：业务也是一等插件

`stage-molex` 就是一个插件：它不碰执行、不碰存储，只声明"这个流程有两个阶段，各自入口和产出是什么"，
然后注册进 `stages` 服务。

于是「换一个业务流程」＝换一个这样的插件，编排器一行不改。这也是本 ADR 唯一一条真正"因为是插件才做得到"的收益。

### 决策 5：只使用 cordis 的一小块 API

允许使用的只有：`ctx.get` / `ctx.provide` / `ctx.effect` / `ctx.plugin`、插件的 `name` / `inject` / `provide` / `Config` / `apply`。
不允许使用 intercept、isolate、events 的派发模式、fiber 的 restart/update。

理由：把耦合面积压到可以手写替代品的程度（决策 7）。这条是纪律，不是能力不足。

### 决策 6：库不认识框架

`packages/contracts`、`packages/session`、`packages/runtime` 里**没有一行 cordis**，可以被单测直接调用；
插件只做适配（`store` 包 `SessionStore`、`exec` 包 `cleanRerun`、`agent-pi` 包 `RpcClient`）。

这决定了测试形态：库的测试用 vitest 直接调，插件的测试用临时清单起一个宿主。

### 决策 7：保留退出通道

因为只用了决策 5 的子集，换掉 cordis 需要改的只有 `host.ts` 和 `kernel.ts` 里 `PluginContext` 的定义（约 200 行），
所有插件的写法不变。这是"用外部框架"和"被外部框架锁死"的区别。

## 5. 当时的插件图（9 个，全部 ACTIVE）

> **2026-11-05 补记**：`stages` / `stage-molex` 已按 [ADR-0007](0007-flow-as-unit.md) 删除，现在是 7 个插件；
> `sessions` 注册的命令也不一样了——现在是 `setup/run/flow/publish/optimize/status/author/db`
> （`verify` 与 `stages` 已不存在）。下面 §5–§7 的图、命令名、`StageSpec` 与测试计数都保留原样作为记录，
> **当前的**看 [ARCHITECTURE.md](../ARCHITECTURE.md)。

```
commands ──────────────┐
telemetry ─────────────┤
store ──► workspace ───┤
exec ──────────────────┼──► sessions ──► (setup/run/verify/publish/status/author/stages/db)
agent-pi ──────────────┤
stages ──► stage-molex ┘        （stage-molex 注册 molex-prep 的两个阶段）
```

| id | 提供 | 依赖 | 职责 | 换掉它意味着 |
|---|---|---|---|---|
| `commands` | commands | — | CLI 子命令注册表 | 换成别的入口（HTTP / RPC） |
| `telemetry` | telemetry | — | `events.jsonl` + `timeline.txt` | 换成 otel / 远程采集 |
| `store` | store | — | 控制面库 | 换成 Postgres |
| `workspace` | workspace | store | 每会话目录 + 排他锁 | 换成容器挂载 |
| `exec` | exec | — | 干净 worktree 执行 | 换成容器 / 远程执行器 |
| `agent-pi` | agent | — | pi 子进程 | 换成 ACP / 别的 agent |
| `stages` | stages | — | 阶段包注册表 | — |
| `stage-molex` | — | stages | 业务包（molex-prep） | **换一个业务流程** |
| `sessions` | sessions | 上列全部 | 生命周期编排 + 命令 | — |

命令也来自插件：`sessions` 注册了 8 条，加命令不用改 CLI 入口。

## 6. 与 ADR-0005 的关系

ADR-0005 的六条决策**没有被推翻**，只是换了承载者：

| ADR-0005 决策 | 现在在哪 |
|---|---|
| 入口契约由编排器定义 | `stage-molex` 的模板 + `exec` 的调用签名 |
| 输入批次内容寻址 | `sessions.setup()` + `fs-util.batchIdOf` |
| run ≡ 干净执行 | `exec` 插件（内部仍是 `cleanRerun`） |
| 数据版本记输入、产物记 artifacts | `sessions` + `StageSpec.mutatesDatabase`（声明式，不写在 if 里） |
| 阶段化而非一个大脚本 | 阶段包 + `stages` 服务 |
| 独立核对是发布证据 | `verify` 命令 + `publish --verify=<文件>` |

## 7. 验证

1. **9 个插件全部 ACTIVE**，`inject` 关系与上面那张图一致（`node packages/harness/src/main.ts plugins`）。
2. **行为等价**：在演示台留下的同一份 `control.db` 上，harness 跑 `run --stage=analyze`，
   4 个产出文件与演示台的产物 **sha256 逐文件相同**（`ladder.json` / `pn_available.csv` / `population.csv` / `summary.json`）。
3. **测试 29 项**（新增 6 项）：清单展开与默认值、补丁按 id 覆盖且整体替换、改不存在的 id 报错、
   启动顺序由依赖决定、依赖缺失停在 PENDING、`Config` 非法记 FAILED、目录/库摘要按内容比对。

## 8. 已知缺口

1. **没有热重载**：DSH 有 `dsh-hmr`，我们没有；改插件要重启进程。
2. **没有 client 半边**：DSH 的 Web 也是插件，我们的 `apps/web` 还没开始，将来要按 `dsh.client` 同样的形状做。
3. **没有分发包**：现在只有一份 `vibe.plugins.yml`，没有 bundle / profile / 版本化 / 市场；
   `id` 与语义化版本的关系没定义。
4. **cordis 是 RC 依赖**：锁 4.0.5，没有升级策略，也没测过其它版本。
5. **插件间共享可变状态没有约束**：服务是任意对象，没有"只读服务 / 可写服务"的约定，
   插件 A 拿到 B 的服务后可以随便改它。
6. **没有插件级权限**：清单里任何插件都能拿到任何服务；在企业部署里这可能不够。
7. **多会话并发仍未实测**：宿主是单进程单宿主，跨会话并发只在目录隔离层面推理过，没跑过。
8. **`settle()` 是启发式**：等"没有 LOADING/UNLOADING"，上限 20 轮；真正的加载图分析没有做。
