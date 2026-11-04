/**
 * flow.ts 的单元测试：解析/校验、拓扑、摘要、优化提示、CLI 描述。
 *
 * 这个模块是纯的，测试不碰文件系统；能跑的 flow 用工厂函数拼出来，
 * 只有「故意写错」的用例才直接改原始 JSON 字段。
 */

import { describe, expect, it } from 'vitest';

import {
  describeFlow,
  digestOf,
  parseCall,
  parseFlow,
  parseFlowJson,
  planFlow,
  suggestOptimizations,
} from '../src/flow.ts';
import type { FlowNode, FlowSpec } from '../src/flow.ts';

/** 跑一次并拿到错误信息；一个用例要核对多个关键字时用它。 */
function errorOf(run: () => unknown): string {
  try {
    run();
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
  throw new Error('期望抛错，但函数正常返回了');
}

/** 最小可用 spec：一个物化 + 一个节点，节点直接写产出表。 */
function minimalFlow(overrides: Partial<FlowSpec> = {}): FlowSpec {
  return {
    flow: 'molex-prep',
    db: 'hr.db',
    materialize: [{ file: 'headcount.xlsx', table: 'raw_headcount' }],
    outputs: ['result'],
    nodes: [{ id: 'build', call: 'pipeline.py:build', reads: ['raw_headcount'], writes: ['result'] }],
    ...overrides,
  };
}

/** 未校验的 JSON 形状：故意留给 parseFlow 去挑错。 */
function rawFlow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    flow: 'molex-prep',
    db: 'hr.db',
    materialize: [{ file: 'headcount.xlsx', table: 'raw_headcount' }],
    outputs: ['result'],
    nodes: [rawNode()],
    ...overrides,
  };
}

function rawNode(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { id: 'build', call: 'pipeline.py:build', reads: ['raw_headcount'], writes: ['result'], ...overrides };
}

/** 拓扑用例里的节点：call 与 id 对齐，省得每次手写。 */
function step(id: string, reads: string[], writes: string[], call = `pipeline.py:${id}`): FlowNode {
  return { id, call, reads, writes };
}

function cloneSpec(spec: FlowSpec): FlowSpec {
  return JSON.parse(JSON.stringify(spec)) as FlowSpec;
}

/** 把 spec 里所有「顺序无关」的数组和对象都反过来，用于摘要不变性。 */
function reversedSpec(spec: FlowSpec): FlowSpec {
  const clone = cloneSpec(spec);
  clone.materialize.reverse();
  clone.outputs.reverse();
  clone.nodes.reverse();
  for (const entry of clone.materialize) {
    if (entry.columns) entry.columns = Object.fromEntries(Object.entries(entry.columns).reverse());
  }
  for (const node of clone.nodes) {
    node.reads.reverse();
    node.writes.reverse();
    if (node.args) node.args = Object.fromEntries(Object.entries(node.args).reverse());
  }
  return clone;
}

describe('parseCall', () => {
  it('接受 py:函数 与 .sql 两种形状，去首尾空白并把反斜杠归一化成 /', () => {
    expect(parseCall('  pick.py:pick  ', 'call')).toEqual({ file: 'pick.py', func: 'pick' });
    expect(parseCall('src\\etl\\load.py:run', 'call')).toEqual({ file: 'src/etl/load.py', func: 'run' });
    expect(parseCall('sql/agg.sql', 'call')).toEqual({ file: 'sql/agg.sql', func: null });
  });

  it('返回的 file 保留扩展名（回归：捕获组曾经把 .py/.sql 吃掉，下游拿不到真路径）', () => {
    expect(parseCall('pipeline.py:compute', 'call')).toEqual({ file: 'pipeline.py', func: 'compute' });
    expect(parseCall('src/etl/build.py:build', 'call')).toEqual({ file: 'src/etl/build.py', func: 'build' });
    expect(parseCall('sql/agg.sql', 'call')).toEqual({ file: 'sql/agg.sql', func: null });
  });

  it('拒绝绝对路径（Windows 盘符、/ 开头、\\ 开头）', () => {
    expect(() => parseCall('/etc/pipeline.py:run', 'call')).toThrow(/绝对路径/);
    expect(() => parseCall('C:\\work\\pipeline.py:run', 'call')).toThrow(/绝对路径/);
    expect(() => parseCall('\\\\server\\share\\pipeline.py:run', 'call')).toThrow(/绝对路径/);
  });

  it('拒绝用 .. 跳出仓库', () => {
    expect(() => parseCall('../pipeline.py:run', 'call')).toThrow(/跳出仓库/);
    expect(() => parseCall('src/../pipeline.py:run', 'call')).toThrow(/跳出仓库/);
    expect(() => parseCall('src\\..\\pipeline.py:run', 'call')).toThrow(/跳出仓库/);
  });

  it('拒绝缺函数名的 .py 和不认识的扩展名', () => {
    expect(() => parseCall('pipeline.py', 'call')).toThrow(/形状不对/);
    expect(() => parseCall('pipeline.py:', 'call')).toThrow(/形状不对/);
    expect(() => parseCall('pipeline.js:run', 'call')).toThrow(/形状不对/);
    expect(() => parseCall('   ', 'call')).toThrow(/不能为空/);
  });
});

