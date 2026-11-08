# Vibe Excel Python

现有 Vibe Excel 项目的独立 Python 实现，依赖由 uv 管理。
核心执行与 HTML 导出只需要 Python 和 Git，不依赖原始代码检出或 Node。
可选的 pi RPC 集成需要 Node 和 pi。

## 快速开始

```powershell
uv sync --locked
uv run vibectl plugins
uv run vibectl setup
uv run vibectl run
uv run vibectl publish
uv run pytest
```

`setup` 会创建程序仓库、会话、输入批次与空库基线；`run` 从同一份数据快照跑两遍并比对声明的产出；
`publish` 只接受带「两次一致」证据、且证据提交等于当前草稿 HEAD 的运行。

## 命令

| 命令 | 作用 |
|---|---|
| `setup` | 建程序仓库 / 会话 / 输入批次 / 空库基线（幂等） |
| `author` | 让模型写程序并提交到草稿分支（需要 `--prompt=<文件>`） |
| `run` | 跑整条工作流两遍（物化 → 节点），比对声明的产出 |
| `flow` | 打印草稿里的工作流：物化 / 节点 / 边 / 可优化项 |
| `publish` | 只发布带「两次一致」证据的运行（可带 `--run=<id>`） |
| `optimize` | 默认只读评审；`--apply=<id>` 才实现被批准的建议 |
| `status` | 恢复视图：代码版本 + 数据版本 + 输入批次 + 制品 |
| `db` | 打印业务库的语义摘要（每表行数 + 内容哈希） |
| `plugins` / `help` | 宿主命令：插件状态与命令说明 |

## 配置

清单是 `vibe.plugins.yml`，支持 `${VAR}` / `${VAR:-默认值}` 插值。常用环境变量：

| 变量 | 含义 |
|---|---|
| `VIBE_EXCEL_STORE` | 制品根目录（默认 `.store`） |
| `VIBE_EXCEL_PYTHON` | 数据运行时解释器的绝对路径（`exec` 插件必需） |
| `VIBE_EXCEL_PI` | pi CLI 路径（`author` / `optimize` 需要） |
| `VIBE_EXCEL_GROUND_TRUTH` | 输入批次目录（`setup` 必需） |
| `VIBE_EXCEL_AS_OF` | 固定业务日期（默认 `2026-01-01`） |
| `VIBE_EXCEL_MANIFEST` | 覆盖默认清单路径 |

## 只读导出

```powershell
uv run python -m vibe_excel.book build --store C:\data\store --out .\exports\book.html
uv run python -m vibe_excel.sqlmap build --db C:\data\hr.db --lineage .\lineage.json --csv C:\data\reports --out .\exports\sqlmap
uv run vibe-encode record --run .\runs\author-01 --id import-01 --family import --question "把 HR 导出表建成 hr.db" --corpus .\corpus
```

## 边界

- 业务库是 SQLite，Excel 只是边界格式；平台不解析公式语义，也不推断 SQL 血缘。
- 双跑比对范围是声明的产出（`flow.outputs` 表 + `out` 目录文件），不是整库哈希。
- 发布证据绑定代码提交：证据过期时拒绝合入，而不是静默使用旧证据。
- HTML 导出内嵌全部 CSS/JS/JSON，可离线打开；前端资产与原版逐字节一致。
