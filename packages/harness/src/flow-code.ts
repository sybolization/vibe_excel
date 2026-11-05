/**
 * 代码即流程（ADR-0008）：把入口文件里的 `main` 解析成节点列表。
 *
 * 这里**刻意什么都不懂**：不懂 AST，不懂 `t:` 标注，不懂 docstring。所有规则都在
 * `py/flow_parse.py` 里（用 Python 自己的 `ast`，注释/字符串归属才有唯一答案），
 * 这个文件只做三件事：把源码喂过去、把 JSON 拿回来、把错误翻译成人话。
 *
 * 为什么错误要在这里翻译：解析器的报错是 `{code, message, line}`，而人（和模型）
 * 需要看到的是 `pipeline.py:31: main 里有 for：…`——文件 + 行号 + 下一步。
 *
 * 这个模块不读文件、不连库、不执行程序：源码由调用方从**提交**里取出来传进来。
 */

import { spawnSync } from 'node:child_process';

import type { FlowNode, FlowStep, FlowWarning } from './flow.ts';

export interface CodeFlowNode {
  /** 函数名，也是节点 id。 */
  func: string;
  /** `def` 所在行。 */
  line: number;
  /** docstring 第一行，流程图上的节点标题。 */
  title: string;
  /** `# name：中文`，节点在界面上的名字。没有就用函数名。 */
  name?: string;
  reads: string[];
  writes: string[];
  steps: FlowStep[];
}

export interface CodeFlow {
  /** 入口文件（相对程序仓库）。 */
  entry: string;
  nodes: CodeFlowNode[];
  warnings: FlowWarning[];
}

export interface CodeFlowInput {
  /** 入口文件路径，只用于报错和拼 `call`。 */
  entry: string;
  /** 入口文件的源码（UTF-8）。 */
  source: string;
  /** 解释器绝对路径（`exec.python`）。 */
  python: string;
  /** `py/flow_parse.py` 的绝对路径。 */
  parserPath: string;
}

interface ParserFailure {
  code: string;
  message: string;
  line: number | null;
}

function fail(message: string): never {
  throw new Error(message);
}

/**
 * 解析器只需要「解释器能启动」：不传 PATH 找不到依赖之外的 dll，不传 SystemRoot 有些 Windows API 会挂。
 * 除此之外一律不给——这个子进程不该看见模型凭证或工作区变量。
 */
function parserEnv(): Record<string, string> {
  const env: Record<string, string> = { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
  for (const key of ['PATH', 'Path', 'SystemRoot', 'SYSTEMROOT', 'windir', 'TEMP', 'TMP']) {
    const value = process.env[key];
    if (value !== undefined) env[key] = value;
  }
  return env;
}

/** 解析器返回的节点形状：我们自己的脚本，做轻量校验就够，但形状不对要说得清。 */
function asCodeFlowNode(raw: unknown, index: number): CodeFlowNode {
  const where = `解析结果的 nodes[${index}]`;
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) fail(`${where} 应该是对象`);
  const node = raw as Record<string, unknown>;
  const text = (value: unknown, at: string): string => {
    if (typeof value !== 'string' || value.trim() === '') fail(`${at} 应该是非空字符串`);
    return value;
  };
  const tables = (value: unknown, at: string): string[] => {
    if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) fail(`${at} 应该是字符串数组`);
    return value as string[];
  };

  const steps: FlowStep[] = Array.isArray(node['steps'])
    ? (node['steps'] as unknown[]).map((item, stepIndex) => {
        const step = item as Record<string, unknown>;
        return {
          line: Number(step['line']),
          kind: step['kind'] === 'block' ? ('block' as const) : ('statement' as const),
          of: typeof step['of'] === 'string' ? step['of'] : '?',
          label: typeof step['label'] === 'string' ? step['label'] : '',
        };
      })
    : [];

  return {
    func: text(node['func'], `${where}.func`),
    line: Number(node['line'] ?? 0),
    title: text(node['title'], `${where}.title`),
    ...(typeof node['name'] === 'string' && node['name'].trim() !== '' ? { name: node['name'].trim() } : {}),
    reads: tables(node['reads'], `${where}.reads`),
    writes: tables(node['writes'], `${where}.writes`),
    steps: steps.filter((step) => step.label !== ''),
  };
}

