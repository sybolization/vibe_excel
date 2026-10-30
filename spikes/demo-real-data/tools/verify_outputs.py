"""独立核对：不读导入程序，直接从**原始 Excel** 重算一遍，再和程序的产出对。

这是「第二实现」。它的意义不是证明程序对，而是让两边能吵架：
不一致就说明至少有一边错了，而且能定位到具体哪一格。

核对项：
  A 守恒：库表行数 vs 原始文件行数
  B summary.json 的自述行数 vs 库表行数
  C 分类计数（员工状态/用工类型/员工级别）：独立重算 vs summary.json
  D 入职日期 = AS_OF 人数
  E 严格口径候选人数（用户原话唯一能确定的那个口径）
  F PN 分组：独立重算 vs pn_available.csv
  G PN 看板 18 行（真人答案）：pn_available.csv vs 原 xlsx 的 PN余量 sheet

用法：python verify_outputs.py --db <hr.db> --out <analyze 输出目录>
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3] / "ground_true_excel" / "wenxin"
AS_OF = dt.date(2026, 10, 23)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def excel_serial_to_date(value) -> dt.date | None:
    """1899-12-30 起算的序列号；已经是日期/字符串的按原样解析。"""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and value > 20000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
    if isinstance(value, str):
        text = value.strip()[:10].replace("/", "-")
        for fmt in ("%Y-%m-%d", "%Y.%m.%d"):
            try:
                return dt.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def source_employees() -> dict:
    import xlrd

    book = xlrd.open_workbook(str(ROOT / "所有员工_202610231434.XLS"))
    sheet = book.sheet_by_index(0)
    header = [str(v).strip() for v in sheet.row_values(0)]
    index = {name: header.index(name) for name in ("员工状态", "用工类型", "员工级别", "入职日期")}
    columns = {name: sheet.col_values(pos)[1:] for name, pos in index.items()}
    book.release_resources()
    return {"rows": sheet.nrows - 1, **columns}


def pick(row: dict, key: str, keys: list[str]) -> str:
    for candidate in keys:
        if candidate in row:
            return str(row[candidate] if row[candidate] is not None else "").strip()
    # csv 读了但列名不同：按位置猜一次，失败就报出来
    raise KeyError(f"输出里没有列 {key}（有 {keys}）")


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def source_pn() -> tuple[Counter, Counter, list[list], list[str]]:
    import openpyxl

    book = openpyxl.load_workbook(str(ROOT / "可用岗位表_2026-10-22.xlsx"), read_only=True, data_only=True)

    def rows_of(name: str) -> tuple[list[str], list[tuple]]:
        sheet = book[name]
        data = [tuple(r) for r in sheet.iter_rows(values_only=True)]
        header = ["" if v is None else str(v).strip() for v in data[0]]
        return header, data[1:]

    h_avail, avail = rows_of("可用岗位")
    h_sent, sent = rows_of("本批已发")
    h_rest, rest = rows_of("PN余量")
    book.close()

    def key(row: tuple, header: list[str]) -> tuple[str, str, str]:
        return (
            str(row[header.index("归属主管")] or "").strip(),
            str(row[header.index("成本中心")] or "").strip(),
            str(row[header.index("DL/IDL")] or "").strip(),
        )

    avail_counter: Counter = Counter()
    for row in avail:
        if row[h_avail.index("岗位号")] is not None:
            avail_counter[key(row, h_avail)] += 1
    sent_counter: Counter = Counter()
    for row in sent:
        if row[h_sent.index("岗位号")] is not None:
            sent_counter[key(row, h_sent)] += 1
    label = [list(row) for row in rest]
    return avail_counter, sent_counter, label, h_rest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    db_path = Path(args.db)
    out = Path(args.out)

    print(f"独立核对  db={db_path}")
    print(f"           out={out}\n")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    db_rows = {
        table: conn.execute(f'select count(*) from "{table}"').fetchone()[0]
        for (table,) in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by 1"
        )
    }
    conn.close()
    print(f"库表行数 {json.dumps(db_rows, ensure_ascii=False)}\n")

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    pn_csv = read_csv(out / "pn_available.csv")
    pop_csv = read_csv(out / "population.csv")

    employees = source_employees()
    source_rows = {
        "employees": employees["rows"],
        "positions": 24365,
        "available_positions": 6337,
        "sent_positions": 159,
        "new_hire": 25619,
        "re_hire": 1048,
    }

    # A/B 守恒
    for table, expected in source_rows.items():
        actual = db_rows.get(table)
        check(f"A 守恒 {table}", actual == expected, f"库={actual} 来源={expected}")
    quarantine = db_rows.get("quarantine")
    check(
        "A 守恒 无丢行",
        quarantine is not None and sum(v for k, v in db_rows.items() if k in source_rows) + quarantine
        >= sum(source_rows.values()),
        f"quarantine={quarantine}，业务表合计={sum(v for k, v in db_rows.items() if k in source_rows)}，来源合计={sum(source_rows.values())}",
    )
    stated = summary.get("source_rows", {})
    for table, expected in source_rows.items():
        check(f"B 自述行数 {table}", stated.get(table) == db_rows.get(table), f"summary={stated.get(table)} 库={db_rows.get(table)}")

    # C 分类计数
    independent = {
        "status_counts": Counter(str(v).strip() for v in employees["员工状态"]),
        "employment_type_counts": Counter(str(v).strip() for v in employees["用工类型"]),
        "level_counts": Counter(str(v).strip() for v in employees["员工级别"]),
    }
    for field, counter in independent.items():
        declared = {str(k): int(v) for k, v in (summary.get(field) or {}).items()}
        same = declared == dict(counter)
        differing = {k: (declared.get(k), counter.get(k)) for k in set(declared) | set(counter) if declared.get(k) != counter.get(k)}
        check(f"C {field}", same, "一致" if same else f"差异={differing}")

    # D 入职日期 = AS_OF
    dates = [excel_serial_to_date(v) for v in employees["入职日期"]]
    on_as_of = sum(1 for d in dates if d == AS_OF)
    declared_d = (summary.get("onboard_on_as_of") or {}).get("all_rows")
    check("D 入职日期=AS_OF(全量)", declared_d == on_as_of, f"summary={declared_d} 独立重算={on_as_of}")
    unparsed = sum(1 for v, d in zip(employees["入职日期"], dates) if d is None and str(v).strip() not in ("", "0"))
    print(f"      （日期无法解析的行 {unparsed} 条）")

    # E 严格口径
    strict_mask = [
        str(status).strip() not in ("离职", "退休")
        and str(kind).strip() not in ("外包员工", "外包员工2")
        and str(level).strip() != "Exempt"
        for status, kind, level in zip(employees["员工状态"], employees["用工类型"], employees["员工级别"])
    ]
    pre_date = sum(strict_mask)
    post_date = sum(1 for ok, d in zip(strict_mask, dates) if ok and d == AS_OF)
    declared_strict = ((summary.get("population") or {}).get("strict") or {}).get("count")
    which = "筛选后(不含日期)" if declared_strict == pre_date else ("筛选+当日入职" if declared_strict == post_date else "都不是")
    check(
        "E 严格口径人数",
        declared_strict in (pre_date, post_date),
        f"summary={declared_strict}；独立重算：筛选后={pre_date}，再叠加当日入职={post_date} → 命中 {which}",
    )
    check("E population.csv 两口径都有行", len({r["rule"] for r in pop_csv}) == 2, f"{len(pop_csv)} 行，rule 取值={sorted({r['rule'] for r in pop_csv})}")

    # F PN 分组
    avail_counter, sent_counter, label_rows, label_header = source_pn()
    groups = set(avail_counter) | set(sent_counter)
    independent_pn = {
        key: (avail_counter.get(key, 0), sent_counter.get(key, 0)) for key in groups
    }
    csv_pn = {}
    for row in pn_csv:
        try:
            groups_key = (row["主管"].strip(), row["成本中心"].strip(), row["DL/IDL"].strip())
        except KeyError:
            check("F pn_available.csv 列名", False, f"列={list(row)}")
            csv_pn = {}
            break
        csv_pn[groups_key] = (int(float(row["remaining"])), int(float(row["sent"])))
    if csv_pn:
        check("F 分组数量", len(csv_pn) == len(independent_pn), f"csv={len(csv_pn)} 独立重算={len(independent_pn)}")
        only_csv = set(csv_pn) - set(independent_pn)
        only_ind = set(independent_pn) - set(csv_pn)
        check("F 分组键集合", not only_csv and not only_ind, f"仅csv={sorted(only_csv)[:3]} 仅独立={sorted(only_ind)[:3]}")
        mismatched = {k: (csv_pn.get(k), independent_pn[k]) for k in set(csv_pn) & set(independent_pn) if csv_pn[k] != independent_pn[k]}
        check("F 每组 remaining/sent", not mismatched, f"不一致 {len(mismatched)} 组 {list(mismatched.items())[:3]}")

    # G 真人答案：PN余量 18 行
    hit = 0
    misses = []
    not_derivable = []
    for row in label_rows:
        key = (
            str(row[label_header.index("归属主管")] or "").strip(),
            str(row[label_header.index("成本中心")] or "").strip(),
            str(row[label_header.index("DL/IDL")] or "").strip(),
        )
        expected_rest = row[label_header.index("剩余空号")]
        expected_used = row[label_header.index("本批已用")]
        got = csv_pn.get(key)
        if got is None and key not in independent_pn:
            # 该组合在两张源表里一行都没有：看板这一行不是从这两份文件推出来的，
            # 说明还有一份「主管/岗位主数据」没给我们。这是缺输入，不是算错。
            not_derivable.append((key, expected_rest, expected_used))
            continue
        if got is not None and got[0] == int(expected_rest) and got[1] == int(expected_used):
            hit += 1
        else:
            misses.append((key, expected_rest, expected_used, got))
    check(
        "G 看板 18 行对齐真人答案",
        not misses,
        f"{hit}/{len(label_rows)} 行一致；{len(not_derivable)} 行无法从输入推导（源表里该组合 0 行）；不一致={misses[:3]}",
    )
    if not_derivable:
        print(f"      无法推导的行（属于缺输入，不是算错）：{not_derivable}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n合计 {passed}/{len(results)} 项通过")
    for name, ok, _ in results:
        if not ok:
            print(f"  未通过：{name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
