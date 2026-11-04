/**
 * 写程序的提示词要跟契约一起被检查。
 *
 * 提示词是模型唯一的说明书：契约改了、提示词没改，模型就会写出解析器读不出来的代码；
 * 模板变量拼错一个，模型看到的就是 `{{DB}}` 这种原文。两种错都在这里挡住。
 *
 * 最狠的一条：提示词里的 **few-shot 示例要真的能被解析器读**。示例是模型照着抄的模板，
 * 示例错了整批产出都会错——所以这里把那段 Python 抽出来，丢给**生产用的** `flow_parse.py`
 * 跑一遍，逐个核对解析出来的节点。测试里不另写一份检查逻辑，否则测的是副本不是产品。
 */

import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { parseCodeFlow } from '../src/flow-code.ts';
import { bundledPython } from '../src/flow-source.ts';
import { fillTemplate } from '../src/plugins/sessions.ts';

const repoRoot = resolve(import.meta.dirname, '..', '..', '..');
const promptPath = join(repoRoot, 'prompts', 'author-flow.md');
const pythonPath = join(repoRoot, 'runtime-env', '.venv', 'Scripts', 'python.exe');
const text = readFileSync(promptPath, 'utf8');

/** sessions 插件在 author 时提供的变量（大小写两套，见 sessions.ts）。 */
const VARS: Record<string, string> = {
  WORKSPACE: '## 工作区事实\n\n| 项 | 路径 | 实测状态 |',
  PYTHON: 'C:/python.exe',
  CODE: 'C:/code',
  INPUTS: 'C:/inputs',
  DB: 'C:/work/hr.db',
  OUT: 'C:/work/out',
  ASOF: '2026-01-01',
  FLOW: 'flow.json',
  code: 'C:/code',
  inputs: 'C:/inputs',
  db: 'C:/work/hr.db',
  out: 'C:/work/out',
  asOf: '2026-01-01',
};

const placeholders = [...text.matchAll(/\{\{(\w+)\}\}/g)].map((match) => match[1]!).sort();

describe('写程序的提示词：prompts/author-flow.md', () => {
  it('用到的占位符都有值（大小写两种写法都认）', () => {
    expect(placeholders.length).toBeGreaterThan(0);
    const missing = [...new Set(placeholders)].filter((key) => VARS[key] === undefined);
    expect(missing).toEqual([]);
    expect(fillTemplate(text, VARS)).not.toMatch(/\{\{\w+\}\}/);
  });

  it('把契约讲清楚了：物化、产出表、双跑、ask_user、不做无谓折叠', () => {
    expect(text).toContain('materialize');
    expect(text).toContain('outputs');
    expect(text).toMatch(/跑两遍/);
    expect(text).toContain('ask_user');
    expect(text).toMatch(/不要读系统时间/);
    expect(text).toMatch(/物化（materialize）只做映射/);
    expect(text).toMatch(/业务准确性优先/);
    expect(text).toMatch(/optimize/);
  });

  it('把「代码就是流程」的五条规则写成了可执行的句子', () => {
    expect(text).toMatch(/`main` 是唯一入口，必须线性/);
    expect(text).toMatch(/"t:表名"/);
    expect(text).toMatch(/表名必须是字面量/);
    // flow.json 里不再写节点：这条如果哪次被改回去，提示词和解析器就对不上了。
    expect(text).toMatch(/`flow\.json` 里\*\*不写节点\*\*/);
  });

  it('说清楚输入表由平台注入、写表由节点自己、入口文件怎么指定', () => {
    // 「t: 参数 = 平台把表读成 DataFrame 递进来」是 ADR-0008 §3 的运行时含义，
    // 提示词漏了这句，模型就会在节点里重复 read_sql，签名标注也就白写了。
    expect(text).toMatch(/读成 DataFrame 递给你/);
    expect(text).toMatch(/不要再去\s*`?read_sql`?/);
    expect(text).toMatch(/自己开连接/);
    expect(text).toContain('"entry"');
  });

  it('把解析器的拒绝条件列给模型看（它自查时要能对上）', () => {
    for (const needle of ['`main` 里有 `if`', '节点没有 docstring', '返回没标', '不是字符串字面量']) {
      expect(text).toContain(needle);
    }
  });

  it('提供了完整的 few-shot：flow.json + 可运行的 pipeline.py', () => {
    expect(text).toMatch(/```json[\s\S]*"materialize"/);
    expect(text).toMatch(/```python[\s\S]*def main\(db, as_of/);

    // flow.json 示例必须是合法 JSON，且形状是解析器认的那一版（只有 materialize / outputs，没有 nodes）。
    const jsonBlocks = [...text.matchAll(/```json\n([\s\S]*?)```/g)].map((match) => match[1]!);
    const flow = JSON.parse(jsonBlocks[0]!) as Record<string, unknown>;
    expect(Object.keys(flow).sort()).toEqual(['db', 'flow', 'materialize', 'outputs']);
    expect(text).toContain('"t:raw_employees"');
    expect(text).toContain('-> "t:employees"');
    // 反向教材：main 里不许出现的写法，提示词要明确点名。
    expect(text).toMatch(/`for f in \[a, b\]: f\(\)` 解析不出顺序/);
  });

  it.skipIf(!existsSync(pythonPath))('few-shot 里的 Python 真的能被解析器读成一条流', () => {
    const blocks = [...text.matchAll(/```python\n([\s\S]*?)```/g)].map((match) => match[1]!);
    expect(blocks.length).toBeGreaterThan(0);

    const code = parseCodeFlow({
      entry: 'pipeline.py',
      source: blocks[0]!,
      python: pythonPath,
      parserPath: bundledPython('py', 'flow_parse.py'),
    });

    // 节点顺序就是 main 里的调用顺序
    expect(code.nodes.map((node) => node.func)).toEqual(['clean_employees', 'clean_positions', 'match_available']);
    // 边全部来自签名标注
    expect(code.nodes.map((node) => ({ reads: node.reads, writes: node.writes }))).toEqual([
      { reads: ['raw_employees'], writes: ['employees'] },
      { reads: ['raw_positions'], writes: ['available_positions'] },
      { reads: ['employees', 'available_positions'], writes: ['match_result'] },
    ]);
    // 标题来自 docstring，块说明来自块首字符串
    expect(code.nodes[0]!.title).toContain('清洗花名册');
    expect(code.nodes[0]!.steps.some((step) => step.label.includes('空表就别往下走'))).toBe(true);
    // 示例里 main 的 pandas 流水账不该产生警告（否则模型会学着写一堆无意义提示）
    expect(code.warnings).toEqual([]);
  });
});
