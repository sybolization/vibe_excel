/**
 * 把视图模型渲染成**一个自包含的 HTML**。
 *
 * 和 sqlmap 同一条路子：单文件、内联 CSS/JS/数据，不依赖服务器与网络，双击就能点。
 * 骨架只留空壳，托盘 / 画布 / 底栏 / 右侧对话区全由 app.js 与 chat.js 按数据生成，
 * 所以「没有哪个节点就不画哪块」这种判断只存在于一处。
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { BookView } from "./types.ts";

const here = dirname(fileURLToPath(import.meta.url));
const appDir = join(here, "..", "app");

const read = (name: string): string => readFileSync(join(appDir, name), "utf8");

/** 内联的 JS 里若出现 `</script`，浏览器会提前收尾；一律挡掉。 */
const inline = (source: string): string => source.replace(/<\/script/gi, "<\\/script");

/** 嵌进 <script> 的 JSON 必须挡住 </script> 与行分隔符。 */
export function escapeForScript(json: string): string {
  return json
    .replace(/</g, "\\u003c")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

const escapeHtml = (value: string): string =>
  value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export function renderBook(view: BookView): string {
  const css = read("app.css");
  const marked = read(join("vendor", "marked.min.js"));
  const js = read("app.js");
  const chat = read("chat.js");
  const payload = escapeForScript(JSON.stringify(view));
  const program = view.program === null ? "book" : view.program.name;

  /* 诊断：视图模型、运行图、输入三处都可能留下 warning，去重后折叠成一块。
     它挂在右上角，所以要让开右侧对话区（--cw 由 chat.js 写入）。
     看数的人需要知道「哪一块没读到」，而不是面对一张安静的空页面。 */
  const warnings = [
    ...view.warnings ?? [],
    ...(view.graph?.warnings ?? []),
    ...(view.inputs?.warnings ?? []),
  ].filter((item, index, all) => all.indexOf(item) === index);
  const diagnostics = warnings.length === 0
    ? ""
    : `<details id="book-warnings"><summary>诊断 · ${warnings.length} 条（缺页仍可浏览）</summary><ul>${warnings
        .map((warning) => `<li>${escapeHtml(warning)}</li>`)
        .join("")}</ul></details>\n`;

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(program)} · book</title>
<style>
${css}
#book-warnings{position:fixed;top:12px;right:calc(var(--cw, 360px) + 12px);z-index:1000;max-width:min(640px,90vw);max-height:60vh;overflow:auto;background:var(--panel);border:1px solid var(--amber);border-radius:6px;padding:8px 12px;color:var(--ink)}
#book-warnings summary{cursor:pointer;color:var(--amber)}
#book-warnings li{margin:6px 0;overflow-wrap:anywhere}
</style>
</head>
<body>
<div id="root"></div>
${diagnostics}
<script type="application/json" id="book-data">${payload}</script>
<script>
${inline(marked)}
</script>
<script>
${inline(js)}
</script>
<script>
${inline(chat)}
</script>
</body>
</html>
`;
}
