"""侦察 ground_true_excel 里的真实文件：工作表、行列数、表头。

这不是产品代码，是一次性的侦察工具（交付物是「事实」，不是结论）。
用法：uv run --project runtime-env python spikes/demo-real-data/tools/inspect_inputs.py [文件名过滤]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3] / "ground_true_excel"


def cells(row: tuple, limit: int = 14) -> str:
    out = []
    for value in row[:limit]:
        text = "" if value is None else str(value).replace("\n", " ").strip()
        out.append(text[:22])
    return " | ".join(out)


def inspect_xls(path: Path) -> None:
    import xlrd

    book = xlrd.open_workbook(str(path), on_demand=True)
    print(f"\n=== {path.name}  ({path.stat().st_size / 1048576:.1f} MiB, BIFF/xls)")
    for name in book.sheet_names():
        sheet = book.sheet_by_name(name)
        print(f"  [{name}] rows={sheet.nrows} cols={sheet.ncols}")
        for r in range(min(3, sheet.nrows)):
            print(f"    r{r}: {cells(sheet.row_values(r))}")
        book.unload_sheet(name)
    book.release_resources()


def inspect_xlsx(path: Path) -> None:
    import openpyxl

    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    print(f"\n=== {path.name}  ({path.stat().st_size / 1048576:.1f} MiB, xlsx)")
    for name in book.sheetnames:
        sheet = book[name]
        print(f"  [{name}] dims={sheet.calculate_dimension()} max_row={sheet.max_row} max_col={sheet.max_column}")
        for i, row in enumerate(sheet.iter_rows(max_row=3, max_col=14, values_only=True)):
            print(f"    r{i}: {cells(row)}")
    book.close()


def main() -> int:
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    files = sorted(p for p in ROOT.rglob("*") if p.is_file() and p.suffix.lower() in {".xls", ".xlsx"})
    for path in files:
        if needle and needle not in path.name.lower():
            continue
        try:
            if path.suffix.lower() == ".xlsx":
                inspect_xlsx(path)
            else:
                inspect_xls(path)
        except Exception as error:  # 侦察工具：失败要看见，不要静默
            print(f"\n=== {path.name}  读取失败：{type(error).__name__}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
