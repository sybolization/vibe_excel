/**
 * 按 commit 读程序仓库里的文件。
 *
 * 为什么不用工作区：运行必须对着**提交过的代码**。工作区随时可能被模型改动，
 * 而「这次运行到底是哪份代码跑出来的」必须能被复现——所以 flow.json 和 SQL 节点
 * 一律从 `git show <commit>:<path>` 取，不读磁盘上的当前文件。
 */

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

import { parseFlowJson, type FlowSpec } from './flow.ts';

/** 程序仓库里工作流声明的默认位置。 */
export const DEFAULT_FLOW_PATH = 'flow.json';

function gitShow(repoPath: string, commit: string, path: string): { ok: boolean; text: string; error: string } {
  try {
    const text = execFileSync('git', ['-C', repoPath, 'show', `${commit}:${path}`], {
      encoding: 'utf8',
      maxBuffer: 64 * 1024 * 1024,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    return { ok: true, text, error: '' };
  } catch (error) {
    const detail = error as { stderr?: Buffer | string; message?: string };
    const stderr = typeof detail.stderr === 'string' ? detail.stderr : detail.stderr?.toString('utf8') ?? '';
    return { ok: false, text: '', error: (stderr || detail.message || String(error)).trim() };
  }
}

/** 读 commit 里的一个文本文件；不存在时报出「这个提交里没有它」，而不是一句 git 报错。 */
export function readFileAtCommit(repoPath: string, commit: string, path: string): string {
  const result = gitShow(repoPath, commit, path);
  if (result.ok) return result.text;
  if (/exists on disk|does not exist|not found|path '.*' does not exist/i.test(result.error)) {
    throw new Error(`提交 ${commit.slice(0, 8)} 里没有 ${path}：先让模型把它写进草稿`);
  }
  throw new Error(`读取 ${path}@${commit.slice(0, 8)} 失败：${result.error}`);
}

/** 取这个提交里的工作流声明并校验。 */
export function loadFlowAtCommit(repoPath: string, commit: string, path = DEFAULT_FLOW_PATH): FlowSpec {
  return parseFlowJson(readFileAtCommit(repoPath, commit, path));
}

/** 平台自带的 python 引导器（不是程序仓库里的代码，所以用绝对路径）。 */
export function bundledPython(dir: string, name: string): string {
  const path = join(import.meta.dirname, '..', 'py', name);
  if (!existsSync(path)) throw new Error(`平台自带的 python 脚本缺失：${path}`);
  return path;
}

/** 调用一个节点的引导器。 */
export const nodeRunnerPath = (): string => bundledPython('py', 'node_runner.py');

/** 物化加载器。 */
export const materializeRunnerPath = (): string => bundledPython('py', 'materialize.py');
