/**
 * 干净重跑（指南 §6.4，ADR-0003）。
 *
 * 要求：用确定的代码版本和输入，在**全新工作目录**执行，并且不隐含依赖当前时间。
 * 做法：从程序仓库按 commit 开一个 detached worktree——它天然是全新目录且内容确定，
 * 不需要复制文件，也不会被主工作区的未提交改动污染。
 *
 * 输出比较按业务内容进行，不要求 xlsx 二进制哈希相同（指南 §6.4）：
 * 这里只记录每个文件的内容哈希，是否「等价」由调用方按业务口径判断。
 */

import { spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export interface CleanRunEntry {
  command: string;
  args: string[];
}

export interface CleanRunOptions {
  repoPath: string;
  commit: string;
  /** 每次重跑在其下建独立目录。 */
  runRoot: string;
  entry: CleanRunEntry;
  /** 声明的产出文件（相对 worktree 根的路径）。 */
  outputs?: string[];
  env?: Record<string, string>;
  timeoutMs?: number;
  /** 固定业务日期（YYYY-MM-DD），避免程序隐含依赖「今天」。 */
  asOfDate?: string;
}

export interface FileDigest {
  path: string;
  sha256: string;
  bytes: number;
}

export interface CleanRunResult {
  exitCode: number;
  timedOut: boolean;
  stdout: string;
  stderr: string;
  /** 执行用的 worktree，执行后已移除；保留路径仅用于记录。 */
  workDir: string;
  artifactsDir: string;
  outputs: FileDigest[];
  /** worktree 里全部文件的内容哈希，便于跨次比较业务内容。 */
  files: FileDigest[];
  manifestPath: string;
}

/** 干净重跑只需要操作系统基础变量；模型凭证一律不带进去。 */
const BASE_ENV_KEYS = [
  'PATH',
  'Path',
  'SystemRoot',
  'SYSTEMROOT',
  'windir',
  'TEMP',
  'TMP',
  'USERPROFILE',
  'HOMEDRIVE',
  'HOMEPATH',
  'APPDATA',
  'LOCALAPPDATA',
  'COMSPEC',
  'PATHEXT',
  'NUMBER_OF_PROCESSORS',
  'PROCESSOR_ARCHITECTURE',
  'OS',
  'PYTHONUTF8',
  'PYTHONIOENCODING',
];

function baseEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const key of BASE_ENV_KEYS) {
    const value = process.env[key];
    if (value !== undefined) env[key] = value;
  }
  return env;
}

const sha256Of = (path: string): string => createHash('sha256').update(readFileSync(path)).digest('hex');

/** 递归列出 worktree 内所有文件（跳过 .git）。 */
function listFiles(root: string, base = ''): FileDigest[] {
  const out: FileDigest[] = [];
  for (const entry of readdirSync(join(root, base), { withFileTypes: true })) {
    if (entry.name === '.git') continue;
    const rel = base === '' ? entry.name : join(base, entry.name);
    const absolute = join(root, rel);
    if (entry.isDirectory()) {
      out.push(...listFiles(root, rel));
    } else if (entry.isFile()) {
      out.push({ path: rel.replace(/\\/g, '/'), sha256: sha256Of(absolute), bytes: statSync(absolute).size });
    }
  }
  return out;
}

function gitWorktree(repoPath: string, args: string[]): void {
  const result = spawnSync('git', ['-C', repoPath, 'worktree', ...args], { encoding: 'utf8' });
  if (result.status !== 0) {
    throw new Error(`git worktree ${args.join(' ')} 失败：${result.stderr || result.stdout}`);
  }
}

export function cleanRerun(options: CleanRunOptions): CleanRunResult {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const unique = `${stamp}-${process.pid}-${randomUUID().slice(0, 8)}`;
  mkdirSync(options.runRoot, { recursive: true });
  const workDir = join(options.runRoot, `clean-${unique}`);
  const artifactsDir = join(options.runRoot, `clean-${unique}-artifacts`);
  mkdirSync(artifactsDir, { recursive: true });

  gitWorktree(options.repoPath, ['add', '--detach', workDir, options.commit]);

  let exitCode: number;
  let timedOut = false;
  let stdout = '';
  let stderr = '';

  try {
    const env: Record<string, string> = { ...baseEnv(), ...(options.env ?? {}) };
    if (options.asOfDate) env['VIBE_EXCEL_AS_OF'] = options.asOfDate;

    const run = spawnSync(options.entry.command, options.entry.args, {
      cwd: workDir,
      env,
      encoding: 'utf8',
      timeout: options.timeoutMs ?? 5 * 60 * 1000,
      maxBuffer: 32 * 1024 * 1024,
      shell: false,
    });
    stdout = run.stdout ?? '';
    stderr = run.stderr ?? '';
    timedOut = run.error !== undefined && (run.error as NodeJS.ErrnoException).code === 'ETIMEDOUT';
    exitCode = run.status ?? (timedOut ? 124 : 1);

    const files = listFiles(workDir);

    const outputs: FileDigest[] = [];
    for (const declared of options.outputs ?? []) {
      const source = join(workDir, declared);
      if (!existsSync(source)) continue;
      const target = join(artifactsDir, declared.replace(/[/\\]/g, '__'));
      cpSync(source, target, { recursive: true });
      outputs.push({ path: declared, sha256: sha256Of(source), bytes: statSync(source).size });
    }

    const manifestPath = join(artifactsDir, 'manifest.json');
    writeFileSync(
      manifestPath,
      `${JSON.stringify(
        {
          commit: options.commit,
          entry: options.entry,
          asOfDate: options.asOfDate ?? null,
          exitCode,
          timedOut,
          files,
          outputs,
          executedAt: new Date().toISOString(),
        },
        null,
        2,
      )}\n`,
      'utf8',
    );

    return { exitCode, timedOut, stdout, stderr, workDir, artifactsDir, outputs, files, manifestPath };
  } finally {
    // worktree 移除失败不该掩盖运行结果，但也不能静默——记到 artifacts 里。
    try {
      gitWorktree(options.repoPath, ['remove', '--force', workDir]);
    } catch (error) {
      writeFileSync(
        join(artifactsDir, 'worktree-cleanup-error.txt'),
        `${error instanceof Error ? error.message : String(error)}\n`,
        'utf8',
      );
    }
  }
}

/** 两次干净重跑之间，按文件内容比较差异（不看二进制格式本身）。 */
export function diffRunFiles(a: FileDigest[], b: FileDigest[]): {
  onlyInA: string[];
  onlyInB: string[];
  changed: string[];
  identical: boolean;
} {
  const mapA = new Map(a.map((f) => [f.path, f.sha256]));
  const mapB = new Map(b.map((f) => [f.path, f.sha256]));
  const onlyInA = [...mapA.keys()].filter((p) => !mapB.has(p)).sort();
  const onlyInB = [...mapB.keys()].filter((p) => !mapA.has(p)).sort();
  const changed = [...mapA.keys()].filter((p) => mapB.has(p) && mapA.get(p) !== mapB.get(p)).sort();
  return {
    onlyInA,
    onlyInB,
    changed,
    identical: onlyInA.length === 0 && onlyInB.length === 0 && changed.length === 0,
  };
}
