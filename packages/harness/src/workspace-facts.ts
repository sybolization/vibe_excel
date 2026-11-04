/**
 * 工作区事实：把「数据库在哪、Python 在哪、venv 里装了什么」变成一份**探测出来的**表。
 *
 * 为什么不是写死在提示词里：路径会变（换机器、目录搬家、venv 被 uv 重建）。写死的句子
 * 在路径失效时会**安静地**变成谎言——模型拿着一条不存在的路径，提示词还理直气壮地说
 * 「只能用这个」。所以这里每次运行前重新探测，探测不到的东西必须给出**可执行的下一步**，
 * 而不是一句 missing。
 *
 * 这个模块只回答「工作区长什么样」，不回答「代码要怎么写」——后者是提示词的事。
 */

import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync, type Dirent } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

/** 探测时要知道的坐标，全部来自 resolveContext()。 */
export interface WorkspaceFactsInput {
  program: string;
  sessionId: string;
  /** 会话根。 */
  root: string;
  work: string;
  code: string;
  /** pi 的 agent 目录（`PI_CODING_AGENT_DIR`），AGENTS.md 就落在这里。 */
  agent: string;
  python: string;
  dbPath: string;
  /** `code` 下的流程声明文件（绝对路径）。 */
  flowPath: string;
  inputsDir: string;
  outDir: string;
  asOf: string;
  /** 要检查的包名；默认是读 ground truth 那三个。 */
  probePackages?: readonly string[];
}

export interface PackageState {
  name: string;
  /** null = 没装。 */
  version: string | null;
}

export interface TableState {
  name: string;
  rows: number;
}

export interface WorkspaceFacts {
  probeAt: string;
  program: string;
  sessionId: string;
  asOf: string;
  root: string;
  code: { path: string; pyFiles: number };
  python: {
    path: string;
    exists: boolean;
    version: string | null;
    venv: { dir: string; managedBy: 'uv' | 'venv' | 'unknown' } | null;
    packages: PackageState[];
  };
  uv: { path: string | null; version: string | null };
  db: { path: string; exists: boolean; bytes: number; mtime: string | null; tables: TableState[] };
  inputs: { dir: string; exists: boolean; files: Array<{ name: string; bytes: number }> };
  out: { dir: string; exists: boolean; count: number };
  flow: { path: string; exists: boolean };
}

/** 可替换的副作用（测试用；生产走默认实现）。 */
export interface ProbeDeps {
  /** 同步执行并返回 stdout，失败（含超时/找不到）返回 null。 */
  run?: (file: string, args: string[]) => string | null;
  /** 读表清单；打不开返回空数组。 */
  tables?: (dbPath: string) => TableState[];
  now?: () => Date;
}

const DEFAULT_PACKAGES = ['pandas', 'openpyxl', 'xlrd'] as const;

/** 读 venv 里的包：只查 dist-info，不真的 import pandas（快一个数量级）。 */
const PACKAGE_SCRIPT = [
  'import json,sys,importlib.metadata as m',
  'r={}',
  'for n in sys.argv[1:]:',
  '    try:r[n]=m.version(n)',
  '    except Exception:r[n]=None',
  'print(json.dumps(r))',
].join('\n');

function defaultRun(file: string, args: string[]): string | null {
  try {
    return execFileSync(file, args, { encoding: 'utf8', timeout: 8000, windowsHide: true });
  } catch {
    return null;
  }
}

/**
 * 表清单 + 行数。故意不带 digest：这里只要「库里有几张表」，
 * 全量哈希是 run 的活（74 MB 的库要三秒，探测不该这么贵）。
 */
