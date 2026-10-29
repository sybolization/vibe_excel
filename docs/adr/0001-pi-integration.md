# ADR-0001：pi 接入形态与 M0 技术基线

- 状态：已接受（M0）
- 日期：2026-10-29
- 决策者：Orchestrator（本轮）
- 相关：实施指南 §2、§3、§8、§13(M0)；ADR-0002（待建：SQLite 与数据分层）

## 1. 背景

指南 §2.1 要求「Agent 核心复用 pi，不重建模型接入、agent 循环和对话机制」，§3.4 要求在动手封装前先验证裸 pi 的纵向闭环。M0 要回答的问题是**事实问题**，不是设计问题：

1. pi 实际是哪个包、哪个版本，SDK 长什么样；
2. 裸 pi 能不能在独立 cwd 里跑 Python / SQLite；
3. 对话能不能恢复；
4. 日志、取消、环境变量限制这些控制能不能拿到；
5. 数据库备份、HTML 数据桥接的安全边界（后者不在本 ADR）。

本 ADR 只记录**已验证的事实**与**明确缺口**。凡是没跑过的，一律写「未验证」，不用设计文档的口吻代替证据。

## 2. 决策

### 决策 1：接入形态用「RPC 子进程 + 包内 RpcClient」，不用进程内 SDK，也不用 worker 线程

指南 §3.3 提到「可选一个 worker 线程承载一个 agent」。**在 pi 里没有这个对应物**，且线程本身既不是隔离边界（指南自己也这么写），也不是可靠的取消边界——取消一个跑着 `time.sleep(120)` 的 Python，需要的是能杀进程树的能力，不是设置一个标志位。

pi 提供两种真实形态（`docs/rpc.md`）：

| 形态 | 进程边界 | 控制模型 | 适用 |
|---|---|---|---|
| SDK | 进程内 | 直接 TypeScript 方法 | 需要完整 API 访问的同一进程宿主 |
| RPC | 子进程 | JSONL 命令/响应/事件 | 隔离进程、IDE、自定义客户端 |

选择 **RPC**，理由与指南的对应关系：

- **可重启**（§3.2「Harness 进程重启后要能恢复到确定节点」）：pi 崩了不带走 Harness；
- **可取消**（§12 S2「用户停止」）：`abort` / `abortBash` 有真实语义；
- **可观测**（§7.5「事实与解释分离」）：事件流是子进程自己发出的，不是 Harness 转述的；
- **cwd 与环境可控**（§3.3、§8）：`spawn` 的 `cwd` / `env` 是一等公民。

同时**不自己写 JSONL 客户端**，直接用包内导出的 `RpcClient`（`start/stop/onEvent/prompt/promptAndWait/abort/bash/abortBash/getState/getMessages/getSessionStats/waitForIdle`）。理由：一方面这是「复用 pi」而不是重造；另一方面 `docs/rpc.md` 明确指出帧格式有坑——Node 的 `readline` 会按 `U+2028`/`U+2029` 切分，而那些字符在 JSON 字符串里是合法的。自己实现等于重复踩这个坑。

### 决策 2：版本钉死

| 组件 | 版本 | 依据 |
|---|---|---|
| `@earendil-works/pi-coding-agent` | 1.4.0 | registry 最新；`engines.node >=22.19.0` |
| pi 其余包（`pi-agent-core` / `pi-ai` / `pi-codemode` / `pi-mcp` / `pi-tui` / `pi-telemetry`） | 1.4.0 | 与 coding-agent 同版，避免混装 |
| Node | ≥22.19.0（本机 v22.23.2） | pi engines |
| pnpm | 12.11.2 | 现有 `packageManager` |
| Python | 3.12.14（uv 管理） | `runtime-env/.python-version` |
| pandas / openpyxl | 2.3.3 / 3.1.5 | `runtime-env/uv.lock` |
| SQLite（Python stdlib） | 3.53.1 | 实测 |

三条容易踩的事实：

1. **上一轮残留的依赖是错配的**：`pi-coding-agent@1.2.0` 与其它包 `1.4.0` 混装，`pi-ai` 还同时存在 1.2.0 与 1.4.0 两份。已重装统一到 1.4.0。
2. **`~/.pi/agent/settings.json` 里的模型 id 在 1.4.0 上不存在**：写的是 `deepseek-v4-flash`，而 pi 1.4.0 的 deepseek catalog 只有 `deepseek-flash` 与 `deepseek-v4-pro`（`pi --list-models deepseek` 实测）。因此本项目的调用**一律显式传 `--provider` / `--model`**，不依赖用户全局 settings。
3. **pnpm v11 起 `onlyBuiltDependencies` 已被移除**，改为 `pnpm-workspace.yaml` 里的 `allowBuilds` **映射**（`{ esbuild: true, ... }`）；未列出的包默认视为未审查，`strictDepBuilds` 默认开启会直接让安装失败（`ERR_PNPM_IGNORED_BUILDS`）。