describe('parseFlow：合法输入', () => {
  it('最小 flow：db 默认 hr.db，materialize/outputs 缺省为空，字段被 trim', () => {
    const spec = parseFlow({
      flow: '  molex-prep  ',
      nodes: [{ id: 'build', call: ' pipeline.py:build ', reads: [], writes: ['result'] }],
    });
    expect(spec.flow).toBe('molex-prep');
    expect(spec.db).toBe('hr.db');
    expect(spec.materialize).toEqual([]);
    expect(spec.outputs).toEqual([]);
    expect(spec.nodes).toEqual([{ id: 'build', call: 'pipeline.py:build', reads: [], writes: ['result'] }]);
    // 和工厂造的默认 spec 完全一致
    expect(parseFlow(rawFlow())).toEqual(minimalFlow());
  });

  it('完整条目：sheet/columns/load、args（允许空串）与 timeoutMs（数字字符串也收）', () => {
    const spec = parseFlow({
      flow: 'molex-prep',
      db: 'molex.db',
      materialize: [
        {
          file: 'sub/molex.xlsx',
          sheet: 'Sheet1',
          table: 'raw_molex',
          columns: { 源列: 'src_col', 数量: 'qty' },
          load: 'loaders.py:load_molex',
        },
      ],
      outputs: ['result'],
      nodes: [
        {
          id: 'build',
          call: 'src/etl/build.py:build',
          reads: ['raw_molex'],
          writes: ['result'],
          args: { year: '2025', label: '' },
          timeoutMs: '1500',
        },
      ],
    });

    expect(spec.db).toBe('molex.db');
    expect(spec.materialize[0]).toEqual({
      file: 'sub/molex.xlsx',
      sheet: 'Sheet1',
      table: 'raw_molex',
      columns: { 源列: 'src_col', 数量: 'qty' },
      load: 'loaders.py:load_molex',
    });
    expect(spec.nodes[0]!.args).toEqual({ year: '2025', label: '' });
    expect(spec.nodes[0]!.timeoutMs).toBe(1500);
  });

  it('节点 call 只 trim、不归一化反斜杠（校验用的解析结果没有写回）', () => {
    const spec = parseFlow(rawFlow({ nodes: [rawNode({ call: ' src\\etl.py:run ' })] }));
    expect(spec.nodes[0]!.call).toBe('src\\etl.py:run');
    // parseCall 会把 \ 归一化成 /，但 parseFlow 把原字符串原样留下了（下游 parseCall 时会归一化）
    expect(parseCall(spec.nodes[0]!.call, 'call')).toEqual({ file: 'src/etl.py', func: 'run' });
  });
});

