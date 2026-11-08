# 编码器

这个独立、仅依赖标准库的编码器把已有的运行目录转换为可评估的案例。它不解析 Excel，也不在运行时调用 Node、TypeScript 或原项目。

在 `python-replica` 目录下，使用由 uv 管理的环境：

```powershell
uv run vibe-encode claims add --file claims.jsonl --id c1 --kind alias `
  --subject 部门 --value "HRBP→人力资源业务伙伴" --evidence "timeline#12"
uv run vibe-encode claims confirm --file claims.jsonl --id c1 --by jinny --note "对"
uv run vibe-encode claims reject --file claims.jsonl --id c1 --by jinny --reason "旧口径"
uv run vibe-encode claims show --file claims.jsonl

uv run vibe-encode record --run .\runs\author-01 --id import-01 --family import `
  --question "把 HR 导出表建成 hr.db" --corpus .\corpus --claims claims.jsonl `
  --outcome partial --score 0.8 --category incomplete --note "外部核对结果"
uv run vibe-encode list --corpus .\corpus
uv run vibe-encode digest --corpus .\corpus
```

`uv run python -m vibe_excel.encoder` 支持同样的命令。`record`、`list` 和 `digest` 必须提供 `--corpus`。重复的 `--evidence` 标志用于收集多个指针；其他重复标志则取最后一个值。

## Python API

```python
from vibe_excel.encoder import (
    BuildTraceOptions, TraceOutcome, build_trace, fold_claims, parse_claims,
    save_trace, trace_hash,
)

claims = fold_claims(parse_claims(
    '{"op":"claim","id":"c1","kind":"unit","subject":"金额","value":"元"}'
))
trace = build_trace(BuildTraceOptions(
    run_dir=r".\runs\author-01",
    id="import-01",
    family="import",
    question="把表入库",
    outcome=TraceOutcome(label="partial"),
    claims=claims,
))
result = save_trace(r".\corpus", trace)
payload = trace.to_dict()  # 外部 JSON 保留 camelCase 字段名。
fingerprint = trace_hash(trace)
```

API 与 dataclass 属性使用 snake_case。`InteractionTrace.from_dict()` 和 `read_trace()` 从外部 JSON schema 还原带类型的记录。`extract_run()` 暴露步骤、消息计数、token/成本总计、坏行计数和事件计数。`run_cli(argv: list[str]) -> int` 返回状态码而不终止调用方；控制台入口点是 `vibe_excel.encoder.cli:main`。

## 保留的规则

- 按 `toolCallId` 配对工具调用。未配对的结束事件仍会展示；未完成的开始事件会成为失败/未完成的步骤。缺失的结构化 `exit_code` 保持缺失。`isError` 与退出状态是两个独立事实。
- 参数哈希会递归排序对象键；预览折叠空白并在 220 个 UTF-16 代码单元处截断。成本四舍五入到六位小数。
- 声明是只追加的。每个 ID 只能声明一次；重复确认会保留历史。已拒绝的 ID 不能再确认：先拒绝旧声明，再创建 **新 ID**。`evidence` 接受字符串或字符串数组。
- 来源与策略快照来自 `author.json`、`verify.json` 或 `run.json`，按此优先级判定；其中 `provider`、`model`、`piVersion` 与 `promptFile` 只从 `author.json` 读取。验证目录不需要事件流。只有顶层文件会成为带哈希的产物；嵌套的业务数据库被排除。提示词哈希反映提示词的当前内容，缺失时为空哈希。
- 结果是 `pass`、`partial` 或 `fail`，可附带 `0..1` 的外部评分和失败类别。工具成功和 `verify.identical` 都不能推断结果。CLI 保留原有保守的 `partial` 默认值。
- 案例是不可变的：`cases\<id>\trace.json` 永远不会被覆盖。相同 ID/哈希即使 `recordedAt` 不同也视为未变；内容变化需要新的 ID。`index.jsonl` 只追加。其摘要完全取决于排序后的 ID/哈希对；格式错误的索引行会报告其物理行号。

声明类型：`alias`、`field-meaning`、`measure-definition`、`join`、`filter`、`unit`、`exception`。失败类别：`schema-misread`、`alias-ambiguity`、`join-wrong`、`type-coercion`、`unit-confusion`、`filter-rule`、`tooling-error`、`incomplete`、`other`。

定向验证：`uv run pytest tests\test_encoder.py`。
