/**
 * 代码即流程的解析（ADR-0008）：真实 python、真实 `ast`，不 mock。
 *
 * 这些用例是**契约的一部分**，不是实现细节：每一条都对应「模型可能写错的一种方式」，
 * 报错文案里必须带文件和行号——因为最终读它的是模型自己（和半夜排查的人）。
 *
 * 缺解释器时整组跳过：不把「本机没装」伪装成通过。
 */

import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { nodesOfCodeFlow, parseCodeFlow } from '../src/flow-code.ts';
import { bundledPython } from '../src/flow-source.ts';
import { digestOf, parseFlow, planFlow } from '../src/flow.ts';

const repoRoot = resolve(import.meta.dirname, '..', '..', '..');
const pythonPath = join(repoRoot, 'runtime-env', '.venv', 'Scripts', 'python.exe');
const ready = existsSync(pythonPath) && existsSync(bundledPython('py', 'flow_parse.py'));

const parse = (source: string, entry = 'pipeline.py') =>
  parseCodeFlow({ entry, source, python: pythonPath, parserPath: bundledPython('py', 'flow_parse.py') });

function errorOf(run: () => unknown): string {
  try {
    run();
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
  throw new Error('期望抛错，但解析成功了');
}

/** 三个节点的最小合法流：main 线性、标注齐全、docstring 齐全。 */
const GOOD = `
def main(db, as_of):
    """把原始表算成两张交付表。"""
    build_clean(db)
    enrich(db, as_of)


def build_clean(db) -> "t:clean_table":
    """清洗：去重、补空串。"""
    """同一 ID 只留一行"""
    rows = 1
    if rows:
        """空表就别往下走"""
        return None
    for name in ("a", "b"):
        """文本列补空串"""
        pass
    return None


def enrich(db, source: "t:clean_table", as_of: str) -> "t:result":
    """补业务日期。"""
    return None
`;

describe.skipIf(!ready)('parseCodeFlow：合法代码', () => {
  it('从 main 读出节点顺序，从签名标注读出边，从 docstring 读出标题', () => {
    const flow = parse(GOOD);
    expect(flow.entry).toBe('pipeline.py');
    expect(flow.warnings).toEqual([]);
    expect(flow.nodes.map((node) => node.func)).toEqual(['build_clean', 'enrich']);

    const [clean, enrich] = flow.nodes;
    expect(clean).toMatchObject({
      reads: [],
      writes: ['clean_table'],
      title: '清洗：去重、补空串。',
    });
    // as_of: str 是固定入参，不要求表标注
    expect(enrich).toMatchObject({
      reads: ['clean_table'],
      writes: ['result'],
      title: '补业务日期。',
    });
    expect(clean!.line).toBeGreaterThan(0);
  });

  it('节点内部的块说明和裸语句说明都被收成 steps（块说明记的是 if/for 那一行）', () => {
    const [clean] = parse(GOOD).nodes;
    expect(clean!.steps).toEqual([
      { line: 10, kind: 'statement', of: 'Assign', label: '同一 ID 只留一行' },
      { line: 12, kind: 'block', of: 'if', label: '空表就别往下走' },
      { line: 15, kind: 'block', of: 'for', label: '文本列补空串' },
    ]);
  });

  it('翻译成平台节点：id=函数名、call=entry:函数名，不带 args', () => {
    const nodes = nodesOfCodeFlow(parse(GOOD, 'src/build.py'));
    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toMatchObject({
      id: 'build_clean',
      call: 'src/build.py:build_clean',
      reads: [],
      writes: ['clean_table'],
      title: '清洗：去重、补空串。',
    });
    expect(nodes[0]!.args).toBeUndefined();
  });

  it('固定入参 db/as_of/inputs/out 不要求标注；main 里的 pandas 流水账不算节点', () => {
    const flow = parse(`
import sqlite3
import pandas as pd


def main(db, as_of):
    """入口。"""
    conn = sqlite3.connect(db)
    raw = pd.read_sql("select * from raw_t", conn)
    conn.close()
    print("start", as_of)
    write_t(db, raw)


def write_t(db, raw: "t:raw_t") -> "t:t":
    """写 t。"""
`);
    expect(flow.nodes.map((node) => node.func)).toEqual(['write_t']);
    expect(flow.warnings).toEqual([]);
  });

  it('同一个函数的两个参数可以吃同一张表：reads 去重（表的机制允许自连接）', () => {
    const [node] = parse(`
def main(db):
    """入口。"""
    join_t(db)


def join_t(db, left: "t:employees", right: "t:employees") -> "t:joined":
    """自连接。"""
`).nodes;
    expect(node!.reads).toEqual(['employees']);
    expect(node!.writes).toEqual(['joined']);
  });

  it('读写同一张表（原地更新）是合法的', () => {
    const [node] = parse(`
def main(db):
    """入口。"""
    refresh(db)


def refresh(db, current: "t:board") -> "t:board":
    """原地刷新看板。"""
`).nodes;
    expect(node).toMatchObject({ reads: ['board'], writes: ['board'] });
  });

  it('带表标注却没被 main 调用的函数：不是节点，但要说出来（死代码）', () => {
    const flow = parse(`
def main(db):
    """入口。"""
    real(db)


def real(db) -> "t:used":
    """真的会跑。"""


def never_called(db) -> "t:orphan":
    """没人调，多半是忘记在 main 里加一行。"""
`);
    expect(flow.nodes.map((node) => node.func)).toEqual(['real']);
    expect(flow.warnings).toHaveLength(1);
    expect(flow.warnings[0]).toMatchObject({ code: 'UNUSED_TABLE_FUNC', line: 11 });
    expect(flow.warnings[0]!.message).toContain('never_called');
  });

  it('from __future__ import annotations / BOM / CRLF 都不影响解析', () => {
    const future = parse(`
from __future__ import annotations


def main(db):
    """入口。"""
    build(db, 1)


def build(db, raw: "t:raw_a") -> "t:a":
    """写 a。"""
`);
    expect(future.nodes.map((node) => node.func)).toEqual(['build']);

    const messy = `\ufeff${GOOD.replace(/\n/g, '\r\n')}`;
    expect(parse(messy).nodes.map((node) => node.func)).toEqual(['build_clean', 'enrich']);
  });

  it('同一张图：手写 nodes 与从代码解析，摘要必须相等（ADR-0008 的等价性判据）', () => {
    const code = parse(GOOD);
    const declared = parseFlow({
      flow: 'molex-prep',
      db: 'hr.db',
      outputs: ['result'],
      materialize: [],
      nodes: [
        { id: 'build_clean', call: 'pipeline.py:build_clean', reads: [], writes: ['clean_table'] },
        { id: 'enrich', call: 'pipeline.py:enrich', reads: ['clean_table'], writes: ['result'] },
      ],
    });
    const fromCode = { ...parseFlow({ flow: 'molex-prep', db: 'hr.db', outputs: ['result'], materialize: [] }), nodes: nodesOfCodeFlow(code) };

    expect(planFlow(fromCode).digest).toBe(digestOf(declared));
    expect(planFlow(fromCode).order).toEqual(planFlow(declared).order);
  });
});

describe.skipIf(!ready)('parseCodeFlow：main 必须是线性的', () => {
  it('没有 main 就报错（并说清楚要写什么）', () => {
    const message = errorOf(() => parse('def build(db) -> "t:a":\n    """x"""\n'));
    expect(message).toContain('MAIN_MISSING');
    expect(message).toContain('def main');
  });

  it.each([
    ['for', '    for f in (a, b):\n        f(db)\n'],
    ['if', '    if db:\n        build(db)\n'],
    ['while', '    while False:\n        build(db)\n'],
    ['try', '    try:\n        build(db)\n    except Exception:\n        pass\n'],
    ['with', '    with open(db) as fh:\n        build(db)\n'],
  ])('main 里有 %s → 报行号并指路「搬进被调函数」', (_kind, block) => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n${block}\n\ndef build(db) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('MAIN_CONTROL_FLOW');
    expect(message).toMatch(/pipeline\.py:\d+/);
    expect(message).toContain('线性');
  });

  it('main 里定义函数也算不线性', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    def inner():\n        pass\n    build(db)\n\ndef build(db) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('MAIN_CONTROL_FLOW');
  });

  it('一条语句里塞两个节点调用（f(g(x))）报错', () => {
    const message = errorOf(() =>
      parse(`
def main(db):
    """入口。"""
    outer(db, inner(db))


def inner(db) -> "t:i":
    """内。"""


def outer(db, x: "t:i") -> "t:o":
    """外。"""
`),
    );
    expect(message).toContain('MAIN_NESTED_CALL');
    expect(message).toMatch(/inner \/ outer|outer \/ inner/);
  });

  it('main 里一个节点都没调用（例如全靠 import 进来的函数）报错', () => {
    const message = errorOf(() =>
      parse(`import helpers\n\ndef main(db):\n    """入口。"""\n    helpers.clean(db)\n`),
    );
    expect(message).toContain('MAIN_NO_NODES');
    expect(message).toContain('import');
  });

  it('main 调用了本文件没定义的裸名函数 → 拒绝（跨文件的节点会安静地少一步）', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    clean_employes(db)\n\ndef build(db) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('MAIN_CALL_NOT_LOCAL');
    expect(message).toContain('clean_employes');
  });

  it('main 里重复调用同一个函数 → 报错（一个函数只能是一个节点）', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    build(db)\n    build(db)\n\ndef build(db) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('NODE_DUPLICATE');
  });

  it('main 自己带 t: 标注 → 报错（main 不参与图）', () => {
    const message = errorOf(() =>
      parse(`def main(db, raw: "t:raw_t"):\n    """入口。"""\n    build(db)\n\ndef build(db) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('MAIN_TABLE_PARAM');
  });
});

describe.skipIf(!ready)('parseCodeFlow：节点签名必须是表标注', () => {
  it('没有 docstring 就没有节点标题，直接拒绝', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    build(db)\n\ndef build(db) -> "t:a":\n    pass\n`),
    );
    expect(message).toContain('NODE_NO_DOC');
  });

  it('参数既不是固定入参也没有标注 → 拒绝，并列出可用的固定入参', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    build(db, 1)\n\ndef build(db, thing) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('NODE_PARAM_UNANNOTATED');
    expect(message).toContain('thing');
    expect(message).toContain('db / as_of / inputs / out');
  });

  it('report 是没实现的固定入参：拒绝时要说清楚', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    build(db)\n\ndef build(db, report) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('NODE_PARAM_UNANNOTATED');
    expect(message).toContain('report');
  });

  it('返回没标注 / 标注成 None → 拒绝（节点必须声明它写哪张表）', () => {
    expect(errorOf(() => parse(`def main(db):\n    """入口。"""\n    build(db)\n\ndef build(db):\n    """写 a。"""\n`))).toContain(
      'NODE_RETURN_NOT_TABLE',
    );
    expect(
      errorOf(() => parse(`def main(db):\n    """入口。"""\n    build(db)\n\ndef build(db) -> None:\n    """写 a。"""\n`)),
    ).toContain('NODE_RETURN_NOT_TABLE');
  });

  it.each([
    ['f-string', 'raw: f"t:{name}"'],
    ['变量', 'raw: TABLE_NAME'],
    ['下标', 'raw: Table["raw"]'],
    ['普通类型', 'raw: str'],
  ])('表名是 %s 而不是字面量 → 拒绝', (_kind, annotation) => {
    const message = errorOf(() =>
      parse(
        `name = "raw"\nTABLE_NAME = "raw"\n\n\ndef main(db):\n    """入口。"""\n    build(db)\n\n\ndef build(db, ${annotation}) -> "t:a":\n    """写 a。"""\n`,
      ),
    );
    expect(message).toContain('NODE_PARAM_NOT_TABLE');
    expect(message).toContain('字面量');
  });

  it('中文/带点/带空格的表名 → 拒绝（表名要能直接拼进 SQL）', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    build(db)\n\ndef build(db) -> "t:员工表":\n    """写 a。"""\n`),
    );
    expect(message).toContain('NODE_TABLE_NAME_BAD');
    expect(message).toContain('ASCII');
  });

  it('async 函数不能当节点', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    build(db)\n\nasync def build(db) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('NODE_ASYNC');
  });

  it('位置限定参数（`/`）不能当节点：平台按关键字传参', () => {
    const message = errorOf(() =>
      parse(`def main(db):\n    """入口。"""\n    build(db)\n\ndef build(db, raw, /) -> "t:a":\n    """写 a。"""\n`),
    );
    expect(message).toContain('NODE_POSONLY');
  });

  it('语法错误报行号，而不是抛 traceback', () => {
    const message = errorOf(() => parse('def main(db)\n    """缺冒号"""\n'));
    expect(message).toContain('SYNTAX_ERROR');
    expect(message).toMatch(/pipeline\.py:\d+/);
  });
});
