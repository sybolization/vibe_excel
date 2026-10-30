/**
 * 插件：commands —— CLI 子命令注册表。
 *
 * 与 DSH 的 `dsh-commands` 同构：命令本身也是插件注册进来的，
 * 所以「加一个子命令」不需要改 CLI 入口，只要多一个插件（或某个插件多注册一条）。
 */

import { definePlugin, type CommandSpec, type CommandsService, type PluginContext } from '../kernel.ts';

export default definePlugin<Record<string, never>>({
  name: 'commands',
  provide: ['commands'],
  apply(ctx: PluginContext): void {
    const registry = new Map<string, CommandSpec>();
    const service: CommandsService = {
      register(spec: CommandSpec): void {
        if (registry.has(spec.name)) throw new Error(`命令重复注册：${spec.name}`);
        registry.set(spec.name, spec);
      },
      all(): CommandSpec[] {
        return [...registry.values()].sort((a, b) => a.name.localeCompare(b.name));
      },
      find(name: string): CommandSpec | undefined {
        return registry.get(name);
      },
    };
    ctx.provide('commands', service);
  },
});
