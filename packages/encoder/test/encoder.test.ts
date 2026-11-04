import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  buildTrace,
  CLAIM_KINDS,
  corpusDigest,
  foldClaims,
  listCorpus,
  parseClaims,
  saveTrace,
  serializeClaimEvent,
  traceHash,
  type InteractionTrace,
} from "../src/index.ts";

const makeRunDir = (): string => mkdtempSync(join(tmpdir(), "ve-enc-"));

const writeEvents = (dir: string, lines: unknown[]): void => {
  writeFileSync(join(dir, "events.jsonl"), `${lines.map((line) => JSON.stringify(line)).join("\n")}\n`, "utf8");
};

const baseEvents = (): unknown[] => [
  { type: "turn_start" },
  {
    type: "tool_execution_start",
    toolCallId: "c1",
    toolName: "bash",
    args: { command: 'python -c "print(1)"', timeout: 60 },
  },
  {
    type: "tool_execution_end",
    toolCallId: "c1",
    toolName: "bash",
    isError: false,
    durationMs: 120,
    result: { content: [{ type: "text", text: "1\n" }], structuredContent: { exit_code: 0 } },
  },
  { type: "tool_execution_start", toolCallId: "c2", toolName: "write", args: { path: "C:/tmp/x.py", content: "print(2)" } },
  {
    type: "tool_execution_end",
    toolCallId: "c2",
    toolName: "write",
    isError: true,
    durationMs: 5,
    result: { content: [{ type: "text", text: "permission denied" }] },
  },
  { type: "turn_start" },
  {
    type: "message_end",
    message: {
      role: "assistant",
      usage: {
        input: 10,
        output: 5,
        cacheRead: 2,
        cacheWrite: 0,
        reasoning: 0,
        totalTokens: 17,
        cost: { input: 0.0004, output: 0.0006, cacheRead: 0, cacheWrite: 0, total: 0.001 },
      },
    },
  },
  { type: "message_end", message: { role: "user", usage: { input: 0, output: 0, totalTokens: 0 } } },
  { type: "tool_execution_start", toolCallId: "c3", toolName: "bash", args: { command: "sleep 100" } },
];

describe("claims：append-only 的语义日志", () => {
  it("按 op 折叠状态，保留完整历史", () => {
    const states = foldClaims(
      parseClaims(
        [
          '{"op":"claim","id":"c1","kind":"alias","subject":"部门","value":"HRBP→人力资源业务伙伴","source":"agent"}',
          '{"op":"confirm","id":"c1","by":"jinny","note":"对"}',
        ].join("\n"),
      ),
    );
    expect(states).toHaveLength(1);
    expect(states[0]?.status).toBe("confirmed");
    expect(states[0]?.history.map((entry) => entry.op)).toEqual(["claim", "confirm"]);
    expect(states[0]?.history[1]?.by).toBe("jinny");
  });

  it("被 reject 的语义不能直接 confirm，必须重新 claim", () => {
    const events = parseClaims(
      [
        '{"op":"claim","id":"c1","kind":"unit","subject":"金额","value":"千元","source":"agent"}',
        '{"op":"reject","id":"c1","by":"jinny","reason":"其实是元"}',
      ].join("\n"),
    );
    const rejected = foldClaims(events);
    expect(rejected[0]?.status).toBe("rejected");
    expect(() => foldClaims([...events, { op: "confirm", id: "c1", by: "jinny" }])).toThrow(/需重新 claim/);
  });

  it("id 重复与未声明 id 都报错，不做宽容解析", () => {
    expect(() =>
      foldClaims(
        parseClaims(
          [
            '{"op":"claim","id":"c1","kind":"filter","subject":"在职","value":"不含试用期","source":"agent"}',
            '{"op":"claim","id":"c1","kind":"filter","subject":"在职","value":"含试用期","source":"agent"}',
          ].join("\n"),
        ),
      ),
    ).toThrow(/id 重复/);
    expect(() => foldClaims(parseClaims('{"op":"confirm","id":"missing","by":"x"}'))).toThrow(/未声明的 id/);
  });

  it("非法 kind 报错时带行号", () => {
    expect(() => parseClaims('{"op":"claim","id":"c1","kind":"whatever","subject":"s","value":"v"}')).toThrow(/第 1 行/);
  });

  it("evidence 写成一个字符串也能接受（手写 JSON 最常见的手误）", () => {
    const states = foldClaims(
      parseClaims(
        '{"op":"claim","id":"c1","kind":"unit","subject":"金额","value":"元","source":"agent","evidence":"import_hr.py:88"}',
      ),
    );
    expect(states[0]?.evidence).toEqual(["import_hr.py:88"]);
  });

  it("kind 清单是封闭的", () => {
    expect(CLAIM_KINDS).toContain("measure-definition");
    expect(new Set(CLAIM_KINDS).size).toBe(CLAIM_KINDS.length);
  });

  it("序列化键顺序固定", () => {
    const line = serializeClaimEvent({
      op: "claim",
      id: "c1",
      kind: "alias",
      subject: "部门",
      value: "A→B",
      source: "agent",
      evidence: ["timeline#12"],
    });
    expect(line).toBe('{"op":"claim","id":"c1","kind":"alias","subject":"部门","value":"A→B","source":"agent","evidence":["timeline#12"]}');
  });
});

