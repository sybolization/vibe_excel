/**
 * 业务库的一致性快照与恢复（ADR-0003，指南 §6.3）。
 *
 * 关键点：**不在数据库正在写入时直接复制文件**。
 * 复制一个活跃的 `x.db` 只拿到某个时刻的主文件，WAL 里未合并的页会丢，
 * 拿到的不是完整快照。这里用 SQLite 自己的 `VACUUM INTO`：
 * 它在一个读事务里输出一份自洽的数据库文件，是 SQLite 保证的一致副本。
 *
 * 恢复时也必须先清掉目标库的 `-wal` / `-shm`，
 * 否则旧 WAL 会覆盖回新数据，看起来像"恢复了但其实是脏的"。
 */

import { createHash } from 'node:crypto';
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
} from 'node:fs';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

export interface SnapshotResult {
  sha256: string;
  path: string;
  bytes: number;
}

/** SQLite 字符串字面量：单引号重复转义。 */
const sqlPath = (path: string): string => `'${path.replace(/\\/g, '/').replace(/'/g, "''")}'`;

/**
 * 对数据库做一致性快照，返回内容寻址的文件。
 *
 * 同一内容只落一份：sha256 相同即复用已有文件。
 */
export function snapshotDatabase(dbPath: string, revisionsDir: string): SnapshotResult {
  if (!existsSync(dbPath)) {
    throw new Error(`数据库不存在，无法快照：${dbPath}`);
  }
  mkdirSync(revisionsDir, { recursive: true });

  const tmpPath = join(revisionsDir, `.tmp-${randomUUID()}.db`);
  if (existsSync(tmpPath)) rmSync(tmpPath);

  const db = new DatabaseSync(dbPath);
  try {
    // VACUUM INTO 目标文件必须不存在；它在读事务中输出，得到自洽副本。
    db.exec(`VACUUM INTO ${sqlPath(tmpPath)}`);
  } finally {
    db.close();
  }

  const bytes = readFileSync(tmpPath);
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  const finalPath = join(revisionsDir, `${sha256}.db`);

  if (existsSync(finalPath)) {
    rmSync(tmpPath);
  } else {
    renameSync(tmpPath, finalPath);
  }

  return { sha256, path: finalPath, bytes: statSync(finalPath).size };
}

/**
 * 把快照恢复到目标路径。
 *
 * 调用方必须持有该会话的锁（保证没有正在写这个库的进程）。
 * 恢复顺序：先落临时文件 → 校验完整性 → 清掉旧 WAL/SHM → 原子替换。
 */
export function restoreDatabase(revisionPath: string, targetPath: string): { bytes: number } {
  if (!existsSync(revisionPath)) {
    throw new Error(`快照不存在：${revisionPath}`);
  }
  mkdirSync(join(targetPath, '..'), { recursive: true });

  const staging = `${targetPath}.restore-${randomUUID()}`;
  copyFileSync(revisionPath, staging);

  // 先验证再替换：宁可恢复失败，也不要用一个坏文件覆盖掉现有数据。
  const probe = new DatabaseSync(staging, { readOnly: true });
  try {
    const row = probe.prepare('PRAGMA quick_check').get() as { quick_check?: string } | undefined;
    const verdict = row?.quick_check ?? 'unknown';
    if (verdict !== 'ok') {
      throw new Error(`快照完整性校验失败（${verdict}）：${revisionPath}`);
    }
  } finally {
    probe.close();
  }

  for (const suffix of ['-wal', '-shm']) {
    rmSync(`${targetPath}${suffix}`, { force: true });
  }
  renameSync(staging, targetPath);
  return { bytes: statSync(targetPath).size };
}
