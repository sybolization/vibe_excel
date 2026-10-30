# Vibe Excel

业务人员用自然语言与 agent 共同建立和维护一个**可复用的数据处理程序**。Excel 是输入输出载体，不是内部计算模型；事实存放进 SQLite，代码与数据版本分开管理。

当前进度：**M0 已完成**（[ADR-0001](docs/adr/0001-pi-integration.md)）；**S1/S3 已落地并有测试**；
**真实 HR 数据第一次端到端跑通**（117,776 行入库、独立核对 23/23、合入 main，见 [RESULTS](spikes/demo-real-data/RESULTS.md)）；
**S2 harness 已插件化落地**（cordis 宿主 + 9 个插件，见 [ADR-0006](docs/adr/0006-plugin-architecture.md)）。
产品链路（探查 → 确认 → 页面）仍未开始。

## 三条贯穿全局的约束

1. **不新增模型可调用的工具。** 动作空间是代码（Python / SQL），agent 通过 pi 自带的 `bash` / `read` / `write` / `edit` 完成工作。能力落在文件约定、上下文和校验上，不落在工具 schema 上。
2. **事实与解释分离。** 执行状态、退出码、制品、验证结果是事实；步骤名、业务解释是模型说明。颜色、部门别名这类语义在用户确认前只是证据。
3. **发布权在用户。** 代码 checkout 不能回滚数据库；恢复一个节点需要「commit + 输入批次 + 数据版本 + 标注版本 + 制品」五件套。

## 目录

| 路径 | 职责 |
|---|---|
| `packages/contracts` | 跨层最小契约（品牌化 ID、运行状态、执行事件、限制） |
| `packages/session` | S1：Program/Session/Run/DataRevision 持久化、草稿与发布、恢复视图 |
| `packages/runtime` | S3：会话工作区、排他锁、SQLite 一致性快照、干净重跑 |
| `packages/harness` | S2：插件化宿主（cordis）+ 9 个插件（store/workspace/exec/agent-pi/telemetry/stages/stage-molex/sessions/commands）+ CLI |
| `vibe.plugins.yml` | 插件清单：一行一个插件，启动顺序由服务依赖决定 |
| `spikes/m0-pi` | M0 验证程序：pi RPC 接入、独立 cwd、取消、会话恢复、环境白名单 |
| `spikes/demo-real-data` | 真实数据演示台（已被 harness 接管，保留作对照与核对脚本） |
| `runtime-env` | 数据运行时：uv 管理的 Python 3.12 + pandas + openpyxl + xlrd + sqlite3 |
| `docs/adr` | 架构决策记录（0001 M0 · 0002 控制面/版本 · 0003 串行化/快照/重跑 · 0004 草稿与发布 · 0005 执行单元 · 0006 插件架构） |

尚未开始：`packages/harness`、`packages/eval`、`packages/growth`、`apps/web`。

## 一次性准备

```powershell
# 1) Python 运行时（uv 负责解释器与依赖锁定）
uv sync --project runtime-env

# 2) Node 依赖（pi 1.4.0 全家桶，版本必须一致）
pnpm install
```

> 本机**没有**可用的系统 Python：`WindowsApps\python.exe` 是 0 字节的 Store 占位程序，`python -V` 返回 9009。一切 Python 调用走 `runtime-env\.venv\Scripts\python.exe`。

## 常用命令

```powershell
pnpm typecheck                      # tsc 类型检查（含 erasableSyntaxOnly）
pnpm test                           # vitest：session + runtime 两套测试
uv run --project runtime-env python runtime-env/smoke.py   # 数据运行时冒烟测试

# M0 验证（需要 OPENCODE_API_KEY 在环境里）
$env:OPENCODE_API_KEY = ...
node spikes/m0-pi/src/main.ts --list
node spikes/m0-pi/src/main.ts --all
```

M0 的检查项各自独立、可单独重跑：`--only=spawn,python,cancel,resume,isolation`。

Harness（S2，插件化；命令都由插件注册，加命令不用改入口）：

