/**
 * 业务语义的沉淀：一份 append-only 的 claims 事件日志。
 *
 * 为什么不把它塞进 IR、也不做成"标注界面"：
 *  agent 的工作介质是 Python 与文件，所以语义沉淀也必须是**文件**——它能被 bash 追加、
 *  被 grep 查、被 git 记录，不需要任何新工具。人确认语义的动作也只是再追加一行。
 *
 * 为什么是事件日志而不是可变状态：
 *  1. 确认/推翻是可追溯的事实（谁在什么时候改成什么），不是覆写；
 *  2. 两个 agent、两次会话可以各自追加，不会互相覆盖；
 *  3. 之后做 RSI 时，"哪条语义被推翻过"本身就是最有价值的信号。
 *
 * 每种 kind 的含义（写进 README，别在这里扩）：
 *  alias              别名映射：源系统里的写法 → 业务口径里的实体
 *  field-meaning      字段含义：这一列到底是什么、单位是什么
 *  measure-definition 指标口径：一个数的定义（含不含谁、以哪一列为准）
 *  join               连接关系：两张表靠什么关联、是否唯一
 *  filter             过滤规则：哪些行不算数（离职、重复投递、测试数据）
 *  unit               单位与量纲：千元/元、人/人次、百分比 0..1 还是 0..100
 *  exception          例外：明确知道不合口径但必须保留的记录
 */

export const CLAIM_KINDS = [
  "alias",
  "field-meaning",
  "measure-definition",
  "join",
  "filter",
  "unit",
  "exception",
] as const;
export type ClaimKind = (typeof CLAIM_KINDS)[number];

export const CLAIM_STATUSES = ["unconfirmed", "confirmed", "rejected"] as const;
export type ClaimStatus = (typeof CLAIM_STATUSES)[number];

/** 一次断言。source 说明是谁先说的：模型推断，还是人直接给的。 */
export interface ClaimOp {
  op: "claim";
  id: string;
  kind: ClaimKind;
  subject: string;
  value: string;
  source: "agent" | "human";
  /** 证据指针：时间线行号、列名、文件名都行，只要能被复核。 */
  evidence?: string[];
  at?: string;
}

/** 人确认。确认后 status 变 confirmed；再确认一次是幂等的。 */
export interface ConfirmOp {
  op: "confirm";
  id: string;
  by: string;
  note?: string;
  at?: string;
}

/** 人推翻。推翻后 status 变 rejected，且保留原断言（不要删）。note 在这里就是理由。 */
export interface RejectOp {
  op: "reject";
  id: string;
  by: string;
  note?: string;
  at?: string;
}

export type ClaimEvent = ClaimOp | ConfirmOp | RejectOp;

export interface ClaimHistoryEntry {
  op: ClaimEvent["op"];
  /** confirm/reject 时必填。 */
  by?: string;
  at?: string;
  note?: string;
}

export interface ClaimState {
  id: string;
  kind: ClaimKind;
  subject: string;
  value: string;
  source: "agent" | "human";
  status: ClaimStatus;
  evidence: string[];
  history: ClaimHistoryEntry[];
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

const lineError = (line: number, message: string): never => {
  throw new Error(`claims 第 ${line} 行 ${message}`);
};

const requireString = (value: unknown, line: number, field: string): string => {
  if (typeof value !== "string" || value.trim() === "") return lineError(line, `的 ${field} 必须是非空字符串`);
  return value;
};

const optionalString = (value: unknown, line: number, field: string): string | undefined => {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string") return lineError(line, `的 ${field} 必须是字符串`);
  return value;
};

/**
 * evidence 既是"一条"也是"多条"：agent 在 bash 里手写 JSON 时最容易漏方括号，
 * 所以单个字符串按一条证据接受并归一成数组。数组里仍要求每一项都是字符串。
 */
const optionalEvidence = (value: unknown, line: number, field: string): string[] | undefined => {
  if (value === undefined || value === null) return undefined;
  if (typeof value === "string") return [value];
  if (!Array.isArray(value)) return lineError(line, `的 ${field} 必须是字符串或字符串数组`);
  return value.map((item, i) => {
    if (typeof item !== "string") return lineError(line, `的 ${field}[${i}] 必须是字符串`);
    return item;
  });
};

/** 解析一行 claim 事件；不合规就带着行号报错，不做宽容解析。 */
export function parseClaimLine(text: string, line: number): ClaimEvent {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return lineError(line, "不是合法 JSON");
  }
  if (!isObject(raw)) return lineError(line, "必须是对象");
  const op = raw.op;
  if (op === "claim") {
    const kind = requireString(raw.kind, line, "kind") as ClaimKind;
    if (!CLAIM_KINDS.includes(kind)) return lineError(line, `的 kind 非法：${String(raw.kind)}`);
    const source = requireString(raw.source ?? "agent", line, "source");
    if (source !== "agent" && source !== "human") return lineError(line, `的 source 只能是 agent 或 human`);
    const evidence = optionalEvidence(raw.evidence, line, "evidence");
    const at = optionalString(raw.at, line, "at");
    return {
      op,
      id: requireString(raw.id, line, "id"),
      kind,
      subject: requireString(raw.subject, line, "subject"),
      value: requireString(raw.value, line, "value"),
      source,
      ...(evidence ? { evidence } : {}),
      ...(at ? { at } : {}),
    };
  }
  if (op === "confirm" || op === "reject") {
    const note = optionalString(raw.note ?? raw.reason, line, op === "confirm" ? "note" : "reason");
    const at = optionalString(raw.at, line, "at");
    return {
      op,
      id: requireString(raw.id, line, "id"),
      by: requireString(raw.by, line, "by"),
      ...(note ? { note } : {}),
      ...(at ? { at } : {}),
    };
  }
  return lineError(line, `的 op 只能是 claim / confirm / reject，实际是 ${String(op)}`);
}

