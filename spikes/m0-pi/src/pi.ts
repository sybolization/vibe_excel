/**
 * pi 接入层：解析 CLI、构造受控环境、创建 RpcClient、记录事件。
 *
 * 原则（指南 §2.1 / §3.4）：复用 pi 自带的 RpcClient，不自建 agent 循环，
 * 也不新增模型可调用的工具。这里做的只有「进程、cwd、环境、事件」。
 */

import { execFileSync } from 'node:child_process';
import { createWriteStream, readFileSync, type WriteStream } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { RpcClient } from '@earendil-works/pi-coding-agent';

/**
 * 包根目录。
 *
 * 该包的 exports 只声明了 "import" 条件，`require.resolve` 会报
 * ERR_PACKAGE_PATH_NOT_EXPORTED，所以必须走 ESM 解析。
 */
function packageRoot(): string {
  const entry = fileURLToPath(import.meta.resolve('@earendil-works/pi-coding-agent'));
  return dirname(dirname(entry)); // <pkg>/dist/index.js -> <pkg>
}

/** 用绝对路径，避免 RpcClient 默认的 "dist/cli.js" 被当成相对 cwd 的路径。 */
export function resolvePiCli(): string {
  return resolve(join(packageRoot(), 'dist', 'cli.js'));
}

/** pi 包的实际版本，写进报告，避免"以为装的是 1.4.0"。 */
export function piVersion(): string {
  const pkg = JSON.parse(readFileSync(join(packageRoot(), 'package.json'), 'utf8')) as { version: string };
  return pkg.version;
}

/**
 * 子进程环境白名单。
 *
 * 只放行「进程能启动、shell 能用、模型能鉴权」所必需的最小集合：
 *  - Windows 上缺 SystemRoot / TEMP / PATHEXT 这类变量，Git Bash 与 node 会直接跑不起来；
 *  - OPENCODE_API_KEY 是必要的，因为 pi 从环境变量取 provider 凭证。
 * 其余（其它 provider 的 key、本机路径、会话外标识）一律不继承。
 */
export const PROVIDER_KEY_ENV = 'OPENCODE_API_KEY';

export const ENV_WHITELIST: readonly string[] = [
  PROVIDER_KEY_ENV,
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
  'PROGRAMFILES',
  'PROGRAMFILES(X86)',
  'COMMONPROGRAMFILES',
  'COMSPEC',
  'PATHEXT',
  'OS',
  'NUMBER_OF_PROCESSORS',
  'PROCESSOR_ARCHITECTURE',
  'PI_CODING_AGENT_DIR',
  'PI_OFFLINE',
  'PI_TELEMETRY',
];

export function whitelistedEnv(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of ENV_WHITELIST) {
    const value = process.env[key];
    if (value !== undefined) out[key] = value;
  }
  return out;
}

/**
 * 在受控环境下执行一段异步逻辑。
 *
 * RpcClient.start() 内部是 `env: { ...process.env, ...options.env }`——它**合并**而非替换
 * 父进程环境（已读源码确认）。所以真白名单只能靠临时替换 process.env 实现。
 * 这是本 spike 明确记录的一个缺口，见 ADR-0001。
 */
export async function withSanitizedEnv<T>(fn: () => Promise<T>): Promise<T> {
  const original = process.env;
  const sanitized = whitelistedEnv();
  process.env = sanitized;
  try {
    return await fn();
  } finally {
    process.env = original;
  }
}

export interface PiSessionConfig {
  cliPath: string;
  cwd: string;
  /** 每会话独立的 pi 配置目录，避免共用 ~/.pi/agent。 */
  agentDir: string;
  provider: string;
  model: string;
  /** 额外 CLI 参数，例如 ['--session-dir', dir] 或 ['--continue']。 */
  args?: string[];
}

export function createClient(cfg: PiSessionConfig): RpcClient {
  return new RpcClient({
    cliPath: cfg.cliPath,
    cwd: cfg.cwd,
    provider: cfg.provider,
    model: cfg.model,
    env: { PI_CODING_AGENT_DIR: cfg.agentDir },
    args: [
      // 不读项目本地资源，避免交互式信任提示把 RPC 卡死。
      '--no-approve',
      // 关闭启动期网络与遥测，减少与本机无关的外部行为。
      '--offline',
      ...(cfg.args ?? []),
    ],
  });
}

/* ------------------------------------------------------------------ *
 * 事件记录
 *
 * 三份产物，各有用途：
 *   events.jsonl  —— RPC 原始事件流，逐条落盘，不加工（体积有上限）
 *   timeline.txt  —— 给人和模型看的压缩时间线（跳过流式 delta）
 *   <session>.html —— pi 自己的会话导出，含每条消息的 token 与成本
 *
 * 只记录观测到的事实：事件类型、序号、时间。不做任何"进度百分比"之类的推断。
 * 注意：字段抽取（tool/role/result…）是**防御性**的，事件形状没有对外契约，
 * 所以取不到就退回整条 JSON 截断，不假设一定有某个字段。
 * ------------------------------------------------------------------ */

export interface EventFact {
  seq: number;
  at: string;
  type: string;
}

const truncate = (value: string, max: number): string =>
  value.length <= max ? value : `${value.slice(0, max)}…`;