describe('parseFlow：形状与字段校验', () => {
  it('顶层必须是对象；flow 名必填；db 必须以 .db 结尾', () => {
    expect(() => parseFlow(null)).toThrow(/flow 应该是一个对象/);
    expect(() => parseFlow([])).toThrow(/flow 应该是一个对象/);
    expect(() => parseFlow({})).toThrow(/flow\.flow/);
    expect(() => parseFlow(rawFlow({ flow: '   ' }))).toThrow(/不能为空/);
    expect(() => parseFlow(rawFlow({ db: 'hr.sqlite' }))).toThrow(/以 \.db 结尾/);
    expect(() => parseFlow(rawFlow({ nodes: 'nope' }))).toThrow(/nodes 应该是数组/);
    expect(() => parseFlow(rawFlow({ nodes: [null] }))).toThrow(/应该是对象/);
    expect(() => parseFlow(rawFlow({ materialize: {} }))).toThrow(/materialize 应该是数组/);
  });

  it('不写 nodes（或写成空数组）= 节点从代码解析，entry 缺省 pipeline.py', () => {
    const absent = rawFlow();
    delete absent['nodes'];
    expect(parseFlow(absent)).toMatchObject({ nodes: [], entry: 'pipeline.py' });
    expect(parseFlow(rawFlow({ nodes: [] }))).toMatchObject({ nodes: [], entry: 'pipeline.py' });
    // 显式写了 entry 就用它
    expect(parseFlow(rawFlow({ nodes: [], entry: 'src/pipeline.py' })).entry).toBe('src/pipeline.py');
  });

  it('entry 必须是仓库内的相对 .py 路径，且不能和 nodes 同时出现', () => {
    expect(() => parseFlow(rawFlow({ nodes: [], entry: '/abs/pipeline.py' }))).toThrow(/相对路径/);
    expect(() => parseFlow(rawFlow({ nodes: [], entry: 'C:\\code\\pipeline.py' }))).toThrow(/相对路径/);
    expect(() => parseFlow(rawFlow({ nodes: [], entry: '../pipeline.py' }))).toThrow(/跳出仓库/);
    expect(() => parseFlow(rawFlow({ nodes: [], entry: 'pipeline.sql' }))).toThrow(/必须指向一个 \.py/);
    expect(() => parseFlow(rawFlow({ entry: 'pipeline.py' }))).toThrow(/二选一/);
  });

  it('节点 id 必须是标识符，且不能重复', () => {
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ id: 'bad-id' })] }))).toThrow(/不是合法标识符/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ id: '1st' })] }))).toThrow(/不是合法标识符/);
    expect(() =>
      parseFlow(rawFlow({ nodes: [rawNode({ id: 'build' }), rawNode({ id: 'build', reads: [], writes: ['other'] })] })),
    ).toThrow(/id 重复/);
  });

  it('节点 call 的三种坏形状（绝对路径、跳目录、缺函数名）都被挡住', () => {
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ call: '/abs/pipeline.py:build' })] }))).toThrow(/绝对路径/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ call: '../pipeline.py:build' })] }))).toThrow(/跳出仓库/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ call: 'pipeline.py' })] }))).toThrow(/形状不对/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ call: 'pipeline.py:' })] }))).toThrow(/形状不对/);
  });

  it('表名必须是标识符，同一列表内重复声明要报错', () => {
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ writes: ['result', 'bad-name'] })] }))).toThrow(/不是合法表名/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ reads: ['1st_table', 'result'], writes: ['result'] })] }))).toThrow(
      /不是合法表名/,
    );
    expect(() => parseFlow(rawFlow({ outputs: ['result', 'result'] }))).toThrow(/重复声明/);
    expect(() =>
      parseFlow(rawFlow({ nodes: [rawNode({ reads: ['raw_headcount', 'raw_headcount'], writes: ['result'] })] })),
    ).toThrow(/重复声明/);
    // 「既不读也不写」属于图的有效性，不是形状：节点可能来自代码解析，所以放在 planFlow 这一关。
    expect(() => planFlow(parseFlow(rawFlow({ nodes: [rawNode({ reads: [], writes: [] })] })))).toThrow(/既不读也不写/);
  });

  it('节点不能写物化产出的表（物化是图的上游边界）', () => {
    const message = errorOf(() =>
      planFlow(parseFlow(rawFlow({ nodes: [rawNode({ writes: ['raw_headcount', 'result'] })] }))),
    );
    expect(message).toMatch(/物化/);
    expect(message).toContain('raw_headcount');
  });

  it('flow.outputs 里的表必须由某个节点写', () => {
    expect(() => planFlow(parseFlow(rawFlow({ outputs: ['result', 'ghost'] })))).toThrow(/没有任何节点写它/);
  });

  it('args 的键必须是参数名、值必须是字符串；timeoutMs 必须是正数', () => {
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ args: { 'bad-key': 'x' } })] }))).toThrow(/不是合法参数名/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ args: 'x' })] }))).toThrow(/args 应该是对象/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ args: { year: 2025 } })] }))).toThrow(/应该是字符串/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ timeoutMs: 0 })] }))).toThrow(/正数/);
    expect(() => parseFlow(rawFlow({ nodes: [rawNode({ timeoutMs: -5 })] }))).toThrow(/正数/);
  });
});

