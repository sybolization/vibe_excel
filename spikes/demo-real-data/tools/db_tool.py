"""业务库小工具：结构摘要（行数 + 内容哈希）、显式 SQL。

内容哈希按行序列化后累加，和文件字节无关——这样「同一份数据」不会因为
SQLite 页布局差异而被判成不同，也不会因为文件时间戳变化而误报。
用法：
  python db_tool.py digest <db>
  python db_tool.py sql <db> "<语句>"
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")


def tables_of(con: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by 1"
        )
    ]


def digest(db: str) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out: dict = {}
    for table in tables_of(con):
        hasher = hashlib.sha256()
        count = 0
        for row in con.execute(f'select * from "{table}" order by rowid'):
            hasher.update(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
            hasher.update(b"\n")
            count += 1
        out[table] = {"rows": count, "sha256": hasher.hexdigest()}
    con.close()
    return out


def sql(db: str, statement: str) -> dict:
    con = sqlite3.connect(db)
    try:
        cursor = con.execute(statement)
        rows = cursor.fetchall()
        con.commit()
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return {"columns": columns, "rows": rows, "rowcount": cursor.rowcount}
    finally:
        con.close()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    command = argv[1]
    if command == "digest":
        print(json.dumps(digest(argv[2]), ensure_ascii=False))
    elif command == "sql":
        print(json.dumps(sql(argv[2], argv[3]), ensure_ascii=False, default=str))
    else:
        print(f"未知子命令：{command}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
