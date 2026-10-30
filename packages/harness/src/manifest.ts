/**
 * 清单（manifest）与补丁层。
 *
 * 形状照抄 DSH 的 cordis.patch.yml：一行一个插件，`id` 是行的身份，
 * 后来的补丁按 `id` 覆盖同一行（**整体替换 config，不深合并**），或者用 `insert:` 追加行。
 *
 *   plugins:
 *     - id: store
 *       name: ./plugins/store.ts
 *       config: { storeRoot: '${VIBE_EXCEL_STORE:-../vibe_excel-store}' }
 *
 *   # 补丁文件（--patch=...）
 *   - id: store
 *     disabled: true
 *   - insert:
 *       - id: my-plugin
 *         name: ./my-plugin.ts
 *
 * 为什么不做深合并：DSH 的 dsh-base 补丁注释写得很清楚——「A patch replaces the targeted
 * row's whole config rather than merging into it」，因为深合并会让"这行最终是什么"难以回答。
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, isAbsolute, resolve } from 'node:path';

import { parse as parseYaml } from 'yaml';

export interface PluginRow {
  id: string;
  /** 模块说明符：相对路径（./x.ts）或包名。insert 行必填。 */
  name?: string;
  disabled?: boolean;
  config?: Record<string, unknown>;
}

export interface Manifest {
  /** 清单文件绝对路径。 */
  path: string;
  /** 相对路径的解析基准（清单所在目录）。 */
  baseDir: string;
  /** 按出现顺序保留的插件行；`disabled` 的行仍在列表里，只是不加载。 */
  rows: PluginRow[];
}

/** 声明成函数而不是箭头常量：只有函数声明才能让 TS 在 `if (!x) fail()` 之后做收窄。 */
function fail(message: string): never {
  throw new Error(message);
}

/** `${VAR}` / `${VAR:-默认值}` 展开；只对字符串生效，数组与对象递归。 */
export function interpolate(value: unknown, env: NodeJS.ProcessEnv = process.env): unknown {
  if (typeof value === 'string') {
    return value.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}/g, (whole, name: string, fallback?: string) => {
      const fromEnv = env[name];
      if (fromEnv !== undefined && fromEnv !== '') return fromEnv;
      if (fallback !== undefined) return fallback;
      return fail(`清单里的 \${${name}} 没有值，也没有默认值`);
    });
  }
  if (Array.isArray(value)) return value.map((item) => interpolate(item, env));
  if (value !== null && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [key, inner] of Object.entries(value as Record<string, unknown>)) out[key] = interpolate(inner, env);
    return out;
  }
  return value;
}

const asRows = (value: unknown, where: string): PluginRow[] => {
  const candidate = value as { plugins?: unknown } | unknown[] | null;
  const list = Array.isArray(candidate) ? candidate : (candidate as { plugins?: unknown } | null)?.plugins;
  if (!Array.isArray(list)) fail(`${where}：期望插件行数组，实际是 ${typeof value}`);
  return list.map((row: unknown, index: number) => {
    const record = row as Partial<PluginRow>;
    if (typeof record?.id !== 'string' || record.id.length === 0) fail(`${where}：第 ${index + 1} 行缺少 id`);
    const out: PluginRow = { id: record.id };
    if (typeof record.name === 'string') out.name = record.name;
    if (typeof record.disabled === 'boolean') out.disabled = record.disabled;
    if (record.config !== undefined) {
      if (record.config === null || typeof record.config !== 'object' || Array.isArray(record.config)) {
        fail(`${where}：行 ${record.id} 的 config 必须是对象`);
      }
      out.config = record.config as Record<string, unknown>;
    }
    return out;
  });
};

/** 解析清单：接受 `plugins: [...]` 或裸数组。 */
export function parseManifest(path: string, env: NodeJS.ProcessEnv = process.env): Manifest {
  const absolute = isAbsolute(path) ? path : resolve(path);
  if (!existsSync(absolute)) fail(`清单不存在：${absolute}`);
  const text = readFileSync(absolute, 'utf8');
  const raw = text.trim().length === 0 ? [] : parseYaml(text);
  const rows = asRows(interpolate(raw, env), absolute);
  return { path: absolute, baseDir: dirname(absolute), rows };
}

/**
 * 应用一层补丁。行按 id 覆盖：
 *  - `insert:` 追加（同 id 视为更新）；
 *  - 只写 `id` + 部分字段 → 只覆盖写出来的字段，其余保持不变；
 *  - 找不到 id 直接报错（拼错 id 是最常见的配置错误，静默忽略会让"我明明改了却没生效"）。
 */
export function applyPatch(manifest: Manifest, patchPath: string, env: NodeJS.ProcessEnv = process.env): Manifest {
  const absolute = isAbsolute(patchPath) ? patchPath : resolve(manifest.baseDir, patchPath);
  if (!existsSync(absolute)) fail(`补丁不存在：${absolute}`);
  const raw = interpolate(parseYaml(readFileSync(absolute, 'utf8')), env);
  if (!Array.isArray(raw)) fail(`补丁 ${absolute}：期望数组`);

  const rows = manifest.rows.map((row) => ({ ...row }));
  const index = new Map(rows.map((row, position) => [row.id, position]));

  const upsert = (row: PluginRow, where: string): void => {
    const position = index.get(row.id);
    if (position === undefined) {
      index.set(row.id, rows.length);
      rows.push(row);
      return;
    }
    const current = rows[position]!;
    rows[position] = {
      id: row.id,
      name: row.name ?? current.name,
      disabled: row.disabled ?? current.disabled,
      // 整体替换，不深合并（见文件头）
      config: row.config ?? current.config,
    };
    void where;
  };

  for (const [position, entry] of raw.entries()) {
    const record = entry as { insert?: unknown; id?: unknown } | null;
    if (!record || typeof record !== 'object') fail(`补丁 ${absolute}：第 ${position + 1} 项不是对象`);
    if ('insert' in record) {
      const inserted = asRows(record.insert, `补丁 ${absolute} insert`);
      for (const row of inserted) upsert(row, 'insert');
      continue;
    }
    if (typeof record.id !== 'string') fail(`补丁 ${absolute}：第 ${position + 1} 项既没有 insert 也没有 id`);
    const target = asRows([record], `补丁 ${absolute}`)[0]!;
    if (!index.has(target.id)) fail(`补丁 ${absolute}：找不到要修改的行 id=${target.id}`);
    upsert(target, 'patch');
  }

  return { ...manifest, rows };
}

/** 相对路径按清单目录解析成绝对路径；包名原样返回。 */
export function resolveModuleSpecifier(manifest: Manifest, name: string): string {
  if (name.startsWith('.') || isAbsolute(name)) return resolve(manifest.baseDir, name);
  return name;
}
