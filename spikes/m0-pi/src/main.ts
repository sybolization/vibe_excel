/**
 * M0 验证入口。
 *
 *   node src/main.ts --list
 *   node src/main.ts --only=spawn
 *   node src/main.ts --only=spawn,python,cancel,resume,isolation
 *   node src/main.ts --all
 *
 * 每一项检查在独立的 run 目录中执行，结果写入 JSON 报告。
 * 报告只记录观测到的事实；「结论」和「缺口」由人写进 ADR，不由脚本推断。
 */

import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

import { ALL_CHECKS, type CheckContext, type CheckName, type CheckResult } from './checks.ts';
import { Log, mask } from './log.ts';
import { piVersion, PROVIDER_KEY_ENV, resolvePiCli } from './pi.ts';

const repoRoot = resolve(import.meta.dirname, '../../..');
const storeRoot = process.env['VIBE_EXCEL_STORE'] ?? resolve(repoRoot, '..', 'vibe_excel-store');
const defaultPython = join(repoRoot, 'runtime-env', '.venv', 'Scripts', 'python.exe');

interface Options {
  checks: CheckName[];
  model: string;
  provider: string;
  pythonPath: string;
}

function parseArgs(argv: string[]): Options | 'list' {
  // 默认走本机**已验证可达**的 provider。
  // 直连 api.deepseek.com 的 TLS 在本机被拦（SEC_E_INTERNAL_ERROR），见 ADR-0001 缺口表。
  let provider = 'opencode-go';
  let model = 'deepseek-v4.1-flash';
  let pythonPath = defaultPython;
  let selected: string[] = [];

  for (const arg of argv) {
    if (arg === '--list') return 'list';
    if (arg.startsWith('--only=')) selected = arg.slice('--only='.length).split(',').filter(Boolean);
    else if (arg === '--all') selected = Object.keys(ALL_CHECKS);
    else if (arg.startsWith('--model=')) model = arg.slice('--model='.length);
    else if (arg.startsWith('--provider=')) provider = arg.slice('--provider='.length);
    else if (arg.startsWith('--python=')) pythonPath = arg.slice('--python='.length);
    else throw new Error(`未知参数：${arg}`);
  }

  if (selected.length === 0) return 'list';
  const unknown = selected.filter((name) => !(name in ALL_CHECKS));
  if (unknown.length > 0) throw new Error(`未知检查项：${unknown.join(', ')}`);
  return {
    checks: selected as CheckName[],
    provider,
    model,
    pythonPath,
  };
}

function usage(): void {
  console.log('用法：');
  console.log('  node src/main.ts --list');
  console.log(`  node src/main.ts --only=<${Object.keys(ALL_CHECKS).join('|')}>`);
  console.log('  node src/main.ts --all');
  console.log('可选：--provider=deepseek --model=deepseek-flash --python=<venv python 绝对路径>');
}

async function main(): Promise<number> {
  let options: Options | 'list';
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`参数错误：${String(error)}`);
    usage();
    return 2;
  }
  if (options === 'list') {
    usage();
    return 0;
  }

  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const outDir = join(repoRoot, 'spikes', 'm0-pi', 'out');
  const log = new Log(join(outDir, `run-${stamp}.log`));

  await log.step('环境自检');
  const cli = resolvePiCli();
  const facts: Record<string, unknown> = {
    node: process.version,
    piVersion: piVersion(),
    piCli: cli,
    storeRoot,
    provider: options.provider,
    model: options.model,
    pythonPath: options.pythonPath,
    apiKey: mask(process.env[PROVIDER_KEY_ENV]),
  };
  await log.info(`node ${process.version} / pi ${String(facts['piVersion'])}`);
  await log.info(`cli ${cli}`);
  await log.info(`模型 ${options.provider}/${options.model}`);
  await log.info(`凭证 ${PROVIDER_KEY_ENV} ${String(facts['apiKey'])}`);
  await log.info(`制品根 ${storeRoot}`);

  if (!existsSync(cli)) {
    await log.fail(`pi CLI 不存在：${cli}`);
    return 1;
  }
  if (!existsSync(options.pythonPath)) {
    await log.fail(`Python 解释器不存在：${options.pythonPath}`);
    return 1;
  }
  if (!process.env[PROVIDER_KEY_ENV]) {
    await log.fail(`缺少 ${PROVIDER_KEY_ENV}：pi 无法鉴权，检查会全部失败`);
    return 1;
  }

  const results: CheckResult[] = [];
  for (const name of options.checks) {
    const runDir = join(storeRoot, 'm0', `${name}-${stamp}`);
    await mkdir(runDir, { recursive: true });
    await log.step(`===== ${name} =====`);
    const ctx: CheckContext = {
      log,
      cli,
      provider: options.provider,
      model: options.model,
      runDir,
      pythonPath: options.pythonPath,
    };
    const startedAt = Date.now();
    try {
      const result = await ALL_CHECKS[name](ctx);
      result.facts['runDir'] = runDir;
      result.facts['durationMs'] = Date.now() - startedAt;
      results.push(result);
      if (result.ok) await log.ok(`${name}: ${result.detail}`);
      else await log.fail(`${name}: ${result.detail}`);
    } catch (error) {
      const message = error instanceof Error ? `${error.message}\n${error.stack ?? ''}` : String(error);
      results.push({
        name,
        ok: false,
        detail: `抛出异常：${message.split('\n')[0]}`,
        facts: { runDir, error: message, durationMs: Date.now() - startedAt },
      });
      await log.fail(`${name}: 抛出异常 ${message.split('\n')[0]}`);
    }
  }

  const report = {
    generatedAt: new Date().toISOString(),
    env: facts,
    results,
    summary: {
      total: results.length,
      passed: results.filter((r) => r.ok).length,
      failed: results.filter((r) => !r.ok).map((r) => r.name),
    },
  };
  const reportPath = join(outDir, `report-${stamp}.json`);
  await log.writeReport(reportPath, report);

  await log.step('汇总');
  for (const result of results) {
    await log.line(result.ok ? 'ok' : 'fail', `${result.name} — ${result.detail}`);
  }
  await log.info(`报告：${reportPath}`);
  await writeFile(join(outDir, 'latest.txt'), `${reportPath}\n`, 'utf8');

  return report.summary.failed.length === 0 ? 0 : 1;
}

raiseExit(await main());

function raiseExit(code: number): never {
  process.exit(code);
}
