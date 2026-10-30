/** 文件与数据库的小工具（纯函数，不依赖 cordis，可单独测试）。 */

import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

export interface Digest {
  path: string;
  sha256: string;
  bytes: number;
}

export const sha256File = (path: string): string => createHash('sha256').update(readFileSync(path)).digest('hex');

/** 目录内所有文件的内容哈希；相对路径统一用正斜杠，跨平台可比。 */
export function dirDigests(dir: string, base = ''): Digest[] {
  if (!existsSync(dir)) return [];
  const out: Digest[] = [];
  for (const entry of readdirSync(join(dir, base), { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const rel = base === '' ? entry.name : `${base}/${entry.name}`;
    const absolute = join(dir, rel);
    if (entry.isDirectory()) out.push(...dirDigests(dir, rel));
    else if (entry.isFile()) out.push({ path: rel, sha256: sha256File(absolute), bytes: statSync(absolute).size });
  }
  return out;
}

export interface TableDigest {
  rows: number;
  sha256: string;
}

/**
 * 业务库的**语义**摘要：每张表的行数 + 内容哈希。
 *
 * 为什么不用文件字节：SQLite 页布局会随写入路径变化，同一份数据可能字节不同；
 * 反过来，字节相同也不代表内容一样（有 -wal 时主文件可能落后）。按行序列化再哈希，
 * 比较的才是"数据本身"。
 */
export function databaseDigest(dbPath: string): Record<string, TableDigest> {
  if (!existsSync(dbPath)) return {};
  const db = new DatabaseSync(dbPath, { readOnly: true });
  try {
    const tables = (
      db
        .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        .all() as { name: string }[]
    ).map((row) => row.name);
    const out: Record<string, TableDigest> = {};
    for (const table of tables) {
      const hasher = createHash('sha256');
      let rows = 0;
      for (const row of db.prepare(`SELECT * FROM "${table.replace(/"/g, '""')}" ORDER BY rowid`).iterate()) {
        hasher.update(JSON.stringify(row));
        hasher.update('\n');
        rows += 1;
      }
      out[table] = { rows, sha256: hasher.digest('hex') };
    }
    return out;
  } finally {
    db.close();
  }
}

/** 内容寻址的批次 id：按文件名排序后的 [{name, sha256, bytes}] 再取哈希前 12 位。 */
export function batchIdOf(entries: { name: string; sha256: string; bytes: number }[]): string {
  const canonical = [...entries].sort((a, b) => a.name.localeCompare(b.name)).map((e) => `${e.name}:${e.sha256}:${e.bytes}`);
  return createHash('sha256').update(canonical.join('\n')).digest('hex').slice(0, 12);
}
