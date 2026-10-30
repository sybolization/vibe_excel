/**
 * 插件：store —— 控制面（Program / Session / Run / DataRevision / Artifact / Publish）。
 *
 * 只是把 `@vibe-excel/session` 适配成服务：库本身不认识 cordis，可以被单测直接调用。
 * 懒打开：只跑 `plugins`/`help` 这类命令时不该创建数据库。
 */

import { isAbsolute, resolve } from 'node:path';

import { SessionStore } from '@vibe-excel/session';

import { definePlugin, type PluginContext, type StoreService } from '../kernel.ts';

export interface StoreConfig {
  storeRoot: string;
}

export default definePlugin<StoreConfig>({
  name: 'store',
  provide: ['store'],
  config(raw: unknown): StoreConfig {
    const input = raw as { storeRoot?: unknown };
    if (typeof input?.storeRoot !== 'string' || input.storeRoot.trim() === '') {
      throw new Error('store 插件需要 config.storeRoot（或用 ${VIBE_EXCEL_STORE} 注入）');
    }
    // 制品根一律用绝对路径：它会出现在 control.db 里，之后所有进程都要能定位到同一个地方。
    return { storeRoot: isAbsolute(input.storeRoot) ? input.storeRoot : resolve(process.cwd(), input.storeRoot) };
  },
  apply(ctx: PluginContext, config: StoreConfig): void {
    let opened: SessionStore | null = null;
    const service: StoreService = {
      db(): SessionStore {
        opened ??= SessionStore.open(config.storeRoot);
        return opened;
      },
      storeRoot(): string {
        return config.storeRoot;
      },
    };
    ctx.provide('store', service);
    ctx.effect(() => () => {
      opened?.close();
      opened = null;
    });
  },
});