### 决策 3：仓库与制品的边界

指南 §6.1 / §11 要求原始 Excel、SQLite、输出、标记、trace 都不进代码仓库。

- **代码仓库** = 本目录（`packages/`、`spikes/`、`apps/`、`runtime-env/`、`docs/`）。
- **制品根** = `<repo>/../vibe_excel-store`，可用环境变量 `VIBE_EXCEL_STORE` 覆盖。
- 每个 session 一个独立 run 目录：`<store>/m0/<检查名>-<时间戳>/`，其下 `work/`（cwd）、`agent/`（pi 配置）、`sessions/`（会话文件）。
- 每会话一个 **独立的 `PI_CODING_AGENT_DIR`**（pi 支持的环境变量），不共用 `~/.pi/agent`。

注意：这是「不把数据放进代码仓库」，**不等于**「agent 访问不到它」。Windows 上同一用户对 `<repo>/../` 有完全权限，agent 当然能读。指南 §8 要求不得用「工具都经过 sandbox」掩盖本机执行的事实——这里同理。

### 决策 4：原生 Windows + Git Bash

WSL 未安装（`wsl -l -v` 报未安装），因此运行平台是原生 Windows：

- pi 的 `bash` 工具按 `shellPath` → `Program Files` 下的 Git Bash → PATH 的顺序解析，本机命中 `C:\Program Files\Git\bin\bash.exe`，**无需额外配置**；
- pi 另有 `powershell` 工具（仅原生 Windows），可用 `defaultTools` 切换，但本阶段不使用——保持「一种执行入口」便于做日志与取消；
- 指南 §8 的「不能保证」清单是按类 Unix 写的，原文里的「不能读取同一用户的其他文件」「共享 venv 不自动只读」在 Windows 上是 ACL 语义，**不能照抄**，S3 的 README 要按本机实测重写。

## 3. 环境事实（2026-10-29 实测）

| 项 | 实测结果 |
|---|---|
| Python | 系统里**没有**可用的 Python：`WindowsApps\python.exe` 是 0 字节 Store 占位程序，`python -V` 返回 9009；无 `py`/conda/`sqlite3`。由 `uv python install` 提供 CPython 3.12.14 |
| uv | 0.12.9（`~/.local/bin/uv.exe`） |
| Git / Git Bash | 2.55.0 / `C:\Program Files\Git\bin\bash.exe` 存在 |
| Node / pnpm | v22.23.2 / 12.11.2 |
| 网络 | astral.sh、pypi、npm registry 均可达 |
| pi 凭证 | `~/.pi/agent/auth.json` 为 `{}`；User 级环境变量存在 `DEEPSEEK_API_KEY`（进程需显式注入） |

## 4. M0 实测结论

运行方式：`node spikes/m0-pi/src/main.ts --all`，报告见 `spikes/m0-pi/out/report-2026-10-29T08-45-13-180Z.json`。
5 项全部通过。以下每一条都是**跑出来的**，不是设计推断。

| # | 验证项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 独立 cwd | 子进程内 `node -p "process.cwd()"` 回报 | 与构造参数逐字符一致 |
| 2 | 事件流可得 | 订阅 `onEvent` 计数 | 模型驱动运行 720 条事件、11 种类型（`agent_start`/`turn_start`/`message_start`/`message_update`/`message_end`/`tool_execution_start`/`tool_execution_update`/`tool_execution_end`/`turn_end`/`agent_end`/`agent_settled`） |
| 3 | **不用自定义工具完成 Python→SQLite** | 5 行脏数据 → 真模型自主调用工具 | 成功。**只用到内置工具 `bash` / `read` / `write`**（无任何自定义工具） |
| 4 | 导入守恒 | 来源行数 vs 落地行数 | staging 3 + quarantine 2 = **5**，与来源 5 行相等 |
| 5 | 脏数据不被静默丢弃 | 检查 quarantine 表结构 | 每行含 `reason`、`raw_line`（未改动的原始整行）、`source_file`、`source_row` |
| 6 | 幂等保护 | 检查表 DDL | `UNIQUE (source_file, source_row)` + `AUTOINCREMENT`，重复导入不会重复插入 |
| 7 | 取消真的生效 | 8 秒后落标记的 Python，`abortBash()` 后等 10 秒 | 标记文件**未出现** → 子进程确实被打断 |
| 8 | 对话恢复 | 进程 A 告知数字 4242 → 进程 B `--continue` 追问 | B 回复 `4242`。会话落盘为 `<sessionDir>/<ts>_<uuid>.jsonl` |
| 9 | 环境白名单 | 父进程设置哨兵变量，子进程内探测 | `{"sentinel":false,"providerKey":true}` → 哨兵未泄漏，凭证可见 |
| 10 | 每会话独立 pi 配置 | `PI_CODING_AGENT_DIR` 指向 run 目录 | 目录被自动创建（`auth.json` + `models-store.json`），模型调用正常 |
| 11 | 凭证不落盘 | 读该 `auth.json` | 2 字节 `{}`，**未写入 provider key**（凭证只从环境读取） |
| 12 | `--offline` 不破坏鉴权 | 单独 A/B | 通过 |

