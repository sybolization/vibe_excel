/**
 * S3：本地 Runtime 与数据边界（指南 §3.3、§6.3、§6.4、§12 S3）。
 *
 * 这里负责「一次执行发生在哪、谁在写、写之前有什么快照、重跑是否干净」，
 * 不负责业务语义，也不做安全边界的宣称（见 ADR-0001 缺口 13）。
 */

import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { SessionId } from '@vibe-excel/contracts';

import { acquireLock, type AcquireOptions, type SessionLock, withLockSync } from './lock.ts';

export { acquireLock, withLockSync } from './lock.ts';
export type { AcquireOptions, SessionLock } from './lock.ts';
export { restoreDatabase, snapshotDatabase } from './snapshot.ts';
export type { SnapshotResult } from './snapshot.ts';
export { cleanRerun, diffRunFiles } from './cleanrun.ts';
export type { CleanRunOptions, CleanRunResult, FileDigest } from './cleanrun.ts';

export interface WorkspacePaths {
  /** 会话根目录。 */
  root: string;
  /** 执行 cwd；业务数据库放在这里。 */
  work: string;
  /** 程序代码工作区（session 包创建的 git worktree）。 */
  code: string;
  /** pi 会话文件。 */
  piSessions: string;
  /** 每会话独立的 pi 配置目录。 */
  agent: string;
  /** run 日志与事件流。 */
  runs: string;
  /** 制品根下本会话可用的快照目录（全局按内容寻址，这里只是便捷引用）。 */
  dataRevisions: string;
}

export function workspacePaths(storeRoot: string, sessionId: SessionId): WorkspacePaths {
  const root = join(storeRoot, 'sessions', sessionId);
  return {
    root,
    work: join(root, 'work'),
    code: join(root, 'code'),
    piSessions: join(root, 'sessions'),
    agent: join(root, 'agent'),
    runs: join(root, 'runs'),
    dataRevisions: join(storeRoot, 'data-revisions'),
  };
}

export function ensureWorkspace(storeRoot: string, sessionId: SessionId): WorkspacePaths {
  const paths = workspacePaths(storeRoot, sessionId);
  for (const dir of [paths.root, paths.work, paths.piSessions, paths.agent, paths.runs, paths.dataRevisions]) {
    mkdirSync(dir, { recursive: true });
  }
  return paths;
}

/** 每个会话一把锁，锁文件就在会话根目录，跟着会话走。 */
export function sessionLockPath(storeRoot: string, sessionId: SessionId): string {
  return join(storeRoot, 'sessions', sessionId, '.session.lock');
}

/**
 * 把一段修改数据的执行放进会话锁内。
 *
 * 串行化的依据是「同一 session 的 work/ 目录只有一个」，锁只是把这条约束
 * 在进程层面显式化。跨 session 并发是安全的，因为它们根本不共用数据库文件。
 */
export function withSessionLock<T>(
  storeRoot: string,
  sessionId: SessionId,
  fn: () => T,
  options: AcquireOptions = {},
): T {
  return withLockSync(sessionLockPath(storeRoot, sessionId), fn, options);
}

export function runDirectory(storeRoot: string, sessionId: SessionId, runId: string): string {
  return join(workspacePaths(storeRoot, sessionId).runs, runId);
}
