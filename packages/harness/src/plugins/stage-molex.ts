/**
 * 插件：stage-molex —— 业务包「每日 Molex ID 前置处理」。
 *
 * 这就是「业务也是一个插件」的样例：它不碰执行、不碰存储，只声明
 * 「这个流程有两个阶段，各自的入口文件、参数、产出是什么」。
 * 换一个业务流程 = 换/加一个这样的插件，编排器一行不改。
 *
 * 参数模板里的占位符由 sessions 插件在执行时填成绝对路径，
 * 所以程序可以被放在任意干净 worktree 里跑（ADR-0005 决策 1）。
 */

import { definePlugin, service, type PluginContext, type StagesService } from '../kernel.ts';

export default definePlugin<Record<string, never>>({
  name: 'stage-molex',
  inject: ['stages'],
  apply(ctx: PluginContext): void {
    const stages = service<StagesService>(ctx, 'stages');
    stages.register({
      pack: 'molex-prep',
      stages: {
        import: {
          entry: 'import_hr.py',
          args: ['--inputs', '{inputs}', '--db', '{db}'],
          outputs: [],
          // 这一阶段把 5 个 Excel 录进业务库：成功后快照成新的数据版本。
          mutatesDatabase: true,
          timeoutMs: 15 * 60 * 1000,
        },
        analyze: {
          entry: 'analyze.py',
          args: ['--db', '{db}', '--out', '{out}', '--as-of', '{asOf}'],
          outputs: ['summary.json', 'population.csv', 'pn_available.csv', 'ladder.json'],
          timeoutMs: 5 * 60 * 1000,
        },
      },
    });
  },
});
