/**
 * 演示台的环境事实。
 *
 * 一条原则：这里只放「事实」——真实路径、真实模型、真实数据日期。
 * 不放任何模型解释，也不在这层做业务判断。
 */

import { join, resolve } from 'node:path';

export const repoRoot = resolve(import.meta.dirname, '../../..');

/** 制品根：控制面库、程序仓库、每会话工作区都在这里，和代码仓库分开。 */
export const storeRoot = process.env['VIBE_EXCEL_STORE'] ?? resolve(repoRoot, '..', 'vibe_excel-store');

/** 唯一可用的 Python（系统 python 是 WindowsApps 的 0 字节壳，已记录在 ADR-0001）。 */
export const pythonExe = join(repoRoot, 'runtime-env', '.venv', 'Scripts', 'python.exe');

/** 真实数据来源。 */
export const groundTruthDir = join(repoRoot, 'ground_true_excel', 'wenxin');

/** 本期输入的 5 个真实文件（文件名带导出时间戳，是业务事实，不改名）。 */
export const inputFiles = [
  '所有员工_202610231434.XLS',
  'Position+-+Export+To+CSV_2026_10_20.xlsx',
  '可用岗位表_2026-10-22.xlsx',
  'New Hire_202610231439.XLS',
  'Re Hire_202610231438.XLS',
] as const;

/**
 * 固定业务日期。
 *
 * 输入是 2026-10-23 导出的，而今天是 2026-10-30。用户流程里的「当日」如果按系统时间算，
 * 会筛出 0 人——所以必须显式钉住，这也是 cleanRun 里 VIBE_EXCEL_AS_OF 存在的原因。
 */
export const asOf = '2026-10-23';

export const programName = 'molex-prep';

export const provider = process.env['VIBE_EXCEL_PROVIDER'] ?? 'opencode-go';
export const model = process.env['VIBE_EXCEL_MODEL'] ?? 'deepseek-v4.1-flash';
export const PROVIDER_KEY_ENV = 'OPENCODE_API_KEY';

/** 两个入口的阶段定义：编排器按这个契约调用程序，模型不能改。 */
export interface StageSpec {
  entry: string;
  args: (paths: { inputs: string; db: string; out: string }) => string[];
  /** 该阶段声明的产出（相对 out 目录；空表示产出就是业务库本身）。 */
  outputs: string[];
  timeoutMs: number;
}

export const stages: Record<string, StageSpec> = {
  import: {
    entry: 'import_hr.py',
    args: (p) => ['--inputs', p.inputs, '--db', p.db],
    outputs: [],
    timeoutMs: 15 * 60 * 1000,
  },
  analyze: {
    entry: 'analyze.py',
    args: (p) => ['--db', p.db, '--out', p.out, '--as-of', asOf],
    outputs: ['summary.json', 'population.csv', 'pn_available.csv'],
    timeoutMs: 15 * 60 * 1000,
  },
};
