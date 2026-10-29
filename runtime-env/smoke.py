"""数据运行时冒烟测试。

验证 M0 需要的三件事真的可用，并**记录**脏数据在默认导入路径下的真实行为：
  1. pandas + openpyxl 能写、能读 xlsx（含中文表头、合并单元格、填充色）；
  2. sqlite3 能建库、写入、查询；
  3. 日期与部门别名在默认路径下会怎样——记录事实，不做猜测也不静默修正。

用法：uv run python smoke.py
最后一行输出 SMOKE_OK 或 SMOKE_FAIL [...]，便于外壳做机器判断。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import openpyxl
import pandas as pd

# Windows 控制台默认是 GBK，中文会变成乱码，导致日志不可读。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        failures.append(name)


def main() -> int:
    print(f"python {sys.version.split()[0]}")
    print(f"pandas {pd.__version__}")
    print(f"openpyxl {openpyxl.__version__}")
    print(f"sqlite3 lib {sqlite3.sqlite_version}")

    with tempfile.TemporaryDirectory(prefix="vibe-excel-smoke-") as tmp:
        tmpdir = Path(tmp)
        xlsx = tmpdir / "in.xlsx"

        # 1) 写一个刻意脏一点的表：标题行 + 两行表头、合并单元格、三种日期写法、空值、颜色。
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "招聘"
        ws["A1"] = "2026 年招聘进展"
        ws.merge_cells("A1:D1")
        ws["A2"] = "姓名"
        ws["B2"] = "部门"
        ws["C2"] = "入职日期"
        ws["D2"] = "状态"
        ws["A3"], ws["B3"], ws["C3"], ws["D3"] = "张三", "研发", "2026/3/1", "已入职"
        ws["A4"], ws["B4"], ws["C4"], ws["D4"] = "李四", "研发部", "2026-03-05", None
        ws["A5"], ws["B5"], ws["C5"], ws["D5"] = "王五", "市场", "三月六日", "待确认"
        # 给“王五”整行涂灰：这类颜色在指南里只是证据，不是事实。
        for col in "ABCD":
            ws[f"{col}5"].fill = openpyxl.styles.PatternFill(
                fill_type="solid", start_color="FFD9D9D9", end_color="FFD9D9D9"
            )
        wb.save(xlsx)
        check("openpyxl 写 xlsx", xlsx.exists(), f"{xlsx.stat().st_size} bytes")

        # 2) pandas 读回来（header=1 跳过标题行）。
        df = pd.read_excel(xlsx, sheet_name="招聘", header=1)
        check("pandas 读 xlsx", len(df) == 3, f"{len(df)} 行 / 列 {list(df.columns)}")

        # 3) 日期列：记录默认路径的真实行为。
        #    pd.to_datetime 对混合格式默认只从首个值推断一种格式，
        #    于是 "2026-03-05" 会被判为无法解析——合法数据被丢掉，且不报错。
        raw_dates = df["入职日期"].tolist()
        print(f"  日期列原始值: {raw_dates!r}")
        naive = pd.to_datetime(df["入职日期"], errors="coerce")
        mixed = pd.to_datetime(df["入职日期"], format="mixed", errors="coerce")
        naive_bad = int(naive.isna().sum())
        mixed_bad = int(mixed.isna().sum())
        print(f"  默认推断 : {naive.tolist()}")
        print(f"  format=mixed: {mixed.tolist()}")
        check(
            "日期列：默认单一格式推断会丢掉合法日期（必须显式处理）",
            naive_bad == 2 and mixed_bad == 1,
            f"默认丢 {naive_bad} 个（含 1 个合法值），mixed 只丢 {mixed_bad} 个（三月六日，确应进隔离区）",
        )

        # 4) 颜色证据能读到（openpyxl 能，pandas 不能）。含义仍未知，只当证据。
        wb2 = openpyxl.load_workbook(xlsx)
        rgb = getattr(wb2["招聘"]["A5"].fill.start_color, "rgb", None)
        check("颜色可作为证据读取", rgb == "FFD9D9D9", f"A5 fill={rgb}（含义未确认，不参与统计）")

        # 5) SQLite：建库、写入、查询。
        db = tmpdir / "biz.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE staging_rows (row_no INTEGER, name TEXT, dept TEXT, raw_date TEXT)"
        )
        con.executemany(
            "INSERT INTO staging_rows VALUES (?,?,?,?)",
            [(i + 3, r["姓名"], r["部门"], r["入职日期"]) for i, r in df.iterrows()],
        )
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM staging_rows").fetchone()[0]
        depts = sorted(d[0] for d in con.execute("SELECT DISTINCT dept FROM staging_rows"))
        check("sqlite3 写入与查询", n == 3, f"{n} 行，部门={depts}")
        con.close()

        # 6) 部门别名不会被自动合并——这是要交给用户确认的语义，不是 bug。
        check(
            "部门别名未被静默合并",
            depts == ["市场", "研发", "研发部"],
            f"研发 / 研发部 仍是两个组：{depts}（需人工确认后才能合并）",
        )

    print("SMOKE_OK" if not failures else f"SMOKE_FAIL {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
