# Vibe Excel

业务人员用自然语言与 agent 共同建立和维护一个**可复用的数据处理程序**。Excel 是输入输出载体，不是内部计算模型；事实存放进 SQLite，代码与数据版本分开管理。

当前进度：**M0（最小技术验证）已完成**，结论与缺口见 [ADR-0001：pi 接入形态与 M0 技术基线](docs/adr/0001-pi-integration.md)。

## 三条贯穿全局的约束

1. **不新增模型可调用的工具。** 动作空间是代码（Python / SQL），agent 通过 pi 自带的 `bash` / `read` / `write` / `edit` 完成工作。能力落在文件约定、上下文和校验上，不落在工具 schema 上。
2. **事实与解释分离。** 执行状态、退出码、制品、验证结果是事实；步骤名、业务解释是模型说明。颜色、部门别名这类语义在用户确认前只是证据。
3. **发布权在用户。** 代码 checkout 不能回滚数据库；恢复一个节点需要「commit + 输入批次 + 数据版本 + 标注版本 + 制品」五件套。

## 目录

| 路径 | 职责 |
|---|---|
| `packages/contracts` | 跨层最小契约（品牌化 ID、运行状态、执行事件、限制） |
| `spikes/m0-pi` | M0 验证程序：pi RPC 接入、独立 cwd、取消、会话恢复、环境白名单 |
| `runtime-env` | 数据运行时：uv 管理的 Python 3.12 + pandas + openpyxl + sqlite3 |
| `docs/adr` | 架构决策记录 |

尚未开始：`packages/session`、`packages/harness`、`packages/runtime`、`packages/eval`、`packages/growth`、`apps/web`。

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
pnpm test                           # vitest
uv run --project runtime-env python runtime-env/smoke.py   # 数据运行时冒烟测试

# M0 验证（需要 OPENCODE_API_KEY 在环境里）
$env:OPENCODE_API_KEY = ...
node spikes/m0-pi/src/main.ts --list
node spikes/m0-pi/src/main.ts --all
```

M0 的检查项各自独立、可单独重跑：`--only=spawn,python,cancel,resume,isolation`。

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

完整清单见 [ADR-0001 §5](docs/adr/0001-pi-integration.md)。最关键的三条：

- 本地模拟环境**不是**安全沙箱：不保证 agent 无法越界读写、不保证断网、不保证凭证不可访问；
- 取消只验证了直接子进程被打断，**未验证**后代进程全部回收；
- 并发写入同一数据库文件**未验证**，当前实现没有任何串行化机制。
