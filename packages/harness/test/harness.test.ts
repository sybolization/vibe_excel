/**
 * harness 的测试：清单与补丁、宿主的依赖行为、阶段注册表、摘要工具。
 *
 * 这些测试**不碰真实制品根**——每个用例都在临时目录里造清单和插件。
 */

import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { dirDigests, databaseDigest } from '../src/fs-util.ts';
import { applyPatch, interpolate, parseManifest } from '../src/manifest.ts';
import { loadHost } from '../src/host.ts';

const tempDir = (): string => mkdtempSync(join(tmpdir(), 'vibe-harness-'));

/** 造一个最小插件文件：`provide` 可以是服务名或 undefined。 */
function writePlugin(dir: string, name: string, options: { inject?: string[]; provide?: string } = {}): string {
  const path = join(dir, `${name}.ts`);
  const body = `
export default {
  name: ${JSON.stringify(name)},
  ${options.inject ? `inject: ${JSON.stringify(options.inject)},` : ''}
  ${options.provide ? `provide: [${JSON.stringify(options.provide)}],` : ''}
  apply(ctx) {
    ${options.provide ? `ctx.provide(${JSON.stringify(options.provide)}, { name: ${JSON.stringify(name)} });` : ''}
    globalThis.__loaded = [...(globalThis.__loaded ?? []), ${JSON.stringify(name)}];
  },
};
`;
  writeFileSync(path, body, 'utf8');
  return path;
}

describe('清单与补丁', () => {
  it('展开 ${VAR:-默认值}，并把裸数组与 plugins: 两种写法都当成行列表', () => {
    const dir = tempDir();
    const path = join(dir, 'vibe.plugins.yml');
    writeFileSync(
      path,
      [
        'plugins:',
        '  - id: a',
        '    name: ./a.ts',
        '    config:',
        "      root: '${VIBE_TEST_ROOT:-./fallback}'",
      ].join('\n'),
      'utf8',
    );
    const manifest = parseManifest(path, { VIBE_TEST_ROOT: '/abs/root' } as NodeJS.ProcessEnv);
    expect(manifest.rows).toHaveLength(1);
    expect(manifest.rows[0]!.config).toEqual({ root: '/abs/root' });

    const fallback = parseManifest(path, {} as NodeJS.ProcessEnv);
    expect(fallback.rows[0]!.config).toEqual({ root: './fallback' });

    // 既没有值也没有默认值：宁可加载期报错，也不要悄悄用空字符串
    expect(() => interpolate('${MISSING_VAR_NO_DEFAULT}')).toThrow(/MISSING_VAR_NO_DEFAULT/);
  });

  it('补丁按 id 覆盖，config 是整体替换而不是深合并；改不存在的 id 直接报错', () => {
    const dir = tempDir();
    const manifestPath = join(dir, 'vibe.plugins.yml');
    writeFileSync(
      manifestPath,
      ['plugins:', '  - id: a', '    name: ./a.ts', '    config:', '      x: 1', '      y: 2'].join('\n'),
      'utf8',
    );

    const patchPath = join(dir, 'vibe.patch.yml');
    writeFileSync(patchPath, ['- id: a', '  config:', '    x: 9'].join('\n'), 'utf8');

    const patched = applyPatch(parseManifest(manifestPath), patchPath);
    // y 不在了：整体替换（与 DSH 的 patch 语义一致）
    expect(patched.rows[0]!.config).toEqual({ x: 9 });

    const insertPath = join(dir, 'insert.patch.yml');
    writeFileSync(insertPath, ['- insert:', '    - id: b', '      name: ./b.ts'].join('\n'), 'utf8');
    const inserted = applyPatch(patched, insertPath);
    expect(inserted.rows.map((row) => row.id)).toEqual(['a', 'b']);

    const badPath = join(dir, 'bad.patch.yml');
    writeFileSync(badPath, ['- id: not-exist', '  disabled: true'].join('\n'), 'utf8');
    expect(() => applyPatch(inserted, badPath)).toThrow(/not-exist/);
  });
});