describe('parseFlow：物化条目', () => {
  it('file 必须是批次目录内的相对文件名', () => {
    expect(() =>
      parseFlow(rawFlow({ materialize: [{ file: '/abs/x.xlsx', table: 'raw_headcount' }] })),
    ).toThrow(/相对文件名/);
    expect(() =>
      parseFlow(rawFlow({ materialize: [{ file: 'C:/data/x.xlsx', table: 'raw_headcount' }] })),
    ).toThrow(/相对文件名/);
    expect(() =>
      parseFlow(rawFlow({ materialize: [{ file: '../x.xlsx', table: 'raw_headcount' }] })),
    ).toThrow(/跳出批次目录/);
    expect(() =>
      parseFlow(rawFlow({ materialize: [{ file: 'sub/../../x.xlsx', table: 'raw_headcount' }] })),
    ).toThrow(/跳出批次目录/);
    expect(() => parseFlow(rawFlow({ materialize: [null] }))).toThrow(/应该是对象/);
  });

  it('table 不能重复物化，必须是合法表名', () => {
    expect(() =>
      parseFlow(
        rawFlow({
          materialize: [
            { file: 'a.xlsx', table: 'raw_headcount' },
            { file: 'b.xlsx', table: 'raw_headcount' },
          ],
        }),
      ),
    ).toThrow(/重复物化/);
    expect(() => parseFlow(rawFlow({ materialize: [{ file: 'a.xlsx', table: 'bad name' }] }))).toThrow(/不是合法表名/);
  });

  it('columns 是「源列名 → 列名」的对象，目标列名也要是标识符', () => {
    expect(() =>
      parseFlow(rawFlow({ materialize: [{ file: 'a.xlsx', table: 'raw_headcount', columns: [] }] })),
    ).toThrow(/columns 应该是/);
    expect(() =>
      parseFlow(rawFlow({ materialize: [{ file: 'a.xlsx', table: 'raw_headcount', columns: { 源列: 'bad name' } }] })),
    ).toThrow(/不是合法表名/);
  });

  it('load 必须指向一个函数（file.py:function）', () => {
    expect(() =>
      parseFlow(rawFlow({ materialize: [{ file: 'a.xlsx', table: 'raw_headcount', load: 'loaders.sql' }] })),
    ).toThrow(/必须指向一个函数/);
    expect(() =>
      parseFlow(rawFlow({ materialize: [{ file: 'a.xlsx', table: 'raw_headcount', load: '/abs/loaders.py:load' }] })),
    ).toThrow(/绝对路径/);
  });
});

describe('parseFlowJson', () => {
  it('从 JSON 文本解析，结果与 parseFlow 一致', () => {
    const text = JSON.stringify(rawFlow());
    expect(parseFlowJson(text)).toEqual(parseFlow(rawFlow()));
    expect(parseFlowJson(text).flow).toBe('molex-prep');
  });

  it('JSON 语法错误带上原因，语义错误照常抛出', () => {
    expect(() => parseFlowJson('{ not json')).toThrow(/不是合法 JSON/);
    // 空节点不再在解析期报错（要从代码里解析），但 planFlow 会指出「这条流没有节点」。
    expect(() => planFlow(parseFlowJson('{"flow":"x","nodes":[]}'))).toThrow(/没有节点/);
    expect(() => parseFlowJson(JSON.stringify(rawFlow({ nodes: [rawNode({ call: '/abs/a.py:run' })] })))).toThrow(
      /绝对路径/,
    );
  });
});