function defaultTables(dbPath: string): TableState[] {
  let db: DatabaseSync | null = null;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
    const names = (
      db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").all() as {
        name: string;
      }[]
    ).map((row) => row.name);
    return names.map((name) => {
      try {
        const quoted = `"${name.replace(/"/g, '""')}"`;
        const row = db?.prepare(`SELECT COUNT(*) AS n FROM ${quoted}`).get() as { n: number } | undefined;
        return { name, rows: Number(row?.n ?? 0) };
      } catch {
        return { name, rows: 0 };
      }
    });
  } catch {
    return [];
  } finally {
    try {
      db?.close();
    } catch {
      /* 关不掉就算了，探测不该因为收尾失败而炸。 */
    }
  }
}

/** `<venv>/Scripts/python.exe`（Windows）或 `<venv>/bin/python`（POSIX）→ `<venv>`。 */
function venvDirOf(python: string): string | null {
  const tail = basename(python).toLowerCase();
  const parent = basename(dirname(python)).toLowerCase();
  if ((tail === 'python.exe' && parent === 'scripts') || (tail === 'python' && parent === 'bin')) {
    return dirname(dirname(python));
  }
  return null;
}

/** uv 建 venv 时会在 pyvenv.cfg 里写一行 `uv = <版本>`，靠它区分「uv 建的」和「venv 建的」。 */
function venvManagerOf(dir: string): 'uv' | 'venv' | 'unknown' {
  const cfg = join(dir, 'pyvenv.cfg');
  if (!existsSync(cfg)) return 'unknown';
  try {
    return readFileSync(cfg, 'utf8')
      .split(/\r?\n/)
      .some((line) => /^\s*uv\s*=/.test(line))
      ? 'uv'
      : 'venv';
  } catch {
    return 'unknown';
  }
}

function countPy(dir: string): number {
  if (!existsSync(dir)) return 0;
  let total = 0;
  const walk = (base: string): void => {
    let entries: Dirent<string>[];
    try {
      entries = readdirSync(base, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.name === '.git' || entry.name === 'node_modules') continue;
      const full = join(base, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.isFile() && entry.name.endsWith('.py')) total += 1;
    }
  };
  walk(dir);
  return total;
}

function listInputs(dir: string): Array<{ name: string; bytes: number }> {
  if (!existsSync(dir)) return [];
  try {
    return readdirSync(dir, { withFileTypes: true })
      .filter((entry) => entry.isFile())
      .map((entry) => ({ name: entry.name, bytes: statSync(join(dir, entry.name)).size }))
      .sort((a, b) => a.name.localeCompare(b.name));
  } catch {
    return [];
  }
}

const pad = (n: number): string => String(n).padStart(2, '0');

