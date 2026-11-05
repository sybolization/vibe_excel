/**
 * 端到端：从 setup 到 publish，全走真实的 CLI / 插件 / git / python / SQLite。
 *
 * 这个用例把前几轮手工验证过的东西固定下来：
 *   - `run` 真的跑两遍，两遍一致才提升产物、才登记数据版本；
 *   - 不一致就是失败，失败之后 `publish` 拒绝合入；
 *   - `flow.json` 是从**提交**里读的，不是从工作区。
 *
 * 不复制真实的大文件：批次里只有一个占位文件，流程也不物化它。
 */

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { runCli } from '../src/cli.ts';

const repoRoot = resolve(import.meta.dirname, '..', '..', '..');
const pythonPath = join(repoRoot, 'runtime-env', '.venv', 'Scripts', 'python.exe');
const ready = existsSync(pythonPath);

const DETERMINISTIC = `
import sqlite3


def seed(db):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS people (id INTEGER, name TEXT)")
    con.execute("DELETE FROM people")
    con.executemany("INSERT INTO people VALUES (?, ?)", [(1, "ann"), (2, "bob")])
    con.commit()
    con.close()


def build_report(db, as_of):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS report (id INTEGER, tag TEXT)")
    con.execute("DELETE FROM report")
    rows = con.execute("SELECT id, name FROM people ORDER BY id").fetchall()
    con.executemany("INSERT INTO report VALUES (?, ?)", [(i, f"{n}@{as_of}") for i, n in rows])
    con.commit()
    con.close()
    print(f"report rows={len(rows)}")
`;

const NONDETERMINISTIC = `
import sqlite3, uuid


def seed(db):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS people (id INTEGER, name TEXT)")
    con.execute("DELETE FROM people")
    con.execute("INSERT INTO people VALUES (1, 'ann')")
    con.commit()
    con.close()


def build_report(db, as_of):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS report (id INTEGER, tag TEXT)")
    con.execute("DELETE FROM report")
    con.execute("INSERT INTO report VALUES (?, ?)", (1, str(uuid.uuid4())))
    con.commit()
    con.close()
`;

const FLOW = JSON.stringify(
  {
    flow: 'integ',
    db: 'hr.db',
    outputs: ['report'],
    nodes: [
      { id: 'seed', call: 'pipeline.py:seed', reads: [], writes: ['people'] },
      { id: 'build_report', call: 'pipeline.py:build_report', reads: ['people'], writes: ['report'] },
    ],
  },
  null,
  2,
);

/** ADR-0008 的形状：flow.json 不写节点，图从 main + "t:" 标注解析。 */
const CODE_FLOW = JSON.stringify({ flow: 'integ-code', db: 'hr.db', materialize: [], outputs: ['report'] }, null, 2);

const CODE_PIPELINE = `
import sqlite3


def main(db, as_of):
    """集成测试：seed → build_report，节点由解析器从 main 读出。"""
    seed(db)
    build_report(db, as_of)


def seed(db) -> "t:people":
    """写入 people。"""
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS people (id INTEGER, name TEXT)")
    con.execute("DELETE FROM people")
    con.executemany("INSERT INTO people VALUES (?, ?)", [(1, "ann"), (2, "bob")])
    con.commit()
    con.close()


def build_report(db, people: "t:people", as_of: str) -> "t:report":
    """people → report，带上业务日期（输入表由平台递进来，不自己查）。"""
    rows = [(int(row["id"]), str(row["name"]) + "@" + as_of) for _, row in people.iterrows()]
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS report (id INTEGER, tag TEXT)")
    con.execute("DELETE FROM report")
    con.executemany("INSERT INTO report VALUES (?, ?)", rows)
    con.commit()
    con.close()
    print(f"report rows={len(rows)}")
`;

function manifestFor(dir: string, store: string, inputs: string): string {
  const p = (name: string): string => join(repoRoot, 'packages', 'harness', 'src', 'plugins', name).replace(/\\/g, '/');
  const lines = [
    'plugins:',
    `  - id: commands`,
    `    name: ${p('commands.ts')}`,
    '  - id: telemetry',
    `    name: ${p('telemetry.ts')}`,
    '    config:',
    '      echo: false',
    '  - id: store',
    `    name: ${p('store.ts')}`,
    '    config:',
    `      storeRoot: '${store.replace(/\\/g, '/')}'`,
    '  - id: workspace',
    `    name: ${p('workspace.ts')}`,
    '  - id: exec',
    `    name: ${p('exec.ts')}`,
    '    config:',
    `      python: '${pythonPath.replace(/\\/g, '/')}'`,
    '      timeoutMs: 60000',
    '  - id: agent-pi',
    `    name: ${p('agent-pi.ts')}`,
    '    config:',
    '      provider: stub',
    '      model: stub',
    '  - id: sessions',
    `    name: ${p('sessions.ts')}`,
    '    config:',
    '      program: integ',
    "      asOf: '2026-01-01'",
    '      inputs:',
    `        dir: '${inputs.replace(/\\/g, '/')}'`,
    '        files:',
    '          - dummy.txt',
    '',
  ];
  const path = join(dir, 'vibe.plugins.yml');
  writeFileSync(path, lines.join('\n'), 'utf8');
  return path;
}

