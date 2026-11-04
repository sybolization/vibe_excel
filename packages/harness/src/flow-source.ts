/**
 * 按 commit 读程序仓库里的文件。
 *
 * 为什么不用工作区：运行必须对着**提交过的代码**。工作区随时可能被模型改动，
 * 而「这次运行到底是哪份代码跑出来的」必须能被复现——所以 flow.json、入口文件和
 * SQL 节点一律从 `git show <commit>:<path>` 取，不读磁盘上的当前文件。
 *
 * 节点的两种来路在这里汇合（ADR-0008）：`flow.json` 里手写了 `nodes` 就用它；
 * 没写就把 `entry` 指向的源码取出来，交给 `flow_parse.py` 解析成节点。
 * 汇合点放在这里而不是 `flow.ts`，因为只有这一层可以碰 git 和子进程。
 */

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

import { nodesOfCodeFlow, parseCodeFlow } from './flow-code.ts';
import { DEFAULT_ENTRY, parseFlowJson, type FlowSpec } from './flow.ts';

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

/** 取这个提交里的工作流声明并校验；节点没写就从代码里解析。 */
export function loadFlowAtCommit(
  repoPath: string,
  commit: string,
  path = DEFAULT_FLOW_PATH,
  options: { python?: string } = {},
): FlowSpec {
  const spec = parseFlowJson(readFileAtCommit(repoPath, commit, path));
  if (spec.nodes.length > 0) return spec;

  // 代码即流程：图从 main 读。解析器是平台自带的（不在程序仓库里），所以用绝对路径。
  const entry = spec.entry ?? DEFAULT_ENTRY;
  let source: string;
  try {
    source = readFileAtCommit(repoPath, commit, entry);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    const hint =
      spec.entry === undefined
        ? `flow.json 没写 entry，默认找 ${DEFAULT_ENTRY}：把主文件命名成 ${DEFAULT_ENTRY}，或在 flow.json 里写 "entry"`
        : `flow.json 写了 entry=${spec.entry}，但这个提交里读不到它`;
    throw new Error(`读不到入口文件 ${entry}（${hint}）：${detail}`);
  }
  if (options.python === undefined) {
    throw new Error(`解析 ${entry} 需要解释器路径（exec.python 没传进来）`);
  }

  const code = parseCodeFlow({
    entry,
    source,
    python: options.python,
    parserPath: bundledPython('py', 'flow_parse.py'),
  });
  return {
    ...spec,
    entry: code.entry,
    nodes: nodesOfCodeFlow(code),
    warnings: code.warnings,
  };
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
