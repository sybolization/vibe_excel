# @vibe-excel/encoder

把**一次运行**编码成可评估的案例。它不包装 Excel、不做预解析：agent 用 Python 直接读写表格才是最佳介质，这一层只负责把已经发生的事实收拢成结构。

## 四块内容

| 文件 | 职责 |
|---|---|
| `src/events.ts` | 解析 `events.jsonl` → 工具调用序列（配对、退出码、耗时、参数哈希、输出预览）+ 用量（token / 成本） |
| `src/claims.ts` | 业务语义的 append-only 日志：断言 → 确认 / 推翻，保留全部历史 |
| `src/trace.ts` | 上面两者 + 口径快照 + 结果标签 = 一个案例（`trace.json`） |
| `src/corpus.ts` | 案例入库、去重、索引指纹 |

## 为什么语义是文件，不是界面、也不是 IR

1. agent 的工作介质就是文件：它能用 `>>` 追加、用 `grep` 查、用 `git` 记录，不需要任何新工具；
2. 人确认语义的动作也只是**再追加一行**，不需要 UI；
3. append-only 意味着"谁在什么时候改成什么"是可追溯的事实，`reject` 之后原断言仍在，不会被悄悄抹掉；
4. 之后做 RSI 时，"哪条语义被推翻过"本身就是最有价值的信号。

## claims 约定

位置建议与程序代码放在一起（它属于业务语义，应该被 git 管理）：

```text
<program-repo>/semantics/claims.jsonl
```

录入/分析两轮提示词（`spikes/demo-real-data/prompts/01-import.md`、`02-analyze.md`）已经要求 agent 在运行中自行追加。
`commitDraft` 走 `git add -A`，所以这些行会随草稿提交，语义因此和代码同版本——"这次运行用的是哪版口径"由 commit 就能回答。

`evidence` 可以写成一个字符串，解析时按一条证据归一成数组（agent 手写 JSON 最常漏的是方括号）。

每行一个事件（JSON）。`op` 只有三种：

```json
{"op":"claim","id":"c1","kind":"alias","subject":"部门","value":"HRBP→人力资源业务伙伴","source":"agent","evidence":["timeline#12"],"at":"..."}
{"op":"confirm","id":"c1","by":"jinny","note":"对，别名表就是这个","at":"..."}
{"op":"reject","id":"c2","by":"jinny","note":"试用期要算，按 budget 口径","at":"..."}
```

`kind` 是封闭枚举，含义固定：

| kind | 用来记什么 |
|---|---|
| `alias` | 源系统写法 → 业务实体（部门、岗位、公司别名） |
| `field-meaning` | 这一列到底是什么、单位是什么 |
| `measure-definition` | 指标口径：含不含谁、以哪一列为准 |
| `join` | 两张表靠什么字段关联、是否唯一 |
| `filter` | 哪些行不算数（离职、重复投递、测试数据） |
| `unit` | 千元/元、人/人次、百分比 0..1 还是 0..100 |
| `exception` | 明确不合口径但必须保留的记录 |

规矩：**同一 id 只能 claim 一次**。改口要 `reject` 旧的、再 claim 新的——覆盖会让"被推翻过"这个信号消失。被 reject 的 id 不能直接 confirm。

## 命令

```powershell
$cli = "packages/encoder/src/main.ts"

# 语义沉淀（agent 与人都用同一组命令）
node $cli claims add     --file claims.jsonl --id c1 --kind alias --subject 部门 --value "HRBP→人力资源业务伙伴" --evidence "timeline#12"
node $cli claims confirm --file claims.jsonl --id c1 --by jinny --note "对"
node $cli claims reject  --file claims.jsonl --id c2 --by jinny --reason "试用期要算"
node $cli claims show    --file claims.jsonl

# 记一次运行（--claims 可选；没有 claims 的案例在 RSI 里只能算"待判"）
node $cli record --run <run 目录> --id molex-import-author-01 --family molex-import `
  --question "把 5 份 HR 导出表建成 hr.db" --corpus <制品根>/corpus `
  --claims claims.jsonl --outcome partial --note "authoring 阶段"

# 看案例库与指纹（切分测试集前把它公开，事后可证明对照组没被动过）
node $cli list   --corpus <制品根>/corpus
node $cli digest --corpus <制品根>/corpus
```

## 案例的不可变性

- `cases/<id>/trace.json` 写完不改。要修正结论就换 id（`<id>-r2`）。
- 同 id + 同哈希重复写是幂等的（`recordedAt` 不参与哈希）；同 id + 不同内容直接报错。
- 语料库一旦允许"悄悄覆盖"，切出来的测试集就不再可信。

## 刻意不做的事

- **不做 Excel 预解析、不做 IR**：Python 已经是最好的环境交互方式，中间再套一层契约只会挡路。
- **不做切分**：7/3 是语料够量之后的事，现在 `family` 字段已经把分组键准备好了。
- **不推断结果标签**：`outcome` 必须由人或外部核对给出；从运行自身推断"成功"会把错觉写进语料。
