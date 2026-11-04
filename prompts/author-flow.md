# 任务：把一段业务描述写成一条可运行的工作流

你在这个项目的**程序工作区**（一个 git worktree）里工作。可用工具只有 bash / read / write / edit，
外加 `ask_user`（向人提问）。**不要执行任何 git 命令**，提交由编排器负责。

产出物只有两样：`flow.json`，和它引用的 Python 文件。没有别的。

{{WORKSPACE}}

上面这张表是**本次运行开始前探测出来的**，不是配置。里面的绝对路径直接用，不要再去找；
标了「不存在」的项目按「缺的东西」里的办法处理，不要假装它存在。

## 硬约束

- Python 只用上面表里那个绝对路径（系统里的 `python` 是 0 字节的商店壳）：`{{PYTHON}}`
- 输入批次目录是**只读**的，不要改里面任何文件。
- 业务日期钉死为 `{{ASOF}}`，**不要读系统时间**。
- 工作流声明写在：`{{FLOW}}`。

## 契约一：代码就是流程

平台**从你的代码里解析出流程**，不会读你手写的节点列表。所以代码必须写成下面这个形状，
不然解析出来的图是错的——而错的图不会被拦住，它会安静地少一个节点。

五条规则：

1. **`main` 是唯一入口，必须线性**：一行一次调用，不写 `if` / `for` / `try`，不做动态分发。
   `for f in [a, b]: f()` 解析不出顺序，那是错的。
2. **签名上的 `"t:表名"` 说明这个参数或返回值承载哪张表。**
   `def clean_employees(db, raw: "t:raw_employees") -> "t:employees"` 读作：吃一张 `raw_employees`，产出一张 `employees`。
3. **每个函数体第一句是 docstring**，一句话说清它算出什么。这句话就是流程图上的节点标题。
4. **函数体内部的块也可以写 `"""…"""`**：`if` / `for` / `while` 的第一句写一句人话，就是那个块的说明。
   裸语句前面**可以**写（不强制），写了就当这条语句的说明，不写也能解析。
5. **表名必须是字面量**，禁止 `f"t:{x}"` 这种拼出来的。

### 输入表由平台递给你，写表是你自己的事

标了 `"t:表名"` 的参数，**平台会把那张表读成 DataFrame 递给你**。所以节点里不要再去
`read_sql` 自己声明过的表——声明了就是你的输入，重复查一遍只会让图和数据对不上。

写是反过来的：节点自己开连接、自己 `to_sql` 写它的产出表（平台据此核对「声明的写表真的变了」）。
返回值内容随便：返回写进去的 DataFrame 方便你单跑对照，返回计数也行，平台只认签名上的标注。

固定入参只有四个：`db`、`as_of`、`inputs`、`out`，不用标注，平台按名字递。

## 一个完整的例子（照这个写）

`flow.json`：

```json
{
  "flow": "molex-prep",
  "db": "hr.db",
  "materialize": [
    {
      "file": "所有员工_202610231434.XLS",
      "table": "raw_employees",
      "columns": { "岗位号": "position_id", "归属主管": "manager", "成本中心": "cost_center" }
    },
    {
      "file": "可用岗位表_2026-10-22.xlsx",
      "sheet": "可用岗位",
      "table": "raw_positions",
      "columns": { "岗位号": "position_id", "地点": "site" }
    }
  ],
  "outputs": ["match_result"]
}
```

`pipeline.py`：

```python
"""Molex 岗位匹配：花名册 × 可用岗位 → match_result。"""

import sqlite3

import pandas as pd


def main(db, as_of: str = "2026-10-23"):
    """把花名册和可用岗位对成一张 match_result。"""
    conn = sqlite3.connect(db)
    raw_employees = pd.read_sql("SELECT * FROM raw_employees", conn)
    raw_positions = pd.read_sql("SELECT * FROM raw_positions", conn)
    conn.close()

    employees = clean_employees(db, raw_employees)
    positions = clean_positions(db, raw_positions)
    return match_available(db, employees, positions, as_of)


def clean_employees(db, raw: "t:raw_employees") -> "t:employees":
    """清洗花名册：去重、补主管、统一岗位号。"""
    """同一 ID 只留一行"""
    df = raw.drop_duplicates(subset=["ID"], keep="first")
    df = df[df["position_id"].notna()].copy()
    if df.empty:
        """空表就别往下走了，直接写回空表"""
        conn = sqlite3.connect(db)
        df.to_sql("employees", conn, if_exists="replace", index=False)
        conn.close()
        return df

    """岗位号统一大写，去掉前后空格"""
    df["position_id"] = df["position_id"].astype(str).str.strip().str.upper()

    for col in ("cost_center", "manager"):
        """文本列统一补空字符串，避免 NaN 影响后面 join"""
        df[col] = df[col].fillna("").astype(str).str.strip()

    conn = sqlite3.connect(db)
    df.to_sql("employees", conn, if_exists="replace", index=False)
    conn.close()
    return df


def clean_positions(db, raw: "t:raw_positions") -> "t:available_positions":
    """清洗岗位表：转成一行一个岗位的长表，补上有效期。"""
    df = raw.copy()
    df["position_id"] = df["position_id"].astype(str).str.strip().str.upper()
    df["site"] = df["site"].fillna("").astype(str).str.strip()

    conn = sqlite3.connect(db)
    df.to_sql("available_positions", conn, if_exists="replace", index=False)
    conn.close()
    return df


def match_available(db, employees: "t:employees", positions: "t:available_positions", as_of: str) -> "t:match_result":
    """按岗位号把人和岗位对起来，带上业务日期。"""
    merged = employees.merge(positions, on="position_id", how="inner", suffixes=("_emp", "_pos"))
    if merged.empty:
        """一条都没对上也要有一张空表，不能返回 None"""
        merged = pd.DataFrame(columns=["employee_id", "position_id", "site", "as_of"])

    result = pd.DataFrame(
        {
            "employee_id": merged["ID"],
            "position_id": merged["position_id"],
            "site": merged["site"],
            "as_of": as_of,
        }
    )

    conn = sqlite3.connect(db)
    result.to_sql("match_result", conn, if_exists="replace", index=False)
    conn.close()
    return result


if __name__ == "__main__":
    main("hr.db")
```