/**
 * 跑一次解析。契约违反会抛错，错误里**带着入口文件和行号**，可以直接贴给模型。
 *
 * 用 `spawnSync` 而不是异步：调用链（`loadFlowAtCommit` → 命令）本来就是同步的，
 * 而解析只有毫秒级（只跑 `ast`，不 import pandas）。
 */
export function parseCodeFlow(input: CodeFlowInput): CodeFlow {
  const result = spawnSync(input.python, [input.parserPath, input.entry], {
    input: input.source,
    encoding: 'utf8',
    maxBuffer: 16 * 1024 * 1024,
    timeout: 60_000,
    // 只给解释器活下来需要的东西：解析器不 import 目标文件，也不该继承模型凭证。
    env: parserEnv(),
  });

  if (result.error) {
    const code = (result.error as NodeJS.ErrnoException).code;
    fail(
      code === 'ETIMEDOUT'
        ? `解析 ${input.entry} 超时（60s）：解析器只做静态分析，不该卡住，检查 ${input.parserPath}`
        : `无法启动解析器（${input.python}）：${result.error.message}`,
    );
  }

  const stdout = (result.stdout ?? '').trim();
  const stderr = (result.stderr ?? '').trim();
  if (stdout === '') {
    fail(
      `解析 ${input.entry} 没有输出（退出码 ${String(result.status)}）：解析器应当总是返回 JSON。` +
        `${stderr ? `\n${stderr}` : ''}`,
    );
  }

  let payload: unknown;
  try {
    payload = JSON.parse(stdout);
  } catch {
    fail(
      `解析 ${input.entry} 的返回不是 JSON（退出码 ${String(result.status)}）：` +
        `\n${stdout.slice(0, 800)}${stderr ? `\n${stderr.slice(0, 400)}` : ''}`,
    );
  }
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    fail(`解析 ${input.entry} 的返回不是对象`);
  }
  const body = payload as Record<string, unknown>;

  if (body['ok'] !== true) {
    const error = (body['error'] ?? {}) as Partial<ParserFailure>;
    const line = typeof error.line === 'number' ? `:${error.line}` : '';
    const code = typeof error.code === 'string' ? `[${error.code}] ` : '';
    fail(`${input.entry}${line} ${code}${error.message ?? '解析失败'}`);
  }

  const rawNodes = body['nodes'];
  if (!Array.isArray(rawNodes) || rawNodes.length === 0) {
    fail(`${input.entry} 解析出了 0 个节点：main 里必须调用本文件定义的函数`);
  }
  const warnings: FlowWarning[] = Array.isArray(body['warnings'])
    ? (body['warnings'] as unknown[]).map((item) => {
        const warning = item as Record<string, unknown>;
        return {
          code: typeof warning['code'] === 'string' ? warning['code'] : 'WARNING',
          message: typeof warning['message'] === 'string' ? warning['message'] : '',
          line: typeof warning['line'] === 'number' ? warning['line'] : null,
        };
      })
    : [];

  return {
    entry: typeof body['entry'] === 'string' ? body['entry'] : input.entry,
    nodes: rawNodes.map((item, index) => asCodeFlowNode(item, index)),
    warnings,
  };
}

/**
 * 把解析结果翻成平台认的节点：`id` 用函数名，`call` 用 `entry:函数名`。
 *
 * 不带 `args`：`t:` 参数由 `node_runner` 按签名注入，不是命令行 kwarg。
 */
export function nodesOfCodeFlow(flow: CodeFlow): FlowNode[] {
  return flow.nodes.map((node) => ({
    id: node.func,
    call: `${flow.entry}:${node.func}`,
    reads: [...node.reads],
    writes: [...node.writes],
    title: node.title,
    ...(node.name === undefined ? {} : { name: node.name }),
    ...(node.steps.length > 0 ? { steps: node.steps } : {}),
  }));
}
