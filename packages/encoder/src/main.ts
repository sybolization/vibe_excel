/**
 * 命令行入口。三条命令，不做别的：
 *   record   把一次运行记成案例（可带 claims）
 *   claims   show / add / confirm / reject —— 语义沉淀与人的确认
 *   list     看案例库清单与指纹
 *
 * 设计取向：每条命令只做一件事，参数错误就退出并打印用法，不猜。
 */

import { appendFileSync, existsSync, readFileSync } from "node:fs";

import {
  buildTrace,
  CLAIM_KINDS,
  corpusDigest,
  FAILURE_CATEGORIES,
  foldClaims,
  listCorpus,
  OUTCOME_LABELS,
  parseClaims,
  saveTrace,
  serializeClaimEvent,
  type ClaimEvent,
  type ClaimKind,
  type FailureCategory,
  type InteractionTrace,
  type OutcomeLabel,
  type TraceOutcome,
} from "./index.ts";

const USAGE = `用法：
  record --run <dir> --id <id> --family <f> --question <q> [--corpus <dir>]
         [--claims <file>] [--outcome pass|partial|fail] [--score 0.8]
         [--category <${FAILURE_CATEGORIES.join("|")}>] [--note <text>]

  claims show    --file <claims.jsonl>
  claims add     --file <f> --id <id> --kind <${CLAIM_KINDS.join("|")}>
                 --subject <s> --value <v> [--source agent|human] [--evidence <e>]...
  claims confirm --file <f> --id <id> --by <who> [--note <text>]
  claims reject  --file <f> --id <id> --by <who> [--reason <text>]

  list           --corpus <dir>
  digest         --corpus <dir>`;

interface Parsed {
  positionals: string[];
  flags: Map<string, string[]>;
}

function parseArgs(argv: string[]): Parsed {
  const positionals: string[] = [];
  const flags = new Map<string, string[]>();
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i] ?? "";
    if (token.startsWith("--")) {
      const name = token.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        const list = flags.get(name) ?? [];
        list.push(next);
        flags.set(name, list);
        i += 1;
      } else {
        const list = flags.get(name) ?? [];
        list.push("");
        flags.set(name, list);
      }
    } else {
      positionals.push(token);
    }
  }
  return { positionals, flags };
}

function flag(args: Parsed, name: string): string | undefined {
  const values = args.flags.get(name);
  return values && values.length > 0 ? values[values.length - 1] : undefined;
}

function required(args: Parsed, name: string): string {
  const value = flag(args, name);
  if (value === undefined || value === "") throw new Error(`缺少参数 --${name}\n\n${USAGE}`);
  return value;
}

function appendClaimEvent(file: string, event: ClaimEvent): void {
  const existing = existsSync(file) ? readFileSync(file, "utf8") : "";
  const prefix = existing !== "" && !existing.endsWith("\n") ? "\n" : "";
  const line = `${prefix}${serializeClaimEvent(event)}\n`;
  // 先折叠一遍：写坏一行会污染整份日志，宁可拒绝写入。
  foldClaims(parseClaims(existing + line));
  appendFileSync(file, line, "utf8");
}

function claimsFile(args: Parsed): string {
  return required(args, "file");
}

function printClaims(file: string): void {
  const text = existsSync(file) ? readFileSync(file, "utf8") : "";
  const states = foldClaims(parseClaims(text));
  if (states.length === 0) {
    console.log("（还没有任何语义断言）");
    return;
  }
  for (const state of states) {
    const evidence = state.evidence.length > 0 ? `  evidence=${state.evidence.join(",")}` : "";
    console.log(`${state.id}\t${state.status}\t${state.kind}\t${state.subject}\t${state.value}${evidence}`);
  }
  const pending = states.filter((state) => state.status === "unconfirmed").length;
  console.log(`\n共 ${states.length} 条，未确认 ${pending} 条`);
}

