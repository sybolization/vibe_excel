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
import type { ChildProcess } from 'node:child_process';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { RpcClient } from '@earendil-works/pi-coding-agent';

import {
  definePlugin,
  type AgentOutcome,
  type AgentRequest,
  type AgentService,
  type AskQuestion,
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
  const entry = resolvePackageEntry();
  return dirname(dirname(entry));
}

/**
 * 找到 pi 包的入口文件。
 *
 * 正常走 `import.meta.resolve`；但它不是所有运行时都有（vite/vitest 会把 `import.meta` 换掉）。
 * 没有就退回 `createRequire().resolve`——插件要能在测试里被加载，否则集成测试只能靠手跑。
 */
function resolvePackageEntry(): string {
  const meta = import.meta as ImportMeta & { resolve?: (specifier: string) => string };
  if (typeof meta.resolve === 'function') {
    return fileURLToPath(meta.resolve('@earendil-works/pi-coding-agent'));
  }
  return createRequire(import.meta.url).resolve('@earendil-works/pi-coding-agent');
}

export function resolvePiCli(): string {
  return resolve(join(packageRoot(), 'dist', 'cli.js'));
}

/**
 * 我们自己的 pi 扩展：`ask_user`。
 *
 * 不带扩展的模型只能「一口气写完再交」，中途冒出来的疑问就只能靠猜。
 * 带上它之后，模型可以调 `ask_user` 停下来问人；RPC 模式下这会变成一条
 * `extension_ui_request` 事件，由宿主（这个插件）转给上层去显示和回收答案。
 */
export function resolveAskUserExtension(): string {
  const path = resolve(import.meta.dirname, '..', '..', 'ext', 'ask-user.ts');
  if (!existsSync(path)) throw new Error(`pi 扩展缺失：${path}`);
  return path;
}

/**
 * 把回答写回 pi 子进程的 stdin。
 *
 * RpcClient 只暴露了事件、没有暴露回话口（它的 `send` 是私有的），而 RPC 协议本身
 * 就是一问一答的 JSONL：`{type:"extension_ui_response", id, value|cancelled}`。
 * 这里直接往子进程的 stdin 写一行——协议是公开的，绕开的是客户端封装的缺口。
 */
export function writeUiResponse(client: unknown, payload: Record<string, unknown>): boolean {
  const child = (client as { process?: ChildProcess }).process;
  if (!child?.stdin || child.stdin.destroyed || child.stdin.writableEnded) return false;
  return child.stdin.write(`${JSON.stringify(payload)}\n`);
}

async function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} 超时（${Math.round(ms / 1000)}s）`)), ms);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * 处理一条 `extension_ui_request`：是提问就转给人、拿到回答写回去；其余忽略。
 *
 * 单独抽出来是为了能测：这条路径决定「模型到底等到了什么」，比 UI 长什么样重要得多。
 * 返回值只用于日志与测试（`handled` / `ignored`）。
 */
export async function answerUiRequest(input: {
  event: Record<string, unknown>;
  onQuestion?: (question: AskQuestion) => Promise<string | undefined>;
  respond: (payload: Record<string, unknown>) => boolean;
  timeoutMs: number;
  log?: (line: string) => void;
}): Promise<'handled' | 'ignored'> {
  const method = String(input.event['method'] ?? '');
  if (method !== 'input' && method !== 'select') return 'ignored';
  const id = String(input.event['id'] ?? '');
  if (!id) return 'ignored';

  const question = String(input.event['title'] ?? '');
  const options = Array.isArray(input.event['options'])
    ? (input.event['options'] as unknown[]).map((item) => String(item))
    : [];

  let answer: string | undefined;
  if (input.onQuestion) {
    try {
      answer = await withTimeout(input.onQuestion({ id, question, options }), input.timeoutMs, '等待用户回答');
    } catch (caught) {
      input.log?.(`[ask_user] ${caught instanceof Error ? caught.message : String(caught)}：按未回答处理`);
      answer = undefined;
    }
  }
  const payload =
    answer === undefined || answer === null
      ? { type: 'extension_ui_response', id, cancelled: true }
      : { type: 'extension_ui_response', id, value: String(answer) };
  if (!input.respond(payload)) input.log?.('[ask_user] 子进程已退出，回答没有送达');
  return 'handled';
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
  /** 等用户回答的超时；超时按「未回答」处理，不无限挂着。 */
  askTimeoutMs: number;
}

export default definePlugin<AgentConfig>({
  name: 'agent-pi',
  provide: ['agent'],
  config(raw: unknown): AgentConfig {
    const input = raw as { provider?: unknown; model?: unknown; apiKeyEnv?: unknown; timeoutMs?: unknown; askTimeoutMs?: unknown };
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
      askTimeoutMs: Number(input.askTimeoutMs ?? 5 * 60 * 1000),
    };
  },
  apply(ctx: PluginContext, config: AgentConfig): void {
    /**
     * pi 的路径只在真要跑的时候才解析。
     *
     * 在 apply 里解析会让「只想用一下 store/run」的进程也依赖 pi 的安装布局——
     * 集成测试、离线命令都不该因为这个连插件都加载不了。
     */
    const requirePiCli = (): string => {
      const path = resolvePiCli();
      if (!existsSync(path)) throw new Error(`pi CLI 不存在：${path}`);
      return path;
    };

    const service: AgentService = {
      async run(request: AgentRequest): Promise<AgentOutcome> {
        const cliPath = requirePiCli();
        const apiKey = process.env[config.apiKeyEnv];
        if (!apiKey) throw new Error(`缺少凭证环境变量 ${config.apiKeyEnv}`);

        const started = Date.now();
        const extensionPath = resolveAskUserExtension();
        const client = new RpcClient({
          cliPath,
          cwd: request.cwd,
          provider: config.provider,
          model: config.model,
          env: { PI_CODING_AGENT_DIR: request.agentDir },
          args: ['--no-approve', '--offline', '--session-dir', request.sessionDir, '--extension', extensionPath],
        });

        let ok = false;
        let error: string | null = null;
        const tools = new Set<string>();
        let toolCalls = 0;
        let events = 0;

        /**
         * 模型调 `ask_user` 时，pi 会发出一条 `extension_ui_request` 并阻塞等回话。
         * 这里把它转给上层（终端里问人），拿到答案再写回子进程。
         * 没有登记 onQuestion 时按「取消」回，模型会收到「用户没有回答」并标注未确认。
         */
        const answerQuestion = async (obj: Record<string, unknown>): Promise<void> => {
          await answerUiRequest({
            event: obj,
            ...(request.onQuestion ? { onQuestion: request.onQuestion } : {}),
            respond: (payload) => writeUiResponse(client, payload),
            timeoutMs: config.askTimeoutMs,
            log: (line) => console.log(line),
          });
        };

        client.onEvent((event: unknown) => {
          events += 1;
          const obj = (event ?? {}) as Record<string, unknown>;
          const type = obj['type'];
          if (type === 'extension_ui_request') void answerQuestion(obj);
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
          execFileSync(process.execPath, [requirePiCli(), '--export', sessionFile, outFile], {
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
