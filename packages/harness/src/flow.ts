/**
 * 工作流（flow）：整个后端唯一的「业务结构」，也是版本管理的最小单位。
 *
 * 三样东西：
 *   物化 = 文件 → 表的映射（只做选 sheet、选列、改名，**不允许有业务规则**）；
 *   节点 = 代码里的一个函数（`pipeline.py:compute_population`），或一份 SQL 文件；
 *   边   = 一张 SQL 表。上游写的表就是下游能读的表，边的图形不需要人另外画。
 *
 * 三条硬规则（都是这次设计里定下来的）：
 *   1. 没被物化成表的文件，不能进计算图 —— 节点拿到的永远是表，不是 Excel；
 *   2. 节点的依赖写在**函数签名**上（`db` / `inputs` / `out` / `as_of` / `report`），
 *      平台按签名过滤传参；传一个 ctx 会把依赖藏起来，所以不这么做；
 *   3. 节点按用户描述写，优先业务准确性；「能不能更短」是 optimize 命令的事，
 *      模型给建议、人批准，构建路径上不做折叠。
 *
 * 这一层是纯的：只做解析、校验、拓扑、摘要、优化提示。
 * 不碰文件系统、不碰数据库、不执行任何东西——执行在 flow-run.ts。
 */

import { createHash } from 'node:crypto';

export type NodeId = string;

/** 物化条目：一个文件（可选 sheet）落成一张表。 */
export interface MaterializeEntry {
  /** 批次目录里的文件名。 */
  file: string;
  /** 电子表格的 sheet 名；单 sheet 文件可以不写。 */
  sheet?: string;
  /** 落成的表名。 */
  table: string;
  /** 源列名 → 表列名；不写就是原样落表。没列出来的列不落。 */
  columns?: Record<string, string>;
  /** 例外：文件太脏、纯映射表达不了时，指向一个加载函数（平台记录为偏离常规）。 */
  load?: string;
}

export interface FlowNode {
  /** 节点名，也是它在日志和运行记录里的身份。 */
  id: NodeId;
  /** `相对路径.py:函数名` 或 `相对路径.sql`。 */
  call: string;
  /** 读哪些表（同时也是对上游的依赖声明）。 */
  reads: string[];
  /** 写哪些表。 */
  writes: string[];
  /** 额外 kwargs；值里可以用 `{db}` `{inputs}` `{out}` `{asOf}` 占位符。 */
  args?: Record<string, string>;
  timeoutMs?: number;
}

export interface FlowSpec {
  /** 流程名，例如 molex-prep。 */
  flow: string;
  /** 业务库文件名（会话 work/ 下）。 */
  db: string;
  /** 文件 → 表的映射。图只从表开始。 */
  materialize: MaterializeEntry[];
  /** 这个流程最终要产出的表（数据契约，也是双跑比对的默认范围）。 */
  outputs: string[];
  nodes: FlowNode[];
}

export interface FlowEdge {
  table: string;
  from: NodeId;
  to: NodeId;
}

export interface FlowPlan {
  spec: FlowSpec;
  /** 拓扑序（同一个 DAG 的确定性顺序：入度相同的按 id 排序）。 */
  order: NodeId[];
  /** 节点之间的边（按表推导，不由人写）。 */
  edges: FlowEdge[];
  /** 表 → 产出它的节点。 */
  producedBy: Map<string, NodeId>;
  /** 被物化的表。 */
  materialized: Set<string>;
  /** 被读、但既没有节点产出、也没有物化声明的表：要么是库里的基础表，要么是拼错了。 */
  unknownReads: string[];
  /** 内容摘要，`flow@<12>`；版本管理用它，不用文件 mtime。 */
  digest: string;
}

/** 优化提示：只给 optimize 命令用，不参与构建期校验。 */
export interface OptimizationHint {
  kind: 'dead-node' | 'single-consumer' | 'unused-materialize';
  nodes: string[];
  tables: string[];
  note: string;
}

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;
const PY_CALL = /^([A-Za-z0-9_][A-Za-z0-9_./\\-]*)\.py:([A-Za-z_][A-Za-z0-9_]*)$/;
const SQL_CALL = /^([A-Za-z0-9_][A-Za-z0-9_./\\-]*)\.sql$/;

/** 声明成函数而不是箭头常量：只有函数声明才能让 TS 在 `if (!x) fail()` 之后做收窄。 */
function fail(message: string): never {
  throw new Error(message);
}

const asString = (value: unknown, where: string, allowEmpty = false): string => {
  if (typeof value !== 'string') fail(`${where} 应该是字符串，实际是 ${typeof value}`);
  const text = value.trim();
  if (!allowEmpty && text === '') fail(`${where} 不能为空`);
  return text;
};

const asPositiveInt = (value: unknown, where: string): number => {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) fail(`${where} 应该是正数，实际是 ${String(value)}`);
  return n;
};