describe("extractRun：工具调用与用量", () => {
  it("按 toolCallId 配对，保留退出码与失败标记", async () => {
    const { extractRun } = await import("../src/events.ts");
    const dir = makeRunDir();
    writeEvents(dir, baseEvents());
    const run = extractRun(join(dir, "events.jsonl"));

    expect(run.steps).toHaveLength(3);
    expect(run.steps[0]?.tool).toBe("bash");
    expect(run.steps[0]?.ok).toBe(true);
    expect(run.steps[0]?.exitCode).toBe(0);
    expect(run.steps[0]?.argsPreview).toContain("python -c");
    expect(run.steps[1]?.ok).toBe(false);
    expect(run.steps[1]?.files).toEqual(["C:/tmp/x.py"]);
    expect(run.steps[1]?.exitCode).toBeUndefined();
    expect(run.turns).toBe(2);
    expect(run.usage).toMatchObject({ input: 10, output: 5, cacheRead: 2, totalTokens: 17, costUsd: 0.001 });
    expect(run.messages.assistant).toBe(1);
    expect(run.messages.user).toBe(1);
  });

  it("没有 end 的调用记成未完成，而不是丢掉", async () => {
    const { extractRun } = await import("../src/events.ts");
    const dir = makeRunDir();
    writeEvents(dir, baseEvents());
    const run = extractRun(join(dir, "events.jsonl"));
    const incomplete = run.steps[2];
    expect(incomplete?.tool).toBe("bash");
    expect(incomplete?.ok).toBe(false);
    expect(incomplete?.outputPreview).toContain("未完成");
  });

  it("坏行计数，不中断解析", async () => {
    const { extractRun } = await import("../src/events.ts");
    const dir = makeRunDir();
    writeFileSync(join(dir, "events.jsonl"), '{"type":"turn_start"}\nnot json\n{"type":"turn_start"}\n', "utf8");
    const run = extractRun(join(dir, "events.jsonl"));
    expect(run.badLines).toBe(1);
    expect(run.turns).toBe(2);
  });
});

describe("trace + corpus：案例的不可变性", () => {
  const buildRun = (): string => {
    const dir = makeRunDir();
    writeFileSync(
      join(dir, "author.json"),
      JSON.stringify({ provider: "opencode-go", model: "deepseek-v4.1-flash", piVersion: "1.4.0", commit: "abc123", elapsedMs: 1000 }),
      "utf8",
    );
    writeEvents(dir, baseEvents());
    return dir;
  };

  it("同一运行的哈希与 recordedAt 无关", () => {
    const dir = buildRun();
    const a = buildTrace({
      runDir: dir,
      id: "case-1",
      family: "import",
      question: "把表入库",
      outcome: { label: "partial" },
      recordedAt: "2026-01-01T00:00:00.000Z",
    });
    const b = buildTrace({
      runDir: dir,
      id: "case-1",
      family: "import",
      question: "把表入库",
      outcome: { label: "partial" },
      recordedAt: "2026-02-02T00:00:00.000Z",
    });
    expect(traceHash(a)).toBe(traceHash(b));
    expect(a.policy.commit).toBe("abc123");
    expect(a.metrics.elapsedMs).toBe(1000);
    expect(a.artifacts.map((item) => item.path)).toEqual(["author.json", "events.jsonl"]);
  });

  it("重复写同一案例幂等；换了内容就拒绝覆盖", () => {
    const dir = buildRun();
    const corpus = makeRunDir();
    const trace = buildTrace({ runDir: dir, id: "case-1", family: "import", question: "q", outcome: { label: "partial" } });

    expect(saveTrace(corpus, trace).status).toBe("added");
    expect(saveTrace(corpus, trace).status).toBe("unchanged");

    const changed: InteractionTrace = { ...trace, outcome: { label: "fail", failureCategory: "schema-misread" } };
    expect(() => saveTrace(corpus, changed)).toThrow(/不可变/);
  });

  it("索引可读，指纹只随 (id, hash) 变化", () => {
    const dir = buildRun();
    const corpus = makeRunDir();
    const trace = buildTrace({ runDir: dir, id: "case-1", family: "import", question: "q", outcome: { label: "pass" } });
    saveTrace(corpus, trace);

    const entries = listCorpus(corpus);
    expect(entries).toHaveLength(1);
    expect(entries[0]?.claims.total).toBe(0);
    const digest = corpusDigest(entries);
    expect(corpusDigest([...entries].reverse())).toBe(digest);
    expect(readFileSync(join(corpus, "cases", "case-1", "trace.json"), "utf8")).toContain('"traceVersion": "0.1"');
  });

  it("案例 id 不允许路径符号", () => {
    const dir = buildRun();
    const corpus = makeRunDir();
    const trace = buildTrace({ runDir: dir, id: "../evil", family: "import", question: "q", outcome: { label: "pass" } });
    expect(() => saveTrace(corpus, trace)).toThrow(/只允许字母数字/);
  });

  it("verify 目录没有事件流也能记：步骤为空，事实来自 verify.json", () => {
    const dir = makeRunDir();
    writeFileSync(
      join(dir, "verify.json"),
      JSON.stringify({ stage: "import", commit: "def456", batchId: "b1", identical: true }),
      "utf8",
    );
    const trace = buildTrace({ runDir: dir, id: "verify-1", family: "import", question: "双跑是否一致", outcome: { label: "partial" } });
    expect(trace.steps).toHaveLength(0);
    expect(trace.source.kind).toBe("verify");
    expect(trace.source.verify).toEqual({ stage: "import", batchId: "b1", identical: true });
    expect(trace.policy.commit).toBe("def456");
    expect(trace.artifacts.map((item) => item.path)).toEqual(["verify.json"]);
  });
});
