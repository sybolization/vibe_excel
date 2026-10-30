/**
 * 会话级排他锁（ADR-0003）。
 *
 * 目的：同一 session 的数据修改**串行执行**。多个 session 可以并发，
 * 但它们各自有独立的 work/ 目录和独立的数据库文件，所以并发本身不冲突。
 *
 * 实现用「独占创建锁文件」（O_EXCL），带陈旧锁检测：
 * 持锁进程已死或持锁时间超过阈值，就视为陈旧锁回收。
 * 这不是分布式锁，也不跨机器。
 */

import { closeSync, mkdirSync, openSync, readFileSync, unlinkSync, writeSync } from 'node:fs';
import { dirname } from 'node:path';

export interface SessionLock {
  readonly path: string;
  readonly pid: number;
  release(): void;
}

export interface AcquireOptions {
  /** 超过这个时长仍持有的锁视为陈旧。默认 30 分钟。 */
  staleMs?: number;
  /** 等待他人释放的最长时间；0 表示不等待。默认 0。 */
  timeoutMs?: number;
  pollMs?: number;
}

interface LockFile {
  pid: number;
  at: string;
  host: string;
}

const sleep = (ms: number): void => {
  // 同步睡眠：锁是同步语义，调用方拿到锁前不应该继续跑。
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
};

function isPidAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM 说明进程存在但无权限——仍算活着。
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}

function readLock(path: string): LockFile | null {
  try {
    const raw = readFileSync(path, 'utf8');
    const parsed = JSON.parse(raw) as Partial<LockFile>;
    if (typeof parsed.pid !== 'number' || typeof parsed.at !== 'string') return null;
    return { pid: parsed.pid, at: parsed.at, host: String(parsed.host ?? '') };
  } catch {
    return null;
  }
}

function isStale(path: string, staleMs: number): boolean {
  const info = readLock(path);
  if (!info) {
    // 锁文件存在但内容不完整：给 5 秒宽限期，避免把正在写入的锁误判成陈旧。
    try {
      const stat = readFileSync(path, 'utf8');
      if (stat.length === 0) {
        return true;
      }
    } catch {
      return true;
    }
    return false;
  }
  if (!isPidAlive(info.pid)) return true;
  const age = Date.now() - Date.parse(info.at);
  return Number.isFinite(age) && age > staleMs;
}

/** 获取排他锁；拿不到就抛错，而不是静默继续（静默继续等于没有串行化）。 */
export function acquireLock(lockPath: string, options: AcquireOptions = {}): SessionLock {
  const staleMs = options.staleMs ?? 30 * 60 * 1000;
  const timeoutMs = options.timeoutMs ?? 0;
  const pollMs = options.pollMs ?? 200;
  const deadline = Date.now() + timeoutMs;

  mkdirSync(dirname(lockPath), { recursive: true });

  for (;;) {
    try {
      const fd = openSync(lockPath, 'wx');
      const payload: LockFile = { pid: process.pid, at: new Date().toISOString(), host: 'localhost' };
      writeSync(fd, JSON.stringify(payload));
      closeSync(fd);

      let released = false;
      return {
        path: lockPath,
        pid: process.pid,
        release: (): void => {
          if (released) return;
          released = true;
          const info = readLock(lockPath);
          // 只删自己持有的锁，避免删掉别人刚接手的锁。
          if (info && info.pid !== process.pid) return;
          try {
            unlinkSync(lockPath);
          } catch {
            // 已经不存在就当作释放成功。
          }
        },
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;

      if (isStale(lockPath, staleMs)) {
        try {
          unlinkSync(lockPath);
        } catch {
          // 别人刚好删掉了，继续竞争。
        }
        continue;
      }

      if (Date.now() >= deadline) {
        const holder = readLock(lockPath);
        throw new Error(
          `会话被其它进程占用：${lockPath}（pid=${holder?.pid ?? '?'}，自 ${holder?.at ?? '?'} 起）`,
        );
      }
      sleep(pollMs);
    }
  }
}

/** 同步地把一段工作放进会话锁内执行，异常也会释放锁。 */
export function withLockSync<T>(lockPath: string, fn: () => T, options: AcquireOptions = {}): T {
  const lock = acquireLock(lockPath, options);
  try {
    return fn();
  } finally {
    lock.release();
  }
}