function record(args: Parsed): void {
  const runDir = required(args, "run");
  const id = required(args, "id");
  const family = required(args, "family");
  const question = required(args, "question");
  const corpusDir = required(args, "corpus");

  const label = (flag(args, "outcome") ?? "partial") as OutcomeLabel;
  if (!OUTCOME_LABELS.includes(label)) throw new Error(`--outcome 只能是 ${OUTCOME_LABELS.join(" / ")}`);
  const category = flag(args, "category");
  if (category && !(FAILURE_CATEGORIES as readonly string[]).includes(category)) {
    throw new Error(`--category 只能是 ${FAILURE_CATEGORIES.join(" / ")}`);
  }
  const scoreText = flag(args, "score");
  const outcome: TraceOutcome = {
    label,
    ...(scoreText
      ? (() => {
          const score = Number(scoreText);
          if (!Number.isFinite(score) || score < 0 || score > 1) throw new Error("--score 必须在 0..1");
          return { score };
        })()
      : {}),
    ...(category ? { failureCategory: category as FailureCategory } : {}),
    ...(flag(args, "note") ? { notes: flag(args, "note") } : {}),
  };

  const claimsPath = flag(args, "claims");
  const claims = claimsPath && existsSync(claimsPath) ? foldClaims(parseClaims(readFileSync(claimsPath, "utf8"))) : [];

  const trace: InteractionTrace = buildTrace({ runDir, id, family, question, outcome, claims });
  const result = saveTrace(corpusDir, trace);
  const pending = trace.claimCounts.unconfirmed;

  console.log(`案例 ${trace.id}（${result.status}）`);
  console.log(`  hash      ${result.hash.slice(0, 16)}`);
  console.log(`  工具调用  ${trace.metrics.toolCalls}（失败 ${trace.metrics.toolErrors}）· 轮次 ${trace.metrics.turns}`);
  console.log(`  成本      $${trace.metrics.usage.costUsd.toFixed(4)} · tokens ${trace.metrics.usage.totalTokens}`);
  console.log(`  语义      ${trace.claimCounts.total} 条，未确认 ${pending} 条`);
  console.log(`  结果      ${trace.outcome.label}${trace.outcome.failureCategory ? `（${trace.outcome.failureCategory}）` : ""}`);
  console.log(`  落盘      ${result.path}`);
  if (pending > 0) console.log(`\n提示：${pending} 条语义还没人确认，这个案例在 RSI 里只能算"待判"。`);
}

function main(argv: string[]): void {
  const args = parseArgs(argv);
  const command = args.positionals[0];
  const sub = args.positionals[1];

  if (command === "record") return record(args);
  if (command === "list") {
    const corpusDir = required(args, "corpus");
    const entries = listCorpus(corpusDir);
    if (entries.length === 0) {
      console.log("（案例库为空）");
      return;
    }
    for (const entry of entries) {
      console.log(
        `${entry.id}\t${entry.family}\t${entry.outcome}\t语义 ${entry.claims.confirmed}/${entry.claims.total}\t${entry.hash.slice(0, 12)}\t${entry.recordedAt}`,
      );
    }
    console.log(`\n共 ${entries.length} 条；指纹 ${corpusDigest(entries).slice(0, 16)}`);
    return;
  }
  if (command === "digest") {
    const entries = listCorpus(required(args, "corpus"));
    console.log(corpusDigest(entries));
    return;
  }
  if (command === "claims") {
    const file = claimsFile(args);
    if (sub === undefined || sub === "show") {
      printClaims(file);
      return;
    }
    if (sub === "add") {
      const kind = required(args, "kind") as ClaimKind;
      if (!CLAIM_KINDS.includes(kind)) throw new Error(`--kind 只能是 ${CLAIM_KINDS.join(" / ")}`);
      const source = (flag(args, "source") ?? "agent") as "agent" | "human";
      if (source !== "agent" && source !== "human") throw new Error("--source 只能是 agent 或 human");
      const evidence = (args.flags.get("evidence") ?? []).filter((item) => item !== "");
      appendClaimEvent(file, {
        op: "claim",
        id: required(args, "id"),
        kind,
        subject: required(args, "subject"),
        value: required(args, "value"),
        source,
        ...(evidence.length > 0 ? { evidence } : {}),
        at: new Date().toISOString(),
      });
      printClaims(file);
      return;
    }
    if (sub === "confirm" || sub === "reject") {
      const id = required(args, "id");
      const by = required(args, "by");
      const note = sub === "confirm" ? flag(args, "note") : flag(args, "reason");
      appendClaimEvent(file, {
        op: sub,
        id,
        by,
        ...(note ? { note } : {}),
        at: new Date().toISOString(),
      });
      printClaims(file);
      return;
    }
    throw new Error(`未知子命令：claims ${sub}\n\n${USAGE}`);
  }
  throw new Error(USAGE);
}

try {
  main(process.argv.slice(2));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
