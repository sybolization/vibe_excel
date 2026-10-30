/**
 * 控制面存储（ADR-0002 决策 1）。
 *
 * 这里只放「任务事实与索引」，业务数据在会话工作区自己的库里。
 * 选 SQLite 是为了让「结束一次 run + 写制品指针 + 更新 session」在一个事务里完成。
 *
 * 风险：node:sqlite 在 Node 22 上是实验特性。真出问题，替换点只有本文件。
 */

import { DatabaseSync } from 'node:sqlite';

export const SCHEMA_VERSION = 2;

/** 行形状在本层统一收口，上层不用关心 sqlite 的返回类型。 */
export type Row = Record<string, unknown>;

const MIGRATION_V1 = `
CREATE TABLE IF NOT EXISTS programs (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  repo_path   TEXT NOT NULL,
  main_branch TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  program_id   TEXT NOT NULL REFERENCES programs(id),
  name         TEXT,
  draft_branch TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('active','archived')),
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id               TEXT PRIMARY KEY,
  session_id       TEXT NOT NULL REFERENCES sessions(id),
  program_id       TEXT NOT NULL REFERENCES programs(id),
  code_commit      TEXT,
  input_batch_id   TEXT NOT NULL,
  data_revision_id TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  exit_code        INTEGER,
  note             TEXT
);

CREATE TABLE IF NOT EXISTS data_revisions (
  id         TEXT PRIMARY KEY,
  program_id TEXT NOT NULL REFERENCES programs(id),
  session_id TEXT NOT NULL REFERENCES sessions(id),
  sha256     TEXT NOT NULL UNIQUE,
  path       TEXT NOT NULL,
  bytes      INTEGER NOT NULL,
  parent_id  TEXT,
  created_at TEXT NOT NULL,
  note       TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
  id         TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL REFERENCES runs(id),
  kind       TEXT NOT NULL,
  path       TEXT NOT NULL,
  sha256     TEXT NOT NULL,
  bytes      INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publishes (
  id         TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  program_id TEXT NOT NULL REFERENCES programs(id),
  commit_sha TEXT NOT NULL,
  at         TEXT NOT NULL,
  note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_program ON sessions(program_id);
CREATE INDEX IF NOT EXISTS idx_runs_session     ON runs(session_id);
CREATE INDEX IF NOT EXISTS idx_revisions_session ON data_revisions(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run     ON artifacts(run_id);
`;

/**
 * v2：记录会话创建时 main 的提交。
 *
 * 用途（指南 §6.5）：发布前确认 main 是以「正常前进」的方式变化的。
 * 如果 main 被重置或改写，这个基线就不再是 main 的祖先，必须停止发布并报告，
 * 而不是把改写当成正常历史合过去。
 */
const MIGRATION_V2 = `
ALTER TABLE sessions ADD COLUMN base_commit TEXT;
`;

function tableExists(db: DatabaseSync, name: string): boolean {
  const row = db
    .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name = ?")
    .get(name) as { name?: string } | undefined;
  return row?.name === name;
}

/**
 * 打开控制面库并迁移。
 *
 * 迁移只向前：库里的版本比代码新就拒绝启动，而不是静默降级——
 * 静默降级会写坏新版本的数据。
 */
export function openControlDb(path: string): DatabaseSync {
  const db = new DatabaseSync(path);
  db.exec('PRAGMA journal_mode = WAL');
  db.exec('PRAGMA foreign_keys = ON');

  db.exec('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)');
  const existing = db.prepare('SELECT value FROM meta WHERE key = ?').get('schema_version') as
    | { value?: string }
    | undefined;
  const version = existing?.value === undefined ? 0 : Number.parseInt(existing.value, 10);

  if (version > SCHEMA_VERSION) {
    db.close();
    throw new Error(
      `控制面库版本 ${version} 高于本代码支持的 ${SCHEMA_VERSION}：拒绝以旧代码写新数据`,
    );
  }

  if (version < 1) {
    db.exec(MIGRATION_V1);
  }
  // 表结构存在但版本号缺失时补齐，避免「有表无版本」导致的迁移歧义。
  if (!tableExists(db, 'programs')) {
    db.exec(MIGRATION_V1);
  }
  if (version < 2) {
    const columns = db.prepare('PRAGMA table_info(sessions)').all() as { name?: string }[];
    if (!columns.some((column) => column.name === 'base_commit')) {
      db.exec(MIGRATION_V2);
    }
  }
  db.prepare('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)').run(
    'schema_version',
    String(Math.max(version, SCHEMA_VERSION)),
  );

  return db;
}
