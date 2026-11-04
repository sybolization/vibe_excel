# 任务：把 5 个真实 HR 导出文件录入 SQLite

你在 Windows 上工作，当前目录是这个项目的**程序工作区**（一个 git worktree）。
可用工具只有 pi 内置的 bash / read / write / edit，没有别的专用工具。
**不要执行任何 git 命令**——提交由编排器负责。

## 环境事实（是事实，不是建议）

- Python 只能用这个绝对路径（系统里的 `python` 是 0 字节的商店壳，跑不起来）：
  `{{PYTHON}}`
- 已安装：pandas 2.3.3、openpyxl 3.1.5、xlrd 2.0.2。`.XLS` 是老 BIFF 格式，只能用 xlrd 读。
- 输入目录（**只读**，不要改里面的文件）：`{{INPUTS}}`
- 业务库（你要创建的）：`{{DB}}`
- 固定业务日期 `AS_OF={{ASOF}}`

输入文件与真实规模：

| 文件 | 规模 | 备注 |
|---|---|---|
| `所有员工_202610231434.XLS` | 60248 行 × 85 列，60.7 MiB | sheet `SHEET1`，读一次约 10-20 秒，别反复重读 |
| `Position+-+Export+To+CSV_2026_10_20.xlsx` | 24365 行 × 18 列 | sheet `Sheet0` |
| `可用岗位表_2026-10-22.xlsx` | 可用岗位 6337 / 本批已发 159 / PN余量 18 | 三个 sheet |
| `New Hire_202610231439.XLS` | 25619 行 × 65 列 | sheet `SHEET1` |
| `Re Hire_202610231438.XLS` | 1048 行 × 69 列 | sheet `SHEET1` |

## 用户（HR）对这些文件的原始描述

> 1、从jiayang系统内导出所有员工，并在所有员工Excel中排除员工状态为离职、退休&用工类型为外包员工及外包员工2&员工级别为Exempt的员工，再筛选出入职日期为当日的人员。这批人员即为要生成Molex Id的人员。
> 2、新建一个Excel，将已经筛选过的所有员工（截取至员工性质（SAP））copy进Excel内，接着将PN Excel中将ZZDirectIndirect筛选为DL，将ActiveWorkAssignmentCount筛选为0，然后根据新建Excel中的Supervisor一列，将Position Number从PN Excel中一一对应Copy进position Numer一列。在这期间需要注意识别员工性质（SAP），如果为间接员工需要在PN Excel中将ZZDirectIndirect一列筛为IDL再进行获取PN。员工性质为直接员工：DL，员工性质为间接员工：IDL。
> 3、根据Excel或群内识别到的特殊岗位薪资手动修改为相应数额，其余均为2800。
> 4、从Jiayang系统中导出已经生成的New Hire和Re Hire模板，将已经整理好的Position Number和Pay以身份证号为识别依据Vlookup进模板内Position和PayRate两列。

这一轮只做**录入**，不做筛选、不做 Vlookup。

## 这轮要做的事

写 `import_hr.py`，把数据录入 `{{DB}}`。表名与来源固定如下：

| 表 | 来源 |
|---|---|
| `employees` | 所有员工 `SHEET1` |
| `positions` | Position export `Sheet0` |
| `available_positions` | 可用岗位表 / `可用岗位` |
| `sent_positions` | 可用岗位表 / `本批已发` |
| `new_hire` | New Hire `SHEET1` |
| `re_hire` | Re Hire `SHEET1` |
| `quarantine` | 任何解析失败、无法落入上面某张表的行 |

硬要求（逐条都要满足）：

1. 每张业务表都带来源定位列：`_source_file`、`_source_sheet`、`_source_row`（1 基，与原始文件行号一致，表头是第 1 行）。
2. 列名保留原始表头：中文就用中文（SQLite 里用双引号包起来），**不要自己翻译或改名**；表头重复时加 `_2`、`_3` 后缀。
3. 不能丢行：解析不了的行进 `quarantine`，写清 `_source_file` / `_source_row` / `_reason`。
4. 大文件必须完整读，不许截断（不要 `nrows=1000` 这种）。所有值**原样保留**，不要预先清洗、不要就地改类型。
5. 行数必须对上；如果确实有行进了 quarantine，把差额和原因说清楚，不要为了让数字好看而丢行。
6. 日期列在库里保留原值（Excel 序列号的 float 或原字符串），但要在报告里说明哪些列是序列号、换算基准是什么。
7. `可用岗位表` 里的 `PN余量` sheet 是人工维护的看板，这一轮**不要导入、不要读**——下一轮要拿它当独立对答案的基准。
8. 幂等：重复运行能从零重建出同样的库（先 DROP TABLE IF EXISTS 再建）。

## 语义沉淀（必须做，不是可选）

你在录入过程中做的每一项业务判断，都是这一轮最有价值的产出；但现在它只活在代码和你的思考里，人和下个月的重跑都看不见。所以：**每做出一项判断，立即追加一行**到 `semantics/claims.jsonl`（相对当前目录；首次追加前先 `mkdir -p semantics`）。

判断只有这七类，`kind` 只能取其中之一：

| kind | 记什么 |
|---|---|
| `field-meaning` | 这一列到底是什么、单位是什么 |
| `alias` | 源系统写法 → 业务实体（部门、岗位、公司别名的对应） |
| `measure-definition` | 一个数的口径：含不含谁、以哪一列为准 |
| `join` | 两张表靠什么字段关联、是否唯一 |
| `filter` | 哪些行不算数（离职、重复、测试数据） |
| `unit` | 千元/元、人/人次、百分比 0..1 还是 0..100 |
| `exception` | 明确不合口径但必须保留的记录 |

每行一个 JSON 对象，用 bash 追加（**不要**用 write 覆盖整个文件）：

```bash
mkdir -p semantics && echo '{"op":"claim","id":"c-001","kind":"field-meaning","subject":"入职日期","value":"Excel 序列号 float，基准 1899-12-30","source":"agent","evidence":["employees.SHEET1 第 12 列"]}' >> semantics/claims.jsonl
```

规矩：

- `id` 用 `c-001`、`c-002`… 递增，不要重复；
- `evidence` 必须指向回去能查的位置：文件加行号、源表加列名、或原始取值；
- **拿不准的也照写**，`source` 仍是 `agent`，由 HR 事后确认或推翻；漏写比写错更糟；
- 已经别处确认过的口径不必重复；同一件事不要写两条；
- 不要攒到最后一次性补，`evidence` 会失真；
- 不要执行 git 命令，`semantics/` 会随草稿一起被提交。

## 入口契约（编排器会一字不差地这样调用，不能改）

```
{{PYTHON}} import_hr.py --inputs <输入目录> --db <库路径>
```

- 缺少参数时打印用法并以 exit code 2 退出
- 所有路径从参数取，不硬编码、不依赖当前目录
- 正常结束 exit 0，失败 exit 非 0 并打印原因

## 交付

1. 用上面那条命令**真的跑一遍**（不是"我觉得能跑"），并确认 7 张表都有数据。
2. 把行数、列数、脏数据情况写进 `import_report.md`（中文）。要写：每张表多少行、进 quarantine 多少行及原因、哪些列脏（空值/掩码证件号/日期序列号/重复表头）、你怎么处理的。
3. 最后用 5-8 句话向我汇报**事实**。数据脏就说脏，没解决的就说没解决，不要美化。
