/**
 * `optimize`：让模型自检「这条流能不能更短」，给建议，由人批准后才动。
 *
 * 为什么不做成自动折叠：折叠会改变口径（合并两张中间表，等于删掉一个可以单独检查的中间结果）。
 * 这是人的决定，不是模型的。所以这里只有两件事：
 *   1. 评审（review）：只读地看代码和 flow，把建议写成一份 JSON，不许改任何文件；
 *   2. 应用（apply）：拿着**被批准的那一条**去改代码和 flow，改完提交到草稿。
 *
 * 这份文件是纯的：只做校验、拼提示词、挑建议，不碰文件系统和子进程。
 */

import type { OptimizationHint } from './flow.ts';

export type ProposalKind = 'fold-nodes' | 'drop-node' | 'drop-materialize' | 'rewrite-node' | 'other';

const KINDS: readonly ProposalKind[] = ['fold-nodes', 'drop-node', 'drop-materialize', 'rewrite-node', 'other'];

export interface FlowProposal {
  /** 短 id（p1、p2…），批准时用它。 */
  id: string;
  title: string;
  kind: ProposalKind;
  /** 涉及的节点 / 表名（会被校验是否真的存在于当前流程）。 */
  nodes: string[];
  /** 省了什么：节点数、表数。 */
  saves: { nodes: number; tables: number };
  /** 什么情况下这条建议会伤到业务（必须写，写不出来就不该提）。 */
  risk: string;
  /** 怎么验证改完等价（必须写，没有验证办法的建议不接受）。 */
  verify: string;
}

export interface ProposalFile {
  /** 建议基于哪一版流程；流程变过之后旧建议自动失效。 */
  flowDigest: string;
  proposals: FlowProposal[];
}

function fail(message: string): never {
  throw new Error(message);
}

const asText = (value: unknown, where: string): string => {
  if (typeof value !== 'string') fail(`${where} 应该是字符串`);
  const text = value.trim();
  if (text === '') fail(`${where} 不能为空`);
  return text;
};

const asCount = (value: unknown, where: string): number => {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 0) fail(`${where} 应该是非负整数，实际是 ${String(value)}`);
  return n;
};

/** 解析模型写出来的建议文件；形状不对就整份拒绝，不做部分接受。 */
export function parseProposalFile(text: string): ProposalFile {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (error) {
    fail(`建议文件不是合法 JSON：${error instanceof Error ? error.message : String(error)}`);
  }
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) fail('建议文件应该是一个对象');
  const input = raw as Record<string, unknown>;

  const flowDigest = asText(input['flowDigest'], 'flowDigest');
  if (!Array.isArray(input['proposals'])) fail('proposals 应该是数组');
  if (input['proposals'].length === 0) fail('proposals 是空的：模型没有提出任何建议（这本身也是结论，但别写成空文件）');

  const seen = new Set<string>();
  const proposals: FlowProposal[] = input['proposals'].map((item, index) => {
    const where = `proposals[${index}]`;
    if (item === null || typeof item !== 'object' || Array.isArray(item)) fail(`${where} 应该是对象`);
    const proposal = item as Record<string, unknown>;
    const id = asText(proposal['id'], `${where}.id`);
    if (seen.has(id)) fail(`${where}.id 重复：${id}`);
    seen.add(id);
    const kind = asText(proposal['kind'], `${where}.kind`);
    if (!KINDS.includes(kind as ProposalKind)) fail(`${where}.kind 不认识：${kind}（可选：${KINDS.join(' / ')}）`);
    const nodes = proposal['nodes'];
    if (!Array.isArray(nodes) || nodes.some((node) => typeof node !== 'string')) {
      fail(`${where}.nodes 应该是字符串数组`);
    }
    const saves = (proposal['saves'] ?? {}) as Record<string, unknown>;
    return {
      id,
      title: asText(proposal['title'], `${where}.title`),
      kind: kind as ProposalKind,
      nodes: (nodes as string[]).map((node) => node.trim()).filter((node) => node !== ''),
      saves: { nodes: asCount(saves['nodes'] ?? 0, `${where}.saves.nodes`), tables: asCount(saves['tables'] ?? 0, `${where}.saves.tables`) },
      risk: asText(proposal['risk'], `${where}.risk`),
      verify: asText(proposal['verify'], `${where}.verify`),
    };
  });

  return { flowDigest, proposals };
}

/** 按 id 取一条建议；取不到时把可用的 id 列出来。 */
export function selectProposal(file: ProposalFile, id: string): FlowProposal {
  const found = file.proposals.find((proposal) => proposal.id === id);
  if (!found) {
    fail(`没有这条建议：${id}（这份建议里有：${file.proposals.map((proposal) => proposal.id).join(', ')}）`);
  }
  return found;
}