function stampOf(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function probeWorkspace(input: WorkspaceFactsInput, deps: ProbeDeps = {}): WorkspaceFacts {
  const run = deps.run ?? defaultRun;
  const tables = deps.tables ?? defaultTables;
  const now = deps.now ?? (() => new Date());

  const pythonExists = existsSync(input.python);
  const pythonVersion = pythonExists
    ? (() => {
        const raw = run(input.python, ['--version']);
        return raw?.match(/(\d+\.\d+\.\d+)/)?.[1] ?? null;
      })()
    : null;

  const warned = input.probePackages ?? DEFAULT_PACKAGES;
  const packages: PackageState[] =
    pythonExists && pythonVersion
      ? (() => {
          const raw = run(input.python, ['-c', PACKAGE_SCRIPT, ...warned]);
          if (!raw) return warned.map((name) => ({ name, version: null }));
          try {
            const parsed = JSON.parse(raw) as Record<string, string | null>;
            return warned.map((name) => ({ name, version: parsed[name] ?? null }));
          } catch {
            return warned.map((name) => ({ name, version: null }));
          }
        })()
      : warned.map((name) => ({ name, version: null }));

  const venvDir = venvDirOf(input.python);
  const uvRaw = run('uv', ['--version']);
  const uvVersion = uvRaw?.match(/(\d+\.\d+\.\d+)/)?.[1] ?? null;
  const uvPath = uvVersion === null ? null : findUv(run);

  const dbExists = existsSync(input.dbPath);
  const dbStat = dbExists
    ? (() => {
        try {
          return statSync(input.dbPath);
        } catch {
          return null;
        }
      })()
    : null;

  const outExists = existsSync(input.outDir);
  let outCount = 0;
  if (outExists) {
    try {
      outCount = readdirSync(input.outDir).length;
    } catch {
      outCount = 0;
    }
  }

  return {
    probeAt: stampOf(now()),
    program: input.program,
    sessionId: input.sessionId,
    asOf: input.asOf,
    root: input.root,
    code: { path: input.code, pyFiles: countPy(input.code) },
    python: {
      path: input.python,
      exists: pythonExists,
      version: pythonVersion,
      venv: venvDir ? { dir: venvDir, managedBy: venvManagerOf(venvDir) } : null,
      packages,
    },
    uv: { path: uvPath, version: uvVersion },
    db: {
      path: input.dbPath,
      exists: dbExists,
      bytes: dbStat?.size ?? 0,
      mtime: dbStat ? stampOf(dbStat.mtime) : null,
      tables: dbExists && dbStat && dbStat.size > 0 ? tables(input.dbPath) : [],
    },
    inputs: { dir: input.inputsDir, exists: existsSync(input.inputsDir), files: listInputs(input.inputsDir) },
    out: { dir: input.outDir, exists: outExists, count: outCount },
    flow: { path: input.flowPath, exists: existsSync(input.flowPath) },
  };
}

/** `uv --version` 成功只说明它在 PATH 上；具体位置给不出来就算了，不为了好看去查注册表。 */
function findUv(run: (file: string, args: string[]) => string | null): string | null {
  const raw = run(process.platform === 'win32' ? 'where' : 'which', ['uv']);
  const first = raw
    ?.split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line !== '');
  return first ?? 'uv';
}