describe('planFlow：拓扑与边', () => {
  it('拓扑序确定性：入度相同的按 id 字典序，与 nodes 书写顺序无关', () => {
    const nodes = [step('zeta', [], ['t_z']), step('mid', ['t_a'], ['t_m']), step('alpha', [], ['t_a'])];
    const spec = minimalFlow({ materialize: [], outputs: [], nodes });
    const first = planFlow(spec);
    expect(first.order).toEqual(['alpha', 'mid', 'zeta']);

    const shuffled = minimalFlow({
      materialize: [],
      outputs: [],
      nodes: [nodes[2]!, nodes[0]!, nodes[1]!],
    });
    expect(planFlow(shuffled).order).toEqual(first.order);
  });

  it('边由表推导（表 → 生产者 → 消费者），producedBy/materialized 同步填好', () => {
    const spec = minimalFlow({
      materialize: [{ file: 'src.xlsx', table: 'raw' }],
      outputs: ['result'],
      nodes: [step('a', ['raw'], ['t1']), step('b', ['raw'], ['t2']), step('c', ['t1', 't2'], ['result'])],
    });
    const plan = planFlow(spec);

    expect(plan.order).toEqual(['a', 'b', 'c']);
    expect(plan.edges).toEqual([
      { table: 't1', from: 'a', to: 'c' },
      { table: 't2', from: 'b', to: 'c' },
    ]);
    expect(plan.producedBy.get('t1')).toBe('a');
    expect(plan.producedBy.get('t2')).toBe('b');
    expect(plan.producedBy.has('raw')).toBe(false);
    expect([...plan.materialized]).toEqual(['raw']);
    expect(plan.unknownReads).toEqual([]);
  });

  it('节点读自己写的表是原地更新，不算依赖', () => {
    const spec = minimalFlow({
      materialize: [{ file: 'src.xlsx', table: 'raw' }],
      outputs: ['snapshot'],
      nodes: [step('solo', ['raw', 'snapshot'], ['snapshot'])],
    });
    const plan = planFlow(spec);
    expect(plan.order).toEqual(['solo']);
    expect(plan.edges).toEqual([]);
    expect(plan.unknownReads).toEqual([]);
  });

  it('被读但既没物化也没人产出的表进 unknownReads，并排序去重', () => {
    const spec = minimalFlow({
      materialize: [{ file: 'src.xlsx', table: 'raw' }],
      outputs: ['result'],
      nodes: [
        step('build', ['raw', 'zeta_base', 'alpha_base'], ['result']),
        step('audit', ['alpha_base'], ['snapshot']),
      ],
    });
    expect(planFlow(spec).unknownReads).toEqual(['alpha_base', 'zeta_base']);
  });

  it('同一张表被两个节点声明为产出时报错，并指出两个节点', () => {
    const message = errorOf(() =>
      planFlow(
        minimalFlow({
          outputs: ['result'],
          nodes: [step('one', ['raw_headcount'], ['result']), step('two', [], ['result'])],
        }),
      ),
    );
    expect(message).toMatch(/被两个节点都声明为产出/);
    expect(message).toContain('one');
    expect(message).toContain('two');
  });

  it('成环时报错，并列出环上的节点', () => {
    const message = errorOf(() =>
      planFlow(
        minimalFlow({
          materialize: [],
          outputs: [],
          nodes: [
            step('alpha', ['t_beta'], ['t_alpha']),
            step('beta', ['t_gamma'], ['t_beta']),
            step('gamma', ['t_alpha'], ['t_gamma']),
            step('delta', [], ['t_delta']),
          ],
        }),
      ),
    );
    expect(message).toMatch(/环/);
    expect(message).toContain('alpha → beta → gamma');
    expect(message).not.toContain('delta');
  });

  it('实际行为：环下游被阻塞的节点也会被列进错误信息，不只是环上的节点', () => {
    const message = errorOf(() =>
      planFlow(
        minimalFlow({
          materialize: [],
          outputs: [],
          nodes: [step('a', ['t_back'], ['t_a']), step('b', ['t_a'], ['t_back']), step('c', ['t_back'], ['t_c'])],
        }),
      ),
    );
    expect(message).toMatch(/环/);
    expect(message).toContain('a → b → c');
  });
});

