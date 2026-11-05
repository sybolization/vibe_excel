/**
 * 把视图模型渲染成**一个自包含的 HTML**。
 *
 * 和 sqlmap 同一条路子：单文件、内联 CSS/JS/数据，不依赖服务器与网络，双击就能点。
 * 骨架只留空壳，左中右三栏全由 app.js 按数据生成，所以「没有哪个节点就不画哪块」
 * 这种判断只存在于一处。
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { BookView } from "./types.ts";

const here = dirname(fileURLToPath(import.meta.url));
const appDir = join(here, "..", "app");

const read = (name: string): string => readFileSync(join(appDir, name), "utf8");

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
  const js = read("app.js");
  const payload = escapeForScript(JSON.stringify(view));
  const program = view.program === null ? "book" : view.program.name;

  return `<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(program)} · book</title>
<style>
${css}
</style>
</head>
<body>
<div id="root"></div>
<script type="application/json" id="book-data">${payload}</script>
<script>
${js}
</script>
</body>
</html>
`;
}
