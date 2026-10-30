# 真实数据演示台（临时脚手架）

> **状态：已被 [packages/harness](../../packages/harness/src/index.ts) 接管（ADR-0006）。**
> lifecycle 命令（setup/run/verify/publish/status/author）现在走 harness；
> 这里保留两个仍然有用的东西：**独立核对脚本** [tools/verify_outputs.py](tools/verify_outputs.py)
> 与[这次运行的结果](RESULTS.md)。harness 在同一份 `control.db` 上重跑 `analyze`，
> 四个产出与该脚手架当时的产物 sha256 逐文件相同。

> 这不是产品代码。它存在的目的是：用 `ground_true_excel/wenxin` 里的**真实 HR 导出文件**，
> 把已经交付的 session / runtime 能力跑一遍，看看在真数据上是什么样子。

## 为什么是一堆子命令

状态（program / session / run / data revision / artifact / publish）全在
`<store>/control.db` 里，脚本本身无状态。所以：

- 中途挂了，重跑同一条命令即可，不用从头上车；
- 每跑一条命令都能单独看到它改了什么；
- 「上次跑到哪」不是靠对话记忆，是查库。

## 数据

| 项 | 值 |
|---|---|
| 来源 | `ground_true_excel/wenxin/`（HR 手工流程的真实导出） |
| 输入批次 | 5 个文件、75.9 MiB，按内容寻址复制到 `work/inputs/<batchId>/` |
| 固定业务日期 | `AS_OF=2026-10-23`（导出日；按系统时间算「当日」会筛出 0 人） |
| 业务库 | `work/hr.db`（WAL） |

## 两个阶段的入口契约

程序由模型写，但**调用方式由编排器定**，模型不能改：

```
<python> import_hr.py --inputs <DIR> --db <PATH>
<python> analyze.py   --db <PATH> --out <DIR> --as-of <YYYY-MM-DD>
```

路径全部从参数传入，所以程序可以被放在任意干净 worktree 里执行。

## 命令

```powershell
node spikes/demo-real-data/src/main.ts setup                       # 程序仓库 + 会话 + 输入批次（幂等）
node spikes/demo-real-data/src/main.ts author --prompt=... --label=import
node spikes/demo-real-data/src/main.ts run    --stage=import       # 在干净 worktree 里按 commit 执行
node spikes/demo-real-data/src/main.ts verify --stage=import       # 同 commit 跑两次比内容哈希
node spikes/demo-real-data/src/main.ts publish --verify=<verify.json>
node spikes/demo-real-data/src/main.ts status                      # 恢复视图（四个版本引用）
node spikes/demo-real-data/src/main.ts db | damage | restore       # 看库 / 制造误操作 / 从快照恢复
node spikes/demo-real-data/src/main.ts lock --probe                # 会话锁：证明写执行是串行的
```

需要环境变量 `OPENCODE_API_KEY`（只有 `author` 需要，`run`/`verify` 不需要——干净重跑不带凭证）。

## 独立核对

```powershell
uv run --project runtime-env python spikes/demo-real-data/tools/verify_outputs.py `
  --db C:/project/vibe_excel-store/sessions/<id>/work/hr.db `
  --out C:/project/vibe_excel-store/sessions/<id>/work/out/analyze
```

这份脚本**不读导入程序**，直接从原始 Excel 重算一遍再对答案（第二实现）。
其中「PN余量」这张 18 行的人工作业看板可以当独立标签用：它给了 18 个分组的
`剩余空号` 与 `本批已用`，程序算出来的值要和它对上。
