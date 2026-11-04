/**
 * 节点契约的端到端验证：flow.json + 函数节点 + 真实 python + 真实 SQLite。
 *
 * 这里不 mock 任何东西——用 `runtime-env` 里的解释器按平台的方式调用
 * `packages/harness/py/node_runner.py`，验证「节点 = 函数」这条约定真的能跑：
 *   - 平台按函数签名传参（多了不传、少了不硬塞）；
 *   - 节点写表之后，声明的写表真的变了；
 *   - 同一个流程跑两遍，声明的产出完全一致。
 *
 * 缺解释器时跳过（不把「本机没装」伪装成通过，也不让它挡住别的用例）。
 */

import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { describe, expect, it } from 'vitest';

import { parseFlow, planFlow } from '../src/flow.ts';
import { diffRounds, runFlowRound } from '../src/flow-run.ts';
import { nodeRunnerPath } from '../src/flow-source.ts';

const repoRoot = resolve(import.meta.dirname, '..', '..', '..');
const pythonPath = join(repoRoot, 'runtime-env', '.venv', 'Scripts', 'python.exe');
const pythonReady = existsSync(pythonPath) && existsSync(nodeRunnerPath());

const tempDir = (): string => mkdtempSync(join(tmpdir(), 'vibe-node-'));

const PIPELINE = `
import sqlite3


def make_source(db):
    """只声明 db：平台就只传 db，不会硬塞 as_of/out。"""
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS source (id INTEGER, name TEXT)")
    con.execute("DELETE FROM source")
    con.executemany("INSERT INTO source VALUES (?, ?)", [(1, "a"), (2, "b")])
    con.commit()
    con.close()
    return {"rows": 2}


def compute_result(db, as_of):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS result (id INTEGER, label TEXT)")
    con.execute("DELETE FROM result")
    rows = con.execute("SELECT id, name FROM source ORDER BY id").fetchall()
    con.executemany("INSERT INTO result VALUES (?, ?)", [(i, f"{name}-{as_of}") for i, name in rows])
    con.commit()
    con.close()
    print(f"result rows={len(rows)}")
`;

function makeFlow() {
  return parseFlow({
    flow: 'smoke',
    db: 'hr.db',
    outputs: ['result'],
    nodes: [
      { id: 'make_source', call: 'pipeline.py:make_source', reads: [], writes: ['source'] },
      { id: 'compute_result', call: 'pipeline.py:compute_result', reads: ['source'], writes: ['result'] },
    ],
  });
}

function makeDb(dir: string): string {
  const path = join(dir, 'hr.db');
  const db = new DatabaseSync(path);
  db.exec('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)');
  db.close();
  return path;
}

/** 按平台的方式跑一个 python 节点：python node_runner.py <file> <func> --key value ... */
function runPythonNode(fields: {
  cwd: string;
  file: string;
  func: string;
  kwargs: Record<string, string>;
}): { exitCode: number; timedOut: boolean; stdout: string; stderr: string; elapsedMs: number } {
  const started = Date.now();
  try {
    const stdout = execFileSync(
      pythonPath,
      [
        nodeRunnerPath(),
        fields.file,
        fields.func,
        ...Object.entries(fields.kwargs).flatMap(([key, value]) => [`--${key.replace(/_/g, '-')}`, value]),
      ],
      {
        encoding: 'utf8',
        cwd: fields.cwd,
        stdio: ['ignore', 'pipe', 'pipe'],
        // 平台（cleanRerun）会强制 UTF-8；这个 helper 在模拟平台，所以也得设，
        // 否则中文报错在 Windows 上会以 cp936 写出来、被 utf8 读成乱码。
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      },
    );
    return { exitCode: 0, timedOut: false, stdout, stderr: '', elapsedMs: Date.now() - started };
  } catch (error) {
    const detail = error as { status?: number; stdout?: string; stderr?: string };
    return {
      exitCode: detail.status ?? 1,
      timedOut: false,
      stdout: detail.stdout ?? '',
      stderr: detail.stderr ?? String(error),
      elapsedMs: Date.now() - started,
    };
  }
}

