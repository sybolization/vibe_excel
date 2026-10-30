/**
 * 插件：exec —— 在干净的 detached worktree 里执行一段入口契约。
 *
 * 这里刻意**不**知道任何业务语义：不知道 import/analyze，不知道 hr.db。
 * 它只回答「用哪个 commit、跑哪个可执行文件、带什么参数、跑在哪个干净目录、退出码与耗时多少」。
 * 换容器/换远程执行器时，替换的是这一个插件（ADR-0006 决策 4）。
 */

import { existsSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';

import { cleanRerun } from '@vibe-excel/runtime';

import { definePlugin, type ExecOutcome, type ExecRequest, type ExecService, type PluginContext } from '../kernel.ts';

export interface ExecConfig {
  /** 数据运行时的解释器绝对路径。 */
  python: string;
  /** 默认超时。 */
  timeoutMs: number;
}

export default definePlugin<ExecConfig>({
  name: 'exec',
  provide: ['exec'],
  config(raw: unknown): ExecConfig {
    const input = raw as { python?: unknown; timeoutMs?: unknown };
    if (typeof input?.python !== 'string' || input.python.trim() === '') {
      throw new Error('exec 插件需要 config.python（解释器绝对路径）');
    }
    // 干净执行发生在 detached worktree 里，cwd 不是仓库根，所以相对路径必须在加载期就解析成绝对路径。
    const python = isAbsolute(input.python) ? input.python : resolve(process.cwd(), input.python);
    if (!existsSync(python)) throw new Error(`exec.python 不存在：${python}`);
    const timeoutMs = Number(input.timeoutMs ?? 15 * 60 * 1000);
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new Error('exec.timeoutMs 必须是正数');
    return { python, timeoutMs };
  },
  apply(ctx: PluginContext, config: ExecConfig): void {
    const service: ExecService = {
      run(request: ExecRequest): ExecOutcome {
        const started = Date.now();
        const result = cleanRerun({
          repoPath: request.repoPath,
          commit: request.commit,
          runRoot: request.runRoot,
          entry: { command: config.python, args: [request.entry.entry, ...request.entry.args] },
          outputs: [],
          timeoutMs: request.timeoutMs ?? config.timeoutMs,
          ...(request.asOf ? { asOfDate: request.asOf } : {}),
        });
        return {
          exitCode: result.exitCode,
          timedOut: result.timedOut,
          stdout: result.stdout,
          stderr: result.stderr,
          manifestPath: result.manifestPath,
          workDir: result.workDir,
          elapsedMs: Date.now() - started,
        };
      },
      interpreter(): string {
        return config.python;
      },
    };
    ctx.provide('exec', service);
  },
});
