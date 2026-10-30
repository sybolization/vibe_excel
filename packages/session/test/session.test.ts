/**
 * S1 的验收测试（指南 §12 S1 验收项）。
 *
 * 覆盖：进程重启后恢复确定节点、查看历史不修改当前状态、
 * 代码与数据版本配对、发布必须由调用方给出干净重跑结论。
 */

import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { SessionStore } from '../src/index.ts';

let storeRoot: string;
let store: SessionStore;

beforeEach(() => {
  storeRoot = mkdtempSync(join(tmpdir(), 've-session-'));
  store = SessionStore.open(storeRoot);
});

afterEach(() => {
  store.close();
});

describe('Program / Session', () => {
  it('建程序会初始化 git 仓库，main 上有初始提交', () => {
    const program = store.createProgram({ name: '招聘周报' });
    expect(existsSync(join(program.repoPath, '.git'))).toBe(true);
    expect(program.mainBranch).toBe('main');
  });

  it('建会话会切出草稿分支，并给该分支一个独立 worktree', () => {
    const program = store.createProgram({ name: 'p' });
    const session = store.createSession(program.id, { name: '第一轮' });

    expect(session.draftBranch.startsWith('draft/')).toBe(true);
    expect(existsSync(session.worktreePath)).toBe(true);
    // worktree 与主仓库不能是同一个目录，否则两个草稿会互相踩。
    expect(session.worktreePath).not.toBe(program.repoPath);
  });
});

describe('Run 与版本配对', () => {
  it('beginRun 固定当时的代码版本，之后的提交不属于该 run', () => {
    const program = store.createProgram({ name: 'p' });
    const session = store.createSession(program.id);
    const revision = store.registerDataRevision({
      programId: program.id,
      sessionId: session.id,
      sha256: 'a'.repeat(64),
      path: join(storeRoot, 'data-revisions', `${'a'.repeat(64)}.db`),
      bytes: 1024,
    });

    const run = store.beginRun(session.id, { inputBatchId: 'batch-1', dataRevisionId: revision.id });
    expect(run.status).toBe('running');
    const fixedCommit = run.codeCommit;

    writeFileSync(join(session.worktreePath, 'import.py'), 'print("hi")\n', 'utf8');
    const sha = store.commitDraft(session.id, 'feat: 导入脚本');
    expect(sha).not.toBeNull();
    expect(run.codeCommit).toBe(fixedCommit);
    expect(store.draftHead(session.id)).toBe(sha);
  });

  it('终态不可回退', () => {
    const program = store.createProgram({ name: 'p' });
    const session = store.createSession(program.id);
    const revision = store.registerDataRevision({
      programId: program.id,
      sessionId: session.id,
      sha256: 'b'.repeat(64),
      path: '/nowhere.db',
      bytes: 1,
    });
    const run = store.beginRun(session.id, { inputBatchId: 'b', dataRevisionId: revision.id });
    store.finishRun(run.id, { status: 'succeeded', exitCode: 0 });
    expect(() => store.finishRun(run.id, { status: 'failed', exitCode: 1 })).toThrow(/终态/);
  });

  it('同一内容只登记一次数据修订', () => {
    const program = store.createProgram({ name: 'p' });
    const session = store.createSession(program.id);
    const input = {
      programId: program.id,
      sessionId: session.id,
      sha256: 'c'.repeat(64),
      path: '/same.db',
      bytes: 42,
    };
    const first = store.registerDataRevision(input);
    const second = store.registerDataRevision(input);
    expect(second.id).toBe(first.id);
    expect(store.listDataRevisions(session.id)).toHaveLength(1);
  });
});