describe('digestOf', () => {
  it('是 12 位十六进制，planFlow 的 digest 与它一致', () => {
    const spec = minimalFlow();
    expect(digestOf(spec)).toMatch(/^[0-9a-f]{12}$/);
    expect(planFlow(spec).digest).toBe(digestOf(spec));
    expect(digestOf(spec)).toBe(digestOf(cloneSpec(spec)));
  });

  it('与 nodes 顺序、reads/writes 书写顺序、materialize/outputs/columns/args 的键序都无关', () => {
    const spec = minimalFlow({
      materialize: [
        { file: 'b.xlsx', table: 'raw_b', columns: { y: 'col_y', x: 'col_x' } },
        { file: 'a.xlsx', table: 'raw_a' },
      ],
      outputs: ['out_b', 'out_a'],
      nodes: [
        {
          id: 'build_b',
          call: 'pipeline.py:build_b',
          reads: ['raw_b', 'raw_a'],
          writes: ['out_b'],
          args: { k2: 'v2', k1: 'v1' },
        },
        step('build_a', ['raw_a'], ['out_a']),
      ],
    });
    const digest = digestOf(spec);

    const reversed = reversedSpec(spec);
    expect(reversed).not.toEqual(spec); // 确认真的换过顺序，别让断言空转
    expect(digestOf(reversed)).toBe(digest);
  });

  it('call/reads/writes/args/timeoutMs/materialize/outputs 任何一处改动都会改变摘要', () => {
    const base = parseFlow({
      flow: 'molex-prep',
      db: 'hr.db',
      materialize: [
        {
          file: 'molex.xlsx',
          sheet: 'S1',
          table: 'raw_molex',
          columns: { 源列: 'src_col' },
          load: 'loaders.py:load_molex',
        },
      ],
      outputs: ['result'],
      nodes: [
        {
          id: 'build',
          call: 'pipeline.py:build',
          reads: ['raw_molex'],
          writes: ['result'],
          args: { year: '2025' },
          timeoutMs: 5000,
        },
      ],
    });
    const baseDigest = digestOf(base);

    const mutations: Array<[string, (spec: FlowSpec) => void]> = [
      ['flow', (spec) => void (spec.flow = 'other-flow')],
      ['db', (spec) => void (spec.db = 'other.db')],
      ['outputs', (spec) => void (spec.outputs = ['result', 'extra'])],
      ['materialize.file', (spec) => void (spec.materialize[0]!.file = 'molex-v2.xlsx')],
      ['materialize.table', (spec) => void (spec.materialize[0]!.table = 'raw_molex_v2')],
      ['materialize.sheet', (spec) => void (spec.materialize[0]!.sheet = 'S2')],
      ['materialize.sheet 删除', (spec) => void delete spec.materialize[0]!.sheet],
      ['materialize.columns 的值', (spec) => void (spec.materialize[0]!.columns!['源列'] = 'src_col2')],
      ['materialize.columns 删除', (spec) => void delete spec.materialize[0]!.columns],
      ['materialize.load', (spec) => void (spec.materialize[0]!.load = 'loaders.py:load_other')],
      ['node.call', (spec) => void (spec.nodes[0]!.call = 'pipeline.py:build2')],
      ['node.reads', (spec) => void (spec.nodes[0]!.reads = ['raw_molex', 'extra_base'])],
      ['node.writes', (spec) => void (spec.nodes[0]!.writes = ['result', 'extra'])],
      ['node.args 改值', (spec) => void (spec.nodes[0]!.args = { year: '2026' })],
      ['node.args 加键', (spec) => void (spec.nodes[0]!.args = { year: '2025', extra: '' })],
      ['node.timeoutMs', (spec) => void (spec.nodes[0]!.timeoutMs = 6001)],
    ];

    for (const [label, mutate] of mutations) {
      const clone = cloneSpec(base);
      mutate(clone);
      expect(digestOf(clone), label).not.toBe(baseDigest);
    }
  });
});

