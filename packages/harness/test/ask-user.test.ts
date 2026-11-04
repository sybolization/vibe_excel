/**
 * `ask_user` 通道的两端：pi 那边的工具定义、harness 这边的回话。
 *
 * 真正跑一次模型需要 API key，这里把两端拆开各自验证：
 *   - 工具拿到答案 / 拿不到答案时的行为（免得模型把「没人回答」当成「用户同意」）；
 *   - 回话是不是真的按 RPC 协议写进了子进程的 stdin。
 */

import { PassThrough } from 'node:stream';

import { describe, expect, it } from 'vitest';

import askUserExtension from '../ext/ask-user.ts';
import { answerUiRequest, writeUiResponse } from '../src/plugins/agent-pi.ts';

interface RegisteredTool {
  name: string;
  description: string;
  parameters: { required?: string[]; properties?: Record<string, unknown> };
  execute: (
    id: string,
    params: Record<string, unknown>,
    signal: AbortSignal | undefined,
    update: undefined,
    ctx: unknown,
  ) => Promise<{ content: { type: string; text: string }[] }>;
}

/** 造一个最小的 pi API，把注册进来的工具抓出来。 */
function loadTool(): RegisteredTool {
  let tool: RegisteredTool | undefined;
  const pi = {
    registerTool(candidate: RegisteredTool) {
      tool = candidate;
    },
    on() {},
    registerCommand() {},
  };
  askUserExtension(pi as unknown as Parameters<typeof askUserExtension>[0]);
  if (!tool) throw new Error('扩展没有注册任何工具');
  return tool;
}

const uiCtx = (ui: unknown) => ({
  hasUI: true,
  mode: 'rpc',
  ui,
});

const textOf = (result: { content: { type: string; text: string }[] }): string => result.content.map((part) => part.text).join('\n');

describe('pi 扩展：ask_user', () => {
  it('注册的名字、必填参数都对得上', () => {
    const tool = loadTool();
    expect(tool.name).toBe('ask_user');
    expect(tool.parameters.required).toEqual(['question']);
    expect(Object.keys(tool.parameters.properties ?? {})).toEqual(
      expect.arrayContaining(['question', 'options', 'why', 'placeholder']),
    );
    expect(tool.description).toMatch(/口径/);
  });

  it('自由回答：把用户的话原样交给模型', async () => {
    const tool = loadTool();
    const asked: string[] = [];
    const result = await tool.execute(
      'call-1',
      { question: '离职再入职算新人吗？', placeholder: '算是/不算' },
      undefined,
      undefined,
      uiCtx({
        input: async (title: string) => {
          asked.push(title);
          return '不算，按重新入职处理';
        },
      }),
    );
    expect(textOf(result)).toBe('不算，按重新入职处理');
    expect(asked[0]).toContain('离职再入职算新人吗？');
  });

  it('给选项时走 select，而不是自由输入', async () => {
    const tool = loadTool();
    let usedSelect = false;
    const result = await tool.execute(
      'call-2',
      { question: '按哪个日期切？', options: ['入职日', '生效日'] },
      undefined,
      undefined,
      uiCtx({
        select: async () => {
          usedSelect = true;
          return '生效日';
        },
        input: async () => {
          throw new Error('有选项时不应该走自由输入');
        },
      }),
    );
    expect(usedSelect).toBe(true);
    expect(textOf(result)).toBe('生效日');
  });

  it('用户取消/超时时，明确告诉模型「未确认」，不能当成同意', async () => {
    const tool = loadTool();
    const result = await tool.execute('call-3', { question: '合并单元格怎么摊平？' }, undefined, undefined, uiCtx({ input: async () => undefined }));
    expect(textOf(result)).toMatch(/未确认/);
    expect(textOf(result)).toMatch(/不要当成用户同意/);
  });

  it('没有可问答界面时也不假装有人回答', async () => {
    const tool = loadTool();
    const result = await tool.execute('call-4', { question: 'x' }, undefined, undefined, { hasUI: false, mode: 'print', ui: {} });
    expect(textOf(result)).toMatch(/没有可问答的界面/);
    expect(textOf(result)).toMatch(/未确认/);
  });

  it('空问题被打回去，不浪费人一次回答', async () => {
    const tool = loadTool();
    const result = await tool.execute('call-5', { question: '   ' }, undefined, undefined, uiCtx({ input: async () => 'x' }));
    expect(textOf(result)).toMatch(/问题为空/);
  });
});