const asTableName = (value: unknown, where: string): string => {
  const name = asString(value, where);
  if (!IDENT.test(name)) fail(`${where} 不是合法表名：${name}`);
  return name;
};

/** 表名必须是标识符：带点、带空格的名字在这里就拒绝，不留给运行时才发现。 */
const asTableList = (value: unknown, where: string): string[] => {
  if (value === undefined) return [];
  if (!Array.isArray(value)) fail(`${where} 应该是表名数组`);
  const out: string[] = [];
  for (const [index, item] of value.entries()) {
    const name = asTableName(item, `${where}[${index}]`);
    if (out.includes(name)) fail(`${where} 里重复声明了表 ${name}`);
    out.push(name);
  }
  return out;
};

/** 批次文件名：相对路径、不许跳出目录。 */
const asBatchFile = (value: unknown, where: string): string => {
  const text = asString(value, where);
  if (/^([A-Za-z]:|[/\\])/.test(text)) fail(`${where} 必须是批次目录内的相对文件名：${text}`);
  if (text.split(/[/\\]/).some((segment) => segment === '..')) fail(`${where} 不能跳出批次目录：${text}`);
  return text;
};

/**
 * 解析一个 `call`：`file.py:function` 或 `file.sql`。
 *
 * 路径必须是仓库内的相对路径——绝对路径和 `..` 会让「按 commit 取出干净代码」失去意义。
 */
export function parseCall(call: string, where: string): { file: string; func: string | null } {
  const text = asString(call, where);
  if (/^([A-Za-z]:|[/\\])/.test(text)) fail(`${where} 必须是仓库内的相对路径，不能是绝对路径：${text}`);
  const segments = text.split(/[/\\]/);
  if (segments.some((segment) => segment === '..')) fail(`${where} 不能跳出仓库：${text}`);

  const py = PY_CALL.exec(text);
  // 正则把扩展名留在了捕获组外面：路径要带上 `.py` 才是真的文件名。
  if (py) return { file: `${py[1]!.replace(/\\/g, '/')}.py`, func: py[2]! };
  const sql = SQL_CALL.exec(text);
  if (sql) return { file: `${sql[1]!.replace(/\\/g, '/')}.sql`, func: null };
  fail(`${where} 形状不对：要么 \`file.py:function\`，要么 \`file.sql\`。实际是 ${text}`);
}

function parseMaterialize(raw: unknown): MaterializeEntry[] {
  if (raw === undefined) return [];
  if (!Array.isArray(raw)) fail('flow.materialize 应该是数组');
  const seen = new Set<string>();
  return raw.map((item, index) => {
    const where = `flow.materialize[${index}]`;
    if (item === null || typeof item !== 'object' || Array.isArray(item)) fail(`${where} 应该是对象`);
    const entry = item as Record<string, unknown>;
    const file = asBatchFile(entry['file'], `${where}.file`);
    const table = asTableName(entry['table'], `${where}.table`);
    if (seen.has(table)) fail(`${where} 重复物化同一张表：${table}`);
    seen.add(table);

    const out: MaterializeEntry = { file, table };
    if (entry['sheet'] !== undefined) out.sheet = asString(entry['sheet'], `${where}.sheet`);
    if (entry['columns'] !== undefined) {
      const columns = entry['columns'];
      if (columns === null || typeof columns !== 'object' || Array.isArray(columns)) {
        fail(`${where}.columns 应该是「源列名 → 列名」的对象`);
      }
      const mapping: Record<string, string> = {};
      for (const [source, target] of Object.entries(columns as Record<string, unknown>)) {
        const key = asString(source, `${where}.columns 的键`);
        const value = asTableName(target, `${where}.columns.${source}`);
        mapping[key] = value;
      }
      out.columns = mapping;
    }
    if (entry['load'] !== undefined) {
      const load = asString(entry['load'], `${where}.load`);
      const parsed = parseCall(load, `${where}.load`);
      if (parsed.func === null) fail(`${where}.load 必须指向一个函数（file.py:function）`);
      out.load = load;
    }
    return out;
  });
}

