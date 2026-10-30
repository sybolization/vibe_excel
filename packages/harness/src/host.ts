/**
 * 宿主：把清单变成一棵 cordis 上下文树。
 *
 * 这里只做四件事：解析清单 → 逐个 import 插件模块 → 交给 cordis 注册 → 汇报每个插件的状态。
 * 不做的事：不排序（顺序由服务依赖决定）、不 try/catch 吞错误（加载失败要能看见）。
 */

import { pathToFileURL } from 'node:url';

import { Context } from '@deepseek-ai/cordis';

import type { PluginShape } from './kernel.ts';
import { applyPatch, parseManifest, resolveModuleSpecifier, type Manifest, type PluginRow } from './manifest.ts';

/** cordis 的 FiberState 是 const enum，运行时取不到，只能按数值映射。 */
export const FIBER_STATES = ['PENDING', 'LOADING', 'ACTIVE', 'FAILED', 'DISPOSED', 'UNLOADING'] as const;

export interface PluginReport {
  id: string;
  module: string;
  /** ACTIVE 之外的都值得看一眼。 */
  state: string;
  inject: string[];
  provides: string[];
  error: string | null;
}

export interface Host {
  ctx: Context;
  manifest: Manifest;
  /** 按清单顺序，一行一项（含 disabled 的行）。 */
  reports(): PluginReport[];
  dispose(): Promise<void>;
}

const stateName = (state: number): string => FIBER_STATES[state] ?? String(state);

async function importPlugin(specifier: string): Promise<PluginShape<unknown>> {
  const url = specifier.startsWith('.') || specifier.startsWith('/') || /^[A-Za-z]:/.test(specifier)
    ? pathToFileURL(specifier).href
    : specifier;
  const module = (await import(url)) as { default?: PluginShape<unknown>; plugin?: PluginShape<unknown> };
  const plugin = module.default ?? module.plugin;
  if (!plugin || typeof plugin.apply !== 'function') {
    throw new Error(`模块 ${specifier} 没有默认导出插件（期望 export default { name, apply }）`);
  }
  return plugin;
}

export interface LoadOptions {
  manifestPath: string;
  patches?: string[];
  env?: NodeJS.ProcessEnv;
}

export async function loadHost(options: LoadOptions): Promise<Host> {
  const env = options.env ?? process.env;
  let manifest = parseManifest(options.manifestPath, env);
  for (const patch of options.patches ?? []) manifest = applyPatch(manifest, patch, env);

  const ctx = new Context();
  const reports: PluginReport[] = [];
  const fibers: { row: PluginRow; fiber: ReturnType<Context['plugin']> }[] = [];

  for (const row of manifest.rows) {
    const module = row.name ?? row.id;
    const specifier = resolveModuleSpecifier(manifest, module);
    if (row.disabled) {
      reports.push({ id: row.id, module, state: 'DISABLED', inject: [], provides: [], error: null });
      continue;
    }
    let plugin: PluginShape<unknown>;
    try {
      plugin = await importPlugin(specifier);
    } catch (error) {
      reports.push({
        id: row.id,
        module,
        state: 'FAILED',
        inject: [],
        provides: [],
        error: error instanceof Error ? error.message : String(error),
      });
      continue;
    }
    const fiber = ctx.plugin(plugin as never, (row.config ?? {}) as never);
    fibers.push({ row, fiber });
    reports.push({
      id: row.id,
      module,
      state: 'PENDING',
      inject: [...(plugin.inject ?? [])],
      provides: Array.isArray(plugin.provide) ? plugin.provide : plugin.provide ? [plugin.provide] : [],
      error: null,
    });
  }

  // 两个已实测的 cordis 事实决定了这段代码：
  //  1. `await fiber.await()` 对「依赖未满足而挂起」的插件也会 resolve，所以状态必须读 fiber.state；
  //  2. 提供服务会**级联唤醒**依赖方，激活是异步的，所以要等到没有 LOADING/UNLOADING 才算稳定。
  const errors = new Map<string, string>();
  const settle = async (): Promise<void> => {
    for (let round = 0; round < 20; round += 1) {
      for (const entry of fibers) {
        try {
          await entry.fiber.await();
        } catch (error) {
          errors.set(entry.row.id, error instanceof Error ? error.message : String(error));
        }
      }
      const busy = fibers.some((entry) => {
        const state = entry.fiber.state as number;
        return state === 1 || state === 5; // LOADING / UNLOADING
      });
      if (!busy) return;
      await new Promise<void>((done) => setTimeout(done, 5));
    }
  };
  await settle();

  for (const entry of fibers) {
    const report = reports.find((item) => item.id === entry.row.id);
    if (!report) continue;
    report.state = stateName(entry.fiber.state as number);
    const error = errors.get(entry.row.id);
    if (error && !report.error) report.error = error;
  }

  return {
    ctx,
    manifest,
    reports: () => reports.map((report) => ({ ...report })),
    // 逆序卸载：依赖方先走，提供方后走（cordis 的依赖级联会处理剩下的）。
    dispose: async (): Promise<void> => {
      for (const entry of [...fibers].reverse()) {
        await entry.fiber.dispose();
      }
    },
  };
}

/** 打印一张插件表（`harness plugins` 用）。 */
export function formatReports(reports: PluginReport[]): string {
  const width = Math.max(...reports.map((report) => report.id.length), 4);
  const lines = reports.map((report) => {
    const services = report.provides.length > 0 ? ` provides=${report.provides.join(',')}` : '';
    const inject = report.inject.length > 0 ? ` inject=${report.inject.join(',')}` : '';
    const error = report.error ? `  !! ${report.error.split('\n')[0]}` : '';
    return `${report.id.padEnd(width)}  ${report.state.padEnd(9)}${services}${inject}${error}`;
  });
  return lines.join('\n');
}
