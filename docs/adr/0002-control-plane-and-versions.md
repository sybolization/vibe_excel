# ADR-0002：控制面存储与版本模型

- 状态：已接受
- 日期：2026-10-30
- 相关：指南 §3.1、§4、§6.1、§6.3、§11；ADR-0001（M0 接入形态）

## 1. 问题

指南 §12 S1 的验收要求是「进程重启后恢复到确定节点」，而 M0 结束时 Program / Session / Run / DataRevision **只有类型定义、没有任何存储**（见 STATUS.md）。要解决的问题有三个：

1. 这些事实存哪里，谁能写，写进去的是什么；
2. 代码版本和数据版本怎么配对，才不会出现「checkout 了代码就以为回滚了数据」；
3. 制品放在仓库外，靠什么引用它们。

## 2. 决策

### 决策 1：控制面用独立 SQLite（`<store>/control.db`），与业务库分开

控制面记录的是**任务事实与索引**（程序、会话、运行、数据修订、制品指针），业务库记录的是**业务数据**。两者生命周期、备份策略、损坏影响完全不同，因此**不放在同一个库里**。

- 控制面：`<store>/control.db`，WAL，单文件，事务保证「结束一次 run 并写入制品」是原子的。
- 业务数据：每个会话工作区里的 `work/*.db`，按需快照为 DataRevision（见 ADR-0003）。

选 SQLite 而不是 JSON 文件：一次 run 结束时需要同时更新 run 状态、写制品指针、更新 session 的 `updated_at`，JSON 做不到原子多记录写；而且恢复时要 join。单文件 SQLite 仍然可以整份拷走。

> 风险已记录：`node:sqlite` 在本机 Node 22.23.2 上是**实验特性**（有 ExperimentalWarning，实测 SQLite 3.51.3 可用）。若其 API 变更，替换为 `better-sqlite3` 只影响 `packages/session/src/store.ts` 一个文件——这是刻意收窄的耦合面。

### 决策 2：程序代码仓库在制品根下，git 由 session 包管理

`<store>/program-repos/<programId>/` 是一个独立 git 仓库，**只放程序代码**（Python / SQL / 页面 / 规则）。

理由（指南 §6.1）：原始 Excel、数据库、输出、标注、trace 不进代码仓库。把它们放在同一个仓库里再声称「agent 访问不到」是自欺——目录位置不是权限。

仓库初始化时创建 `main` 分支；每个会话从 `main` 切出自己的草稿分支（见 ADR-0004）。

### 决策 3：恢复节点 = 一个四元组，不是一次 checkout

一次可恢复的节点固定包含：

```
(codeCommit, dataRevisionId, inputBatchId) + 该 run 的制品
```

- `codeCommit`：草稿分支上的 commit sha，未提交时为空（表示"不可复现的未提交改动"）；
- `dataRevisionId`：业务库的一致性快照；
- `inputBatchId`：原始输入批次的标识，指向制品根的输入哈希。

**代码 checkout 不会恢复 SQLite**（指南 §6.3）。本设计里代码与数据是两条独立引用，`recoverSession()` 只负责把它们一起取出来给人看，恢复动作必须由调用方显式执行，不允许"顺手回滚"。

### 决策 4：数据修订是内容寻址的快照文件

`<store>/data-revisions/<sha256>.db`，用 SQLite 的 `VACUUM INTO` 生成（见 ADR-0003）。同一内容只存一份；内容相同即同一 revision 复用同一文件。数据库里记 `path / sha256 / bytes / parentId`。

`parentId` 允许表达「这次修订是从哪次改出来的」，但**不承诺可重放**——只有事实完整时才能重算（指南 §4.4）。

## 3. 制品根布局

```
<store>/                          # 默认 <repo>/../vibe_excel-store，VIBE_EXCEL_STORE 可覆盖
  control.db                      # 控制面（本 ADR）
  program-repos/<programId>/      # 程序 git 仓库
  sessions/<sessionId>/
    work/                         # 执行 cwd（业务库在这里）
    sessions/                     # pi 会话文件
    agent/                        # 每会话独立 PI_CODING_AGENT_DIR
    runs/<runId>/{events.jsonl,timeline.txt,artifacts/}
  data-revisions/<sha256>.db      # 业务库快照
  inputs/<inputBatchId>/          # 原始输入批次
```

## 4. 控制面表结构

```sql
programs(id PK, name, repo_path, main_branch, created_at)
sessions(id PK, program_id, name, draft_branch, status, created_at, updated_at)
runs(id PK, session_id, program_id, code_commit, input_batch_id, data_revision_id,
     status, started_at, finished_at, exit_code, note)
data_revisions(id PK, program_id, session_id, sha256, path, bytes, parent_id, created_at, note)
artifacts(id PK, run_id, kind, path, sha256, bytes, created_at)
meta(key PK, value)               -- schema 版本
```

约束：`data_revisions.sha256` 唯一；`runs.status` 只能从 `pending/running` 走向终态；终态不可回退（`succeeded|failed|cancelled`）。

## 5. 不做什么（明确边界）

- **不做**跨机器的分布式一致性；单机单用户原型。
- **不做**自动 git push 或远程同步（指南 §6.2）。
- **不承诺**控制面能自动修复：`control.db` 损坏时宁可拒绝启动并让人处理，也不静默重建（静默重建等于丢失版本历史）。
- **不承诺**「发布 = 正确」：发布记录的是**用户的授权动作**，不是正确性标签（指南 §12 S1 禁止项）。
