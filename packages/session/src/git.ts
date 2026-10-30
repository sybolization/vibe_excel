/**
 * Git 操作（ADR-0002 决策 2、ADR-0004）。
 *
 * 只封装「草稿分支 / 提交 / 合并」这几件事，不重写 Git。
 * 所有函数都是对 `git -C <repo>` 的薄包装，失败即抛错——不吞错误。
 */

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export interface GitResult {
  stdout: string;
  status: number;
}

function git(repo: string, args: string[], allowFail = false): GitResult {
  try {
    const stdout = execFileSync('git', ['-C', repo, ...args], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    return { stdout: stdout.trim(), status: 0 };
  } catch (error) {
    if (allowFail) {
      const e = error as { status?: number; stdout?: string };
      return { stdout: (e.stdout ?? '').trim(), status: e.status ?? 1 };
    }
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(`git ${args.join(' ')} 失败：${message}`);
  }
}

/** 初始化程序仓库：建目录、git init、一个空提交，让 main 分支真实存在。 */
export function initRepo(repoPath: string, mainBranch = 'main'): void {
  mkdirSync(repoPath, { recursive: true });
  if (!existsSync(join(repoPath, '.git'))) {
    git(repoPath, ['init', '-b', mainBranch]);
    git(repoPath, ['config', 'user.name', 'Vibe Excel']);
    git(repoPath, ['config', 'user.email', 'vibe-excel@local']);
  }
  // .gitignore 与说明文件属于程序仓库自身的约定，不涉及用户数据。
  const ignorePath = join(repoPath, '.gitignore');
  if (!existsSync(ignorePath)) {
    writeFileSync(
      ignorePath,
      ['# 数据与制品不进代码仓库（指南 §6.1）', '*.db', '*.db-wal', '*.db-shm', '*.xlsx', '*.xls', 'out/', ''].join(
        '\n',
      ),
      'utf8',
    );
  }
  const readmePath = join(repoPath, 'README.md');
  if (!existsSync(readmePath)) {
    writeFileSync(
      readmePath,
      '# 程序仓库\n\n这里放的是 agent 生成的程序代码（Python / SQL / 页面 / 规则），由 Git 管理版本。\n',
      'utf8',
    );
  }
  commitAll(repoPath, 'init: 程序仓库初始化');
}

export function headSha(repoPath: string, ref = 'HEAD'): string | null {
  const result = git(repoPath, ['rev-parse', '--verify', '--quiet', `${ref}^{commit}`], true);
  return result.status === 0 && result.stdout.length > 0 ? result.stdout : null;
}

export function currentBranch(repoPath: string): string {
  return git(repoPath, ['rev-parse', '--abbrev-ref', 'HEAD']).stdout;
}

export function branchExists(repoPath: string, branch: string): boolean {
  return git(repoPath, ['rev-parse', '--verify', '--quiet', `refs/heads/${branch}`], true).status === 0;
}

export function createBranch(repoPath: string, branch: string, from: string): void {
  git(repoPath, ['branch', branch, from]);
}

/** 切到指定分支（工作区干净时才允许）。 */
export function checkout(repoPath: string, branch: string): void {
  git(repoPath, ['checkout', branch]);
}

/**
 * 提交工作区全部改动。
 * 返回新 commit sha；没有任何改动时返回 null（这不是错误，是事实）。
 */
export function commitAll(repoPath: string, message: string): string | null {
  if (git(repoPath, ['status', '--porcelain'], true).stdout.length === 0) return null;
  git(repoPath, ['add', '-A']);
  git(repoPath, ['commit', '-m', message]);
  return headSha(repoPath);
}

/** 分支之间相差多少个提交（a 相对 b 领先数）。 */
export function commitsAhead(repoPath: string, branch: string, base: string): number {
  const out = git(repoPath, ['rev-list', '--count', `${base}..${branch}`], true).stdout;
  const n = Number.parseInt(out, 10);
  return Number.isFinite(n) ? n : 0;
}

/** 非快进合并，保留「这次发布来自哪个草稿」的历史。 */
export function mergeInto(repoPath: string, source: string, target: string): string {
  git(repoPath, ['checkout', target]);
  git(repoPath, ['merge', '--no-ff', '--no-edit', source, '-m', `publish: ${source} → ${target}`]);
  return headSha(repoPath, target) ?? '';
}

/**
 * 给一个草稿分支开独立的 worktree。
 *
 * 为什么必须用 worktree：一个仓库只有一个工作区，多个会话的草稿分支
 * 无法同时 checkout。用 worktree 之后，每个会话有自己的一份文件树，
 * 互不干扰，提交也发生在各自的 worktree 里。
 */
export function addWorktree(repoPath: string, worktreePath: string, branch: string, from: string): void {
  if (!branchExists(repoPath, branch)) createBranch(repoPath, branch, from);
  git(repoPath, ['worktree', 'add', worktreePath, branch]);
}

export function removeWorktree(repoPath: string, worktreePath: string): void {
  if (!existsSync(worktreePath)) return;
  git(repoPath, ['worktree', 'remove', '--force', worktreePath], true);
}

export function isClean(repoPath: string): boolean {
  return git(repoPath, ['status', '--porcelain'], true).stdout.length === 0;
}

/** ancestor 是否仍是 descendant 的祖先（用来发现 main 被重置/改写）。 */
export function isAncestor(repoPath: string, ancestor: string, descendant: string): boolean {
  return git(repoPath, ['merge-base', '--is-ancestor', ancestor, descendant], true).status === 0;
}
