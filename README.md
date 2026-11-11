# Vibe Excel Python
> 这是 `python` 分支：Python 版自成一体，项目根就是这个目录。
> `main` 分支上的 `python-replica/` 与本分支同源，主分支以那份为准。

现有 Vibe Excel 项目的独立 Python 实现，依赖由 uv 管理。
核心执行与 HTML 导出只需要 Python 和 Git，不依赖原始代码检出或 Node。
可选的 pi RPC 集成需要 Node 和 pi。

## 启动

```powershell
cd C:\project\vibe_excel-python
.\start.ps1
```

默认：找到 store → 起本地服务 → 打开 `http://127.0.0.1:8731/`。
双击 `start.cmd` 等价于不带参数运行，能绕开脚本执行策略。

**为什么要起服务**：页面上「导入 / 移出 / 撤回 / 重建输入批次」这些动作要写磁盘
（ground-truth 目录、清单名单、store 里的归档与事件），静态 HTML 做不到。
双击导出的 `index.html` 仍然能看，只是这些按钮会置灰并说明原因。

| 参数 | 作用 |
| --- | --- |
| `-Store <目录>` | 指定 store，**可给多个**，合并成一张项目列表 |
| `-Root <目录>` | 换个位置找 store，默认是仓库的上一级 |
| `-Build` | 只导出静态站点到 `..\site\`，不起服务（发给别人看用） |
| `-Port` | 服务端口，默认 8731 |
| `-Full` | 从零跑一轮：setup → run → publish（只接一个 store） |
| `-Status` | 只打印每个 store 的状态，不生成文件 |
| `-Test` | 跑测试 |
| `-NoOpen` | 生成但不打开浏览器 |

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

## 构建期输入（导入 / 移出 / 撤回）

只在项目页、只在服务模式下可用。导入的文件写进清单 `sessions.inputs.dir` 指向的目录，
同时把文件名登记进 `inputs.files`——`setup` 只认这份名单，不登记等于没导入。

移出不是真删：文件先进 `<store>/.trash/<时间戳>/`，同目录写一份 `packet.json`
（文件元信息、它物化出的表、受影响的链路、移出理由）；重建批次成功后转进
`<store>/.archive/<时间戳>/` 长期保留，agent 改脚本时还能读回原文。
事件流水在 `<store>/input-events.jsonl`。

页面上「已移出」的链路会整条变灰但仍可点开——历史是历史，不能装作没发生。

## 只读导出

```powershell
# 站点（推荐）：一个目录，索引 + 每个项目一页 + 字体，离线可开
uv run python -m vibe_excel.book build --store C:\data\store --out .\exports\site

# 多个 store 合成一张列表（--store 可以重复）
uv run python -m vibe_excel.book build --store C:\data\a --store C:\data\b --out .\exports\site

# 单页：只出某一个项目的账页，样式与站点里的项目页相同
uv run python -m vibe_excel.book build --store C:\data\store --out .\exports\book.html

uv run python -m vibe_excel.sqlmap build --db C:\data\hr.db --lineage .\lineage.json --csv C:\data\reports --out .\exports\sqlmap
uv run vibe-encode record --run .\runs\author-01 --id import-01 --family import --question "把 HR 导出表建成 hr.db" --corpus .\corpus
```

`--out` 以 `.html` / `.htm` 结尾时出单页，否则当作站点目录。站点目录的结构：

```
site/
  index.html      项目列表：每个项目一行，点进去
  p-<id前8位>.html  一个项目的账页（页面左上角有返回）
  fonts/          鸿蒙字体 + 许可
```

顶部黑色导航条两端都能切换深色 / 浅色，选择记在浏览器里。

## 字体

页面用 **HarmonyOS Sans SC**，原版 TTF 随包分发在 `src/vibe_excel/assets/book/fonts/`，
`book build` 时会原样复制到 `fonts/`。

- 不做子集化、不转格式：`HarmonyOS Sans Fonts License Agreement` 第 2 条禁止对字体做任何修改。
- 版权声明与协议随字体一起走（`fonts/LICENSE.txt`，第 4 条）。
- 只随软件分发，不单独分发字体（第 3 条）。
- 只带 Regular / Medium 两个字重，600 由 Medium 合成。

## 边界

- 业务库是 SQLite，Excel 只是边界格式；平台不解析公式语义，也不推断 SQL 血缘。
- 双跑比对范围是声明的产出（`flow.outputs` 表 + `out` 目录文件），不是整库哈希。
- 发布证据绑定代码提交：证据过期时拒绝合入，而不是静默使用旧证据。
- HTML 导出内嵌全部 CSS/JS/JSON，可离线打开；站点模式的字体是侧车文件（体积原因），其余仍然内嵌。
