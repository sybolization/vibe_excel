/**
 * 工作区事实：探测要准，缺东西要给出下一步。
 *
 * 这里守的不是「渲染得好不好看」，而是两件会造成实际错误的事：
 *   1. 表里写的路径必须是**探测出来的**（照抄配置就会在 venv 被重建后说假话）；
 *   2. 探测不到的东西必须带一条可执行的建议，而不是只写 missing。
 */

import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { afterEach, describe, expect, it } from 'vitest';

import { fmtBytes, probeWorkspace, renderWorkspaceFacts, writeAgentFactsFile, type WorkspaceFacts } from '../src/workspace-facts.ts';

const tempDirs: string[] = [];

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'vibe-facts-'));
  tempDirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

/** 造一个「什么都齐」的工作区。 */
function fullWorkspace() {
  const root = tempDir();
  const code = join(root, 'code');
  const agent = join(root, 'agent');
  const work = join(root, 'work');
  const inputs = join(work, 'inputs', 'batch-a', 'files');
  const out = join(work, 'out');
  const venv = join(root, 'runtime-env', '.venv');
  for (const dir of [code, agent, work, inputs, out, join(venv, 'Scripts')]) mkdirSync(dir, { recursive: true });

  writeFileSync(join(venv, 'pyvenv.cfg'), 'home = C:/python\nversion = 3.13.7\nuv = 0.12.9\n');
  writeFileSync(join(venv, 'Scripts', 'python.exe'), '');
  writeFileSync(join(code, 'pipeline.py'), 'def build(): pass\n');
  writeFileSync(join(code, 'helpers.py'), '');
  writeFileSync(join(code, 'flow.json'), '{}');
  writeFileSync(join(inputs, '可用岗位表.xlsx'), 'x'.repeat(2048));
  writeFileSync(join(inputs, '所有员工.XLS'), 'y'.repeat(1024));

  const dbPath = join(work, 'hr.db');
  const db = new DatabaseSync(dbPath);
  db.exec('CREATE TABLE employees(id INTEGER); INSERT INTO employees VALUES (1),(2),(3); CREATE TABLE positions(id INTEGER);');
  db.close();

  return { root, code, agent, work, inputs, out, venv, dbPath };
}

describe('probeWorkspace', () => {
  it('探测真实文件：venv 由 uv 建、库里有表、输入有几个文件', () => {
    const ws = fullWorkspace();
    const facts = probeWorkspace(
      {
        program: 'molex-prep',
        sessionId: 's1',
        root: ws.root,
        work: ws.work,
        code: ws.code,
        agent: ws.agent,
        python: join(ws.venv, 'Scripts', 'python.exe'),
        dbPath: ws.dbPath,
        flowPath: join(ws.code, 'flow.json'),
        inputsDir: ws.inputs,
        outDir: ws.out,
        asOf: '2026-10-23',
      },
      {
        // 不真的起进程：python / uv 的输出由桩给。
        run: (file, args) => {
          if (file.endsWith('python.exe') && args[0] === '--version') return 'Python 3.13.7\n';
          if (file.endsWith('python.exe')) return JSON.stringify({ pandas: '2.2.3', openpyxl: '3.1.5', xlrd: null });
          if (file === 'uv') return 'uv 0.12.9 (9f9286029 2026-09-01)\n';
          if (file === 'where') return 'C:\\Users\\x\\.local\\bin\\uv.exe\r\n';
          return null;
        },
        now: () => new Date('2026-11-04T14:52:10'),
      },
    );

    expect(facts.python.exists).toBe(true);
    expect(facts.python.version).toBe('3.13.7');
    expect(facts.python.venv?.managedBy).toBe('uv');
    expect(facts.uv).toEqual({ path: 'C:\\Users\\x\\.local\\bin\\uv.exe', version: '0.12.9' });
    expect(facts.db.exists).toBe(true);
    expect(facts.db.tables).toEqual([
      { name: 'employees', rows: 3 },
      { name: 'positions', rows: 0 },
    ]);
    // 顺序取决于 locale collation，这里只关心「两个文件都看到了」。
    expect(facts.inputs.files.map((file) => file.name).sort()).toEqual(['可用岗位表.xlsx', '所有员工.XLS'].sort());
    expect(facts.code.pyFiles).toBe(2);
    expect(facts.flow.exists).toBe(true);
    expect(facts.probeAt).toBe('2026-11-04 14:52');
    expect(facts.python.packages).toEqual([
      { name: 'pandas', version: '2.2.3' },
      { name: 'openpyxl', version: '3.1.5' },
      { name: 'xlrd', version: null },
    ]);
  });

  it('什么都缺的时候不抛异常，而是把「缺」记下来', () => {
    const root = tempDir();
    const facts = probeWorkspace(
      {
        program: 'p',
        sessionId: 's',
        root,
        work: join(root, 'work'),
        code: join(root, 'code'),
        agent: join(root, 'agent'),
        python: join(root, 'nope', 'python.exe'),
        dbPath: join(root, 'work', 'hr.db'),
        flowPath: join(root, 'code', 'flow.json'),
        inputsDir: join(root, 'inputs'),
        outDir: join(root, 'out'),
        asOf: '2026-10-23',
      },
      { run: () => null, now: () => new Date('2026-11-04T00:00:00') },
    );
    expect(facts.python.exists).toBe(false);
    expect(facts.python.venv).toBeNull();
    expect(facts.db.exists).toBe(false);
    expect(facts.inputs.files).toEqual([]);
    expect(facts.uv.version).toBeNull();
  });
});

