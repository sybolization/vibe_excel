/**
 * 插件：telemetry —— 一次执行的事实流（events.jsonl + timeline.txt）。
 *
 * 只记录观测到的事实：事件类型、序号、时间、工具名、截断的载荷。
 * 不做「进度百分比」之类的推断——那是展示层的事。
 * 换 otel / 远程采集时，替换的是这一个插件。
 */

import { createWriteStream, mkdirSync, writeFileSync, type WriteStream } from 'node:fs';
import { join } from 'node:path';

import { definePlugin, type PluginContext, type TelemetryService, type TelemetrySink } from '../kernel.ts';

const truncate = (value: string, max: number): string => (value.length <= max ? value : `${value.slice(0, max)}…`);

/** 从一条事件里抠出人能读的要点；事件形状没有对外契约，取不到就退回截断的 JSON。 */
export function brief(raw: unknown): string {
  if (raw === null || typeof raw !== 'object') return truncate(String(raw), 140);
  const obj = raw as Record<string, unknown>;
  const parts: string[] = [];
  const toolName = obj['toolName'] ?? (obj['type'] === 'tool_execution_start' ? obj['name'] : undefined);
  if (typeof toolName === 'string') parts.push(`tool=${toolName}`);
  if (typeof obj['isError'] === 'boolean') parts.push(`isError=${String(obj['isError'])}`);
  const payload = obj['result'] ?? obj['output'] ?? obj['input'] ?? obj['text'];
  if (typeof payload === 'string' && payload.trim().length > 0) {
    parts.push(`«${truncate(payload.replace(/\s+/g, ' ').trim(), 180)}»`);
  } else if (payload !== undefined) {
    parts.push(`«${truncate(JSON.stringify(payload), 180)}»`);
  }
  if (parts.length === 0) parts.push(truncate(JSON.stringify(raw), 160));
  return parts.join(' ');
}

class JsonlSink implements TelemetrySink {
  readonly runDir: string;
  private readonly stream: WriteStream;
  private readonly echo: boolean;
  private readonly timelineLines: string[] = [];
  private readonly types: Record<string, number> = {};
  private total = 0;
  private skippedDeltas = 0;
  private seq = 0;
  private closed = false;

  constructor(runDir: string, echo: boolean) {
    mkdirSync(runDir, { recursive: true });
    this.runDir = runDir;
    this.echo = echo;
    this.stream = createWriteStream(join(runDir, 'events.jsonl'), { flags: 'a' });
  }

  event(raw: unknown): void {
    const type =
      typeof raw === 'object' && raw !== null && 'type' in raw && typeof (raw as { type: unknown }).type === 'string'
        ? (raw as { type: string }).type
        : 'unknown';
    this.total += 1;
    this.types[type] = (this.types[type] ?? 0) + 1;
    this.stream.write(`${JSON.stringify(raw)}\n`);
    if (type === 'message_update') {
      this.skippedDeltas += 1;
      return;
    }
    const line = `${String(this.seq++).padStart(4, '0')}  ${new Date().toISOString()}  ${type.padEnd(22)}  ${brief(raw)}`;
    this.timelineLines.push(line);
    if (this.echo) console.log(`  ${line}`);
  }

  timeline(): string {
    return [
      ...this.timelineLines,
      '',
      `# 共 ${this.total} 条事件，类型 ${Object.keys(this.types).length} 种；折叠 ${this.skippedDeltas} 条流式增量`,
      `# 类型分布：${Object.entries(this.types).map(([k, v]) => `${k}=${v}`).join(' ')}`,
    ].join('\n');
  }

  eventCount(): number {
    return this.total;
  }

  typeCounts(): Record<string, number> {
    return { ...this.types };
  }

  /** 落盘时间线并关闭流；重复调用是空操作。 */
  async flush(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    writeFileSync(join(this.runDir, 'timeline.txt'), `${this.timeline()}\n`, 'utf8');
    await new Promise<void>((done) => this.stream.end(() => done()));
  }

  async close(): Promise<void> {
    await this.flush();
  }
}

export interface TelemetryConfig {
  /** 是否把事件逐条回显到 stdout（长任务的可观测性：静默即缺陷）。 */
  echo: boolean;
}

export default definePlugin<TelemetryConfig>({
  name: 'telemetry',
  provide: ['telemetry'],
  config(raw: unknown): TelemetryConfig {
    const input = (raw ?? {}) as { echo?: unknown };
    return { echo: input.echo === true };
  },
  apply(ctx: PluginContext, config: TelemetryConfig): void {
    const open = new Set<JsonlSink>();
    const service: TelemetryService = {
      begin(runDir: string): TelemetrySink {
        const sink = new JsonlSink(runDir, config.echo);
        open.add(sink);
        return sink;
      },
    };
    ctx.provide('telemetry', service);
    ctx.effect(() => () => {
      void Promise.all([...open].map((sink) => sink.flush()));
      open.clear();
    });
  },
});