export function fmtBytes(bytes: number): string {
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

/**
 * 渲染成 markdown。这是给模型看的，也是给人 review 的；
 * 探测不到的东西一律落到「缺的东西」一节，写成下一步动作。
 */
export function renderWorkspaceFacts(facts: WorkspaceFacts): string {
  const lines: string[] = [];
  const venvNote =
    facts.python.venv === null
      ? '不在 venv 里'
      : facts.python.venv.managedBy === 'uv'
        ? 'uv 建的 venv'
        : facts.python.venv.managedBy === 'venv'
          ? 'venv 建的（非 uv）'
          : 'venv（来源不明）';

  lines.push(`## 工作区事实（vibectl 探测于 ${facts.probeAt}，本文件每次运行前重写，别手改）`);
  lines.push('');
  lines.push(`程序 \`${facts.program}\`　会话 \`${facts.sessionId}\`　业务日期 \`${facts.asOf}\``);
  lines.push('');
  lines.push('| 项 | 路径 | 实测状态 |');
  lines.push('|---|---|---|');
  lines.push(`| 程序仓 | \`${facts.code.path}\` | ${facts.code.pyFiles} 个 .py 文件 |`);
  lines.push(
    facts.python.exists
      ? `| Python | \`${facts.python.path}\` | ${facts.python.version ?? '版本未知'}（${venvNote}） |`
      : `| Python | \`${facts.python.path}\` | **不存在** |`,
  );
  lines.push(
    facts.uv.version === null
      ? '| uv | — | **不在 PATH 上** |'
      : `| uv | \`${facts.uv.path ?? 'uv'}\` | ${facts.uv.version} |`,
  );
  lines.push(
    facts.db.exists
      ? `| 业务库 | \`${facts.db.path}\` | ${fmtBytes(facts.db.bytes)}，${facts.db.tables.length} 张表，写于 ${facts.db.mtime ?? '未知'} |`
      : `| 业务库 | \`${facts.db.path}\` | **不存在** |`,
  );
  lines.push(
    facts.inputs.exists
      ? `| 输入批次 | \`${facts.inputs.dir}\` | ${facts.inputs.files.length} 个文件 |`
      : `| 输入批次 | \`${facts.inputs.dir}\` | **目录不存在** |`,
  );
  lines.push(
    `| 输出目录 | \`${facts.out.dir}\` | ${facts.out.exists ? (facts.out.count === 0 ? '空' : `${facts.out.count} 个文件`) : '**不存在**'} |`,
  );
  lines.push(
    facts.flow.exists
      ? `| 流程声明 | \`${facts.flow.path}\` | 存在 |`
      : `| 流程声明 | \`${facts.flow.path}\` | **不存在** |`,
  );

  if (facts.db.tables.length > 0) {
    const shown = facts.db.tables.slice(0, 12).map((table) => `${table.name}(${table.rows})`);
    const more = facts.db.tables.length > 12 ? ` …另 ${facts.db.tables.length - 12} 张` : '';
    lines.push('');
    lines.push(`业务库里的表：${shown.join(' / ')}${more}`);
  }

  if (facts.inputs.files.length > 0) {
    const shown = facts.inputs.files.slice(0, 6).map((file) => `${file.name} (${fmtBytes(file.bytes)})`);
    const more = facts.inputs.files.length > 6 ? ` …另 ${facts.inputs.files.length - 6} 个` : '';
    lines.push('');
    lines.push(`输入文件：${shown.join('、')}${more}`);
  }

  const installed = facts.python.packages.filter((pkg) => pkg.version !== null);
  const missing = facts.python.packages.filter((pkg) => pkg.version === null);
  if (facts.python.exists && facts.python.packages.length > 0) {
    lines.push('');
    lines.push(
      `Python 包：${installed.length === 0 ? '（一个都没装上）' : installed.map((pkg) => `${pkg.name} ${pkg.version}`).join('、')}`,
    );
  }

  const todo: string[] = [];
  if (!facts.python.exists) {
    todo.push(`解释器不存在：先跑 \`vibectl setup\`，或用 \`uv venv\` 建一个再把路径写进 \`vibe.plugins.yml\``);
  }
  if (missing.length > 0) {
    todo.push(`没装 ${missing.map((pkg) => `\`${pkg.name}\``).join('、')}：\`uv pip install ${missing.map((pkg) => pkg.name).join(' ')}\``);
  }
  if (!facts.db.exists) {
    todo.push('业务库还不存在：先跑 `vibectl setup`（或让导入节点先建表），不要假设里面有数据');
  }
  if (!facts.flow.exists) {
    todo.push('流程声明还不存在：`flow` 命令会因此失败；先写 `flow.json` 再跑 run');
  }
  if (!facts.inputs.exists || facts.inputs.files.length === 0) {
    todo.push('输入批次是空的：物化会读到 0 个文件，先确认 `inputs.dir` 指对了');
  }
  if (todo.length > 0) {
    lines.push('');
    lines.push('### 缺的东西（照这个做，别猜）');
    lines.push('');
    for (const item of todo) lines.push(`- ${item}`);
  }

  return lines.join('\n');
}

/**
 * 把事实写成 pi 的 context file。
 *
 * 落在 agent 目录而不是程序仓：pi 会读 `<agent-dir>/AGENTS.md` 作为用户指令，作用域是全局；
 * 而且 agent 目录在 git 之外，不会每次 author 都在程序仓留下一个 diff。
 */
export function writeAgentFactsFile(facts: WorkspaceFacts, agentDir: string): string {
  const path = join(agentDir, 'AGENTS.md');
  mkdirSync(agentDir, { recursive: true });
  writeFileSync(
    path,
    `${renderWorkspaceFacts(facts)}

## 使用方式

- 上面的路径是**实测的绝对路径**，直接用，不要再去找、不要在代码里写死别的路径。
- 这份表是探测结果，不是配置；表过期了以实测为准，并且应当在回复里说明哪里对不上。
- 业务日期 \`${facts.asOf}\` 是钉住的，不要把当前系统日期当成业务日期。
`,
    'utf8',
  );
  return path;
}