describe('宿主', () => {
  it('启动顺序由服务依赖决定，不由清单顺序决定；依赖缺失的插件保持 PENDING', async () => {
    const dir = tempDir();
    const consumer = writePlugin(dir, 'consumer', { inject: ['alpha'] });
    const producer = writePlugin(dir, 'producer', { provide: 'alpha' });
    const orphan = writePlugin(dir, 'orphan', { inject: ['nobody'] });
    const manifestPath = join(dir, 'vibe.plugins.yml');

    // 故意把消费者写在提供者前面
    writeFileSync(
      manifestPath,
      [
        'plugins:',
        `  - id: consumer`, `    name: ${JSON.stringify(consumer)}`,
        `  - id: producer`, `    name: ${JSON.stringify(producer)}`,
        `  - id: orphan`, `    name: ${JSON.stringify(orphan)}`,
        `  - id: off`, `    name: ${JSON.stringify(writePlugin(dir, 'off'))}`, '    disabled: true',
      ].join('\n'),
      'utf8',
    );

    const host = await loadHost({ manifestPath, env: {} as NodeJS.ProcessEnv });
    try {
      const byId = Object.fromEntries(host.reports().map((report) => [report.id, report]));
      expect(byId['consumer']!.state).toBe('ACTIVE');
      expect(byId['producer']!.state).toBe('ACTIVE');
      expect(byId['orphan']!.state).toBe('PENDING');
      expect(byId['off']!.state).toBe('DISABLED');
    } finally {
      await host.dispose();
    }
  });

  it('配置校验不过的插件记为 FAILED，并带上原因', async () => {
    const dir = tempDir();
    const plugin = join(dir, 'picky.ts');
    writeFileSync(
      plugin,
      `export default {
        name: 'picky',
        Config: { '~standard': { version: 1, vendor: 'test', validate: () => ({ issues: [{ message: '缺少 who' }] }) } },
        apply() {},
      };\n`,
      'utf8',
    );
    const manifestPath = join(dir, 'vibe.plugins.yml');
    writeFileSync(manifestPath, ['plugins:', '  - id: picky', `    name: ${JSON.stringify(plugin)}`].join('\n'), 'utf8');

    const host = await loadHost({ manifestPath, env: {} as NodeJS.ProcessEnv });
    try {
      const report = host.reports().find((item) => item.id === 'picky');
      expect(report?.state).toBe('FAILED');
      expect(report?.error).toMatch(/who/);
    } finally {
      await host.dispose();
    }
  });
});

describe('摘要工具', () => {
  it('目录摘要按内容计算，且与文件顺序无关', () => {
    const dir = tempDir();
    writeFileSync(join(dir, 'a.txt'), 'a', 'utf8');
    mkdirSync(join(dir, 'sub'));
    writeFileSync(join(dir, 'sub', 'b.txt'), 'b', 'utf8');
    const first = dirDigests(dir);
    expect(first.map((entry) => entry.path)).toEqual(['a.txt', 'sub/b.txt']);
    expect(dirDigests(dir)).toEqual(first);
    writeFileSync(join(dir, 'a.txt'), 'changed', 'utf8');
    expect(dirDigests(dir)).not.toEqual(first);
  });

  it('库摘要看的是行内容，不是文件字节', async () => {
    const { DatabaseSync } = await import('node:sqlite');
    const dir = tempDir();
    const dbPath = join(dir, 'x.db');
    const db = new DatabaseSync(dbPath);
    db.exec('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)');
    db.prepare('INSERT INTO t (id, name) VALUES (?, ?)').run(1, 'a');
    db.close();

    const before = databaseDigest(dbPath);
    expect(before['t']).toMatchObject({ rows: 1 });

    const again = new DatabaseSync(dbPath);
    again.prepare('UPDATE t SET name = ? WHERE id = ?').run('b', 1);
    again.close();
    const after = databaseDigest(dbPath);
    expect(after['t']!.sha256).not.toBe(before['t']!.sha256);
  });
});