/** 建议必须基于**当前这版**流程：流程改过了，旧建议就可能已经指错节点。 */
export function assertSameFlow(file: ProposalFile, currentDigest: string): void {
  if (file.flowDigest !== currentDigest) {
    fail(
      `这份建议是基于流程 ${file.flowDigest} 写的，现在的草稿是 ${currentDigest}：流程变过了，重新跑 optimize`,
    );
  }
}

/** 评审提示词：只读、只写建议文件。 */
export function buildReviewPrompt(input: {
  flowJson: string;
  flowDigest: string;
  planLines: string[];
  hints: OptimizationHint[];
  proposalPath: string;
  codeRoot: string;
  /** 探测出来的工作区事实（数据库/解释器在哪）；不传就不渲染这一节。 */
  environment?: string;
}): string {
  const hints =
    input.hints.length === 0
      ? '（机器没有发现形式冗余——死节点 / 单消费者 / 空物化都没有。所以你只能从代码的语义里找。）'
      : input.hints.map((hint) => `- [${hint.kind}] ${hint.note}`).join('\n');

  const environment = input.environment ? `\n${input.environment}\n` : '';

  return `# 任务：评审这条工作流能不能更短（**只读**，不要改任何文件）

你在一个**只读的干净工作目录**里：\`${input.codeRoot}\`。这个目录是当前草稿提交的一份拷贝，
你在这里只能读。不要执行 git 命令，不要修改任何文件（改了也不会被采用）。
${environment}
## 当前工作流（flow@${input.flowDigest}）

\`\`\`json
${input.flowJson}
\`\`\`

拓扑与依赖：

\`\`\`
${input.planLines.join('\n')}
\`\`\`

机器能找到的形式冗余：

${hints}

## 你要做的事

读代码，判断有没有**更短且等价**的做法。允许的思路：

- 两个节点能不能合成一个（中间表只被一个下游读、而且没有别的用途）；
- 某个节点是不是空转（声明的写表跟上游一样）；
- 某个中间表能不能不落盘，直接在下一个节点里算；
- 物化了却没人用的表。

**不要**为了好看而合并：合并会删掉一个可以单独检查的中间结果。每条建议都必须能回答
「省了什么」和「什么情况下这条建议会伤到业务」。

## 输出

把建议写成一份 JSON，**写到这个绝对路径**：\`${input.proposalPath}\`

\`\`\`json
{
  "flowDigest": "${input.flowDigest}",
  "proposals": [
    {
      "id": "p1",
      "title": "一句话说清改什么",
      "kind": "fold-nodes | drop-node | drop-materialize | rewrite-node | other",
      "nodes": ["涉及", "的节点"],
      "saves": { "nodes": 1, "tables": 1 },
      "risk": "什么情况下这条建议会改变业务结果或删掉有用的检查点",
      "verify": "改完之后怎么证明等价（具体的表、行数或哈希对比）"
    }
  ]
}
\`\`\`

规则：

- 一条一个 id（p1、p2…），一次最多 5 条；
- \`risk\` 写不出来就不要提这条；
- \`verify\` 必须是能实际执行的检查，不许写「人工确认」；
- 如果**没有**更短且等价的写法，就写 \`"proposals": []\` 并在最终回复里说明为什么——
  这是好答案，不是失败。

除了这份 JSON 文件，什么都不要写。`;
}

/** 应用提示词：只实现被批准的那一条，改完自查。 */
export function buildApplyPrompt(input: {
  proposal: FlowProposal;
  approvedAt: string;
  codeRoot: string;
  flowJson: string;
  planLines: string[];
  /** 探测出来的工作区事实；不传就不渲染这一节。 */
  environment?: string;
}): string {
  const environment = input.environment ? `\n${input.environment}\n` : '';

  return `# 任务：实现一条**已被人批准**的优化建议

用户逐条看过建议，批准了这一条。你的工作是把它实现出来，并证明行为等价。
除了这一条，不许做别的优化。
${environment}

\`\`\`json
${JSON.stringify({ approvedAt: input.approvedAt, proposal: input.proposal }, null, 2)}
\`\`\`

当前工作流（flow@摘要见上），你在可写的工作目录 \`${input.codeRoot}\` 里：

\`\`\`json
${input.flowJson}
\`\`\`

\`\`\`
${input.planLines.join('\n')}
\`\`\`

## 要求

1. 只改这条建议涉及的节点/表；其他行为保持不变。
2. 改完之后**必须**用下面这个检查证明等价（这是批准时写下的验证办法）：
   > ${input.proposal.verify}
   把检查结果（行数、哈希、或你实际跑出来的对比）写进最终回复。
3. 如果实现过程中发现等价性不成立——**停下来**，不要硬改：把原因写清楚，保持工作区不变。
4. 不要执行 git 命令；提交由编排器负责。
5. 不要顺手修别的东西。`;
}