/** 解析整份 claims 文件（空行忽略）。 */
export function parseClaims(text: string): ClaimEvent[] {
  const events: ClaimEvent[] = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]?.trim() ?? "";
    if (line === "") continue;
    events.push(parseClaimLine(line, i + 1));
  }
  return events;
}

/**
 * 折叠成当前状态。同一 id 的 claim 只允许出现一次（改口请用 reject + 新 claim），
 * 这样"语义被推翻过"不会被悄悄抹掉。
 */
export function foldClaims(events: ClaimEvent[]): ClaimState[] {
  const order: string[] = [];
  const states = new Map<string, ClaimState>();
  for (const event of events) {
    if (event.op === "claim") {
      if (states.has(event.id)) throw new Error(`claims 中 id 重复：${event.id}（改口请先 reject 再新增）`);
      order.push(event.id);
      states.set(event.id, {
        id: event.id,
        kind: event.kind,
        subject: event.subject,
        value: event.value,
        source: event.source,
        status: "unconfirmed",
        evidence: event.evidence ? [...event.evidence] : [],
        history: [{ op: "claim", ...(event.at ? { at: event.at } : {}) }],
      });
      continue;
    }
    const state = states.get(event.id);
    if (!state) throw new Error(`claims 中对未声明的 id 执行 ${event.op}：${event.id}`);
    if (event.op === "confirm") {
      // 已 rejected 的语义不能被"再确认一次"悄悄复活，必须先重新 claim。
      if (state.status === "rejected") throw new Error(`id ${event.id} 已被 reject，需重新 claim 后才能 confirm`);
      state.status = "confirmed";
    } else {
      state.status = "rejected";
    }
    state.history.push({
      op: event.op,
      by: event.by,
      ...(event.at ? { at: event.at } : {}),
      ...(event.note ? { note: event.note } : {}),
    });
  }
  return order.map((id) => {
    const state = states.get(id);
    if (!state) throw new Error(`内部错误：${id} 丢失`);
    return state;
  });
}

export interface ClaimCounts {
  total: number;
  unconfirmed: number;
  confirmed: number;
  rejected: number;
}

export const countClaims = (states: ClaimState[]): ClaimCounts => ({
  total: states.length,
  unconfirmed: states.filter((s) => s.status === "unconfirmed").length,
  confirmed: states.filter((s) => s.status === "confirmed").length,
  rejected: states.filter((s) => s.status === "rejected").length,
});

/** 序列化一行事件（追加用）。键顺序固定，便于人读与 diff。 */
export function serializeClaimEvent(event: ClaimEvent): string {
  if (event.op === "claim") {
    const ordered: Record<string, unknown> = {
      op: event.op,
      id: event.id,
      kind: event.kind,
      subject: event.subject,
      value: event.value,
      source: event.source,
    };
    if (event.evidence && event.evidence.length > 0) ordered.evidence = event.evidence;
    if (event.at) ordered.at = event.at;
    return JSON.stringify(ordered);
  }
  const ordered: Record<string, unknown> = { op: event.op, id: event.id, by: event.by };
  if (event.note) ordered.note = event.note;
  if (event.at) ordered.at = event.at;
  return JSON.stringify(ordered);
}
