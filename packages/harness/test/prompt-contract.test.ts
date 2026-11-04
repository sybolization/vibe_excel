/**
 * 写程序的提示词要跟契约一起被检查。
 *
 * 提示词是模型唯一的说明书：契约改了、提示词没改，模型就会写出解析器读不出来的代码；
 * 模板变量拼错一个，模型看到的就是 `{{DB}}` 这种原文。两种错都在这里挡住。
 *
 * 更狠的一条：提示词里的 **few-shot 示例要真的能被解析器读**。示例是模型照着抄的模板，
 * 示例错了，整批产出都会错——所以这里真的把那段 Python 抽出来丢给 ast 跑一遍。
 */

import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { afterAll, describe, expect, it } from 'vitest';

import { fillTemplate } from '../src/plugins/sessions.ts';

const repoRoot = resolve(import.meta.dirname, '..', '..', '..');
const promptPath = join(repoRoot, 'prompts', 'author-flow.md');
const pythonPath = join(repoRoot, 'runtime-env', '.venv', 'Scripts', 'python.exe');
const text = readFileSync(promptPath, 'utf8');

/** sessions 插件在 author 时提供的变量（大小写两套，见 sessions.ts）。 */
const VARS: Record<string, string> = {
  WORKSPACE: '## 工作区事实\n\n| 项 | 路径 | 实测状态 |',
  PYTHON: 'C:/python.exe',
  CODE: 'C:/code',
  INPUTS: 'C:/inputs',
  DB: 'C:/work/hr.db',
  OUT: 'C:/work/out',
  ASOF: '2026-01-01',
  FLOW: 'flow.json',
  code: 'C:/code',
  inputs: 'C:/inputs',
  db: 'C:/work/hr.db',
  out: 'C:/work/out',
  asOf: '2026-01-01',
};

const placeholders = [...text.matchAll(/\{\{(\w+)\}\}/g)].map((match) => match[1]!).sort();

const scratch = mkdtempSync(join(tmpdir(), 'vibe-prompt-'));
afterAll(() => rmSync(scratch, { recursive: true, force: true }));

describe('写程序的提示词：prompts/author-flow.md', () => {
  it('用到的占位符都有值（大小写两种写法都认）', () => {
    expect(placeholders.length).toBeGreaterThan(0);
    const missing = [...new Set(placeholders)].filter((key) => VARS[key] === undefined);
    expect(missing).toEqual([]);
    expect(fillTemplate(text, VARS)).not.toMatch(/\{\{\w+\}\}/);
  });

  it('把契约讲清楚了：物化、产出表、双跑、ask_user、不做无谓折叠', () => {
    expect(text).toContain('materialize');
    expect(text).toContain('outputs');
    expect(text).toMatch(/跑两遍/);
    expect(text).toContain('ask_user');
    expect(text).toMatch(/不要读系统时间/);
    expect(text).toMatch(/物化（materialize）只做映射/);
    expect(text).toMatch(/业务准确性优先/);
    expect(text).toMatch(/optimize/);
  });

  it('把「代码就是流程」的五条规则写成了可执行的句子', () => {
    expect(text).toMatch(/`main` 是唯一入口，必须线性/);
    expect(text).toMatch(/"t:表名"/);
    expect(text).toMatch(/表名必须是字面量/);
    // flow.json 里不再写节点：这条如果哪次被改回去，提示词和解析器就对不上了。
    expect(text).toMatch(/`flow\.json` 里\*\*不写节点\*\*/);
  });

  it('提供了完整的 few-shot：flow.json + 可运行的 pipeline.py', () => {
    expect(text).toMatch(/```json[\s\S]*"materialize"/);
    expect(text).toMatch(/```python[\s\S]*def main\(db, as_of/);

    // flow.json 示例必须是合法 JSON，且形状是解析器认的那一版（只有 materialize / outputs，没有 nodes）。
    const jsonBlocks = [...text.matchAll(/```json\n([\s\S]*?)```/g)].map((match) => match[1]!);
    const flow = JSON.parse(jsonBlocks[0]!) as Record<string, unknown>;
    expect(Object.keys(flow).sort()).toEqual(['db', 'flow', 'materialize', 'outputs']);
    expect(text).toContain('"t:raw_employees"');
    expect(text).toContain('-> "t:employees"');
    // 反向教材：main 里不许出现的写法，提示词要明确点名。
    expect(text).toMatch(/`for f in \[a, b\]: f\(\)` 解析不出顺序/);
  });

  it.skipIf(!existsSync(pythonPath))('few-shot 里的 Python 真的能被 ast 读成一条流', () => {
    const blocks = [...text.matchAll(/```python\n([\s\S]*?)```/g)].map((match) => match[1]!);
    expect(blocks.length).toBeGreaterThan(0);
    const file = join(scratch, 'pipeline.py');
    writeFileSync(file, blocks[0]!, 'utf8');

    // 这条脚本就是解析器将来要做的事的最小版本：main 线性 + 标注齐全。
    const check = `
import ast, sys

src = open(sys.argv[1], encoding="utf-8").read()
tree = ast.parse(src)

defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
assert "main" in defs, "示例里没有 main"

main = defs["main"]
bad = [type(s).__name__ for s in main.body if isinstance(s, (ast.If, ast.For, ast.While, ast.Try, ast.With))]
assert not bad, f"main 里有控制流: {bad}"

RUNTIME = {"db", "as_of", "inputs", "out", "report"}
calls = []
for stmt in main.body:
    node = stmt.value if isinstance(stmt, ast.Expr) else (stmt.value if isinstance(stmt, ast.Assign) else None)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in defs:
        calls.append(node.func.id)
assert calls, "main 里没有调用本文件的函数"

for name in calls:
    fn = defs[name]
    for arg in fn.args.args:
        if arg.arg in RUNTIME:
            continue
        ann = ast.unparse(arg.annotation) if arg.annotation else None
        assert ann is not None, f"{name} 的参数 {arg.arg} 没有标注"
        assert ann.startswith('"t:') or ann.startswith("'t:"), f"{name} 的参数 {arg.arg} 标注不是表: {ann}"
    ret = ast.unparse(fn.returns) if fn.returns else None
    assert ret is not None and (ret.startswith('"t:') or ret.startswith("'t:")), f"{name} 没有表返回值标注: {ret}"
    assert ast.get_docstring(fn), f"{name} 没有 docstring（节点标题）"

print("ok", len(calls))
`;
    const script = join(scratch, 'check.py');
    writeFileSync(script, check, 'utf8');
    const out = execFileSync(pythonPath, [script, file], { encoding: 'utf8' });
    expect(out.trim()).toMatch(/^ok \d+$/);
  });
});
