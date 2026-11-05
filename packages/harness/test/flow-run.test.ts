/**
 * flow-run 的测试：只跑「SQL 节点」这条不依赖 Python 的路径。
 *
 * 这里验证的是**事实核对**有没有真的发生：
 *  - 节点声明的写表必须真的变了（没变要报空转）；
 *  - 节点读的表必须存在（不存在要报缺表，而不是让下游拿空数据继续算）；
 *  - 两次重跑比对的是声明的产出；
 *  - 一个节点失败后不再往下跑。
 */

import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { describe, expect, it } from 'vitest';

import { parseFlow, planFlow } from '../src/flow.ts';
import { diffRounds, executeSqlNode, runFlowRound, tableNames, type FlowRoundResult } from '../src/flow-run.ts';

const tempDir = (): string => mkdtempSync(join(tmpdir(), 'vibe-flow-run-'));

/** 造一个空库，带上 sqlite 之外的一张 meta 表（贴近真实业务库）。 */
function makeDb(dir: string): string {
  const path = join(dir, 'hr.db');
  const db = new DatabaseSync(path);
  db.exec('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)');
  db.prepare('INSERT INTO meta (key, value) VALUES (?, ?)').run('created_at', 'fixed-for-test');
  db.close();
  return path;
}

const rows = (dbPath: string, table: string): Record<string, unknown>[] => {
  const db = new DatabaseSync(dbPath, { readOnly: true });
  try {
    return db.prepare(`SELECT * FROM "${table}" ORDER BY rowid`).all() as Record<string, unknown>[];
  } finally {
    db.close();
  }
};

const base = (nodes: unknown[], extra: Record<string, unknown> = {}): ReturnType<typeof parseFlow> =>
  parseFlow({ flow: 'demo', db: 'hr.db', outputs: ['result'], nodes, ...extra });

describe('SQL 节点执行', () => {
  it('在一个事务里执行，失败时回滚', () => {
    const dir = tempDir();
    const dbPath = makeDb(dir);

    const ok = executeSqlNode(dbPath, 'CREATE TABLE result AS SELECT 1 AS x;');
    expect(ok.exitCode).toBe(0);
    expect(tableNames(dbPath)).toContain('result');

    const bad = executeSqlNode(dbPath, 'CREATE TABLE broken AS SELECT 1; INSERT INTO nope VALUES (1);');
    expect(bad.exitCode).toBe(1);
    expect(bad.stderr).toMatch(/nope/);
    // 回滚是硬要求：半张表比没有表更危险。
    expect(tableNames(dbPath)).not.toContain('broken');
  });
});

