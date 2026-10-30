"""探一个假设：PN余量 这张表是不是就能由 可用岗位 − 本批已发 推出来。

如果能对上，它就不是「人工估计」，而是这 18 行有真人答案的标签。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import openpyxl

ROOT = Path(__file__).resolve().parents[3] / "ground_true_excel" / "wenxin"
BOOK = ROOT / "可用岗位表_2026-10-22.xlsx"


def sheet_rows(book, name):
    rows = [list(r) for r in book[name].iter_rows(values_only=True)]
    header = ["" if v is None else str(v).strip() for v in rows[0]]
    return header, rows[1:]


book = openpyxl.load_workbook(str(BOOK), read_only=True, data_only=True)

h_avail, avail = sheet_rows(book, "可用岗位")
h_sent, sent = sheet_rows(book, "本批已发")
h_rest, rest = sheet_rows(book, "PN余量")
book.close()

key = lambda row, h: (
    str(row[h.index("归属主管")] or "").strip(),
    str(row[h.index("成本中心")] or "").strip(),
    str(row[h.index("DL/IDL")] or "").strip(),
)

avail_by = Counter(key(r, h_avail) for r in avail if r[h_avail.index("岗位号")] is not None)
sent_by = Counter(key(r, h_sent) for r in sent if r[h_sent.index("岗位号")] is not None)

avail_ids = {str(r[h_avail.index("岗位号")]).strip() for r in avail}
sent_ids = {str(r[h_sent.index("岗位号")]).strip() for r in sent}
print(f"可用岗位 {len(avail)} 行 / {len(avail_ids)} 个岗位号；本批已发 {len(sent)} 行 / {len(sent_ids)} 个岗位号")
print(f"本批已发 中不在可用岗位里的：{len(sent_ids - avail_ids)} 个 -> {sorted(sent_ids - avail_ids)[:8]}")

print(f"\n{'归属主管':<22}{'成本中心':<12}{'DL/IDL':<16}{'可用':>5}{'已发':>5}{'算得剩余':>8}{'表里剩余':>8}{'一致':>6}")
mismatch = 0
for row in rest:
    k = key(row, h_rest)
    stated = row[h_rest.index("剩余空号")]
    stated = int(stated) if isinstance(stated, (int, float)) else stated
    stated_used = row[h_rest.index("本批已用")]
    computed_avail = avail_by.get(k, 0)
    computed_used = sent_by.get(k, 0)
    computed_rest = computed_avail - computed_used
    ok = str(computed_rest) == str(stated) and str(computed_used) == str(stated_used)
    if not ok:
        mismatch += 1
    print(
        f"{k[0]:<22}{k[1]:<12}{k[2]:<16}{computed_avail:>5}{computed_used:>5}"
        f"{computed_rest:>8}{str(stated):>8}{'OK' if ok else 'DIFF':>6}"
    )

print(f"\nPN余量 共 {len(rest)} 行，不一致 {mismatch} 行")

groups = set(avail_by) | set(sent_by)
missing = groups - {key(r, h_rest) for r in rest}
print(f"可用岗位/本批已发 里出现、但 PN余量 没列出的组合：{len(missing)} 个")
for k in sorted(missing)[:10]:
    print(f"  {k} 可用={avail_by.get(k,0)} 已发={sent_by.get(k,0)}")

low = [r for r in rest if str(r[h_rest.index("是否低于预警线")]).strip() == "是"]
print(f"\n标了低于预警线的组合 {len(low)} 个；预警线取值 {sorted({r[h_rest.index('预警线')] for r in rest})}")
