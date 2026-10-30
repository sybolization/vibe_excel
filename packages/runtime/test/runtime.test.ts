/**
 * S3 的验收测试（指南 §12 S3 验收项）。
 *
 * 覆盖：快照是自洽的（含 WAL 中已提交但未 checkpoint 的数据）、
 * 恢复会清掉陈旧 WAL、恢复前校验完整性、会话级串行化、
 * 干净重跑在全新目录执行且内容可比对。
 */

import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, describe, expect, it } from 'vitest';

import {
  acquireLock,
  cleanRerun,
  diffRunFiles,
  restoreDatabase,
  snapshotDatabase,
  withSessionLock,
} from '../src/index.ts';

const makeDir = (prefix: string): string => mkdtempSync(join(tmpdir(), prefix));

const openDb = (path: string): DatabaseSync => {
  const db = new DatabaseSync(path);
  db.exec('PRAGMA journal_mode = WAL');
  return db;
};

const row = <T>(db: DatabaseSync, sql: string): T => db.prepare(sql).get() as T;

describe('快照', () => {
  it('连接仍然打开、数据可能还在 WAL 里时，快照依然自洽', () => {
    const dir = makeDir('ve-snap-');
    const dbPath = join(dir, 'biz.db');
    const db = openDb(dbPath);
    db.exec('CREATE TABLE t (a INTEGER)');
    db.prepare('INSERT INTO t VALUES (?)').run(7);

    // 故意不 close，也不 checkpoint：模拟「库正在被使用」。
    const snap = snapshotDatabase(dbPath, join(dir, 'revisions'));
    db.close();

    expect(existsSync(snap.path)).toBe(true);
    const copy = new DatabaseSync(snap.path, { readOnly: true });
    expect(row<{ c: number }>(copy, 'SELECT COUNT(*) AS c FROM t').c).toBe(1);
    expect(row<{ a: number }>(copy, 'SELECT a FROM t').a).toBe(7);
    copy.close();
  });

  it('内容相同则复用同一快照文件（内容寻址）', () => {
    const dir = makeDir('ve-snap2-');
    const dbPath = join(dir, 'biz.db');
    const db = openDb(dbPath);
    db.exec('CREATE TABLE t (a INTEGER)');
    db.prepare('INSERT INTO t VALUES (?)').run(1);
    db.close();

    const first = snapshotDatabase(dbPath, join(dir, 'revisions'));
    const second = snapshotDatabase(dbPath, join(dir, 'revisions'));
    expect(second.sha256).toBe(first.sha256);
    expect(second.path).toBe(first.path);
  });
});

describe('恢复', () => {
  it('恢复会覆盖目标库，并清掉陈旧的 -wal / -shm', () => {
    const dir = makeDir('ve-restore-');
    const dbPath = join(dir, 'biz.db');
    const db = openDb(dbPath);
    db.exec('CREATE TABLE t (a INTEGER)');
    db.prepare('INSERT INTO t VALUES (?)').run(1);
    db.close();
    const snap = snapshotDatabase(dbPath, join(dir, 'revisions'));

    // 目标库先被改脏，并留下一个假的陈旧 WAL。
    const target = join(dir, 'target.db');
    const dirty = openDb(target);
    dirty.exec('CREATE TABLE t (a INTEGER)');
    dirty.prepare('INSERT INTO t VALUES (?)').run(999);
    dirty.close();
    writeFileSync(`${target}-wal`, 'stale-wal-should-be-removed', 'utf8');

    const restored = restoreDatabase(snap.path, target);
    expect(restored.bytes).toBeGreaterThan(0);
    expect(existsSync(`${target}-wal`)).toBe(false);

    const check = new DatabaseSync(target, { readOnly: true });
    expect(row<{ a: number }>(check, 'SELECT a FROM t').a).toBe(1);
    check.close();
  });

  it('快照文件损坏时拒绝恢复，不用坏文件覆盖现有数据', () => {
    const dir = makeDir('ve-restore-bad-');
    const bad = join(dir, 'bad.db');
    writeFileSync(bad, 'this is not a sqlite database at all', 'utf8');
    expect(() => restoreDatabase(bad, join(dir, 'target.db'))).toThrow();
  });
});