describe('进程重启后恢复', () => {
  it('关闭后重新打开同一制品根，能恢复到确定节点', () => {
    const program = store.createProgram({ name: 'p' });
    const session = store.createSession(program.id);
    const revision = store.registerDataRevision({
      programId: program.id,
      sessionId: session.id,
      sha256: 'd'.repeat(64),
      path: join(storeRoot, 'data-revisions', `${'d'.repeat(64)}.db`),
      bytes: 2048,
      note: '首轮导入',
    });

    writeFileSync(join(session.worktreePath, 'main.py'), 'print(1)\n', 'utf8');
    const sha = store.commitDraft(session.id, 'feat: 首个版本');

    const run = store.beginRun(session.id, { inputBatchId: 'batch-9', dataRevisionId: revision.id });
    store.addArtifact(run.id, { kind: 'report', path: '/artifacts/r.xlsx', sha256: 'e'.repeat(64), bytes: 99 });
    store.finishRun(run.id, { status: 'succeeded', exitCode: 0 });

    store.close();

    // 用另一个进程直接读 control.db，证明事实真的落盘、不依赖本进程内存。
    const probe = execFileSync(
      process.execPath,
      [
        '-e',
        `const { DatabaseSync } = require('node:sqlite');
         const db = new DatabaseSync(${JSON.stringify(join(storeRoot, 'control.db'))}, { readOnly: true });
         const runs = db.prepare('SELECT COUNT(*) AS c FROM runs').get().c;
         const revs = db.prepare('SELECT COUNT(*) AS c FROM data_revisions').get().c;
         const arts = db.prepare('SELECT COUNT(*) AS c FROM artifacts').get().c;
         console.log(JSON.stringify({ runs, revs, arts }));`,
      ],
      { encoding: 'utf8' },
    ).trim();
    expect(JSON.parse(probe)).toEqual({ runs: 1, revs: 1, arts: 1 });

    store = SessionStore.open(storeRoot);
    const view = store.recoverSession(session.id);

    expect(view.session.id).toBe(session.id);
    expect(view.draftHead).toBe(sha);
    expect(view.latestRun?.status).toBe('succeeded');
    expect(view.restorableNode).toEqual({
      codeCommit: sha,
      dataRevisionId: revision.id,
      inputBatchId: 'batch-9',
      runId: run.id,
    });
    expect(view.artifactsOfLatestRun).toHaveLength(1);
  });

  it('恢复视图是只读的：查看历史不会改动当前代码状态', () => {
    const program = store.createProgram({ name: 'p' });
    const session = store.createSession(program.id);
    const before = join(session.worktreePath, 'work.txt');
    writeFileSync(before, 'draft content', 'utf8');

    const headBefore = store.draftHead(session.id);
    const statusBefore = execFileSync('git', ['-C', session.worktreePath, 'status', '--porcelain'], {
      encoding: 'utf8',
    });

    store.recoverSession(session.id);

    // HEAD 没动、未提交的改动仍未被提交、文件内容没被覆盖。
    expect(store.draftHead(session.id)).toBe(headBefore);
    expect(
      execFileSync('git', ['-C', session.worktreePath, 'status', '--porcelain'], { encoding: 'utf8' }),
    ).toBe(statusBefore);
    expect(readFileSync(before, 'utf8')).toBe('draft content');
  });
});

describe('发布', () => {
  function setup(): { sessionId: ReturnType<SessionStore['createSession']>['id']; repoPath: string } {
    const program = store.createProgram({ name: 'p' });
    const session = store.createSession(program.id);
    writeFileSync(join(session.worktreePath, 'main.py'), 'print(2)\n', 'utf8');
    store.commitDraft(session.id, 'feat: 发布候选');
    // 记录一下草稿分支与 main 的关系，发布后 main 应前进。
    return { sessionId: session.id, repoPath: program.repoPath };
  }

  it('干净重跑未通过时拒绝发布', () => {
    const { sessionId } = setup();
    expect(() => store.publish(sessionId, { cleanRunPassed: false })).toThrow(/干净重跑未通过/);
  });

  it('通过后合入 main，并留下可追溯的发布记录', () => {
    const { sessionId, repoPath } = setup();
    const result = store.publish(sessionId, { cleanRunPassed: true, evidence: 'clean-run-123' });

    expect(result.commit).toMatch(/^[0-9a-f]{40}$/);
    const publishes = store.listPublishes(sessionId);
    expect(publishes).toHaveLength(1);
    expect(publishes[0]?.commitSha).toBe(result.commit);
    expect(publishes[0]?.note).toBe('clean-run-123');

    // main 上确实能看到发布出来的文件。
    const shown = execFileSync('git', ['-C', repoPath, 'show', 'main:main.py'], { encoding: 'utf8' });
    expect(shown).toContain('print(2)');
  });

  it('草稿没有新提交时拒绝发布', () => {
    const program = store.createProgram({ name: 'p' });
    const session = store.createSession(program.id);
    expect(() => store.publish(session.id, { cleanRunPassed: true })).toThrow(/没有领先/);
  });

  it('main 被非授权改写时停止发布', () => {
    const program = store.createProgram({ name: 'p' });
    // main 上再走一个提交，让会话的基线不是根提交，这样才有可能被"回退到基线之前"。
    writeFileSync(join(program.repoPath, 'README.md'), 'v2\n', 'utf8');
    execFileSync('git', ['-C', program.repoPath, 'commit', '-am', 'v2'], { stdio: 'ignore' });

    const session = store.createSession(program.id);
    writeFileSync(join(session.worktreePath, 'main.py'), 'print(3)\n', 'utf8');
    store.commitDraft(session.id, 'feat: 候选');

    // 模拟非授权改写：把 main 硬回退到根提交，会话基线不再是它的祖先。
    const root = execFileSync('git', ['-C', program.repoPath, 'rev-list', '--max-parents=0', 'HEAD'], {
      encoding: 'utf8',
    }).trim();
    execFileSync('git', ['-C', program.repoPath, 'reset', '--hard', root], { stdio: 'ignore' });

    expect(() => store.publish(session.id, { cleanRunPassed: true })).toThrow(/非授权变化/);
  });
});