describe('harness 回话：extension_ui_response', () => {
  it('把 id、问题、选项原样交给上层，并把回答按同一个 id 写回', async () => {
    const sent: Record<string, unknown>[] = [];
    let seen: { id: string; question: string; options: string[] } | undefined;
    const kind = await answerUiRequest({
      event: { type: 'extension_ui_request', id: 'q-1', method: 'select', title: '按哪个日期切？', options: ['入职日', '生效日'] },
      onQuestion: async (question) => {
        seen = question;
        return '生效日';
      },
      respond: (payload) => {
        sent.push(payload);
        return true;
      },
      timeoutMs: 1000,
    });

    expect(kind).toBe('handled');
    expect(seen).toEqual({ id: 'q-1', question: '按哪个日期切？', options: ['入职日', '生效日'] });
    expect(sent).toEqual([{ type: 'extension_ui_response', id: 'q-1', value: '生效日' }]);
  });

  it('没人回答（onQuestion 返回 undefined 或没登记）时回 cancelled', async () => {
    const cancelled: Record<string, unknown>[] = [];
    const respond = (payload: Record<string, unknown>): boolean => {
      cancelled.push(payload);
      return true;
    };
    await answerUiRequest({
      event: { type: 'extension_ui_request', id: 'q-2', method: 'input', title: '合并单元格怎么摊平？' },
      onQuestion: async () => undefined,
      respond,
      timeoutMs: 1000,
    });
    await answerUiRequest({
      event: { type: 'extension_ui_request', id: 'q-3', method: 'input', title: '没人接' },
      respond,
      timeoutMs: 1000,
    });
    expect(cancelled).toEqual([
      { type: 'extension_ui_response', id: 'q-2', cancelled: true },
      { type: 'extension_ui_response', id: 'q-3', cancelled: true },
    ]);
  });

  it('等太久就按超时取消，不会把整个 run 挂住', async () => {
    const sent: Record<string, unknown>[] = [];
    const logs: string[] = [];
    const started = Date.now();
    await answerUiRequest({
      event: { type: 'extension_ui_request', id: 'q-4', method: 'input', title: '一直没人理' },
      onQuestion: () => new Promise<string | undefined>(() => undefined),
      respond: (payload) => {
        sent.push(payload);
        return true;
      },
      timeoutMs: 30,
      log: (line) => logs.push(line),
    });
    expect(Date.now() - started).toBeLessThan(2000);
    expect(sent).toEqual([{ type: 'extension_ui_response', id: 'q-4', cancelled: true }]);
    expect(logs.join('\n')).toMatch(/超时/);
  });

  it('不是提问的 UI 方法（notify / setStatus）不回应，免得协议串了', async () => {
    for (const method of ['notify', 'setStatus', 'setWidget', 'setTitle']) {
      const sent: Record<string, unknown>[] = [];
      const kind = await answerUiRequest({
        event: { type: 'extension_ui_request', id: 'x', method, message: 'hi' },
        respond: (payload) => {
          sent.push(payload);
          return true;
        },
        timeoutMs: 1000,
      });
      expect(kind).toBe('ignored');
      expect(sent).toEqual([]);
    }
  });

  it('按 RPC 协议往 stdin 写一行 JSON，id 与回答都对得上', () => {
    const stdin = new PassThrough();
    const chunks: string[] = [];
    stdin.on('data', (chunk: Buffer) => chunks.push(chunk.toString('utf8')));

    const ok = writeUiResponse({ process: { stdin } }, { type: 'extension_ui_response', id: 'q-1', value: '按入职日' });
    expect(ok).toBe(true);
    expect(JSON.parse(chunks.join('').trim())).toEqual({ type: 'extension_ui_response', id: 'q-1', value: '按入职日' });
  });

  it('取消也走同一个通道（cancelled: true）', () => {
    const stdin = new PassThrough();
    const chunks: string[] = [];
    stdin.on('data', (chunk: Buffer) => chunks.push(chunk.toString('utf8')));
    writeUiResponse({ process: { stdin } }, { type: 'extension_ui_response', id: 'q-2', cancelled: true });
    expect(JSON.parse(chunks.join('').trim())).toEqual({ type: 'extension_ui_response', id: 'q-2', cancelled: true });
  });

  it('子进程已经没了就返回 false，而不是抛错', () => {
    expect(writeUiResponse({ process: { stdin: undefined } }, { type: 'extension_ui_response', id: 'q-3' })).toBe(false);
    const stdin = new PassThrough();
    stdin.destroy();
    expect(writeUiResponse({ process: { stdin } }, { type: 'extension_ui_response', id: 'q-4' })).toBe(false);
    expect(writeUiResponse({}, { type: 'extension_ui_response', id: 'q-5' })).toBe(false);
  });
});