一次真实导入任务的成本事实（可作为后续评测预算的锚点）：**30 秒、21072 tokens（input 4993 / output 2767 / cacheRead 13312）、$0.0024**。

### 4.1 一个值得记录的模型行为

输入里刻意放了三类问题：混合日期格式、部门别名（研发 / 研发部）、空状态。模型的处理是：

- `2026/3/1` → 归一化为 `2026-03-01`，同时保留 `raw_hire_date = 2026/3/1`；
- **`2026-03-05` 没有因为格式不同被丢弃**（这一点很关键：pandas 的默认 `pd.to_datetime` 会因为从首值推断单一格式而把它判成 `NaT`，见 `runtime-env/smoke.py` 里固化的同一个陷阱）；
- 李四被隔离的原因不是日期，而是**「状态为空，无法确定」**；
- 王五被隔离的原因是**「入职日期缺少年份」**。

由此得到一条要给 M1 的输入：指南 §5.2 的「不可静默处理」清单里**没有「空值是否等价于缺失业务状态」这一条**，而模型自发把它当成了待确认项。这条清单需要补充。

## 5. 明确缺口

每条都写清「没验证什么」，不用「大概可以」收尾。

1. **网络决定了 provider 选择，与最初决定不同。** 直连 `api.deepseek.com` 在本机 TLS 握手失败（PowerShell 与 curl/schannel 均报 `SEC_E_INTERNAL_ERROR`），因此 `DEEPSEEK_API_KEY` 这条路径**不可用**。实际使用的是 `opencode-go` + `OPENCODE_API_KEY`（即本机 DSH 自己在用的凭证）。**需要用户确认这是长期采用还是临时绕行。**
2. **`RpcClient` 的环境变量是「合并」而不是「替换」。** 已读源码确认其 `spawn` 使用 `{ ...process.env, ...options.env }`。因此真正的白名单只能靠临时替换 `process.env` 实现（本 spike 的做法），这在一个有并发 session 的 Harness 里是**不可接受的**。生产需要：自己 spawn CLI，或给 pi 提交可传入完整 env 的选项。
3. **取消只证明了「直接子进程被打断」。** 没有验证后代进程是否全部回收（例如 bash 里再起后台进程、或 Python 再 spawn 子进程）。指南 §8 明确要求不得未经平台验证就承诺杀掉所有后代——这里就是那个未验证点。
4. **并发未验证。** §3.3 要求「同一 session 的数据修改串行执行，多个 session 可并发但不共用同一个可写数据库文件」。本轮全部是单进程串行，**没有任何串行化机制的实现或测试**。
5. **数据库快照未验证。** §6.3 要求「停止写入后备份或关闭连接后复制」。本轮没有实测 WAL 模式下热复制的完整性，只有一个空库级别的常识。
6. **`getState()` 的形状会变。** `--no-session` 时没有 `sessionFile` / `sessionName` 字段，加 `--session-dir` 后才有。恢复定位**不能假设字段恒定**，必须按存在性处理。
7. **事件分类只在两类场景验证过。** 模型驱动有完整的 run 事件；客户端 `bash()` 只有 `bash_execution_update`。compaction、auto-retry、长任务中途观测在真实业务里的表现**未验证**（失败那次重试出现过 `auto_retry_start` ×3，但没有成功恢复的样本）。
8. **pi 的沙箱扩展未评估。** 包里带 `examples/extensions/sandbox`，可能提供比「白名单 + 独立 cwd」更强的边界。本轮没有看它的实现和限制。
9. **`--offline` 的具体语义未深究。** 只验证了它与模型调用兼容，没有验证它到底关掉了哪些启动期网络行为。
10. **Windows 上的 §8 限制条款尚未写成文。** S3 的 README 需要按本机实测重写「不能保证」清单，本轮只确认了「原生 Windows + Git Bash」这个前提。