describe('suggestOptimizations', () => {
  it('正常图不产生任何提示', () => {
    const plan = planFlow(
      parseFlow({
        flow: 'normal',
        materialize: [
          { file: 'a.xlsx', table: 'raw_a' },
          { file: 'b.xlsx', table: 'raw_b' },
        ],
        outputs: ['result', 'audit_log'],
        nodes: [
          { id: 'ingest', call: 'pipeline.py:ingest', reads: ['raw_a', 'raw_b'], writes: ['facts'] },
          { id: 'report', call: 'pipeline.py:report', reads: ['facts'], writes: ['result'] },
          { id: 'audit', call: 'audit.sql', reads: ['facts'], writes: ['audit_log'] },
        ],
      }),
    );
    expect(suggestOptimizations(plan)).toEqual([]);
    expect(suggestOptimizations(planFlow(minimalFlow()))).toEqual([]);
  });

  it('死节点：写的表没人读、也不是流程产出', () => {
    const plan = planFlow(
      parseFlow({
        flow: 'dead',
        materialize: [{ file: 'a.xlsx', table: 'raw_a' }],
        outputs: ['result'],
        nodes: [
          { id: 'main', call: 'pipeline.py:main', reads: ['raw_a'], writes: ['result'] },
          { id: 'orphan', call: 'pipeline.py:orphan', reads: ['raw_a'], writes: ['scratch'] },
        ],
      }),
    );
    const hints = suggestOptimizations(plan);
    expect(hints).toHaveLength(1);
    expect(hints[0]).toEqual({
      kind: 'dead-node',
      nodes: ['orphan'],
      tables: ['scratch'],
      note: expect.stringContaining('orphan'),
    });
  });

  it('单消费者：只喂一个下游、且不是流程产出', () => {
    const plan = planFlow(
      parseFlow({
        flow: 'chain',
        materialize: [{ file: 'a.xlsx', table: 'raw_a' }],
        outputs: ['result'],
        nodes: [
          { id: 'clean', call: 'pipeline.py:clean', reads: ['raw_a'], writes: ['stage'] },
          { id: 'build', call: 'pipeline.py:build', reads: ['stage'], writes: ['result'] },
        ],
      }),
    );
    const hints = suggestOptimizations(plan);
    expect(hints).toHaveLength(1);
    expect(hints[0]!.kind).toBe('single-consumer');
    expect(hints[0]!.nodes).toEqual(['clean', 'build']);
    expect(hints[0]!.tables).toEqual(['stage']);
    expect(hints[0]!.note).toMatch(/clean.*build/);
  });

  it('给 stage 再加一个消费者后，单消费者提示消失', () => {
    const plan = planFlow(
      parseFlow({
        flow: 'fanout',
        materialize: [{ file: 'a.xlsx', table: 'raw_a' }],
        outputs: ['result', 'peek_result'],
        nodes: [
          { id: 'clean', call: 'pipeline.py:clean', reads: ['raw_a'], writes: ['stage'] },
          { id: 'build', call: 'pipeline.py:build', reads: ['stage'], writes: ['result'] },
          { id: 'peek', call: 'pipeline.py:peek', reads: ['stage'], writes: ['peek_result'] },
        ],
      }),
    );
    expect(suggestOptimizations(plan)).toEqual([]);
  });

  it('空物化：物化了却没有任何节点读', () => {
    const plan = planFlow(
      parseFlow({
        flow: 'unused',
        materialize: [{ file: 'unused.xlsx', table: 'raw_unused' }],
        outputs: ['result'],
        nodes: [{ id: 'main', call: 'pipeline.py:main', reads: [], writes: ['result'] }],
      }),
    );
    expect(suggestOptimizations(plan)).toEqual([
      { kind: 'unused-materialize', nodes: [], tables: ['raw_unused'], note: expect.stringContaining('raw_unused') },
    ]);
  });

  it('提示顺序：空物化在前，之后按节点顺序（可同时给出三种提示）', () => {
    const plan = planFlow(
      parseFlow({
        flow: 'mixed',
        materialize: [
          { file: 'unused.xlsx', table: 'raw_unused' },
          { file: 'src.xlsx', table: 'raw_src' },
        ],
        outputs: ['result'],
        nodes: [
          { id: 'a_clean', call: 'pipeline.py:a_clean', reads: ['raw_src'], writes: ['stage'] },
          { id: 'b_build', call: 'pipeline.py:b_build', reads: ['stage'], writes: ['result'] },
          { id: 'c_orphan', call: 'pipeline.py:c_orphan', reads: ['raw_src'], writes: ['scratch'] },
        ],
      }),
    );
    expect(suggestOptimizations(plan).map((hint) => hint.kind)).toEqual([
      'unused-materialize',
      'single-consumer',
      'dead-node',
    ]);
  });

  it('只读不写的节点不报死节点', () => {
    const plan = planFlow(
      parseFlow({
        flow: 'sink',
        materialize: [{ file: 'a.xlsx', table: 'raw_a' }],
        outputs: ['result'],
        nodes: [
          { id: 'reader', call: 'pipeline.py:reader', reads: ['raw_a'], writes: [] },
          { id: 'main', call: 'pipeline.py:main', reads: ['raw_a'], writes: ['result'] },
        ],
      }),
    );
    expect(suggestOptimizations(plan)).toEqual([]);
  });
});