describe('串行化', () => {
  it('同一把锁不能被两个持有者同时拿到', () => {
    const dir = makeDir('ve-lock-');
    const lockPath = join(dir, '.session.lock');
    const lock = acquireLock(lockPath);
    expect(() => acquireLock(lockPath)).toThrow(/占用/);
    lock.release();
    const again = acquireLock(lockPath);
    again.release();
  });

  it('陈旧锁可被回收（持锁时间超过阈值）', () => {
    const dir = makeDir('ve-lock-stale-');
    const lockPath = join(dir, '.session.lock');
    writeFileSync(
      lockPath,
      JSON.stringify({ pid: process.pid, at: new Date(Date.now() - 60_000).toISOString(), host: 'localhost' }),
      'utf8',
    );
    const lock = acquireLock(lockPath, { staleMs: 10 });
    lock.release();
  });

  it('持锁进程已死时锁可被回收', () => {
    const dir = makeDir('ve-lock-dead-');
    const lockPath = join(dir, '.session.lock');
    const dead = spawnSync(process.execPath, ['-e', '0']);
    writeFileSync(
      lockPath,
      JSON.stringify({ pid: dead.pid, at: new Date().toISOString(), host: 'localhost' }),
      'utf8',
    );
    const lock = acquireLock(lockPath, { staleMs: 60 * 60 * 1000, pollMs: 10 });
    lock.release();
  });

  it('withSessionLock 在锁被占用时拒绝执行，而不是并行跑', () => {
    const storeRoot = makeDir('ve-store-');
    const sessionId = 'session-abc' as never;
    const lock = acquireLock(join(storeRoot, 'sessions', sessionId, '.session.lock'));
    let ran = false;
    expect(() =>
      withSessionLock(storeRoot, sessionId, () => {
        ran = true;
      }),
    ).toThrow(/占用/);
    expect(ran).toBe(false);
    lock.release();
    withSessionLock(storeRoot, sessionId, () => {
      ran = true;
    });
    expect(ran).toBe(true);
  });
});

describe('干净重跑', () => {
  function initProgramRepo(script: string): { repo: string; commit: string } {
    const repo = makeDir('ve-repo-');
    execFileSync('git', ['init', '-b', 'main', repo], { stdio: 'ignore' });
    execFileSync('git', ['-C', repo, 'config', 'user.name', 'Test'], { stdio: 'ignore' });
    execFileSync('git', ['-C', repo, 'config', 'user.email', 'test@local'], { stdio: 'ignore' });
    writeFileSync(join(repo, 'gen.mjs'), script, 'utf8');
    execFileSync('git', ['-C', repo, 'add', '-A'], { stdio: 'ignore' });
    execFileSync('git', ['-C', repo, 'commit', '-m', 'init'], { stdio: 'ignore' });
    const commit = execFileSync('git', ['-C', repo, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
    return { repo, commit };
  }

  it('在全新目录执行，产物被收集，工作目录随后移除', () => {
    const { repo, commit } = initProgramRepo(
      'import { writeFileSync } from "node:fs"; writeFileSync("out.txt", "hello\\n");\n',
    );
    const runRoot = makeDir('ve-clean-');
    const result = cleanRerun({
      repoPath: repo,
      commit,
      runRoot,
      entry: { command: process.execPath, args: ['gen.mjs'] },
      outputs: ['out.txt'],
    });

    expect(result.exitCode).toBe(0);
    expect(result.outputs.map((o) => o.path)).toEqual(['out.txt']);
    expect(existsSync(result.manifestPath)).toBe(true);
    // 工作目录是临时的：跑完就撤，避免下次重跑读到上一次的残留。
    expect(existsSync(result.workDir)).toBe(false);
  });

  it('确定性程序两次重跑内容完全一致', () => {
    const { repo, commit } = initProgramRepo(
      'import { writeFileSync } from "node:fs"; writeFileSync("out.txt", "fixed\\n");\n',
    );
    const runRoot = makeDir('ve-clean-det-');
    const options = {
      repoPath: repo,
      commit,
      runRoot,
      entry: { command: process.execPath, args: ['gen.mjs'] },
      outputs: ['out.txt'],
    };
    const first = cleanRerun(options);
    const second = cleanRerun(options);
    expect(diffRunFiles(first.files, second.files).identical).toBe(true);
  });

  it('依赖当前时间（不确定）的程序会被内容比对抓出来', () => {
    const { repo, commit } = initProgramRepo(
      'import { writeFileSync } from "node:fs"; writeFileSync("out.txt", String(Date.now()));\n',
    );
    const runRoot = makeDir('ve-clean-nondet-');
    const options = {
      repoPath: repo,
      commit,
      runRoot,
      entry: { command: process.execPath, args: ['gen.mjs'] },
      outputs: ['out.txt'],
    };
    const first = cleanRerun(options);
    const second = cleanRerun(options);
    const diff = diffRunFiles(first.files, second.files);
    expect(diff.identical).toBe(false);
    expect(diff.changed).toContain('out.txt');
  });

  it('入口失败时退出码如实反映，不会伪造成成功', () => {
    const { repo, commit } = initProgramRepo('process.exit(3);\n');
    const result = cleanRerun({
      repoPath: repo,
      commit,
      runRoot: makeDir('ve-clean-fail-'),
      entry: { command: process.execPath, args: ['gen.mjs'] },
    });
    expect(result.exitCode).toBe(3);
  });
});
