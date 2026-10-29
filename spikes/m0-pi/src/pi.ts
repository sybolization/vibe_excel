/**
 * pi 接入层：解析 CLI、构造受控环境、创建 RpcClient、记录事件。
 *
 * 原则（指南 §2.1 / §3.4）：复用 pi 自带的 RpcClient，不自建 agent 循环，
 * 也不新增模型可调用的工具。这里做的只有「进程、cwd、环境、事件」。
 */

import { readFileSync } from 'node:fs';
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
 * 只记录观测到的事实：事件类型、序号、时间。不做任何"进度百分比"之类的推断。
 * ------------------------------------------------------------------ */

export interface EventFact {
  seq: number;
  at: string;
  type: string;
}

export class EventRecorder {
  readonly facts: EventFact[] = [];
  /** 发现的事件类型 → 次数。用于给报告提供真实的事件分类，而不是猜。 */
  readonly typeCounts: Record<string, number> = {};
  private seq = 0;
  private readonly raw: string[] = [];
  private readonly sink: ((fact: EventFact) => void) | undefined;
  private readonly maxRaw: number;

  constructor(sink?: (fact: EventFact) => void, maxRaw = 400) {
    this.sink = sink;
    this.maxRaw = maxRaw;
  }

  handle(raw: unknown): void {
    const type =
      typeof raw === 'object' && raw !== null && 'type' in raw && typeof (raw as { type: unknown }).type === 'string'
        ? (raw as { type: string }).type
        : 'unknown';
    const fact: EventFact = { seq: this.seq++, at: new Date().toISOString(), type };
    this.facts.push(fact);
    this.typeCounts[type] = (this.typeCounts[type] ?? 0) + 1;
    if (this.raw.length < this.maxRaw) {
      const line = JSON.stringify(raw);
      // 原始事件有界保存，避免把大对象反复塞进日志。
      this.raw.push(line.length > 2000 ? `${line.slice(0, 2000)}…<truncated>` : line);
    }
    this.sink?.(fact);
  }

  get rawLines(): readonly string[] {
    return this.raw;
  }

  /** 把事件流里出现的所有工具名抠出来，用于证明"没有新增自定义工具"。 */
  toolNames(): string[] {
    const names = new Set<string>();
    for (const line of this.raw) {
      const re = /"(?:tool|toolName|name)":"([a-z_]+)"/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(line)) !== null) {
        if (m[1]) names.add(m[1]);
      }
    }
    return [...names].sort();
  }
}