/** 把外部 JSON 校验成 FlowSpec；任何一处不对都直接报错，不做默认值兜底。 */
export function parseFlow(raw: unknown): FlowSpec {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) fail('flow 应该是一个对象');
  const input = raw as Record<string, unknown>;

  const flow = asString(input['flow'], 'flow.flow');
  const db = input['db'] === undefined ? 'hr.db' : asString(input['db'], 'flow.db');
  if (!db.endsWith('.db')) fail(`flow.db 应该以 .db 结尾：${db}`);
  const materialize = parseMaterialize(input['materialize']);
  const outputs = asTableList(input['outputs'], 'flow.outputs');

  if (!Array.isArray(input['nodes']) || input['nodes'].length === 0) fail('flow.nodes 不能为空');
  const seen = new Set<string>();
  const nodes: FlowNode[] = input['nodes'].map((item, index) => {
    const where = `flow.nodes[${index}]`;
    if (item === null || typeof item !== 'object' || Array.isArray(item)) fail(`${where} 应该是对象`);
    const node = item as Record<string, unknown>;
    const id = asString(node['id'], `${where}.id`);
    if (!IDENT.test(id)) fail(`${where}.id 不是合法标识符：${id}`);
    if (seen.has(id)) fail(`${where}.id 重复：${id}`);
    seen.add(id);

    const call = asString(node['call'], `${where}.call`);
    parseCall(call, `${where}.call`);

    const reads = asTableList(node['reads'], `${where}.reads`);
    const writes = asTableList(node['writes'], `${where}.writes`);
    if (reads.length === 0 && writes.length === 0) {
      fail(`${where}（${id}）既不读也不写任何表：这个节点不产生任何可核对的东西`);
    }
    // 物化的表由物化步骤产出，节点不能再声称自己写它。
    for (const table of writes) {
      if (materialize.some((entry) => entry.table === table)) {
        fail(`${where}.writes 里的 ${table} 已经由物化产出：节点不能再写它（物化是图的上游边界）`);
      }
    }

    const out: FlowNode = { id, call, reads, writes };
    if (node['args'] !== undefined) {
      const args = node['args'];
      if (args === null || typeof args !== 'object' || Array.isArray(args)) fail(`${where}.args 应该是对象`);
      const values: Record<string, string> = {};
      for (const [key, value] of Object.entries(args as Record<string, unknown>)) {
        if (!IDENT.test(key)) fail(`${where}.args 的键不是合法参数名：${key}`);
        values[key] = asString(value, `${where}.args.${key}`, true);
      }
      out.args = values;
    }
    if (node['timeoutMs'] !== undefined) out.timeoutMs = asPositiveInt(node['timeoutMs'], `${where}.timeoutMs`);
    return out;
  });

  for (const table of outputs) {
    if (!nodes.some((node) => node.writes.includes(table))) {
      fail(`flow.outputs 里的表 ${table} 没有任何节点写它：要么补节点，要么删掉这条声明`);
    }
  }

  return { flow, db, materialize, outputs, nodes };
}

/** 从 JSON 文本载入；错误里带上原因，免得模型只看到一句 `Unexpected token`。 */
export function parseFlowJson(text: string): FlowSpec {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (error) {
    fail(`flow.json 不是合法 JSON：${error instanceof Error ? error.message : String(error)}`);
  }
  return parseFlow(raw);
}

/** 把表依赖推到节点依赖，得到确定性拓扑序；有环就报出剩下的节点。 */
export function planFlow(spec: FlowSpec): FlowPlan {
  const producedBy = new Map<string, NodeId>();
  for (const node of spec.nodes) {
    for (const table of node.writes) {
      const owner = producedBy.get(table);
      if (owner !== undefined && owner !== node.id) {
        fail(`表 ${table} 被两个节点都声明为产出：${owner} 和 ${node.id}（一张表只能有一个生产者）`);
      }
      producedBy.set(table, node.id);
    }
  }

  const materialized = new Set(spec.materialize.map((entry) => entry.table));
  const edges: FlowEdge[] = [];
  const unknown = new Set<string>();
  for (const node of spec.nodes) {
    for (const table of node.reads) {
      const from = producedBy.get(table);
      if (from === undefined) {
        if (!materialized.has(table)) unknown.add(table);
        continue;
      }
      if (from === node.id) continue; // 自己读自己写的表：原地更新，不算依赖
      edges.push({ table, from, to: node.id });
    }
  }

  const ids = spec.nodes.map((node) => node.id);
  const indegree = new Map<NodeId, number>(ids.map((id) => [id, 0]));
  for (const edge of edges) {
    if (edge.from === edge.to) continue;
    indegree.set(edge.to, (indegree.get(edge.to) ?? 0) + 1);
  }

  const ready = ids.filter((id) => (indegree.get(id) ?? 0) === 0).sort();
  const order: NodeId[] = [];
  while (ready.length > 0) {
    const id = ready.shift()!;
    order.push(id);
    for (const edge of edges) {
      if (edge.from !== id || edge.to === id) continue;
      const left = (indegree.get(edge.to) ?? 0) - 1;
      indegree.set(edge.to, left);
      if (left === 0) {
        ready.push(edge.to);
        ready.sort();
      }
    }
  }
  if (order.length !== ids.length) {
    const onCycle = ids.filter((id) => !order.includes(id)).sort();
    fail(`工作流里有环，涉及节点：${onCycle.join(' → ')}（表依赖必须是有向无环图）`);
  }

  return { spec, order, edges, producedBy, materialized, unknownReads: [...unknown].sort(), digest: digestOf(spec) };
}

