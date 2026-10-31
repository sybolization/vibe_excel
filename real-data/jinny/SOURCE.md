# jinny 真实输入表

这里留档的是 `better-operate-doc-jinny`（MFG 招聘周报工具）的**真实输入数据**，不是合成夹具。

- 来源：`C:\project\better-operate-doc-jinny\original-data`
- 复制时间：2026-10-31
- 放入本仓库的原因：用户明确要求把这份真实数据表随私有仓库一起保存（`.gitignore` 里只对本目录开了 `*.xlsx` 例外）。

| 文件 | 用途 |
|---|---|
| `Recruitment Data-v1 (4).xlsx` | 招聘台账原始导出（候选人/岗位/状态） |
| `MFG Hiring Progress.xlsx` | 上周的招聘进度基线 |
| `Dashboard Data.xlsx`、`Dashboard Data 3.xlsx`、`Dashboard Data 10-26.xlsx` | 周报三张透视表的数据源（逐版更新） |
| `hr自述操作流程.md` | HR 口述的现行周报流程（输入/输出/口径） |

## 注意

- 内容为**真实员工/候选人数据**，含个人信息，只应存在于这个 private 仓库与本地存储；不要改公开、不要贴进 issue/PR、不要转给外部。
- 本目录是数据快照，业务上以 `better-operate-doc-jinny` 的 `original-data` 为准；更新时直接覆盖同名文件并提交即可。
- 与 `ground_true_excel/`（按 `.gitignore` 约定只留本地）不同，这份数据是**有意**入库的，见根 `.gitignore` 的例外说明。