describe('一轮执行', () => {
  it('按拓扑序跑 SQL 节点，并记下「声明的写表真的变了」', () => {
    const dir = tempDir();
    const dbPath = makeDb(dir);
    const plan = planFlow(
      base([
        { id: 'make_source', call: 'sql/source.sql', reads: [], writes: ['source'] },
        { id: 'make_result', call: 'sql/result.sql', reads: ['source'], writes: ['result'] },
      ]),
    );
    const files: Record<string, string> = {
      'sql/source.sql': 'CREATE TABLE source AS SELECT 1 AS id;',
      'sql/result.sql': 'CREATE TABLE result AS SELECT id * 2 AS doubled FROM source;',
    };

    const round = runFlowRound({
      plan,
      dbPath,
      inputsDir: dir,
      outDir: join(dir, 'out'),
      asOf: '2026-01-01',
      runMaterialize: () => ({ exitCode: 0, timedOut: false, stdout: '', stderr: '', elapsedMs: 0 }),
      runPython: () => {
        throw new Error('这个用例不应该跑 python');
      },
      readFile: (path) => files[path] ?? '',
    });

    expect(round.ok).toBe(true);
    expect(round.nodes.map((node) => node.id)).toEqual(['make_source', 'make_result']);
    expect(round.nodes[0]!.changed).toEqual(['source']);
    expect(round.nodes[1]!.changed).toEqual(['result']);
    expect(rows(dbPath, 'result')).toEqual([{ doubled: 2 }]);
    expect(round.outputs['result']?.rows).toBe(1);
  });

  it('节点读的表不存在时直接判失败，并且不再往下跑', () => {
    const dir = tempDir();
    const dbPath = makeDb(dir);
    const plan = planFlow(
      parseFlow({
        flow: 'demo',
        db: 'hr.db',
        outputs: ['final'],
        nodes: [
          { id: 'first', call: 'sql/first.sql', reads: ['missing_table'], writes: ['intermediate'] },
          { id: 'second', call: 'sql/second.sql', reads: ['intermediate'], writes: ['final'] },
        ],
      }),
    );

    const round = runFlowRound({
      plan,
      dbPath,
      inputsDir: dir,
      outDir: join(dir, 'out'),
      asOf: '2026-01-01',
      runMaterialize: () => ({ exitCode: 0, timedOut: false, stdout: '', stderr: '', elapsedMs: 0 }),
      runPython: () => {
        throw new Error('这个用例不应该跑 python');
      },
      readFile: () => 'SELECT 1;',
    });

    expect(round.ok).toBe(false);
    expect(round.nodes).toHaveLength(1);
    expect(round.nodes[0]!.missingReads).toEqual(['missing_table']);
    expect(round.nodes[0]!.exitCode).toBe(1);
  });

  it('声明了写表却没变，记为空转但不判失败（这是给模型看的事实）', () => {
    const dir = tempDir();
    const dbPath = makeDb(dir);
    // 先造出 result 并写定内容：节点接下来写的是同一份内容，才叫「真的没变」。
    // 表根本不存在不是空转、是没产出——那是下一条用例。
    executeSqlNode(dbPath, 'CREATE TABLE result (x INTEGER); INSERT INTO result VALUES (1);');
    const plan = planFlow(base([{ id: 'noop', call: 'sql/noop.sql', reads: [], writes: ['result'] }]));

    const round = runFlowRound({
      plan,
      dbPath,
      inputsDir: dir,
      outDir: join(dir, 'out'),
      asOf: '2026-01-01',
      runMaterialize: () => ({ exitCode: 0, timedOut: false, stdout: '', stderr: '', elapsedMs: 0 }),
      runPython: () => {
        throw new Error('这个用例不应该跑 python');
      },
      readFile: () => 'DELETE FROM result; INSERT INTO result VALUES (1);',
    });

    expect(round.ok).toBe(true);
    expect(round.nodes[0]!.unchanged).toEqual(['result']);
    expect(round.outputs['result']?.rows).toBe(1);
  });

  it('声明的产出表两轮都没出现时判这一轮失败（不给「空集合一致」留缝）', () => {
    const dir = tempDir();
    const dbPath = makeDb(dir);
    const plan = planFlow(base([{ id: 'noop', call: 'sql/noop.sql', reads: [], writes: ['result'] }]));
    const logs: string[] = [];

    const round = runFlowRound({
      plan,
      dbPath,
      inputsDir: dir,
      outDir: join(dir, 'out'),
      asOf: '2026-01-01',
      runMaterialize: () => ({ exitCode: 0, timedOut: false, stdout: '', stderr: '', elapsedMs: 0 }),
      runPython: () => {
        throw new Error('这个用例不应该跑 python');
      },
      // 节点跑成功了，但它只建了一张跟声明无关的表：result 从没出现过。
      readFile: () => 'CREATE TABLE IF NOT EXISTS unrelated (x INTEGER);',
      log: (line) => logs.push(line),
    });

    // 两轮的 outputs 里都没有 result —— 旧实现会因此判 identical，这就是那条缝。
    expect(round.ok).toBe(false);
    expect(round.outputs['result']).toBeUndefined();
    expect(logs.join('\n')).toMatch(/声明的产出表没有出现：result/);
  });

  it('物化声明的表没有产出时要报出来', () => {
    const dir = tempDir();
    const dbPath = makeDb(dir);
    const plan = planFlow(
      base([{ id: 'use', call: 'sql/use.sql', reads: ['available'], writes: ['result'] }], {
        materialize: [{ file: 'a.xlsx', table: 'available' }],
      }),
    );

    const round = runFlowRound({
      plan,
      dbPath,
      inputsDir: dir,
      outDir: join(dir, 'out'),
      asOf: '2026-01-01',
      // 假装加载器什么都没做
      runMaterialize: () => ({ exitCode: 0, timedOut: false, stdout: '', stderr: '', elapsedMs: 0 }),
      runPython: () => {
        throw new Error('不应该跑到节点');
      },
      readFile: () => 'SELECT 1;',
    });

    expect(round.ok).toBe(false);
    expect(round.materialize.entries[0]!.produced).toBe(false);
    expect(round.nodes).toHaveLength(0);
  });
});

describe('两轮比对', () => {
  const round = (outputs: FlowRoundResult['outputs'], files: FlowRoundResult['files']): FlowRoundResult => ({
    ok: true,
    materialize: { exitCode: 0, timedOut: false, elapsedMs: 0, stdout: '', stderr: '', entries: [] },
    nodes: [],
    outputs,
    files,
  });

  it('表行数/哈希相同、文件相同就是一致', () => {
    const a = round({ result: { rows: 2, sha256: 'aaa' } }, [{ path: 'a.csv', sha256: 'f1', bytes: 3 }]);
    const b = round({ result: { rows: 2, sha256: 'aaa' } }, [{ path: 'a.csv', sha256: 'f1', bytes: 3 }]);
    expect(diffRounds(a, b)).toEqual({ tables: [], files: [], identical: true });
  });

  it('表内容不同、文件不同都要点名', () => {
    const a = round({ result: { rows: 2, sha256: 'aaa' }, other: { rows: 1, sha256: 'x' } }, [
      { path: 'a.csv', sha256: 'f1', bytes: 3 },
    ]);
    const b = round({ result: { rows: 2, sha256: 'bbb' }, other: { rows: 1, sha256: 'x' } }, [
      { path: 'a.csv', sha256: 'f2', bytes: 3 },
    ]);
    const diff = diffRounds(a, b);
    expect(diff.identical).toBe(false);
    expect(diff.tables).toEqual(['result']);
    expect(diff.files).toEqual(['a.csv:f1', 'a.csv:f2']);
  });

  it('一边多出表或文件也算不一致', () => {
    const a = round({ result: { rows: 1, sha256: 'a' } }, []);
    const b = round({ result: { rows: 1, sha256: 'a' }, extra: { rows: 1, sha256: 'b' } }, []);
    expect(diffRounds(a, b).tables).toEqual(['extra']);
  });
});