/** 规范化后取内容摘要：节点按 id 排序、列名按键排序，和文件里的写法顺序无关。 */
export function digestOf(spec: FlowSpec): string {
  const canonical = {
    flow: spec.flow,
    db: spec.db,
    outputs: [...spec.outputs].sort(),
    materialize: [...spec.materialize]
      .sort((a, b) => a.table.localeCompare(b.table))
      .map((entry) => ({
        file: entry.file,
        table: entry.table,
        ...(entry.sheet ? { sheet: entry.sheet } : {}),
        ...(entry.columns ? { columns: Object.fromEntries(Object.entries(entry.columns).sort(([a], [b]) => a.localeCompare(b))) } : {}),
        ...(entry.load ? { load: entry.load } : {}),
      })),
    nodes: [...spec.nodes]
      .sort((a, b) => a.id.localeCompare(b.id))
      .map((node) => ({
        id: node.id,
        call: node.call,
        reads: [...node.reads].sort(),
        writes: [...node.writes].sort(),
        ...(node.args ? { args: Object.fromEntries(Object.entries(node.args).sort(([a], [b]) => a.localeCompare(b))) } : {}),
        ...(node.timeoutMs ? { timeoutMs: node.timeoutMs } : {}),
      })),
  };
  return createHash('sha256').update(JSON.stringify(canonical)).digest('hex').slice(0, 12);
}

/**
 * 优化提示：给 `optimize` 命令当事实依据，**不**在构建路径上生效。
 *
 * 三条都是形式冗余，不是业务判断：
 *  - 死节点：写了表没人读、也不是流程产出；
 *  - 单消费者：只喂一个下游、且不是流程产出，可以并进去；
 *  - 空物化：物化了表却没人读。
 * 真正的「这步其实等价于一步」是语义问题，只能由模型读懂描述后提建议、人批准。
 */
export function suggestOptimizations(plan: FlowPlan): OptimizationHint[] {
  const hints: OptimizationHint[] = [];
  const consumersOf = (table: string): NodeId[] =>
    plan.spec.nodes.filter((node) => node.reads.includes(table)).map((node) => node.id);
  const outputs = new Set(plan.spec.outputs);

  for (const entry of plan.spec.materialize) {
    if (outputs.has(entry.table)) continue;
    if (consumersOf(entry.table).length === 0) {
      hints.push({
        kind: 'unused-materialize',
        nodes: [],
        tables: [entry.table],
        note: `物化了 ${entry.table}，但没有任何节点读它`,
      });
    }
  }

  for (const node of plan.spec.nodes) {
    if (node.writes.length === 0) continue;
    const consumed = node.writes.some((table) => outputs.has(table) || consumersOf(table).length > 0);
    if (!consumed) {
      hints.push({ kind: 'dead-node', nodes: [node.id], tables: [...node.writes].sort(), note: `${node.id} 写的表没人读，也不是流程产出` });
      continue;
    }
    if (node.writes.some((table) => outputs.has(table))) continue;
    const consumers = new Set(node.writes.flatMap(consumersOf));
    if (consumers.size === 1) {
      const [into] = [...consumers];
      hints.push({
        kind: 'single-consumer',
        nodes: [node.id, into!],
        tables: [...node.writes].sort(),
        note: `${node.id} 只喂 ${into}，可以考虑并成一个节点`,
      });
    }
  }
  return hints;
}

/** CLI 里打印用：一眼看清物化、节点、边和流程外依赖。 */
export function describeFlow(plan: FlowPlan): string[] {
  const lines: string[] = [];
  lines.push(`flow ${plan.spec.flow}@${plan.digest}  db=${plan.spec.db}  节点 ${plan.spec.nodes.length}  边 ${plan.edges.length}`);
  lines.push(`  产出表：${plan.spec.outputs.join(', ') || '(未声明)'}`);
  for (const entry of plan.spec.materialize) {
    const sheet = entry.sheet ? `#${entry.sheet}` : '';
    lines.push(`  [物化] ${entry.file}${sheet} → ${entry.table}${entry.load ? `（load=${entry.load}）` : ''}`);
  }
  for (const id of plan.order) {
    const node = plan.spec.nodes.find((candidate) => candidate.id === id)!;
    const reads = node.reads.length > 0 ? node.reads.join('+') : '-';
    const writes = node.writes.length > 0 ? node.writes.join('+') : '-';
    lines.push(`  ${id.padEnd(18)} ${reads} → ${writes}   (${node.call})`);
  }
  if (plan.unknownReads.length > 0) {
    lines.push(`  流外读：${plan.unknownReads.join(', ')}（既没物化、也没有节点产出，必须是库里的基础表）`);
  }
  return lines;
}
