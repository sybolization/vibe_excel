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

**模型路线（2026-10-30 确认）**：长期采用 `opencode-go` / `deepseek-v4.1-flash`。原因是本机直连 `api.deepseek.com` 的 TLS 握手被拦（见 §5 第 1 条），而该 provider 走的是本机已有凭证。切换模型只需改 `--provider` / `--model` 两个参数，不写死在代码里，也不依赖用户全局 `settings.json`。

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
| 4 | 来源行不被丢失 | 按「带来源坐标列的表」统计覆盖 | 覆盖 **5/5**，且没有一个来源行落进两个去向 |
| 5 | 脏数据不被静默丢弃 | 检查 quarantine 表结构 | 每行含 `reason`、`raw_line`（未改动的原始整行）、`source_file`、`source_row` |
| 6 | 幂等保护 | 检查表 DDL | `UNIQUE (source_file, source_row)`，重复导入不会重复插入 |
| 7 | 追踪产物齐全 | 检查 run 目录 | `events.jsonl`（376 KiB 原始流）、`timeline.txt`（16 KiB）、会话 `.jsonl`、会话 `.html` |
| 8 | 只用到内置工具 | 统计事件流里的 `toolName` | 本次 `bash` ×6 / `write` ×1 / `edit` ×1，**无任何自定义工具** |
| 9 | 取消真的生效 | 8 秒后落标记的 Python，`abortBash()` 后等 10 秒 | 标记文件**未出现** → 子进程确实被打断 |
| 10 | 对话恢复 | 进程 A 告知数字 4242 → 进程 B `--continue` 追问 | B 回复 `4242`。会话落盘为 `<sessionDir>/<ts>_<uuid>.jsonl` |
| 11 | 环境白名单 | 父进程设置哨兵变量，子进程内探测 | `{"sentinel":false,"providerKey":true}` → 哨兵未泄漏，凭证可见 |
| 12 | 每会话独立 pi 配置 | `PI_CODING_AGENT_DIR` 指向 run 目录 | 目录被自动创建（`auth.json` + `models-store.json`），模型调用正常 |
| 13 | 凭证不落盘 | 读该 `auth.json` | 2 字节 `{}`，**未写入 provider key**（凭证只从环境读取） |
| 14 | `--offline` 不破坏鉴权 | 单独 A/B | 通过 |

一次真实导入任务的成本事实（可作为后续评测预算的锚点）：**59 秒、986 条事件、8 次工具调用、40308 tokens（input 6916 / output 3696 / cacheRead 29696）、$0.0033**。

### 4.1 追踪产物：每个 run 三个层级

指南 §9.1 要的是「可追溯、有界、不只依赖对话历史」。本 spike 每个 run 目录下固定产出：

| 文件 | 内容 | 用途 |
|---|---|---|
| `events.jsonl` | RPC 原始事件流，逐条不加工（有体积上限） | 机器可查的全部事实 |
| `timeline.txt` | 折叠掉流式增量的时间线（`工具名 + 摘要`） | 人一眼看完它做了什么 |
| `sessions/<ts>_<uuid>.jsonl` | pi 自己的会话文件 | 可恢复、可审计 |
| `<ts>_<uuid>.html` | 上者经 `pi --export` 导出的页面 | 含每条消息的 token 与成本，可直接看 |

本次 python 检查的 986 条事件里，`message_update` 占 901 条（流式增量），真正有信息量的只有 85 条——所以时间线必须折叠，否则人会被噪声淹掉。

### 4.2 判据先错了一次（记录在案）

第一版守恒判据是「所有表行数求和 == 来源行数」。第二次运行时模型多建了一张 `import_log` 记账表，求和变成 6 ≠ 5，**检查判为失败，但模型其实完全正确**（4 条入 staging + 1 条入 quarantine = 5）。

修正后的判据才是 §5.3 真正要求的东西：**找出带来源坐标列（`source_row` 之类）的表，统计来源行覆盖**。

- 求和 → 会把任何记账表、日志表算成业务记录；
- 覆盖 → 只问「每个来源行有没有明确去向、是否只落一处」。

这条教训比结论本身重要：**守恒检查的错误形式会制造假失败，而假失败会让人去改正确的模型行为。**

### 4.3 同一份输入、四次运行、两种判定

同样的 5 行 CSV，四次运行的结果：

| 运行 | staging | quarantine | 有没有 `import_log` | 李四（状态为空）去向 |
|---|---|---|---|---|
| A | 3 | 2 | 无 | 隔离 |
| B | 4 | 1 | 有 | 入 staging，status = NULL + notes |
| C | 3 | 2 | 无 | 隔离 |
| D | 4 | 1 | 无 | 入 staging |

**两种判定都说得通，但模型每次自己选了一个，没有问。** 这恰好印证指南 §5.1 第 5 步「只询问影响结果且无法从证据确定的语义」为什么是必需步骤：这不是模型能力问题，是**业务语义本来就该由用户定**。

对后续的直接影响：

- S4 的 verifier **不能把某一种分法当唯一正确答案**，否则会把正确行为判错；
- 「状态为空算不算待确认」必须变成一条**用户确认项**，确认后写进标注与规则，而不是每次让模型即兴决定；
- 评测指标里需要「同一任务多次运行的一致性」这一项，指南 §10 的指标清单里目前没有它。

