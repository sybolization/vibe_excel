# 任务：在已录入的库上做两件可核对的分析

环境事实同上一轮。库 `{{DB}}` 是上一轮 `import_hr.py` 的产物，**只读**，不要修改它（用 `file:...?mode=ro` 打开）。
输出目录 `{{OUT}}`（不存在就创建）。
`AS_OF={{ASOF}}` = 数据导出日，用户说的「当日」都指这一天。

## A. 人员筛选阶梯（用户流程第 1 步）

> 「在所有员工Excel中排除员工状态为离职、退休 & 用工类型为外包员工及外包员工2 & 员工级别为Exempt的员工，再筛选出入职日期为当日的人员。这批人员即为要生成Molex Id的人员。」

要交的**不是一个数，而是一架阶梯**：每一步剩多少人、每一步排掉了多少人。必须包含：

1. `status_counts`：`员工状态` 在库里真实出现的**全部**取值及各自行数
2. `employment_type_counts`：`用工类型` 的全部取值及行数
3. `level_counts`：`员工级别` 的全部取值及行数（注意有空串）
4. `onboard_on_as_of`：`入职日期` 等于 AS_OF 的人数（先在全部行上算，再在筛选后算，两个都要）
5. 两种口径的最终人数：
   - `strict`（严格字面）：只排除用户点名的 `离职`、`退休`、`外包员工`、`外包员工2`、`Exempt`
   - `conservative`（保守）：只保留明确算在职的（`在职`、`试用` 之类你自己判断并在报告里说明），且非外包、非 Exempt

**关键**：用户只点名了 5 个取值，库里还有 `待离职`、`退休返聘`、`实习`、`派遣员工`、`实习生`、`临时工/兼职` 等。
**不要替用户拍板**。两种都算，把差异列清楚，并把这个问题作为待确认项提给我。

## B. 岗位号（PN）余量看板重算

用户流程第 2 步要靠「可用岗位」里的空岗位号分配。这张看板目前是人手工维护的。
用库里的两张表独立算一遍（**不要读** 原 xlsx 里的 `PN余量` sheet，它是我们的对答案基准）：

- `available_positions`（可用岗位）
- `sent_positions`（本批已发）

按 `(归属主管, 成本中心, DL/IDL)` 分组，每组算：

- `remaining`：该组可用岗位数（按 `岗位号` 去重计数）
- `sent`：该组本批已发数（按 `岗位号` 去重计数）
- `under_threshold`：`remaining < 50` 记 1，否则 0

## 交付物（写到 `{{OUT}}`）

### 1. `summary.json` —— 固定结构，**必须确定性**

同一输入必须逐字节相同：不含时间戳、不含耗时、不含随机量，键按字母序，用 `json.dumps(..., ensure_ascii=False, sort_keys=True, indent=2)` 输出。

```json
{
  "as_of": "{{ASOF}}",
  "source_rows": { "employees": <int>, "positions": <int>, "available_positions": <int>, "sent_positions": <int>, "new_hire": <int>, "re_hire": <int>, "quarantine": <int> },
  "status_counts": { "<取值>": <int> },
  "employment_type_counts": { "<取值>": <int> },
  "level_counts": { "<取值>": <int> },
  "onboard_on_as_of": { "all_rows": <int>, "after_filter": <int> },
  "population": {
    "strict": { "count": <int>, "by_employment_type": { "<取值>": <int> } },
    "conservative": { "count": <int>, "by_employment_type": { "<取值>": <int> } }
  },
  "pn_groups": { "total_groups": <int>, "total_remaining": <int>, "total_sent": <int>, "under_threshold": <int> },
  "open_questions": [ "..." ]
}
```

### 2. `population.csv`

列固定：`rule,证件编号,姓名,报表部门,用工类型,员工级别,入职日期`（`rule` 取值 `strict` / `conservative`）。
UTF-8 无 BOM，逗号分隔，首行是表头。

### 3. `pn_available.csv`

列固定：`主管,成本中心,DL/IDL,remaining,sent,under_threshold`
所有组都输出（不只是低于预警线的），按 `remaining` 升序、同级按 `主管` 升序。UTF-8 无 BOM。

## 语义沉淀（必须做，不是可选）

这一轮的判断比上一轮更值钱：口径就藏在这里。**每一个你不得不自己决定的判断，都追加一行**到上一轮那份 `semantics/claims.jsonl`（相对当前目录；先看一眼现有文件，接着最大编号往后排，例如 `c-101`）。

必须写的几类：

- `measure-definition`：`strict` 与 `conservative` 各自的定义（点名排除了哪些取值、保留哪些、依据是什么）；
- `filter`：哪些取值算在职、哪些算离职、空串怎么处理；
- `alias`：分组键里出现的部门/主管写法，如果有别于源系统的写法；
- `unit`：剩余岗位数是「个」还是「人次」，百分比是 0..1 还是 0..100；
- `exception`：你决定不排除但明显可疑的取值。

规矩同上一轮：一行一个 JSON 对象，用 `>>` 追加，不覆盖；`evidence` 指向具体列或行；**拿不准的也照写**（`source` 用 `agent`），漏写比写错更糟。

这些断言会和 `open_questions` 一一对应：`open_questions` 是给人看的摘要，`claims` 是给机器比的真值。两个都要写，不要只写一个。

## 入口契约（编排器会一字不差地这样调用）

```
{{PYTHON}} analyze.py --db <库路径> --out <输出目录> --as-of <YYYY-MM-DD>
```

参数缺失打印用法并 exit 2；路径全部从参数取。

## 交付

1. 用上面那条命令**真的跑一遍**，确认三个文件都生成了。
2. 向我汇报：两个口径各多少人、总共多少个 PN 分组、低于预警线多少组、数据里发现的问题。
3. **待确认项单独列出来**（用户口径里没覆盖的取值、以及任何你不得不自己决定的判断）。不要自己拍板就完事。
