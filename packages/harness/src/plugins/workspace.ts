/**
 * 插件：workspace —— 每会话的目录布局与排他锁。
 *
 * 依赖 store 只为了拿 storeRoot（服务依赖驱动的注入，不是 import 依赖）。
 */

import type { SessionId } from '@vibe-excel/contracts';
import { ensureWorkspace, sessionLockPath, withLockSync, workspacePaths } from '@vibe-excel/runtime';

import { definePlugin, service, type PluginContext, type StoreService, type WorkspaceService } from '../kernel.ts';

export default definePlugin<Record<string, never>>({
  name: 'workspace',
  inject: ['store'],
  provide: ['workspace'],
  apply(ctx: PluginContext): void {
    const store = service<StoreService>(ctx, 'store');
    const root = (): string => store.storeRoot();
    const workspace: WorkspaceService = {
      paths: (id: SessionId) => workspacePaths(root(), id),
      ensure: (id: SessionId) => ensureWorkspace(root(), id),
      lockPath: (id: SessionId) => sessionLockPath(root(), id),
      withLock: <T>(id: SessionId, fn: () => T): T => withLockSync(sessionLockPath(root(), id), fn),
    };
    ctx.provide('workspace', workspace);
  },
});