### 4.4 模型实际做对了的地方（有证据）

- `2026/3/1` → 归一化为 `2026-03-01`，同时**保留 `raw_hire_date = 2026/3/1`**；
- **`2026-03-05` 没有因为格式不同被丢弃**（关键：pandas 默认 `pd.to_datetime` 会从首值推断单一格式而把它判成 `NaT`，见 `runtime-env/smoke.py` 固化的同一陷阱）；
- 运行时先 `bash` 探环境、用 `od`/`xxd` 看原始字节确认编码，**写完 `import.py` 后还用 `edit` 改了两处**——自我纠正行为可从时间线直接看到；
- 隔离行保留 `raw_line` 原文，而不是只存解析后的字段。

由此要补进指南 §5.2 清单的一条：**「空值是否等价于缺失业务状态」**。原清单列了九类不可静默处理的情况，但没有这一类，而它恰恰每次都会触发不确定性。

## 5. 明确缺口

每条都写清「没验证什么」，不用「大概可以」收尾。

1. **网络决定了 provider 选择，与最初决定不同。** 直连 `api.deepseek.com` 在本机 TLS 握手失败（PowerShell 与 curl/schannel 均报 `SEC_E_INTERNAL_ERROR`），因此 `DEEPSEEK_API_KEY` 这条路径**不可用**。实际使用的是 `opencode-go` + `OPENCODE_API_KEY`（即本机 DSH 自己在用的凭证）。**需要用户确认这是长期采用还是临时绕行。**
2. **`RpcClient` 的环境变量是「合并」而不是「替换」。** 已读源码确认其 `spawn` 使用 `{ ...process.env, ...options.env }`。因此真正的白名单只能靠临时替换 `process.env` 实现（本 spike 的做法），这在一个有并发 session 的 Harness 里是**不可接受的**。生产需要：自己 spawn CLI，或给 pi 提交可传入完整 env 的选项。
3. **取消只证明了「直接子进程被打断」。** 没有验证后代进程是否全部回收（例如 bash 里再起后台进程、或 Python 再 spawn 子进程）。指南 §8 明确要求不得未经平台验证就承诺杀掉所有后代——这里就是那个未验证点。
4. **并发未验证。** §3.3 要求「同一 session 的数据修改串行执行，多个 session 可并发但不共用同一个可写数据库文件」。本轮全部是单进程串行，**没有任何串行化机制的实现或测试**。
5. **数据库快照未验证。** §6.3 要求「停止写入后备份或关闭连接后复制」。本轮没有实测 WAL 模式下热复制的完整性，只有一个空库级别的常识。
6. **`getState()` 的形状会变。** `--no-session` 时没有 `sessionFile` / `sessionName` 字段，加 `--session-dir` 后才有。恢复定位**不能假设字段恒定**，必须按存在性处理。
7. **事件已能完整落盘，但分类的语义边界仍未验证。** 现在 `events.jsonl` 保留全部 986 条原始记录。仍**未验证**：compaction、auto-retry 成功恢复、长任务中途观测在真实业务里的行为（失败那次重试出现过 `auto_retry_start` ×3，但没有一次成功恢复的样本）。另外事件字段**没有对外契约**，本 spike 的字段抽取（`toolName` / `role` / `result`）是防御性的，pi 升级后可能静默取不到值。
8. **pi 的沙箱扩展未评估。** 包里带 `examples/extensions/sandbox`，可能提供比「白名单 + 独立 cwd」更强的边界。本轮没有看它的实现和限制。
9. **`--offline` 的具体语义未深究。** 只验证了它与模型调用兼容，没有验证它到底关掉了哪些启动期网络行为。
10. **Windows 上的 §8 限制条款尚未写成文。** S3 的 README 需要按本机实测重写「不能保证」清单，本轮只确认了「原生 Windows + Git Bash」这个前提。
11. **同一输入的多次运行结论不一致**（§4.3 已给出四次运行的证据）。这意味着：正确性不能靠「跑一次看着对」，必须靠独立 verifier + 用户确认；而 §10 的指标清单里缺少「多次运行一致性」这一项。
12. **会话文件的完整性依赖 pi 的写入时机。** 本轮都在 `client.stop()` 之后才导出 HTML；如果进程被强杀，会话文件是否仍完整**未验证**——这直接影响 §3.2「进程重启后恢复到确定节点」能不能成立。
13. **工作区写入约束：已调研，本轮决定不做。** 结论有三条，供后续决策复用：
    - pi 的 `sandbox` 扩展**在 Windows 上不可用**：源码 `examples/extensions/sandbox/index.ts:251-256` 在 `platform` 不是 `darwin`/`linux` 时直接把 `sandboxEnabled` 置 false；其底层 `@anthropic-ai/sandbox-runtime`（v0.0.81）包装的是 macOS `sandbox-exec` 与 Linux `bubblewrap`，Windows 无同形状原语。
    - 可行但**不完整**的做法是扩展 `tool_call` 钩子拦截 `write`/`edit` 的越界路径（`examples/extensions/protected-paths.ts` 就是这个形状）；它**拦不住 `bash` 及任何子进程**，因此只能算「防手滑」，不能当安全边界。
    - 真边界只有四条路：WSL2、容器、Windows 原生令牌/AppContainer、独立用户账户 + ACL。各自代价见会话记录；指南 §3.3 明确首版不引入容器。