describe.skipIf(!pythonReady)('节点 = 函数：真实 python 端到端', () => {
  it('按签名传参、写表、两次重跑的声明产出完全一致', () => {
    const dir = tempDir();
    const outDir = join(dir, 'out');
    mkdirSync(outDir, { recursive: true });
    const codeDir = join(dir, 'code');
    mkdirSync(codeDir, { recursive: true });
    writeFileSync(join(codeDir, 'pipeline.py'), PIPELINE, 'utf8');

    const spec = makeFlow();
    const plan = planFlow(spec);
    const rounds = [];
    for (const label of ['a', 'b']) {
      const roundDb = join(dir, `${label}.db`);
      const seed = new DatabaseSync(roundDb);
      seed.exec('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)');
      seed.close();
      rounds.push(
        runFlowRound({
          plan,
          dbPath: roundDb,
          inputsDir: dir,
          outDir: join(dir, `out-${label}`),
          asOf: '2026-01-01',
          runMaterialize: () => ({ exitCode: 0, timedOut: false, stdout: '', stderr: '', elapsedMs: 0 }),
          runPython: (input) =>
            runPythonNode({ cwd: codeDir, file: input.file, func: input.func, kwargs: input.kwargs }),
          readFile: () => '',
        }),
      );
    }

    const [a, b] = rounds as [(typeof rounds)[number], (typeof rounds)[number]];
    expect(a.ok).toBe(true);
    expect(a.nodes.map((node) => node.id)).toEqual(['make_source', 'compute_result']);
    // 只声明 db 的函数拿到的入参里不该有 as_of
    expect(Object.keys(a.nodes[0]!.kwargs).sort()).toEqual(['as_of', 'db', 'inputs', 'out']);
    expect(a.nodes[0]!.kwargs['as_of']).toBe('2026-01-01');
    expect(a.nodes[1]!.changed).toEqual(['result']);
    expect(a.nodes[1]!.stdout).toMatch(/result rows=2/);

    const db = new DatabaseSync(join(dir, 'a.db'), { readOnly: true });
    const rows = db.prepare('SELECT id, label FROM result ORDER BY id').all() as { id: number; label: string }[];
    db.close();
    expect(rows).toEqual([
      { id: 1, label: 'a-2026-01-01' },
      { id: 2, label: 'b-2026-01-01' },
    ]);

    // 两次重跑：同一份输入、同一个业务日，声明的产出必须逐表一致
    expect(diffRounds(a, b)).toEqual({ tables: [], files: [], identical: true });
  });

  it('函数签名里没有的参数不会硬塞（少传就是少传）', () => {
    const dir = tempDir();
    writeFileSync(join(dir, 'pipeline.py'), PIPELINE, 'utf8');
    const dbPath = makeDb(dir);
    const result = runPythonNode({
      cwd: dir,
      file: 'pipeline.py',
      func: 'make_source',
      kwargs: { db: dbPath, as_of: '2026-01-01' },
    });
    expect(result.exitCode).toBe(0);
    const db = new DatabaseSync(dbPath, { readOnly: true });
    expect((db.prepare('SELECT count(*) AS n FROM source').get() as { n: number }).n).toBe(2);
    db.close();
  });
});

/**
 * ADR-0008 §3 的运行时含义：标了 `"t:表名"` 的参数，由平台从库里读成 DataFrame 递进去。
 *
 * 这条如果没实现，提示词里那个 `def clean(db, raw: "t:raw_employees")` 一跑就
 * 「需要参数 raw，但平台没有提供」——解析出来的图是对的，但跑不了。
 */
describe.skipIf(!pythonReady)('t: 标注 = 平台把表读成 DataFrame 递进来', () => {
  const T_FLOW = `
import sqlite3


def build(db, raw: "t:raw_data") -> "t:built":
    """把平台递进来的表算成 built（自己绝不 read_sql 输入表）。"""
    rows = [(int(row["id"]), str(row["name"]).upper()) for _, row in raw.iterrows()]
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS built (id INTEGER, name TEXT)")
    con.execute("DELETE FROM built")
    con.executemany("INSERT INTO built VALUES (?, ?)", rows)
    con.commit()
    con.close()
    print(f"built rows={len(rows)}")
`;

  /** 建一个带 raw_data 的库，模拟「上游物化好了」。 */
  function seedDb(dir: string): string {
    const path = join(dir, 'hr.db');
    const db = new DatabaseSync(path);
    db.exec('CREATE TABLE raw_data (id INTEGER, name TEXT)');
    db.exec("INSERT INTO raw_data VALUES (1, 'ann'), (2, 'bob')");
    db.close();
    return path;
  }

  it('注入是读真的库：函数只管用 df，写自己的产出表', () => {
    const dir = tempDir();
    writeFileSync(join(dir, 'pipeline.py'), T_FLOW, 'utf8');
    const dbPath = seedDb(dir);

    const result = runPythonNode({ cwd: dir, file: 'pipeline.py', func: 'build', kwargs: { db: dbPath } });
    expect(result.stderr).toBe('');
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toMatch(/built rows=2/);

    const db = new DatabaseSync(dbPath, { readOnly: true });
    expect(db.prepare('SELECT id, name FROM built ORDER BY id').all()).toEqual([
      { id: 1, name: 'ANN' },
      { id: 2, name: 'BOB' },
    ]);
    db.close();
  });

  it('写了 from __future__ import annotations 也一样（注解在运行时是带引号的字符串）', () => {
    const dir = tempDir();
    writeFileSync(join(dir, 'pipeline.py'), `from __future__ import annotations\n${T_FLOW}`, 'utf8');
    const dbPath = seedDb(dir);

    const result = runPythonNode({ cwd: dir, file: 'pipeline.py', func: 'build', kwargs: { db: dbPath } });
    expect(result.stderr).toBe('');
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toMatch(/built rows=2/);
  });

  it('输入表不存在 → 明确说是哪张表读不出来（不是一句 IndexError）', () => {
    const dir = tempDir();
    writeFileSync(join(dir, 'pipeline.py'), T_FLOW, 'utf8');
    const dbPath = makeDb(dir); // 只有 meta，没有 raw_data
    const result = runPythonNode({ cwd: dir, file: 'pipeline.py', func: 'build', kwargs: { db: dbPath } });
    expect(result.exitCode).not.toBe(0);
    expect(result.stderr).toMatch(/raw_data/);
    expect(result.stderr).toMatch(/读不出来/);
  });

  it('声明了 t: 输入却没有 db 入参 → 说清楚平台拿不到连接', () => {
    const dir = tempDir();
    writeFileSync(join(dir, 'pipeline.py'), T_FLOW, 'utf8');
    const result = runPythonNode({ cwd: dir, file: 'pipeline.py', func: 'build', kwargs: { as_of: '2026-01-01' } });
    expect(result.exitCode).not.toBe(0);
    expect(result.stderr).toMatch(/没有 db 入参/);
  });
});
