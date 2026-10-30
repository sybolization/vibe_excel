/**
 * S1：Session 与版本（指南 §3.1、§12 S1；设计见 ADR-0002 / ADR-0004）。
 *
 * 交付的是「事实的索引」：Program / Session / Run / DataRevision 的持久化、
 * 代码与数据版本的配对、草稿提交与发布授权记录、以及进程重启后的恢复视图。
 *
 * 明确不做的事：
 *  - 不把 git checkout 当作数据回滚（指南 §6.3）。恢复视图只负责把四元组取出来，
 *    真正的数据恢复由 runtime 的快照恢复显式执行。
 *  - 不把「用户发布了」当成「程序正确」（指南 §12 S1 禁止项）。
 */

import { randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { DatabaseSync } from 'node:sqlite';

import type {
  CodeCommit,
  DataRevisionId,
  ProgramId,
  RunId,
  RunStatus,
  SessionId,
} from '@vibe-excel/contracts';
import { isTerminal } from '@vibe-excel/contracts';

import {
  addWorktree,
  commitsAhead,
  commitAll,
  headSha,
  initRepo,
  isAncestor,
  isClean,
  mergeInto,
  removeWorktree,
} from './git.ts';
import { openControlDb, type Row } from './store.ts';

export interface Program {
  id: ProgramId;
  name: string;
  repoPath: string;
  mainBranch: string;
  createdAt: string;
}

export interface Session {
  id: SessionId;
  programId: ProgramId;
  name: string | null;
  draftBranch: string;
  /** 该会话的代码工作区（git worktree），agent 在这里写程序。 */
  worktreePath: string;
  /** 建会话时 main 的提交；发布前用它判断 main 是否被非授权改写（§6.5）。 */
  baseCommit: CodeCommit | null;
  status: 'active' | 'archived';
  createdAt: string;
  updatedAt: string;
}

export interface Run {
  id: RunId;
  sessionId: SessionId;
  programId: ProgramId;
  codeCommit: CodeCommit | null;
  inputBatchId: string;
  dataRevisionId: DataRevisionId;
  status: RunStatus;
  startedAt: string;
  finishedAt: string | null;
  exitCode: number | null;
  note: string | null;
}

export interface DataRevision {
  id: DataRevisionId;
  programId: ProgramId;
  sessionId: SessionId;
  sha256: string;
  path: string;
  bytes: number;
  parentId: DataRevisionId | null;
  createdAt: string;
  note: string | null;
}

export interface Artifact {
  id: string;
  runId: RunId;
  kind: string;
  path: string;
  sha256: string;
  bytes: number;
  createdAt: string;
}

export interface PublishRecord {
  id: string;
  sessionId: SessionId;
  programId: ProgramId;
  commitSha: string;
  at: string;
  note: string | null;
}

/**
 * 恢复视图：把「恢复一个确定节点」所需的引用一次性取出来。
 * 注意它**只读**：不会 checkout、不会覆盖数据库。
 */
export interface RecoveryView {
  session: Session;
  program: Program;
  draftHead: CodeCommit | null;
  runs: Run[];
  dataRevisions: DataRevision[];
  latestRun: Run | null;
  artifactsOfLatestRun: Artifact[];
  /** 最近一次成功运行对应的四元组；为空表示没有可恢复节点。 */
  restorableNode: {
    codeCommit: CodeCommit;
    dataRevisionId: DataRevisionId;
    inputBatchId: string;
    runId: RunId;
  } | null;
}

export interface PublishInput {
  /** 调用方必须先跑干净重跑并给出结论；未通过则拒绝发布。 */
  cleanRunPassed: boolean;
  /** 干净重跑证据（run 目录或命令），写进发布记录便于追溯。 */
  evidence?: string;
  note?: string;
}

export interface PublishResult {
  commit: CodeCommit;
  record: PublishRecord;
}

const now = (): string => new Date().toISOString();

const asProgram = (row: Row): Program => ({
  id: row['id'] as ProgramId,
  name: String(row['name']),
  repoPath: String(row['repo_path']),
  mainBranch: String(row['main_branch']),
  createdAt: String(row['created_at']),
});

const asSession = (row: Row, storeRoot: string): Session => {
  const id = row['id'] as SessionId;
  return {
    id,
    programId: row['program_id'] as ProgramId,
    name: row['name'] === null ? null : String(row['name']),
    draftBranch: String(row['draft_branch']),
    worktreePath: join(storeRoot, 'sessions', id, 'code'),
    baseCommit: row['base_commit'] === null || row['base_commit'] === undefined ? null : (row['base_commit'] as CodeCommit),
    status: row['status'] as 'active' | 'archived',
    createdAt: String(row['created_at']),
    updatedAt: String(row['updated_at']),
  };
};

const asRun = (row: Row): Run => ({
  id: row['id'] as RunId,
  sessionId: row['session_id'] as SessionId,
  programId: row['program_id'] as ProgramId,
  codeCommit: row['code_commit'] === null ? null : (row['code_commit'] as CodeCommit),
  inputBatchId: String(row['input_batch_id']),
  dataRevisionId: row['data_revision_id'] as DataRevisionId,
  status: row['status'] as RunStatus,
  startedAt: String(row['started_at']),
  finishedAt: row['finished_at'] === null ? null : String(row['finished_at']),
  exitCode: row['exit_code'] === null ? null : Number(row['exit_code']),
  note: row['note'] === null ? null : String(row['note']),
});

const asRevision = (row: Row): DataRevision => ({
  id: row['id'] as DataRevisionId,
  programId: row['program_id'] as ProgramId,
  sessionId: row['session_id'] as SessionId,
  sha256: String(row['sha256']),
  path: String(row['path']),
  bytes: Number(row['bytes']),
  parentId: row['parent_id'] === null ? null : (row['parent_id'] as DataRevisionId),
  createdAt: String(row['created_at']),
  note: row['note'] === null ? null : String(row['note']),
});

const asArtifact = (row: Row): Artifact => ({
  id: String(row['id']),
  runId: row['run_id'] as RunId,
  kind: String(row['kind']),
  path: String(row['path']),
  sha256: String(row['sha256']),
  bytes: Number(row['bytes']),
  createdAt: String(row['created_at']),
});

export class SessionStore {
  private readonly db: DatabaseSync;
  readonly storeRoot: string;

  private constructor(db: DatabaseSync, storeRoot: string) {
    this.db = db;
    this.storeRoot = storeRoot;
  }

  static open(storeRoot: string): SessionStore {
    mkdirSync(storeRoot, { recursive: true });
    const db = openControlDb(join(storeRoot, 'control.db'));
    return new SessionStore(db, storeRoot);
  }

  close(): void {
    this.db.close();
  }

  programRepoPath(programId: ProgramId): string {
    return join(this.storeRoot, 'program-repos', programId);
  }

  /* ---------------------------------------------------------------- *
   * Program
   * ---------------------------------------------------------------- */

  createProgram(input: { name: string }): Program {
    const id = randomUUID() as ProgramId;
    const repoPath = this.programRepoPath(id);
    initRepo(repoPath);
    const program: Program = {
      id,
      name: input.name,
      repoPath,
      mainBranch: 'main',
      createdAt: now(),
    };
    this.db
      .prepare('INSERT INTO programs (id, name, repo_path, main_branch, created_at) VALUES (?,?,?,?,?)')
      .run(program.id, program.name, program.repoPath, program.mainBranch, program.createdAt);
    return program;
  }

  getProgram(id: ProgramId): Program | undefined {
    const row = this.db.prepare('SELECT * FROM programs WHERE id = ?').get(id) as Row | undefined;
    return row ? asProgram(row) : undefined;
  }

  listPrograms(): Program[] {
    const rows = this.db.prepare('SELECT * FROM programs ORDER BY created_at').all() as Row[];
    return rows.map(asProgram);
  }

  /* ---------------------------------------------------------------- *
   * Session
   * ---------------------------------------------------------------- */

  createSession(programId: ProgramId, input: { name?: string } = {}): Session {
    const program = this.getProgram(programId);
    if (!program) throw new Error(`program 不存在：${programId}`);

    const id = randomUUID() as SessionId;
    const draftBranch = `draft/${id.slice(0, 8)}`;
    const createdAt = now();

    // 每个会话一个 git worktree：多个草稿分支不能同时占用同一个工作区。
    const worktreePath = join(this.storeRoot, 'sessions', id, 'code');
    const baseCommit = headSha(program.repoPath, program.mainBranch);
    mkdirSync(join(this.storeRoot, 'sessions', id), { recursive: true });
    addWorktree(program.repoPath, worktreePath, draftBranch, program.mainBranch);

    this.db
      .prepare(
        `INSERT INTO sessions (id, program_id, name, draft_branch, base_commit, status, created_at, updated_at)
         VALUES (?,?,?,?,?,?,?,?)`,
      )
      .run(id, programId, input.name ?? null, draftBranch, baseCommit, 'active', createdAt, createdAt);

    const row = this.db.prepare('SELECT * FROM sessions WHERE id = ?').get(id) as Row;
    return asSession(row, this.storeRoot);
  }

  getSession(id: SessionId): Session | undefined {
    const row = this.db.prepare('SELECT * FROM sessions WHERE id = ?').get(id) as Row | undefined;
    return row ? asSession(row, this.storeRoot) : undefined;
  }

  listSessions(programId?: ProgramId): Session[] {
    const rows = (
      programId
        ? this.db.prepare('SELECT * FROM sessions WHERE program_id = ? ORDER BY created_at').all(programId)
        : this.db.prepare('SELECT * FROM sessions ORDER BY created_at').all()
    ) as Row[];
    return rows.map((row) => asSession(row, this.storeRoot));
  }

  /** 归档会话并移除其 worktree（分支保留，历史不丢）。 */
  archiveSession(id: SessionId): void {
    const session = this.getSession(id);
    if (!session) throw new Error(`session 不存在：${id}`);
    removeWorktree(this.programRepoPath(session.programId), session.worktreePath);
    this.touchSession(id);
    this.db.prepare("UPDATE sessions SET status = 'archived', updated_at = ? WHERE id = ?").run(now(), id);
  }

  private touchSession(id: SessionId): void {
    this.db.prepare('UPDATE sessions SET updated_at = ? WHERE id = ?').run(now(), id);
  }

  /* ---------------------------------------------------------------- *
   * 代码版本
   * ---------------------------------------------------------------- */

  /** 提交草稿工作区的全部改动；无改动返回 null（事实，不是错误）。 */
  commitDraft(sessionId: SessionId, message: string): CodeCommit | null {
    const session = this.getSession(sessionId);
    if (!session) throw new Error(`session 不存在：${sessionId}`);
    const sha = commitAll(session.worktreePath, message);
    if (sha) this.touchSession(sessionId);
    return sha === null ? null : (sha as CodeCommit);
  }

  draftHead(sessionId: SessionId): CodeCommit | null {
    const session = this.getSession(sessionId);
    if (!session) throw new Error(`session 不存在：${sessionId}`);
    const sha = headSha(session.worktreePath);
    return sha === null ? null : (sha as CodeCommit);
  }

  /* ---------------------------------------------------------------- *
   * Run
   * ---------------------------------------------------------------- */

  beginRun(sessionId: SessionId, input: { inputBatchId: string; dataRevisionId: DataRevisionId }): Run {
    const session = this.getSession(sessionId);
    if (!session) throw new Error(`session 不存在：${sessionId}`);
    const id = randomUUID() as RunId;
    const startedAt = now();
    // 代码版本在开始时就固定下来，运行中途再提交的新代码不属于这次 run。
    const codeCommit = this.draftHead(sessionId);
    this.db
      .prepare(
        `INSERT INTO runs (id, session_id, program_id, code_commit, input_batch_id, data_revision_id,
                           status, started_at, finished_at, exit_code, note)
         VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
      )
      .run(
        id,
        sessionId,
        session.programId,
        codeCommit ?? null,
        input.inputBatchId,
        input.dataRevisionId,
        'running',
        startedAt,
        null,
        null,
        null,
      );
    this.touchSession(sessionId);
    const row = this.db.prepare('SELECT * FROM runs WHERE id = ?').get(id) as Row;
    return asRun(row);
  }

  /** 只能从非终态走向终态；终态不可回退（ADR-0002 §4）。 */
  finishRun(
    runId: RunId,
    input: { status: 'succeeded' | 'failed' | 'cancelled'; exitCode?: number; note?: string },
  ): Run {
    const existing = this.db.prepare('SELECT * FROM runs WHERE id = ?').get(runId) as Row | undefined;
    if (!existing) throw new Error(`run 不存在：${runId}`);
    if (isTerminal(asRun(existing).status)) {
      throw new Error(`run ${runId} 已是终态，不允许再改状态`);
    }
    const result = this.db
      .prepare(
        `UPDATE runs SET status = ?, finished_at = ?, exit_code = ?, note = ?
         WHERE id = ? AND status IN ('pending','running')`,
      )
      .run(input.status, now(), input.exitCode ?? null, input.note ?? null, runId);
    if (Number(result.changes) !== 1) {
      throw new Error(`run ${runId} 状态更新未生效（可能已被其它路径改为终态）`);
    }
    const row = this.db.prepare('SELECT * FROM runs WHERE id = ?').get(runId) as Row;
    const run = asRun(row);
    this.touchSession(run.sessionId);
    return run;
  }

  listRuns(sessionId: SessionId): Run[] {
    const rows = this.db
      .prepare('SELECT * FROM runs WHERE session_id = ? ORDER BY started_at DESC')
      .all(sessionId) as Row[];
    return rows.map(asRun);
  }

  getRun(runId: RunId): Run | undefined {
    const row = this.db.prepare('SELECT * FROM runs WHERE id = ?').get(runId) as Row | undefined;
    return row ? asRun(row) : undefined;
  }

  /* ---------------------------------------------------------------- *
   * DataRevision
   * ---------------------------------------------------------------- */

  /** 登记一个已存在的快照文件；同 sha256 复用同一 revision。 */
  registerDataRevision(input: {
    programId: ProgramId;
    sessionId: SessionId;
    sha256: string;
    path: string;
    bytes: number;
    parentId?: DataRevisionId;
    note?: string;
  }): DataRevision {
    const existing = this.db
      .prepare('SELECT * FROM data_revisions WHERE sha256 = ?')
      .get(input.sha256) as Row | undefined;
    if (existing) return asRevision(existing);

    const id = randomUUID() as DataRevisionId;
    this.db
      .prepare(
        `INSERT INTO data_revisions (id, program_id, session_id, sha256, path, bytes, parent_id, created_at, note)
         VALUES (?,?,?,?,?,?,?,?,?)`,
      )
      .run(
        id,
        input.programId,
        input.sessionId,
        input.sha256,
        input.path,
        input.bytes,
        input.parentId ?? null,
        now(),
        input.note ?? null,
      );
    return asRevision(this.db.prepare('SELECT * FROM data_revisions WHERE id = ?').get(id) as Row);
  }

  getDataRevision(id: DataRevisionId): DataRevision | undefined {
    const row = this.db.prepare('SELECT * FROM data_revisions WHERE id = ?').get(id) as Row | undefined;
    return row ? asRevision(row) : undefined;
  }

  listDataRevisions(sessionId: SessionId): DataRevision[] {
    const rows = this.db
      .prepare('SELECT * FROM data_revisions WHERE session_id = ? ORDER BY created_at DESC')
      .all(sessionId) as Row[];
    return rows.map(asRevision);
  }

  /* ---------------------------------------------------------------- *
   * Artifact
   * ---------------------------------------------------------------- */

  addArtifact(runId: RunId, input: { kind: string; path: string; sha256: string; bytes: number }): Artifact {
    const id = randomUUID();
    this.db
      .prepare('INSERT INTO artifacts (id, run_id, kind, path, sha256, bytes, created_at) VALUES (?,?,?,?,?,?,?)')
      .run(id, runId, input.kind, input.path, input.sha256, input.bytes, now());
    return asArtifact(this.db.prepare('SELECT * FROM artifacts WHERE id = ?').get(id) as Row);
  }

  listArtifacts(runId: RunId): Artifact[] {
    const rows = this.db.prepare('SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at').all(runId) as Row[];
    return rows.map(asArtifact);
  }

  /* ---------------------------------------------------------------- *
   * 发布（ADR-0004）
   * ---------------------------------------------------------------- */

  publish(sessionId: SessionId, input: PublishInput): PublishResult {
    if (!input.cleanRunPassed) {
      throw new Error('发布前的干净重跑未通过：拒绝合入 main');
    }
    const session = this.getSession(sessionId);
    if (!session) throw new Error(`session 不存在：${sessionId}`);
    const program = this.getProgram(session.programId);
    if (!program) throw new Error(`program 不存在：${session.programId}`);

    const ahead = commitsAhead(program.repoPath, session.draftBranch, program.mainBranch);
    if (ahead === 0) throw new Error('草稿分支没有领先 main 的提交，无需发布');

    // 指南 §6.5：main 若被重置或改写（基线不再是它的祖先），必须停止发布并报告，
    // 不能把非授权变化当成正常历史合过去。
    if (session.baseCommit && !isAncestor(program.repoPath, session.baseCommit, program.mainBranch)) {
      throw new Error(
        'main 发生了非授权变化：本会话的基线提交已不是 main 的祖先。停止发布，请人工核对 main。',
      );
    }

    if (!isClean(program.repoPath)) {
      throw new Error('程序仓库主工作区有未提交改动：拒绝在脏状态下合并');
    }

    const commit = mergeInto(program.repoPath, session.draftBranch, program.mainBranch);
    const record: PublishRecord = {
      id: randomUUID(),
      sessionId,
      programId: session.programId,
      commitSha: commit,
      at: now(),
      note: input.evidence ?? input.note ?? null,
    };
    this.db
      .prepare('INSERT INTO publishes (id, session_id, program_id, commit_sha, at, note) VALUES (?,?,?,?,?,?)')
      .run(record.id, record.sessionId, record.programId, record.commitSha, record.at, record.note);
    this.touchSession(sessionId);
    return { commit: commit as CodeCommit, record };
  }

  listPublishes(sessionId: SessionId): PublishRecord[] {
    const rows = this.db
      .prepare('SELECT * FROM publishes WHERE session_id = ? ORDER BY at DESC')
      .all(sessionId) as Row[];
    return rows.map((row) => ({
      id: String(row['id']),
      sessionId: row['session_id'] as SessionId,
      programId: row['program_id'] as ProgramId,
      commitSha: String(row['commit_sha']),
      at: String(row['at']),
      note: row['note'] === null ? null : String(row['note']),
    }));
  }

  /* ---------------------------------------------------------------- *
   * 恢复
   * ---------------------------------------------------------------- */

  /**
   * 恢复视图：只读地把「代码版本 + 数据版本 + 输入批次 + 制品」取出来。
   * 不执行任何 checkout 或数据覆盖——恢复动作必须由调用方显式做。
   */
  recoverSession(sessionId: SessionId): RecoveryView {
    const session = this.getSession(sessionId);
    if (!session) throw new Error(`session 不存在：${sessionId}`);
    const program = this.getProgram(session.programId);
    if (!program) throw new Error(`program 不存在：${session.programId}`);

    const runs = this.listRuns(sessionId);
    const dataRevisions = this.listDataRevisions(sessionId);
    const latestRun = runs[0] ?? null;
    const artifactsOfLatestRun = latestRun ? this.listArtifacts(latestRun.id) : [];

    const restorable = runs.find((run) => run.status === 'succeeded' && run.codeCommit !== null) ?? null;

    return {
      session,
      program,
      draftHead: this.draftHead(sessionId),
      runs,
      dataRevisions,
      latestRun,
      artifactsOfLatestRun,
      restorableNode: restorable
        ? {
            codeCommit: restorable.codeCommit as CodeCommit,
            dataRevisionId: restorable.dataRevisionId,
            inputBatchId: restorable.inputBatchId,
            runId: restorable.id,
          }
        : null,
    };
  }
}

export { initRepo, headSha, commitsAhead, isClean } from './git.ts';
