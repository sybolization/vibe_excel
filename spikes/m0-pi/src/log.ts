/** 进度与报告输出。要求：每步立刻可见（静默即缺陷），同时落盘便于追溯。 */

import { appendFile, mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

export type Level = 'step' | 'ok' | 'warn' | 'fail' | 'info';

const MARK: Record<Level, string> = {
  step: '▶',
  ok: '✔',
  warn: '!',
  fail: '✘',
  info: '·',
};

export class Log {
  private readonly buffer: string[] = [];
  private readonly filePath: string | undefined;

  constructor(filePath?: string) {
    this.filePath = filePath;
  }

  async line(level: Level, message: string): Promise<void> {
    const stamp = new Date().toISOString().slice(11, 23);
    const text = `${MARK[level]} [${stamp}] ${message}`;
    // 立即输出，不要攒到结束。
    console.log(text);
    this.buffer.push(`${level}\t${new Date().toISOString()}\t${message}`);
    if (this.filePath) {
      await mkdir(dirname(this.filePath), { recursive: true });
      await appendFile(this.filePath, `${this.buffer.at(-1)}\n`, 'utf8');
    }
  }

  step(m: string): Promise<void> {
    return this.line('step', m);
  }
  ok(m: string): Promise<void> {
    return this.line('ok', m);
  }
  warn(m: string): Promise<void> {
    return this.line('warn', m);
  }
  fail(m: string): Promise<void> {
    return this.line('fail', m);
  }
  info(m: string): Promise<void> {
    return this.line('info', m);
  }

  async writeReport(path: string, report: unknown): Promise<void> {
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, `${JSON.stringify(report, null, 2)}\n`, 'utf8');
  }
}

/** 凭证永不进日志：只允许打印长度或是否存在。 */
export const mask = (value: string | undefined): string =>
  value === undefined ? '<unset>' : `<set len=${value.length}>`;
