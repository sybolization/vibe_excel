/**
 * 插件：agent-pi —— 用 pi 作为「写程序」的那一半。
 *
 * 与 ADR-0001 的结论一致：走包内 `RpcClient`、不新增任何模型可调用的工具、
 * 子进程只拿到最小环境。换 ACP / 换别的 agent 时替换这一个插件。
 *
 * 两个已经踩过的坑写在这里：
 *  - `RpcClient.start()` 内部是 `env: { ...process.env, ...options.env }`（合并而非替换），
 *    所以真白名单只能临时替换 `process.env`（ADR-0001 缺口 2）；
 *  - `getSessionStats()` 必须在 `stop()` 之前调用，否则拿到空对象（ADR-0005 缺口 5）。
 */

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { RpcClient } from '@earendil-works/pi-coding-agent';

import {
  definePlugin,
  type AgentOutcome,
  type AgentRequest,
  type AgentService,
  type PluginContext,
} from '../kernel.ts';

/**
 * 子进程环境白名单：只放行「能启动、shell 能用、模型能鉴权」的最小集合。
 * 其余变量（其它 provider 的 key、本机路径、会话外标识）一律不继承。
 */
const BASE_ENV = [
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

/** 临时把 process.env 换成白名单，确保子进程真的只拿到这些变量。 */
async function withSanitizedEnv<T>(extra: Record<string, string>, fn: () => Promise<T>): Promise<T> {
  const original = process.env;
  const sanitized: Record<string, string> = {};
  for (const key of BASE_ENV) {
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

export interface AgentConfig {
  provider: string;
  model: string;
  /** provider 凭证所在的环境变量名；值只在内存里用，不落盘、不打印。 */
  apiKeyEnv: string;
  timeoutMs: number;
}

export default definePlugin<AgentConfig>({
  name: 'agent-pi',
  provide: ['agent'],
  config(raw: unknown): AgentConfig {
    const input = raw as { provider?: unknown; model?: unknown; apiKeyEnv?: unknown; timeoutMs?: unknown };
    for (const field of ['provider', 'model'] as const) {
      if (typeof input?.[field] !== 'string' || String(input[field]).trim() === '') {
        throw new Error(`agent-pi 插件需要 config.${field}`);
      }
    }
    return {
      provider: String(input.provider),
      model: String(input.model),
      apiKeyEnv: typeof input.apiKeyEnv === 'string' && input.apiKeyEnv.length > 0 ? input.apiKeyEnv : 'OPENCODE_API_KEY',
      timeoutMs: Number(input.timeoutMs ?? 20 * 60 * 1000),
    };
  },
  apply(ctx: PluginContext, config: AgentConfig): void {
    const cliPath = resolvePiCli();

    const service: AgentService = {
      async run(request: AgentRequest): Promise<AgentOutcome> {
        if (!existsSync(cliPath)) throw new Error(`pi CLI 不存在：${cliPath}`);
        const apiKey = process.env[config.apiKeyEnv];
        if (!apiKey) throw new Error(`缺少凭证环境变量 ${config.apiKeyEnv}`);

        const started = Date.now();
        const client = new RpcClient({
          cliPath,
          cwd: request.cwd,
          provider: config.provider,
          model: config.model,
          env: { PI_CODING_AGENT_DIR: request.agentDir },
          args: ['--no-approve', '--offline', '--session-dir', request.sessionDir],
        });

        let ok = false;
        let error: string | null = null;
        const tools = new Set<string>();
        let toolCalls = 0;
        let events = 0;

        client.onEvent((event: unknown) => {
          events += 1;
          const obj = (event ?? {}) as Record<string, unknown>;
          const type = obj['type'];
          const direct = obj['toolName'] ?? (type === 'tool_execution_start' ? obj['name'] : undefined);
          if (typeof direct === 'string') {
            tools.add(direct);
            if (type === 'tool_execution_start') toolCalls += 1;
          }
          request.onEvent?.(event);
        });

        try {
          // 凭证只在 start 期间存在于 process.env 上。
          await withSanitizedEnv({ [config.apiKeyEnv]: apiKey }, async () => {
            await client.start();
            await client.promptAndWait(request.prompt, undefined, request.timeoutMs ?? config.timeoutMs);
          });
          ok = true;
        } catch (caught) {
          error = caught instanceof Error ? caught.message : String(caught);
        }

        let stats: unknown = null;
        try {
          stats = await client.getSessionStats();
        } catch {
          stats = null;
        }
        await client.stop().catch(() => undefined);

        const sessionFiles = existsSync(request.sessionDir)
          ? readdirSync(request.sessionDir)
              .filter((name) => name.endsWith('.jsonl'))
              .map((name) => join(request.sessionDir, name))
          : [];

        return {
          ok,
          error,
          elapsedMs: Date.now() - started,
          eventCount: events,
          toolNames: [...tools].sort(),
          toolCallCount: toolCalls,
          sessionFiles,
          stats,
        };
      },

      exportHtml(sessionFile: string, outFile: string): boolean {
        try {
          execFileSync(process.execPath, [cliPath, '--export', sessionFile, outFile], {
            stdio: 'ignore',
            timeout: 120_000,
          });
          return true;
        } catch {
          return false;
        }
      },
    };

    ctx.provide('agent', service);
  },
});