```powershell
node packages/harness/src/main.ts plugins            # 插件清单与加载状态（含依赖关系）
node packages/harness/src/main.ts help               # 已注册的命令
node packages/harness/src/main.ts setup              # 程序仓库 + 会话 + 输入批次（幂等）
node packages/harness/src/main.ts run --stage=analyze    # 干净 worktree 里按 commit 执行
node packages/harness/src/main.ts verify --stage=analyze # 同 commit 跑两次比内容哈希
node packages/harness/src/main.ts publish --verify=<verify.json>
node packages/harness/src/main.ts status             # 恢复视图（四个版本引用）
```

只有 `author` 需要 `OPENCODE_API_KEY`；`run`/`verify` 不带任何模型凭证。
清单环境变量：`VIBE_EXCEL_STORE` / `VIBE_EXCEL_PYTHON` / `VIBE_EXCEL_AS_OF` / `VIBE_EXCEL_GROUND_TRUTH`。

真实数据演示（旧脚手架，保留作对照）：

```powershell
node spikes/demo-real-data/src/main.ts setup
node spikes/demo-real-data/src/main.ts author --prompt=<文件> --label=import   # 需要 OPENCODE_API_KEY
node spikes/demo-real-data/src/main.ts run    --stage=import
node spikes/demo-real-data/src/main.ts verify --stage=import
node spikes/demo-real-data/src/main.ts publish --verify=<verify.json>
node spikes/demo-real-data/src/main.ts status
```

## 怎么看一次运行到底做了什么

每次检查在制品根留下一个 run 目录，固定四类产物：

| 文件 | 内容 | 用途 |
|---|---|---|
| `events.jsonl` | RPC 原始事件流，逐条不加工（有体积上限） | 机器可查的全部事实 |
| `timeline.txt` | 折叠掉流式增量的时间线（时间 / 类型 / 工具名 / 摘要） | 人一眼看完它做了什么 |
| `sessions/*.jsonl` | pi 自己的会话文件 | 可 `--continue` 恢复，可审计 |
| `*.html` | 会话经 `pi --export` 导出的页面 | 含每条消息的 token 与成本 |

例：一次真实导入（986 条事件 / 8 次工具调用 / 59 秒 / $0.0033）在
`<制品根>\m0\python-<时间戳>\` 下，时间线只有 16 KiB，剔除的是 901 条流式增量。

```powershell
# 看时间线
Get-Content "<制品根>\m0\python-*\timeline.txt" -Encoding utf8

# 重新导出 HTML
node spikes/m0-pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js `
  --export <会话>.jsonl <输出>.html
```

注意：`events.jsonl` 是**事实**，`timeline.txt` 里的摘要是**人写的压缩**。两者冲突时以原始事件流为准。

## 数据与制品的边界

代码进 Git，**数据不进**：

- 制品根默认 `<repo>/../vibe_excel-store`，可用 `VIBE_EXCEL_STORE` 覆盖；
- 每个 session 一个 `work/`（cwd）、`agent/`（pi 配置）、`sessions/`（会话文件）；
- 原始 Excel、SQLite 库、输出、标注、trace 一律在制品根，不进仓库。

这是「不把数据放进代码仓库」，**不等于**「agent 访问不到」。Windows 上同一用户对 `<repo>/../` 有完全权限，agent 当然能读。任何声称"已隔离"的说法都必须指出具体的机制，而不是指出目录位置。

## 已知限制

完整清单见 [ADR-0001 §5](docs/adr/0001-pi-integration.md) 与 [ADR-0006 §8](docs/adr/0006-plugin-architecture.md)。最关键的四条：

- 本地模拟环境**不是**安全沙箱：不保证 agent 无法越界读写、不保证断网、不保证凭证不可访问；
- 取消只验证了直接子进程被打断，**未验证**后代进程全部回收；
- 串行化只覆盖**同一会话**（会话锁 + 独立 `work/`），锁不跨机器；跨会话并发没有实测；
- 插件宿主用的是 **cordis 4.0.5（RC 阶段的外部依赖）**：没有热重载、没有分发包、没有插件级权限。
