/**
 * optimize 的纯逻辑：建议文件的形状、按 id 取、版本对齐、两段提示词。
 *
 * 这里不跑模型——模型写出来的东西是外部输入，**形状必须在这里挡住**：
 * 一份缺 `risk`/`verify` 的建议，等于让人在不知道代价的情况下批准一个改动。
 */

import { describe, expect, it } from 'vitest';

import {
  assertSameFlow,
  buildApplyPrompt,
  buildReviewPrompt,
  parseProposalFile,
  selectProposal,
  type ProposalFile,
} from '../src/optimize.ts';

const valid = {
  flowDigest: 'abc123def456',
  proposals: [
    {
      id: 'p1',
      title: '把 seed 并进 build_report',
      kind: 'fold-nodes',
      nodes: ['seed', 'build_report'],
      saves: { nodes: 1, tables: 1 },
      risk: '如果以后要单独看 people 表，就不能合',
      verify: '合并后 report 的行数与内容哈希不变',
    },
  ],
};

const text = (value: unknown): string => JSON.stringify(value);
const file = (overrides: Record<string, unknown> = {}): ProposalFile =>
  parseProposalFile(text({ ...valid, ...overrides }));

describe('建议文件的形状', () => {
  it('正常文件：字段被 trim，saves 缺省为 0', () => {
    const parsed = parseProposalFile(
      text({
        flowDigest: ' abc123 ',
        proposals: [
          {
            id: ' p1 ',
            title: ' 合并 ',
            kind: 'fold-nodes',
            nodes: [' a ', ''],
            risk: ' 有风险 ',
            verify: ' 对哈希 ',
          },
        ],
      }),
    );
    expect(parsed.flowDigest).toBe('abc123');
    expect(parsed.proposals[0]).toEqual({
      id: 'p1',
      title: '合并',
      kind: 'fold-nodes',
      nodes: ['a'],
      saves: { nodes: 0, tables: 0 },
      risk: '有风险',
      verify: '对哈希',
    });
  });

  it('不是合法 JSON / 不是对象 / proposals 为空都整份拒绝', () => {
    expect(() => parseProposalFile('{oops')).toThrow(/不是合法 JSON/);
    expect(() => parseProposalFile('[]')).toThrow(/应该是一个对象/);
    expect(() => parseProposalFile(text({ flowDigest: 'a', proposals: [] }))).toThrow(/空的/);
  });

  it('缺 risk 或 verify 的建议不接受（不能让人闭眼批准）', () => {
    const missingRisk = { ...valid.proposals[0]!, risk: '   ' };
    const missingVerify = { ...valid.proposals[0]!, verify: '' };
    expect(() => parseProposalFile(text({ flowDigest: 'a', proposals: [missingRisk] }))).toThrow(/risk 不能为空/);
    expect(() => parseProposalFile(text({ flowDigest: 'a', proposals: [missingVerify] }))).toThrow(/verify 不能为空/);
  });

  it('id 重复、kind 不认识、saves 是负数都被挡住', () => {
    expect(() => parseProposalFile(text({ flowDigest: 'a', proposals: [valid.proposals[0], valid.proposals[0]] }))).toThrow(
      /重复/,
    );
    expect(() =>
      parseProposalFile(text({ flowDigest: 'a', proposals: [{ ...valid.proposals[0]!, kind: 'magic' }] })),
    ).toThrow(/kind 不认识/);
    expect(() =>
      parseProposalFile(text({ flowDigest: 'a', proposals: [{ ...valid.proposals[0]!, saves: { nodes: -1, tables: 0 } }] })),
    ).toThrow(/非负整数/);
  });
});

describe('按 id 取建议', () => {
  it('取得到就返回那一条', () => {
    expect(selectProposal(file(), 'p1').title).toBe('把 seed 并进 build_report');
  });

  it('取不到时把可用的 id 列出来（不让人猜）', () => {
    expect(() => selectProposal(file(), 'p9')).toThrow(/没有这条建议：p9.*p1/);
  });
});

describe('建议必须基于当前这版流程', () => {
  it('摘要一致才放行', () => {
    expect(() => assertSameFlow(file(), 'abc123def456')).not.toThrow();
  });

  it('流程改过之后旧建议失效，错误里同时给出两个摘要', () => {
    expect(() => assertSameFlow(file(), 'ffffffffffff')).toThrow(/abc123def456.*ffffffffffff/);
  });
});

describe('两段提示词', () => {
  const review = buildReviewPrompt({
    flowJson: '{"flow":"demo"}',
    flowDigest: 'abc123def456',
    planLines: ['flow demo@abc123  节点 2'],
    hints: [{ kind: 'single-consumer', nodes: ['seed', 'build_report'], tables: ['people'], note: 'seed 只喂 build_report' }],
    proposalPath: 'C:/runs/optimize-review-1/optimize-proposal.json',
    codeRoot: 'C:/runs/optimize-review-1/clean',
  });

  it('评审提示词：只读、给了建议文件绝对路径、带上了机器提示', () => {
    expect(review).toMatch(/只读/);
    expect(review).toContain('C:/runs/optimize-review-1/optimize-proposal.json');
    expect(review).toMatch(/seed 只喂 build_report/);
    expect(review).toContain('"flowDigest": "abc123def456"');
    expect(review).toMatch(/不要执行 git/);
  });

  it('评审提示词允许「没有建议」这个结论', () => {
    expect(review).toMatch(/没有\*\*更短且等价/);
    expect(review).toMatch(/"proposals": \[\]/);
  });

  it('应用提示词：只说这一条、要求按 verify 证明等价、发现不等价要停', () => {
    const apply = buildApplyPrompt({
      proposal: file().proposals[0]!,
      approvedAt: '2026-01-01T00:00:00.000Z',
      codeRoot: 'C:/code',
      flowJson: '{"flow":"demo"}',
      planLines: ['flow demo@abc123  节点 2'],
    });
    expect(apply).toMatch(/已被人批准/);
    expect(apply).toContain('合并后 report 的行数与内容哈希不变');
    expect(apply).toMatch(/除了这一条，不许做别的优化/);
    expect(apply).toMatch(/停下来/);
    expect(apply).toContain('C:/code');
  });
});
