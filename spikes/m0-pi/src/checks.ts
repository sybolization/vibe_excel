/**
 * M0 的单个验证项。每一项都只回答一个「事实问题」，
 * 并且尽量用**文件系统或子进程的直接证据**判断，而不是相信模型的说明文字。
 */

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import type { Log } from './log.ts';
import { createClient, EventRecorder, exportSessionHtml, PROVIDER_KEY_ENV, withSanitizedEnv } from './pi.ts';

export interface CheckContext {
  log: Log;
  cli: string;
  provider: string;
  model: string;
  /** 本项检查独占的目录。 */
  runDir: string;
  pythonPath: string;
}

export interface CheckResult {
  name: string;
  ok: boolean;
  detail: string;
  facts: Record<string, unknown>;
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/** 统一到正斜杠小写，便于比较 Windows 路径。 */
const norm = (value: string): string => value.replace(/\\/g, '/').toLowerCase();

/** 用 venv 的解释器执行一段 Python，拿回 stdout。 */
function runPython(pythonPath: string, script: string, args: string[] = []): string {
  return execFileSync(pythonPath, ['-c', script, ...args], {
    encoding: 'utf8',
    env: { ...process.env, PYTHONUTF8: '1' },
  }).trim();
}

/** BashResult 的具体字段没有验证过，这里按已知字段名容错取值，绝不假设形状。 */
function bashText(result: unknown): string {
  if (typeof result === 'string') return result;
  if (result && typeof result === 'object') {
    const obj = result as Record<string, unknown>;
    for (const key of ['output', 'stdout', 'text', 'content']) {
      const value = obj[key];
      if (typeof value === 'string') return value;
    }
  }
  return JSON.stringify(result);
}

/**
 * 检查库结构与「来源行去向」。
 *
 * 为什么不能简单把各表行数求和：模型可能额外建记账表（例如 import_log），
 * 那不是业务记录却会被算进去。指南 §5.3 真正要求的是
 * 「每个候选业务来源行都有明确去向」，所以判据是**来源行覆盖**：
 * 找出带来源坐标列的表，看每个来源行是否恰好落在一个去向里。
 */
const SQLITE_INSPECT = `
import json, re, sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(db)
tables = [r[0] for r in con.execute(
    "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
pat = re.compile(r'(source_?row|row_?(no|num|index)|source_?line|line_?no)', re.I)
counts, schema, destinations = {}, {}, {}
for t in tables:
    cols = [d[1] for d in con.execute('pragma table_info("%s")' % t)]
    schema[t] = cols
    counts[t] = con.execute('select count(*) from "%s"' % t).fetchone()[0]
    rowcol = next((c for c in cols if pat.search(c)), None)
    if rowcol is not None:
        for (v,) in con.execute('select distinct "%s" from "%s" order by 1' % (rowcol, t)):
            destinations.setdefault(str(v), []).append(t)
print(json.dumps({"counts": counts, "schema": schema, "destinations": destinations}, ensure_ascii=False))
`;

interface DbInspection {
  counts: Record<string, number>;
  schema: Record<string, string[]>;
  destinations: Record<string, string[]>;
}

/** 读取库结构；库不存在时返回空结构。 */
function inspectDb(pythonPath: string, dbPath: string): DbInspection {
  if (!existsSync(dbPath)) return { counts: {}, schema: {}, destinations: {} };
  const raw = runPython(pythonPath, SQLITE_INSPECT, [dbPath.replace(/\\/g, '/')]);
  return JSON.parse(raw) as DbInspection;
}

/**
 * 收尾：关闭事件流、写时间线、把会话导出成 HTML。
 *
 * 必须在 client.stop() **之后**调用，否则会话文件还没写完整。
 */
async function finishTrace(ctx: CheckContext, recorder: EventRecorder): Promise<Record<string, unknown>> {
  await recorder.close();
  const timelinePath = join(ctx.runDir, 'timeline.txt');
  await writeFile(timelinePath, `${recorder.timelineLines().join('\n')}\n`, 'utf8');

  const sessionDir = join(ctx.runDir, 'sessions');
  const files = await readdir(sessionDir).catch(() => [] as string[]);
  const html: string[] = [];
  for (const name of files.filter((f) => f.endsWith('.jsonl'))) {
    const out = join(ctx.runDir, `${name.replace(/\.jsonl$/, '')}.html`);
    if (exportSessionHtml(ctx.cli, join(sessionDir, name), out)) html.push(out);
  }

  return {
    trace: {
      events: join(ctx.runDir, 'events.jsonl'),
      timeline: timelinePath,
      sessions: files.map((f) => join(sessionDir, f)),
      html,
    },
  };
}

/** 每项检查都开一个独立会话目录，让 pi 自己写可恢复、可导出的会话文件。 */
async function sessionDirFor(ctx: CheckContext): Promise<string> {
  const dir = join(ctx.runDir, 'sessions');
  await mkdir(dir, { recursive: true });
  return dir;
}

/* ================================================================== *
 * 1. 独立 cwd + 事件流
 * ================================================================== */

export async function checkSpawn(ctx: CheckContext): Promise<CheckResult> {
  const work = join(ctx.runDir, 'work');
  const agentDir = join(ctx.runDir, 'agent');
  await mkdir(work, { recursive: true });
  await ctx.log.step(`启动 pi（cwd=${work}）`);

  const recorder = new EventRecorder();
  recorder.attach(join(ctx.runDir, 'events.jsonl'));
  const client = createClient({
    cliPath: ctx.cli,
    cwd: work,
    agentDir,
    provider: ctx.provider,
    model: ctx.model,
    args: ['--session-dir', await sessionDirFor(ctx)],
  });
  const facts: Record<string, unknown> = {};
  try {
    client.onEvent((event) => recorder.handle(event));
    await client.start();
    await ctx.log.ok('子进程已启动');

    const state = await client.getState();
    facts.stateKeys = Object.keys(state as object).sort();

    // 用子进程自己报告的 cwd 作证据，而不是相信构造参数。
    const probe = await client.bash('node -p "process.cwd()"');
    const reported = bashText(probe).trim();
    facts.reportedCwd = reported;
    facts.expectedCwd = work;
    const cwdOk = norm(reported).endsWith(norm(work));

    facts.eventTypes = recorder.typeCounts;
    facts.eventCount = recorder.facts.length;
    const hasEvents = recorder.facts.length > 0;

    facts.stderrBytes = client.getStderr().length;
    await client.stop().catch(() => undefined);
    Object.assign(facts, await finishTrace(ctx, recorder));
    return {
      name: 'spawn',
      ok: cwdOk && hasEvents,
      detail: `cwd 一致=${cwdOk}，事件 ${recorder.facts.length} 条（${Object.keys(recorder.typeCounts).length} 种）`,
      facts,
    };
  } finally {
    await client.stop().catch(() => undefined);
  }
}

/* ================================================================== *
 * 2. 不用自定义工具完成 Python → SQLite 导入
 *
 * 判据用「守恒」而不是规定模型行为：每个来源行都必须有去向
 * （staging 或 quarantine），总数对得上即可（指南 §5.3）。
 * ================================================================== */

const INPUT_CSV = `姓名,部门,入职日期,状态
张三,研发,2026/3/1,已入职
李四,研发部,2026-03-05,
王五,市场,三月六日,待确认
赵六,研发,2026-03-10,已入职
钱七,市场,2026/3/12,待确认
`;

export async function checkPython(ctx: CheckContext): Promise<CheckResult> {
  const work = join(ctx.runDir, 'work');
  const agentDir = join(ctx.runDir, 'agent');
  await mkdir(work, { recursive: true });
  await writeFile(join(work, 'input.csv'), INPUT_CSV, 'utf8');
  await ctx.log.step('已准备 input.csv（5 行，含文本日期与部门别名）');

  const recorder = new EventRecorder();
  recorder.attach(join(ctx.runDir, 'events.jsonl'));
  const client = createClient({
    cliPath: ctx.cli,
    cwd: work,
    agentDir,
    provider: ctx.provider,
    model: ctx.model,
    args: ['--session-dir', await sessionDirFor(ctx)],
  });
  const facts: Record<string, unknown> = {};
  try {
    client.onEvent((event) => recorder.handle(event));
    await client.start();

    const prompt = [
      '你在一个独立工作目录中工作，可以用 bash、read、write、edit 等现有工具，没有别的专用工具。',
      `Python 解释器绝对路径：${ctx.pythonPath.replace(/\\/g, '/')}`,
      '任务：',
      '1. 读取当前目录的 input.csv；',
      '2. 写一个 import.py，把它导入 biz.db：能确定的记录进入 staging_records 表，',
      '   保留原始值并记录来源行号；无法确定的（例如没有年份的日期）进入 quarantine 表并写明原因；',
      '3. 用 bash 运行 import.py；',
      '4. 用 SQL 查询两张表的行数并打印结果。',
      '要求：不要丢弃任何来源行，每条来源行必须有去向。完成后用一两句话说明结果。',
    ].join('\n');

    await ctx.log.step('发送导入任务（真模型，可能要几十秒）');
    const started = Date.now();
    const events = await client.promptAndWait(prompt, undefined, 240_000);
    const elapsedMs = Date.now() - started;

    facts.elapsedMs = elapsedMs;
    facts.eventCount = events.length;
    facts.eventTypes = recorder.typeCounts;
    facts.toolNames = recorder.toolNames();

    const dbPath = join(work, 'biz.db');
    const inspection = inspectDb(ctx.pythonPath, dbPath);
    facts.dbExists = existsSync(dbPath);
    facts.dbTables = inspection.counts;
    facts.dbSchema = inspection.schema;
    facts.destinations = inspection.destinations;

    // 守恒：每个来源行必须恰好有一个去向（staging 或 quarantine），
    // 不能丢失，也不能同时进两张表。
    const coveredRows = Object.keys(inspection.destinations);
    const multiDestination = Object.entries(inspection.destinations)
      .filter(([, where]) => where.length > 1)
      .map(([row]) => row);
    facts.distinctSourceRows = coveredRows.length;
    facts.multiDestinationRows = multiDestination;
    facts.sourceRows = 5;

    const conservationOk = coveredRows.length === 5 && multiDestination.length === 0;

    try {
      const stats = await client.getSessionStats();
      facts.sessionStats = stats;
    } catch (error) {
      facts.sessionStatsError = String(error);
    }

    await client.stop().catch(() => undefined);
    Object.assign(facts, await finishTrace(ctx, recorder));

    return {
      name: 'python',
      ok: facts.dbExists === true && conservationOk,
      detail: facts.dbExists
        ? `表=${JSON.stringify(inspection.counts)}；来源行覆盖 ${coveredRows.length}/5`
          + (multiDestination.length > 0 ? `，重复去向 ${multiDestination.join(',')}` : '')
        : 'biz.db 未生成',
      facts,
    };
  } finally {
    await client.stop().catch(() => undefined);
  }
}

/* ================================================================== *
 * 3. 取消：确定性验证，不依赖模型配合
 *
 * 用客户端发起的 bash 跑一个 8 秒后才写标记文件的 Python，
 * abort 后等待 10 秒；标记文件不出现，才说明子进程真的被打断了。
 * ================================================================== */

export async function checkCancel(ctx: CheckContext): Promise<CheckResult> {
  const work = join(ctx.runDir, 'work');
  const agentDir = join(ctx.runDir, 'agent');
  await mkdir(work, { recursive: true });

  const marker = join(work, 'cancel-finished.txt').replace(/\\/g, '/');
  const recorder = new EventRecorder();
  recorder.attach(join(ctx.runDir, 'events.jsonl'));
  const client = createClient({
    cliPath: ctx.cli,
    cwd: work,
    agentDir,
    provider: ctx.provider,
    model: ctx.model,
    args: ['--session-dir', await sessionDirFor(ctx)],
  });
  const facts: Record<string, unknown> = { marker };
  try {
    client.onEvent((event) => recorder.handle(event));
    await client.start();

    const py = ctx.pythonPath.replace(/\\/g, '/');
    const command = `"${py}" -c "import time,pathlib; time.sleep(8); pathlib.Path(r'${marker}').write_text('finished')"`;
    facts.command = command;
    await ctx.log.step('发起 8 秒后落标记的 Python 命令');
    const pending = client.bash(command).then(
      (r) => ({ settled: 'resolved' as const, text: bashText(r) }),
      (e: unknown) => ({ settled: 'rejected' as const, text: String(e) }),
    );

    await sleep(1_500);
    await ctx.log.step('调用 abortBash()');
    await client.abortBash().catch((e: unknown) => {
      facts.abortError = String(e);
    });
    const outcome = await Promise.race([pending, sleep(15_000).then(() => ({ settled: 'pending' as const, text: '' }))]);
    facts.bashOutcome = outcome.settled;

    await ctx.log.step('等待 10 秒，检查标记文件是否出现');
    await sleep(10_000);
    facts.markerExists = existsSync(marker);

    await client.stop().catch(() => undefined);
    Object.assign(facts, await finishTrace(ctx, recorder));

    return {
      name: 'cancel',
      ok: facts.markerExists === false,
      detail: facts.markerExists === false
        ? `标记文件未出现，子进程确实被打断（bash ${outcome.settled}）`
        : '标记文件出现 —— 取消没有真正终止子进程',
      facts,
    };
  } finally {
    await client.stop().catch(() => undefined);
  }
}

/* ================================================================== *
 * 4. 对话恢复：换一个新进程，把上一轮说过的数字取回来
 * ================================================================== */

export async function checkResume(ctx: CheckContext): Promise<CheckResult> {
  const work = join(ctx.runDir, 'work');
  const agentDir = join(ctx.runDir, 'agent');
  const sessionDir = join(ctx.runDir, 'sessions');
  await mkdir(work, { recursive: true });
  await mkdir(sessionDir, { recursive: true });

  const facts: Record<string, unknown> = { sessionDir };
  const recorder = new EventRecorder();
  recorder.attach(join(ctx.runDir, 'events.jsonl'));
  const base = {
    cliPath: ctx.cli,
    cwd: work,
    agentDir,
    provider: ctx.provider,
    model: ctx.model,
  };

  // 第一轮：把数字告诉它。
  const first = createClient({ ...base, args: ['--session-dir', sessionDir, '--name', 'm0-resume'] });
  try {
    first.onEvent((event) => recorder.handle(event));
    await first.start();
    await ctx.log.step('第一轮：让它记住 4242');
    await first.promptAndWait('请记住这个数字：4242。现在只回复 OK 两个字母。', undefined, 180_000);
    facts.firstReply = await first.getLastAssistantText().catch(() => null);
    const state = (await first.getState().catch(() => null)) as Record<string, unknown> | null;
    facts.firstStateKeys = state ? Object.keys(state).sort() : null;
    facts.firstSessionFile = state && typeof state.sessionFile === 'string' ? state.sessionFile : null;
  } finally {
    await first.stop().catch(() => undefined);
  }
  facts.sessionFiles = await readdir(sessionDir).catch(() => []);

  // 第二轮：全新进程，--continue 恢复同一会话。
  const second = createClient({ ...base, args: ['--session-dir', sessionDir, '--continue'] });
  try {
    second.onEvent((event) => recorder.handle(event));
    await second.start();
    await ctx.log.step('第二轮：新进程 --continue，问它那个数字');
    await second.promptAndWait('我刚才让你记住的数字是多少？只回答数字本身。', undefined, 180_000);
    const reply = await second.getLastAssistantText().catch(() => null);
    facts.secondReply = reply;
    const recovered = typeof reply === 'string' && reply.includes('4242');
    await second.stop().catch(() => undefined);
    Object.assign(facts, await finishTrace(ctx, recorder));
    return {
      name: 'resume',
      ok: recovered,
      detail: recovered ? `新进程恢复了上下文（回复：${String(reply).slice(0, 40)}）` : `未恢复（回复：${String(reply).slice(0, 60)}）`,
      facts,
    };
  } finally {
    await second.stop().catch(() => undefined);
  }
}

/* ================================================================== *
 * 5. 隔离：环境白名单 + 每会话独立 agent 目录 + 模型可达
 * ================================================================== */

export async function checkIsolation(ctx: CheckContext): Promise<CheckResult> {
  const work = join(ctx.runDir, 'work');
  const agentDir = join(ctx.runDir, 'agent');
  await mkdir(work, { recursive: true });

  const SENTINEL = 'M0_SENTINEL_SECRET';
  const previous = process.env[SENTINEL];
  process.env[SENTINEL] = 'canary-should-not-leak';

  const facts: Record<string, unknown> = {};
  const recorder = new EventRecorder();
  recorder.attach(join(ctx.runDir, 'events.jsonl'));
  const client = createClient({
    cliPath: ctx.cli,
    cwd: work,
    agentDir,
    provider: ctx.provider,
    model: ctx.model,
    args: ['--session-dir', await sessionDirFor(ctx)],
  });
  try {
    client.onEvent((event) => recorder.handle(event));
    // 只在 spawn 期间收紧环境：子进程冻结的是那一刻的 env。
    await withSanitizedEnv(async () => {
      await client.start();
    });
    facts.parentSentinel = true;

    const probe = await client.bash(
      `node -e "console.log(JSON.stringify({sentinel:!!process.env.${SENTINEL},providerKey:!!process.env.${PROVIDER_KEY_ENV}}))"`,
    );
    const text = bashText(probe);
    facts.childEnvProbe = text.trim();
    const parsed = (() => {
      try {
        return JSON.parse(text.trim().split('\n').at(-1) ?? '{}') as { sentinel?: boolean; providerKey?: boolean };
      } catch {
        return {} as { sentinel?: boolean; providerKey?: boolean };
      }
    })();
    facts.sentinelLeaked = parsed.sentinel === true;
    facts.providerKeyVisible = parsed.providerKey === true;

    // 每会话独立 config 目录：pi 应在这里写自己的状态，而不是共用 ~/.pi/agent。
    facts.agentDirCreated = existsSync(agentDir);
    facts.agentDirEntries = await readdir(agentDir).catch(() => []);

    await ctx.log.step('在收紧环境下发一次真模型请求，确认鉴权仍可用');
    await client.promptAndWait('只回复 OK 两个字母，不要其它内容。', undefined, 180_000);
    const reply = await client.getLastAssistantText().catch(() => null);
    facts.isolatedReply = reply;

    const ok = facts.sentinelLeaked === false && facts.providerKeyVisible === true && typeof reply === 'string' && reply.length > 0;
    await client.stop().catch(() => undefined);
    Object.assign(facts, await finishTrace(ctx, recorder));
    return {
      name: 'isolation',
      ok,
      detail: `哨兵泄漏=${facts.sentinelLeaked}，provider key 可见=${facts.providerKeyVisible}，独立 agent 目录=${facts.agentDirCreated}，模型回复=${String(reply).slice(0, 20)}`,
      facts,
    };
  } finally {
    if (previous === undefined) delete process.env[SENTINEL];
    else process.env[SENTINEL] = previous;
    await client.stop().catch(() => undefined);
  }
}

export const ALL_CHECKS = {
  spawn: checkSpawn,
  python: checkPython,
  cancel: checkCancel,
  resume: checkResume,
  isolation: checkIsolation,
} as const;

export type CheckName = keyof typeof ALL_CHECKS;