describe('describeFlow', () => {
  it('打印 flow 名@摘要、产出表、物化行、按拓扑序的节点行与流外读', () => {
    const spec = parseFlow({
      flow: 'molex-prep',
      db: 'hr.db',
      materialize: [
        { file: 'molex.xlsx', sheet: 'Sheet1', table: 'raw_molex', load: 'loaders.py:load_molex' },
      ],
      outputs: ['result'],
      nodes: [
        { id: 'build', call: 'pipeline.py:build', reads: ['stage'], writes: ['result'] },
        { id: 'clean', call: 'pipeline.py:clean', reads: ['raw_molex', 'base_table'], writes: ['stage'] },
      ],
    });
    const plan = planFlow(spec);
    const lines = describeFlow(plan);

    expect(Array.isArray(lines)).toBe(true);
    expect(lines[0]).toBe(`flow molex-prep@${plan.digest}  db=hr.db  节点 2  边 1  图=flow.json`);
    expect(lines[1]).toBe('  产出表：result');
    expect(lines).toContain('  [物化] molex.xlsx#Sheet1 → raw_molex（load=loaders.py:load_molex）');

    // 节点行按 plan.order 出现：clean 在前，build 在后
    const nodeLines = lines.filter((line) => line.includes('(pipeline.py:'));
    expect(nodeLines).toHaveLength(2);
    expect(nodeLines[0]).toMatch(/^\s+clean\s+raw_molex\+base_table → stage\s+\(pipeline\.py:clean\)$/);
    expect(nodeLines[1]).toMatch(/^\s+build\s+stage → result\s+\(pipeline\.py:build\)$/);

    expect(lines).toContain('  流外读：base_table（既没物化、也没有节点产出，必须是库里的基础表）');
  });

  it('没有 outputs 时显示占位，物化行不带 sheet/load，无流外读时不打印该行', () => {
    const plan = planFlow(minimalFlow({ outputs: [] }));
    const lines = describeFlow(plan);

    expect(lines[1]).toBe('  产出表：(未声明)');
    expect(lines).toContain('  [物化] headcount.xlsx → raw_headcount');
    expect(lines).toContain(`  ${'build'.padEnd(18)} raw_headcount → result   (pipeline.py:build)`);
    expect(lines.some((line) => line.includes('流外读'))).toBe(false);
  });

  it('代码解析出来的图：标出来路、带上节点标题，警告以 ! 行列出，steps 打开才印块说明', () => {
    const spec: FlowSpec = {
      flow: 'molex-prep',
      db: 'hr.db',
      materialize: [],
      outputs: ['result'],
      entry: 'pipeline.py',
      warnings: [{ code: 'UNUSED_TABLE_FUNC', message: 'dead_code 带表标注但 main 没调用它', line: 40 }],
      nodes: [
        {
          id: 'build',
          call: 'pipeline.py:build',
          reads: ['raw'],
          writes: ['result'],
          title: '把原始表算成结果表',
          steps: [{ line: 12, kind: 'block', of: 'if', label: '空表就别往下走' }],
        },
      ],
    };
    const plan = planFlow(spec);
    const quiet = describeFlow(plan);
    expect(quiet[0]).toContain('图=代码:pipeline.py');
    expect(quiet).toContain(
      `  ${'build'.padEnd(18)} raw → result   (pipeline.py:build)  · 把原始表算成结果表`,
    );
    expect(quiet).toContain('  ! pipeline.py:40 dead_code 带表标注但 main 没调用它');
    expect(quiet.some((line) => line.includes('空表就别往下走'))).toBe(false);

    const verbose = describeFlow(plan, { steps: true });
    expect(verbose).toContain('      [if] 空表就别往下走  (L12)');
  });
});
