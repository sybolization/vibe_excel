/**
 * pi 子进程接入（从 spikes/m0-pi 抄过来的最小集合）。
 *
 * 为什么是抄而不是 import：m0 那個 spike 没有声明 exports，跨包 import 只能靠路径穿透，
 * 比重复 90 行更脆。S2 的 packages/harness 会接管这份代码，届时这里整段删掉。
 */

import { execFileSync } from 'node:child_process';
import { createWriteStream, readFileSync, type WriteStream } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { RpcClient } from '@earendil-works/pi-coding-agent';

function packageRoot(): string {
  const entry = fileURLToPath(import.meta.resolve('@earendil-works/pi-coding-agent'));
  return dirname(dirname(entry));
}

export function resolvePiCli(): string {
  return resolve(join(packageRoot(), 'dist', 'cli.js'));
}

export function piVersion(): string {
  const pkg = JSON.parse(readFileSync(join(packageRoot(), 'package.json'), 'utf8')) as { version: string };
  return pkg.version;
}

/**
 * 子进程环境白名单。
 *
 * RpcClient.start() 内部是 `env: { ...process.env, ...options.env }`——合并而非替换，
 * 所以真白名单只能临时替换 process.env（ADR-0001 缺口 2，这里沿用同样的取舍）。
 */
const ENV_WHITELIST = [
  'OPENCODE_API_KEY',
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
] as const;

export async function withSanitizedEnv<T>(extra: Record<string, string>, fn: () => Promise<T>): Promise<T> {
  const original = process.env;
  const sanitized: Record<string, string> = {};
  for (const key of ENV_WHITELIST) {
    const value = process.env[key];
    if (value !== undefined) sanitized[key] = value;
  }
  Object.assign(sanitized, extra);
  process.env = sanitized;
  try {
    return await fn();
  } finally {
    process.env = original;
  }
}

export interface ClientConfig {
  cliPath: string;
  cwd: string;
  agentDir: string;
  sessionDir: string;
  provider: string;
  model: string;
}

export function createClient(cfg: ClientConfig): RpcClient {
  return new RpcClient({
    cliPath: cfg.cliPath,
    cwd: cfg.cwd,
    provider: cfg.provider,
    model: cfg.model,
    env: { PI_CODING_AGENT_DIR: cfg.agentDir },
    args: ['--no-approve', '--offline', '--session-dir', cfg.sessionDir],
  });
}

const truncate = (value: string, max: number): string => (value.length <= max ? value : `${value.slice(0, max)}…`);

/** 从一条事件里抠出人能读的要点；取不到就退回截断的 JSON，不假设形状。 */
function brief(raw: unknown): string {
  if (raw === null || typeof raw !== 'object') return truncate(String(raw), 140);
  const obj = raw as Record<string, unknown>;
  const parts: string[] = [];
  const toolName = obj['toolName'] ?? (obj['type'] === 'tool_execution_start' ? obj['name'] : undefined);
  if (typeof toolName === 'string') parts.push(`tool=${toolName}`);
  if (typeof obj['isError'] === 'boolean') parts.push(`isError=${String(obj['isError'])}`);
  const payload = obj['result'] ?? obj['output'] ?? obj['input'] ?? obj['text'];
  if (typeof payload === 'string' && payload.trim().length > 0) {
    parts.push(`«${truncate(payload.replace(/\s+/g, ' ').trim(), 180)}»`);
  } else if (payload !== undefined) {
    parts.push(`«${truncate(JSON.stringify(payload), 180)}»`);
  }
  if (parts.length === 0) parts.push(truncate(JSON.stringify(raw), 160));
  return parts.join(' ');
}

/** 事件流落盘 + 时间线 + 工具名统计。只记观测到的事实。 */
export class EventRecorder {
  readonly typeCounts: Record<string, number> = {};
  private seq = 0;
  private total = 0;
  private readonly timeline: string[] = [];
  private readonly tools = new Set<string>();
  private readonly toolCalls: string[] = [];
  private stream: WriteStream | null = null;
  private skippedDeltas = 0;

  attach(filePath: string): void {
    this.stream = createWriteStream(filePath, { flags: 'a' });
  }

  handle(raw: unknown): void {
    const type =
      typeof raw === 'object' && raw !== null && 'type' in raw && typeof (raw as { type: unknown }).type === 'string'
        ? (raw as { type: string }).type
        : 'unknown';
    this.total += 1;
    this.typeCounts[type] = (this.typeCounts[type] ?? 0) + 1;
    this.stream?.write(`${JSON.stringify(raw)}\n`);

    if (typeof raw === 'object' && raw !== null) {
      const obj = raw as Record<string, unknown>;
      const direct = obj['toolName'] ?? (type === 'tool_execution_start' ? obj['name'] : undefined);
      if (typeof direct === 'string') {
        this.tools.add(direct);
        if (type === 'tool_execution_start') this.toolCalls.push(direct);
      }
    }

    if (type === 'message_update') {
      this.skippedDeltas += 1;
      return;
    }
    const line = `${String(this.seq++).padStart(4, '0')}  ${new Date().toISOString()}  ${type.padEnd(22)}  ${brief(raw)}`;
    this.timeline.push(line);
    // 边跑边打印，避免长时间无输出（长任务的可见性）。
    if (type !== 'message_start' && type !== 'message_end') console.log(`  ${line}`);
  }

  get eventCount(): number {
    return this.total;
  }

  toolNames(): string[] {
    return [...this.tools].sort();
  }

  toolCallCount(): number {
    return this.toolCalls.length;
  }

  timelineText(): string {
    return [
      ...this.timeline,
      '',
      `# 共 ${this.total} 条事件，类型 ${Object.keys(this.typeCounts).length} 种；折叠 ${this.skippedDeltas} 条流式增量`,
      `# 类型分布：${Object.entries(this.typeCounts).map(([k, v]) => `${k}=${v}`).join(' ')}`,
    ].join('\n');
  }

  async close(): Promise<void> {
    const stream = this.stream;
    this.stream = null;
    if (!stream) return;
    await new Promise<void>((done) => stream.end(() => done()));
  }
}

export function exportSessionHtml(cliPath: string, sessionFile: string, outFile: string): boolean {
  try {
    execFileSync(process.execPath, [cliPath, '--export', sessionFile, outFile], { stdio: 'ignore', timeout: 120_000 });
    return true;
  } catch {
    return false;
  }
}

/** 凭证只输出掩码，绝不回显。 */
export function mask(value: string | undefined): string {
  if (!value) return '(缺失)';
  return value.length <= 8 ? '*'.repeat(value.length) : `${value.slice(0, 4)}…${value.slice(-2)}（${value.length} 字符）`;
}