describe('renderWorkspaceFacts', () => {
  const base: WorkspaceFacts = {
    probeAt: '2026-11-04 14:52',
    program: 'molex-prep',
    sessionId: '23e16fce',
    asOf: '2026-10-23',
    root: 'C:/store/sessions/23e16fce',
    code: { path: 'C:/store/sessions/23e16fce/code', pyFiles: 3 },
    python: {
      path: 'C:/store/runtime-env/.venv/Scripts/python.exe',
      exists: true,
      version: '3.13.7',
      venv: { dir: 'C:/store/runtime-env/.venv', managedBy: 'uv' },
      packages: [
        { name: 'pandas', version: '2.2.3' },
        { name: 'openpyxl', version: '3.1.5' },
        { name: 'xlrd', version: null },
      ],
    },
    uv: { path: 'C:/Users/x/.local/bin/uv.exe', version: '0.12.9' },
    db: {
      path: 'C:/store/work/hr.db',
      exists: true,
      bytes: 74 * 1024 * 1024,
      mtime: '2026-11-03 16:20',
      tables: [
        { name: 'employees', rows: 117777 },
        { name: 'positions', rows: 6337 },
      ],
    },
    inputs: {
      dir: 'C:/store/inputs/batch-a/files',
      exists: true,
      files: [{ name: '所有员工.XLS', bytes: 66_060_288 }],
    },
    out: { dir: 'C:/store/work/out', exists: true, count: 0 },
    flow: { path: 'C:/store/code/flow.json', exists: true },
  };

  it('把路径和实测状态写进表里，并且说明这是探测结果', () => {
    const text = renderWorkspaceFacts(base);
    expect(text).toContain('vibectl 探测于 2026-11-04 14:52');
    expect(text).toContain('C:/store/work/hr.db');
    expect(text).toContain('74.0 MB');
    expect(text).toContain('2 张表');
    expect(text).toContain('uv 建的 venv');
    expect(text).toContain('3.13.7');
    expect(text).toContain('employees(117777)');
    expect(text).toContain('所有员工.XLS (63.0 MB)');
    expect(text).toContain('输出目录');
    expect(text).toContain('空');
  });

  it('缺包时给出能直接粘的安装命令，而不是只写 missing', () => {
    const text = renderWorkspaceFacts(base);
    expect(text).toContain('### 缺的东西');
    expect(text).toContain('`uv pip install xlrd`');
  });

  it('缺库 / 缺 flow 时给出下一步，而不是让模型去猜', () => {
    const empty: WorkspaceFacts = {
      ...base,
      db: { ...base.db, exists: false, bytes: 0, mtime: null, tables: [] },
      flow: { ...base.flow, exists: false },
      inputs: { ...base.inputs, files: [] },
    };
    const text = renderWorkspaceFacts(empty);
    expect(text).toContain('业务库还不存在');
    expect(text).toContain('vibectl setup');
    expect(text).toContain('流程声明还不存在');
    expect(text).toContain('输入批次是空的');
    // 不能把 0 字节说成「0.0 B，0 张表，写于 未知」这种像是有数据的样子。
    expect(text).toContain('**不存在**');
  });
});

describe('writeAgentFactsFile', () => {
  it('写进 agent 目录的 AGENTS.md（pi 的 context file 位置），并且带使用说明', () => {
    const ws = fullWorkspace();
    const facts = probeWorkspace(
      {
        program: 'p',
        sessionId: 's',
        root: ws.root,
        work: ws.work,
        code: ws.code,
        agent: ws.agent,
        python: join(ws.venv, 'Scripts', 'python.exe'),
        dbPath: ws.dbPath,
        flowPath: join(ws.code, 'flow.json'),
        inputsDir: ws.inputs,
        outDir: ws.out,
        asOf: '2026-10-23',
      },
      {
        run: (file, args) => {
          if (args[0] === '--version' && file.endsWith('python.exe')) return 'Python 3.13.7\n';
          if (file.endsWith('python.exe')) return JSON.stringify({ pandas: '2.2.3' });
          return null;
        },
        now: () => new Date('2026-11-04T14:52:10'),
      },
    );
    const path = writeAgentFactsFile(facts, ws.agent);
    expect(path).toBe(join(ws.agent, 'AGENTS.md'));
    const text = readFileSync(path, 'utf8');
    expect(text).toContain('工作区事实');
    expect(text).toContain('不要再去找');
    expect(text).toContain('2026-10-23');
  });
});

describe('fmtBytes', () => {
  it('按量级给两位有效数字', () => {
    expect(fmtBytes(0)).toBe('0 B');
    expect(fmtBytes(999)).toBe('999 B');
    expect(fmtBytes(2048)).toBe('2.0 KB');
    expect(fmtBytes(66_060_288)).toBe('63.0 MB');
  });
});
