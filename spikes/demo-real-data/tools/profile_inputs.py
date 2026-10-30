"""把真实输入的结构事实打印出来（列名、低基数列取值、日期范围）。

只读，不改任何输入。输出为 JSON，便于后续脚本引用。
用法：uv run --project runtime-env python spikes/demo-real-data/tools/profile_inputs.py <输出 json 路径>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3] / "ground_true_excel"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("profile.json")


def read_xls(path: Path, sheet: str | None = None) -> list[list]:
    import xlrd

    book = xlrd.open_workbook(str(path))
    first = sheet or book.sheet_names()[0]
    sh = book.sheet_by_name(first)
    return [sh.row_values(r) for r in range(sh.nrows)]


def read_xlsx(path: Path, sheet: str | None = None) -> list[list]:
    import openpyxl

    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    sh = book[sheet or book.sheetnames[0]]
    rows = [list(r) for r in sh.iter_rows(values_only=True)]
    book.close()
    return rows


def cardinality(rows: list[list], col: int, top: int = 12) -> dict:
    from collections import Counter

    counter = Counter("" if r[col] is None else str(r[col]).strip() for r in rows[1:])
    return {"distinct": len(counter), "top": counter.most_common(top)}


def main() -> int:
    facts: dict = {}

    emp = ROOT / "wenxin" / "所有员工_202610231434.XLS"
    rows = read_xls(emp)
    header = [("" if v is None else str(v).strip()) for v in rows[0]]
    print(f"所有员工：{len(rows) - 1} 数据行，{len(header)} 列")
    facts["employees"] = {
        "dataRows": len(rows) - 1,
        "columns": header,
        "员工状态": cardinality(rows, header.index("员工状态")),
        "用工类型": cardinality(rows, header.index("用工类型")),
        "员工级别": cardinality(rows, header.index("员工级别")),
        "入职日期样本": [rows[i][header.index("入职日期")] for i in range(1, 6)],
    }
    for name in ("员工性质（SAP）", "员工性质", "supervisor", "position Numer", "Molex ID"):
        if name in header:
            print(f"  含列 [{name}]")

    pn = ROOT / "wenxin" / "Position+-+Export+To+CSV_2026_10_20.xlsx"
    rows = read_xlsx(pn)
    header = [("" if v is None else str(v).strip()) for v in rows[0]]
    print(f"\nPosition export：{len(rows) - 1} 数据行，{len(header)} 列")
    facts["positions"] = {
        "dataRows": len(rows) - 1,
        "columns": header,
        "ZZDirectIndirect": cardinality(rows, header.index("ZZDirectIndirect")),
        "ActiveWorkAssignmentCount": cardinality(rows, header.index("ActiveWorkAssignmentCount")),
        "HROrganizationUnit.Location": cardinality(rows, header.index("HROrganizationUnit.Location")),
    }

    for key, fname in (("newHire", "New Hire_202610231439.XLS"), ("reHire", "Re Hire_202610231438.XLS")):
        rows = read_xls(ROOT / "wenxin" / fname)
        header = [("" if v is None else str(v).strip()) for v in rows[0]]
        print(f"\n{fname}：{len(rows) - 1} 数据行，{len(header)} 列")
        facts[key] = {
            "dataRows": len(rows) - 1,
            "columns": header,
            "EffDate": cardinality(rows, header.index("EffDate"), 5) if "EffDate" in header else None,
        }

    avail = ROOT / "wenxin" / "可用岗位表_2026-10-22.xlsx"
    import openpyxl

    book = openpyxl.load_workbook(str(avail), read_only=True, data_only=True)
    facts["availablePositions"] = {}
    for name in book.sheetnames:
        sh = book[name]
        rows = [list(r) for r in sh.iter_rows(values_only=True)]
        header = [("" if v is None else str(v).strip()) for v in (rows[0] if rows else [])]
        facts["availablePositions"][name] = {"dataRows": max(0, len(rows) - 1), "columns": header}
        print(f"\n可用岗位表[{name}]：{max(0, len(rows) - 1)} 数据行，列={header}")
    book.close()

    OUT.write_text(json.dumps(facts, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
