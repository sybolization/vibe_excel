/**
 * 插件：stages —— 阶段注册表。
 *
 * 「阶段」是这个产品里唯一由业务定义的结构（入口文件 + 参数模板 + 声明的产出）。
 * 把它做成服务而不是写死在编排器里，业务包就能各自插件化：`stage-molex` 只是其中一个包。
 */

import { definePlugin, type PluginContext, type StagePack, type StagesService } from '../kernel.ts';

export default definePlugin<Record<string, never>>({
  name: 'stages',
  provide: ['stages'],
  apply(ctx: PluginContext): void {
    const packs: StagePack[] = [];
    const service: StagesService = {
      register(pack: StagePack): void {
        if (packs.some((existing) => existing.pack === pack.pack)) throw new Error(`阶段包重复注册：${pack.pack}`);
        packs.push(pack);
      },
      packs(): StagePack[] {
        return packs.map((pack) => ({ ...pack }));
      },
      get(stage: string): { pack: string; spec: StagePack['stages'][string] } {
        for (const pack of packs) {
          const spec = pack.stages[stage];
          if (spec) return { pack: pack.pack, spec };
        }
        const known = packs.flatMap((pack) => Object.keys(pack.stages)).sort();
        throw new Error(`未知阶段：${stage}（已注册：${known.join(', ') || '无'}）`);
      },
    };
    ctx.provide('stages', service);
  },
});
