/**
 * CLI：组合根。
 *
 * 它只做「读清单 → 起宿主 → 把子命令交给 commands 服务」。
 * 子命令全部由插件注册（`sessions` 注册了 setup/run/flow/publish/optimize/status/author/db），
 * 所以加一个命令不需要改这个文件。
 */

import { resolve } from 'node:path';

import { formatReports, loadHost } from './host.ts';
import type { CommandContext, CommandSpec, CommandsService } from './kernel.ts';

export interface CliOptions {
  /** 清单路径；默认仓库根的 vibe.plugins.yml。 */
  manifest?: string;
  /** 补丁层，按顺序应用。 */
  patches?: string[];
  env?: NodeJS.ProcessEnv;
  out?: (line: string) => void;
}

const defaultManifest = (): string => resolve(import.meta.dirname, '../../../vibe.plugins.yml');

/** `--key=value` / `--key` → flags；第一个非 flag 是命令名。 */
export function parseArgv(argv: string[]): { command: string | undefined; rest: string[]; flags: Record<string, string> } {
  const rest: string[] = [];
  const flags: Record<string, string> = {};
  for (const arg of argv) {
    if (!arg.startsWith('--')) {
      rest.push(arg);
      continue;
    }
    const eq = arg.indexOf('=');
    if (eq > 0) flags[arg.slice(2, eq)] = arg.slice(eq + 1);
    else flags[arg.slice(2)] = 'true';
  }
  return { command: rest[0], rest: rest.slice(1), flags };
}

function printHelp(commands: CommandSpec[], out: (line: string) => void): void {
  out('用法：node packages/harness/src/main.ts <命令> [--flag=value]');
  out('');
  out('命令（由插件注册）：');
  const width = Math.max(...commands.map((command) => command.name.length), 6);
  for (const command of commands) out(`  ${command.name.padEnd(width)}  ${command.summary}`);
  out('');
  out('宿主命令：');
  const hostWidth = Math.max(width, 9);
  out(`  ${'plugins'.padEnd(hostWidth)}  列出清单里的插件与加载状态`);
  out(`  ${'help'.padEnd(hostWidth)}     显示这段说明`);
}

export async function runCli(argv: string[], options: CliOptions = {}): Promise<number> {
  const out = options.out ?? ((line: string) => console.log(line));
  const manifestPath = options.manifest ?? process.env['VIBE_EXCEL_MANIFEST'] ?? defaultManifest();

  let host;
  try {
    host = await loadHost({
      manifestPath,
      patches: options.patches ?? [],
      ...(options.env ? { env: options.env } : {}),
    });
  } catch (error) {
    console.error(`加载清单失败：${error instanceof Error ? error.message : String(error)}`);
    return 2;
  }

  try {
    // 加载失败必须可见（静默 = 缺陷）。
    for (const report of host.reports()) {
      if (report.state === 'FAILED') console.error(`插件加载失败 ${report.id}：${report.error ?? '未知错误'}`);
    }

    const commands = host.ctx.get('commands') as CommandsService | undefined;
    if (!commands) {
      console.error('commands 服务未就绪：检查清单里 commands 插件是否被禁用');
      return 2;
    }

    const { command, rest, flags } = parseArgv(argv);
    if (!command || command === 'help' || command === '--help') {
      printHelp(commands.all(), out);
      return 0;
    }
    if (command === 'plugins') {
      out(formatReports(host.reports()));
      return 0;
    }

    const spec = commands.find(command);
    if (!spec) {
      console.error(`未知命令：${command}`);
      printHelp(commands.all(), out);
      return 2;
    }
    const context: CommandContext = { argv: rest, flags, print: out };
    return await spec.run(context);
  } finally {
    await host.dispose();
  }
}