const git = (cwd: string, args: string[]): string =>
  execFileSync('git', ['-c', 'user.name=integ', '-c', 'user.email=integ@local', '-C', cwd, ...args], {
    encoding: 'utf8',
  });

const sessionCodeDir = (store: string): string => {
  const sessionsRoot = join(store, 'sessions');
  const session = readdirSync(sessionsRoot, { withFileTypes: true }).find((entry) => entry.isDirectory());
  if (!session) throw new Error(`没有会话目录：${sessionsRoot}`);
  return join(sessionsRoot, session.name, 'code');
};

/**
 * 最后一次 run 的 run.json。
 *
 * 注意：不能按目录名排序取最后一个——`RunId` 是 `randomUUID()`，名字里没有时间，
 * 排序结果跟新旧无关（曾因此偶发「exit 1 但读到上一条成功的 run」）。按 mtime 取。
 */
const latestRunJson = (store: string): {
  identical?: boolean;
  status?: string;
  outRevisionId?: string | null;
  graph?: { source?: string; entry?: string | null; nodes?: { id: string; reads: string[]; writes: string[] }[] };
} => {
  const session = readdirSync(join(store, 'sessions'), { withFileTypes: true }).find((entry) => entry.isDirectory())!;
  const runsRoot = join(store, 'sessions', session.name, 'runs');
  const runs = readdirSync(runsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith('author-') && !entry.name.startsWith('optimize-'))
    // 以 run.json 的落盘时间排序：它是在运行**结束时**写的，最能代表「哪次最新」。
    .filter((entry) => existsSync(join(runsRoot, entry.name, 'run.json')))
    .map((entry) => ({ name: entry.name, mtime: statSync(join(runsRoot, entry.name, 'run.json')).mtimeMs }))
    .sort((a, b) => a.mtime - b.mtime);
  const latest = runs.at(-1)?.name;
  if (!latest) throw new Error('没有 run 目录');
  return JSON.parse(readFileSync(join(runsRoot, latest, 'run.json'), 'utf8')) as {
    identical?: boolean;
    status?: string;
    outRevisionId?: string | null;
    graph?: { source?: string; entry?: string | null; nodes?: { id: string; reads: string[]; writes: string[] }[] };
  };
};

/** 起一个全新的制品根 + 一个只跑得通 setup 的 CLI。两条用例共用，别各抄一遍。 */
async function scaffold(): Promise<{
  store: string;
  lines: string[];
  errors: string[];
  cli: (args: string[]) => Promise<number>;
  code: string;
}> {
  const dir = mkdtempSync(join(tmpdir(), 'vibe-e2e-'));
  const store = join(dir, 'store');
  const inputs = join(dir, 'inputs');
  mkdirSync(inputs, { recursive: true });
  writeFileSync(join(inputs, 'dummy.txt'), 'placeholder\n', 'utf8');
  const manifest = manifestFor(dir, store, inputs);
  const lines: string[] = [];
  const errors: string[] = [];
  const cli = async (args: string[]): Promise<number> => {
    const original = console.error;
    console.error = (...values: unknown[]) => {
      errors.push(values.map((value) => String(value)).join(' '));
    };
    try {
      return await runCli(args, { manifest, env: {} as NodeJS.ProcessEnv, out: (line) => lines.push(line) });
    } finally {
      console.error = original;
    }
  };

  const setupCode = await cli(['setup']);
  expect(setupCode, errors.join('\n')).toBe(0);
  return { store, lines, errors, cli, code: sessionCodeDir(store) };
}

