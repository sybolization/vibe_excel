/**
 * pi 扩展：`ask_user` —— 让模型在写程序的中途停下来问人。
 *
 * 为什么需要它：真实业务里有一类东西机器决定不了——「离职再入职算不算新人」「这条记录按
 * 入职日还是生效日切」。这类问题写进提示词只会变成模型自己猜，猜完还很有信心。
 * 把它变成一个**工具调用**，模型就必须真的停下来，由人回答，回答进入会话记录。
 *
 * 这个文件只做**类型**导入：它由 pi 用 jiti 直接加载，运行时不能有外部依赖。
 *
 * 在 RPC 模式下，`ctx.ui.input/select` 不会打开终端界面，而是发出一条
 * `extension_ui_request` 给宿主（我们的 harness）。宿主负责显示问题和回收答案。
 */

import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';

type AskUserParams = {
  question: string;
  options?: string[];
  why?: string;
  placeholder?: string;
};

/** 工具参数（运行时形状；pi 会按 parameters 做校验）。 */
const PARAMETERS = {
  type: 'object',
  properties: {
    question: {
      type: 'string',
      description: '要问的事。必须具体到能一句话回答，不要问「有没有问题」这种废话。',
    },
    options: {
      type: 'array',
      items: { type: 'string' },
      description: '可选项（给选项比让人自由发挥更快）。不写就是自由回答。',
    },
    why: {
      type: 'string',
      description: '为什么必须问：不回答会导致哪个口径定不下来。这句话会被原样展示给用户。',
    },
    placeholder: {
      type: 'string',
      description: '自由回答时的提示语，例如「按入职日 / 按生效日」。',
    },
  },
  required: ['question'],
  additionalProperties: false,
};

const CANCELLED = '用户没有回答（取消或超时）。按最保守的口径继续，并在产物里标注「未确认」，不要当成用户同意。';

export default function askUserExtension(pi: ExtensionAPI): void {
  pi.registerTool({
    name: 'ask_user',
    label: '问用户',
    description:
      '当某个口径只能由人决定时，停下来问用户，等回答再继续。不要用它问「你还有别的要求吗」这类问题，' +
      '只在「不回答就无法继续或只能靠猜」时使用。一次只问一件事。',
    promptSnippet: 'ask_user：口径只能由人决定时，先问再写，不要猜。',
    promptGuidelines: [
      '写程序前，把「只能由人定」的口径一次性问清楚（可以连续调用几次，但一次只问一件事）。',
      '不要把能自己从数据和代码里查清的事情拿去问人。',
    ],
    parameters: PARAMETERS as never,
    async execute(_toolCallId, params: AskUserParams, _signal, _onUpdate, ctx) {
      const reply = (text: string) => ({ content: [{ type: 'text' as const, text }], details: undefined });
      const question = String(params?.question ?? '').trim();
      if (!question) {
        return reply('问题为空：把要问的事写成一句具体的话再问。');
      }
      const why = typeof params?.why === 'string' && params.why.trim() ? params.why.trim() : '';
      const title = why ? `${question}（为什么问：${why}）` : question;

      if (!ctx?.hasUI) {
        return reply('当前没有可问答的界面（非 RPC 模式）。' + CANCELLED);
      }

      const options = Array.isArray(params?.options) ? params.options.map((item) => String(item)) : [];
      const answer =
        options.length > 0 ? await ctx.ui.select(title, options) : await ctx.ui.input(title, params?.placeholder);

      if (answer === undefined || answer === null || String(answer).trim() === '') {
        return reply(CANCELLED);
      }
      return reply(String(answer));
    },
  });
}