/** 从一条事件里抠出人能读的要点；失败就退回截断的 JSON。 */
function brief(raw: unknown): string {
  if (raw === null || typeof raw !== 'object') return truncate(String(raw), 160);
  const obj = raw as Record<string, unknown>;
  const parts: string[] = [];

  const toolName = obj['toolName'] ?? obj['tool'] ?? (obj['type'] === 'tool_execution_start' ? obj['name'] : undefined);
  if (typeof toolName === 'string') parts.push(`tool=${toolName}`);
  if (typeof obj['role'] === 'string') parts.push(`role=${obj['role'] as string}`);
  if (typeof obj['isError'] === 'boolean') parts.push(`isError=${String(obj['isError'])}`);

  const payload = obj['result'] ?? obj['output'] ?? obj['input'] ?? obj['text'];
  if (typeof payload === 'string' && payload.trim().length > 0) {
    parts.push(`«${truncate(payload.replace(/\s+/g, ' ').trim(), 200)}»`);
  } else if (payload !== undefined) {
    parts.push(`«${truncate(JSON.stringify(payload), 200)}»`);
  }

  if (parts.length === 0) parts.push(truncate(JSON.stringify(raw), 180));
  return parts.join(' ');
}

export class EventRecorder {
  readonly facts: EventFact[] = [];
  /** 发现的事件类型 → 次数。用于给报告提供真实的事件分类，而不是猜。 */
  readonly typeCounts: Record<string, number> = {};
  private seq = 0;
  private readonly timeline: string[] = [];
  private readonly tools = new Set<string>();
  private stream: WriteStream | null = null;
  private bytes = 0;
  private maxBytes = 64 * 1024 * 1024;
  private skippedDeltas = 0;
  private readonly sink: ((fact: EventFact) => void) | undefined;

  constructor(sink?: (fact: EventFact) => void) {
    this.sink = sink;
  }

  /** 把原始事件流逐条写到文件，必须在 start() 之前调用，否则会漏掉开头的事件。 */
  attach(filePath: string, maxBytes = 64 * 1024 * 1024): void {
    this.maxBytes = maxBytes;
    this.stream = createWriteStream(filePath, { flags: 'a' });
  }

  handle(raw: unknown): void {
    const type =
      typeof raw === 'object' && raw !== null && 'type' in raw && typeof (raw as { type: unknown }).type === 'string'
        ? (raw as { type: string }).type
        : 'unknown';
    const fact: EventFact = { seq: this.seq++, at: new Date().toISOString(), type };
    this.facts.push(fact);
    this.typeCounts[type] = (this.typeCounts[type] ?? 0) + 1;

    const line = JSON.stringify(raw);
    this.bytes += line.length;
    if (this.stream && this.bytes <= this.maxBytes) {
      this.stream.write(`${line}\n`);
    }

    // 工具名在**每条事件**上累计，不能只扫内存里截断过的那一小段，
    // 否则会漏掉运行后段才出现的 write / edit，把「没有自定义工具」这个证据削弱。
    if (typeof raw === 'object' && raw !== null) {
      const obj = raw as Record<string, unknown>;
      const direct = obj['toolName'] ?? (type === 'tool_execution_start' ? obj['name'] : undefined);
      if (typeof direct === 'string') this.tools.add(direct);
      const message = obj['message'];
      if (typeof message === 'object' && message !== null) {
        const nested = (message as Record<string, unknown>)['toolName'];
        if (typeof nested === 'string') this.tools.add(nested);
      }
    }

    // 流式 token delta 不进时间线，否则 1000 条里 1000 条都是它。
    if (type === 'message_update') {
      this.skippedDeltas += 1;
    } else {
      this.timeline.push(`${String(fact.seq).padStart(4, '0')}  ${fact.at}  ${type.padEnd(22)}  ${brief(raw)}`);
    }

    this.sink?.(fact);
  }

  get getTruncated(): boolean {
    return this.bytes > this.maxBytes;
  }

  /** 时间线正文。末尾附一行统计，说明哪些内容被折叠了。 */
  timelineLines(): string[] {
    return [
      ...this.timeline,
      '',
      `# 共 ${this.facts.length} 条事件，类型 ${Object.keys(this.typeCounts).length} 种；`
        + `折叠 ${this.skippedDeltas} 条 message_update 流式增量；`
        + `原始流 ${(this.bytes / 1024).toFixed(1)} KiB${this.getTruncated ? '（已超上限截断）' : ''}`,
      `# 类型分布：${Object.entries(this.typeCounts).map(([k, v]) => `${k}=${v}`).join(' ')}`,
    ];
  }

  async close(): Promise<void> {
    const stream = this.stream;
    this.stream = null;
    if (!stream) return;
    await new Promise<void>((done) => stream.end(() => done()));
  }

  /** 事件流里出现过的所有工具名。用于证明"只用了 pi 内置工具，没有新增自定义工具"。 */
  toolNames(): string[] {
    return [...this.tools].sort();
  }
}

/** 用 pi 自带的导出功能把会话文件变成可读 HTML；失败不影响主流程。 */
export function exportSessionHtml(cliPath: string, sessionFile: string, outFile: string): boolean {
  try {
    execFileSync(process.execPath, [cliPath, '--export', sessionFile, outFile], {
      stdio: 'ignore',
      timeout: 120_000,
    });
    return true;
  } catch {
    return false;
  }
}