describe.skipIf(!ready)('端到端：setup → run（双跑）→ publish', () => {
  it('两遍一致才登记数据版本、才允许发布；不一致就失败并拒绝发布', async () => {
    const { store, lines, errors, cli, code } = await scaffold();

    writeFileSync(join(code, 'pipeline.py'), DETERMINISTIC, 'utf8');
    writeFileSync(join(code, 'flow.json'), FLOW, 'utf8');
    git(code, ['add', '-A']);
    git(code, ['commit', '-m', 'integ: flow + pipeline']);

    // 1) 双跑一致
    expect(await cli(['run'])).toBe(0);
    const run = latestRunJson(store);
    expect(run.status).toBe('succeeded');
    expect(run.identical).toBe(true);
    expect(run.outRevisionId).toBeTruthy();

    // 2) 带证据发布
    expect(await cli(['publish'])).toBe(0);

    // 3) 把节点改成不确定：双跑不一致 → run 失败 → publish 拒绝
    writeFileSync(join(code, 'pipeline.py'), NONDETERMINISTIC, 'utf8');
    git(code, ['add', '-A']);
    git(code, ['commit', '-m', 'integ: nondeterministic']);

    // 失败时把 stderr 带进断言：不然「exit 1 但最新 run 是上次那条」看不出到底为什么失败。
    const failedCode = await cli(['run']);
    const failed = latestRunJson(store);
    expect(failedCode, errors.join('\n')).toBe(1);
    expect(failed.status, errors.join('\n')).toBe('failed');
    expect(failed.identical).toBe(false);
    expect(failed.outRevisionId).toBeNull();

    expect(await cli(['publish'])).toBe(1);

    // 4) flow.json 读的是提交：工作区改了但没提交，run 仍然按上一版跑
    writeFileSync(join(code, 'flow.json'), FLOW.replace('"integ"', '"integ-uncommitted"'), 'utf8');
    await cli(['flow']).then((code) => expect(code).toBe(0));
    expect(lines.join('\n')).toMatch(/flow integ@/);
  }, 120_000);

  it('证据必须属于当前草稿头：run 之后再提交一次，publish 拒绝；退回被验证的提交才放行', async () => {
    const { store, cli, code, errors } = await scaffold();

    writeFileSync(join(code, 'pipeline.py'), DETERMINISTIC, 'utf8');
    writeFileSync(join(code, 'flow.json'), FLOW, 'utf8');
    git(code, ['add', '-A']);
    git(code, ['commit', '-m', 'integ: verified']);

    expect(await cli(['run']), errors.join('\n')).toBe(0);
    const verified = latestRunJson(store);
    expect(verified.status).toBe('succeeded');
    expect(verified.identical).toBe(true);

    // 在通过的那次 run 之后再加一个提交：这个提交从没跑过双跑。
    // 旧实现只比 run.json 的 status/identical，会把这条未验证的提交一起合进 main。
    writeFileSync(join(code, 'pipeline.py'), `${DETERMINISTIC}\n# 只在通过之后加的一行注释\n`, 'utf8');
    git(code, ['add', '-A']);
    git(code, ['commit', '-m', 'integ: unverified']);

    expect(await cli(['publish'])).toBe(1);
    expect(errors.join('\n')).toMatch(/证据已经过期/);

    // 回到被验证的那个提交：能发布，说明拒绝的依据是「对不上」而不是「一律不放」。
    git(code, ['reset', '--hard', 'HEAD~1']);
    expect(await cli(['publish']), errors.join('\n')).toBe(0);
  }, 120_000);

  it('flow.json 不写 nodes：图从 main 解析出来，照样双跑一致并登记数据版本', async () => {
    const { store, errors, cli, code } = await scaffold();

    writeFileSync(join(code, 'pipeline.py'), CODE_PIPELINE, 'utf8');
    writeFileSync(join(code, 'flow.json'), CODE_FLOW, 'utf8');
    git(code, ['add', '-A']);
    git(code, ['commit', '-m', 'integ: code as flow']);

    expect(await cli(['run']), errors.join('\n')).toBe(0);
    const run = latestRunJson(store);
    expect(run.status, errors.join('\n')).toBe('succeeded');
    expect(run.identical).toBe(true);
    expect(run.outRevisionId).toBeTruthy();

    // 图不进版本库：它只出现在运行记录里，作为这次运行的证据（ADR-0008 §4）。
    expect(run.graph?.source).toBe('code');
    expect(run.graph?.entry).toBe('pipeline.py');
    expect(run.graph?.nodes?.map((node) => node.id)).toEqual(['seed', 'build_report']);
    expect(run.graph?.nodes?.map((node) => ({ reads: node.reads, writes: node.writes }))).toEqual([
      { reads: [], writes: ['people'] },
      { reads: ['people'], writes: ['report'] },
    ]);

    // 顺带把「解析失败」这条也钉住：main 里写 for，run 必须直接失败、而不是安静跑一半。
    writeFileSync(
      join(code, 'pipeline.py'),
      CODE_PIPELINE.replace('    seed(db)\n', '    for f in (seed,):\n        f(db)\n'),
      'utf8',
    );
    git(code, ['add', '-A']);
    git(code, ['commit', '-m', 'integ: main 里写 for']);
    expect(await cli(['run'])).toBe(1);
    expect(errors.join('\n')).toMatch(/MAIN_CONTROL_FLOW/);
  }, 150_000);
});