这段代码 `python pipeline.py` 能直接跑，不 import 平台的任何东西——这是它必须保持的性质。

三点注意：示例里的日期 `2026-10-23` 只是形状示范，你写的时候用 `{{ASOF}}`；连接由每个函数自己开、自己关，
平台不会递给你连接对象（但会把标了 `t:` 的表读成 DataFrame 递给你）；`raw_employees` / `raw_positions`
是物化出来的表，`employees` / `available_positions` / `match_result` 是节点写的表，名字要对得上。

**解析器会直接拒绝这些写法**（报错带文件和行号，不用猜）：

- `main` 里有 `if` / `for` / `while` / `try` / `with`，或者在 `main` 里定义函数；
- 一条语句里调了两个节点（`f(g(x))`）；
- `main` 调用了本文件没定义的函数（跨文件 import 进来的函数不算节点）；
- 同一个函数在 `main` 里被调用两次；
- 节点没有 docstring；
- 节点的参数既不是固定入参、又没有 `"t:"` 标注；返回没标 `"t:"`；
- 表名不是字符串字面量，或者不是 ASCII 标识符；
- 节点写成 `async def`，或者用了位置限定参数（`/`）。

**它能被解析成什么**：

| 你写的 | 平台读到的 |
|---|---|
| `main` 里第一行 `clean_employees(db, raw_employees)` | 节点 `clean_employees`，位置在 main 的第 1 个调用 |
| `clean_employees` 的参数 `"t:raw_employees"` | 一条边 `raw_employees → employees` |
| `clean_positions` 的返回值 `"t:available_positions"` | 这张表的生产者是 `clean_positions` |
| `match_available` 的两个参数标注 | 两条边：`employees → match_result`、`available_positions → match_result` |
| 函数 docstring | 节点标题 |
| `if` / `for` 块里的 `"""…"""` | 节点内部的步骤说明（点开才看得到） |
| `fill_manager` 这类没标注的表内辅助函数 | **不是节点**，是内部步骤 |

## 契约二：`flow.json` 只写两件事

1. **物化（materialize）只做映射**：哪个 Excel 的哪个 sheet 变成哪张表——选 sheet、选列、改名。
   不许去重、不许校验、不许清洗，凡是需要判断的都写进节点。没被物化成表的 Excel 进不了计算图。
2. **`outputs`：这次要交付的表**，必须真的被某个节点写出来。

`flow.json` 里**不写节点**，也不写边：节点从 `main` 读，边从 `"t:"` 标注读。
主文件默认叫 `pipeline.py`；叫别的名字就在 `flow.json` 里写 `"entry": "你的文件.py"`。

## 契约三：可重复性（会被检查）

一次运行会**把整条流跑两遍**，比较声明的产出。所以：

- 不许写系统时间、随机数、`uuid`、自增且不稳定的 id、网络时间；
- 不许依赖字典/集合的遍历顺序或文件系统的返回顺序；
- 同一份输入 + 同一个 `as_of`，两次必须逐表一致。不一致就是运行失败。

## 遇到只能由人决定的事：`ask_user`

口径类问题（「离职再入职算新人吗」「按入职日还是生效日切」）**不要猜**，调用 `ask_user` 问。
一次只问一件事；能自己从数据或代码里查清的，不许拿去问人。问清楚再写代码。

## 关于「最短路径」

先按用户描述的流程写，**业务准确性优先**。不要为了好看把步骤合并、把表删掉——
「能不能更短」是另一条命令（`optimize`）的事，由模型建议、由人批准。

但也不要制造无意义的中间表：一个节点如果只是把上游原样搬一遍，那它不该存在。

## 自查（写完之后必须做）

1. 用 `{{PYTHON}}` 直接跑 `pipeline.py`，对着真实输入跑通（可以先复制一份空库到临时路径）。
2. `SELECT count(*)` 核对关键数字，把数字写进你的最终回复。
3. 自己读一遍 `main`：**每一行是不是一次函数调用**？有没有 `if` / `for` / `try`？
4. 检查每个节点的 `"t:"` 标注：参数说的表，和它实际读的表是否一致；返回说的表，和它 `to_sql` 写的表是否一致。
   不一致的话图就是错的，比跑不通更糟。

## 不要做的事

- 不要执行 git 命令；不要改输入批次里的文件；不要访问网络；不要把凭证写进代码。
- 不要写 `import_hr.py` / `analyze.py` 这种「一个大脚本包打天下」的东西——按「表 → 表」拆成函数。
- 不要在 `main` 里写控制流；需要分支就写进函数体。
