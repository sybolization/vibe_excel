/**
 * render：把 build 之后的血缘数据渲染成**一个自包含的 HTML 文件**。
 *
 * 为什么是单文件、内联 CSS/JS/数据：这份页面要能发给 HR、丢进邮件、放进制品根，
 * 不依赖服务器、不依赖网络、不需要构建。打开就能点。
 *
 * 骨架只留结构，不写文案：一级（表→表）与二级（列→列）的界面全部由 app.js 按数据生成，
 * 所以"一个原始文件里有几张 sheet 就框几个"这种分区规则只存在于一处。
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { BuiltLineage } from "./lineage.ts";

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

export function renderHtml(built: BuiltLineage): string {
  const css = read("app.css");
  const js = read("app.js");
  const payload = escapeForScript(JSON.stringify(built));
  const title = escapeHtml(`${built.case.title} · SQL 表血缘`);

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>
${css}
</style>
</head>
<body>
<div class="page">
  <div class="topbar">
    <h1>SQL 表血缘</h1>
    <span class="meta" id="case"></span>
  </div>
  <div id="view1">
    <div class="stage" id="stage1">
      <div class="lanes" id="lanes"></div>
      <svg class="wires" id="wires1"></svg>
    </div>
  </div>
  <div id="view2" hidden>
    <div class="crumbs"><button type="button" class="ghost" id="back">← 返回总览</button></div>
    <div class="detail-head" id="detail-head"></div>
    <div class="stage" id="stage2">
      <div class="detail" id="detail"></div>
      <svg class="wires" id="wires2"></svg>
    </div>
    <div class="filters" id="filters" hidden></div>
  </div>
  <div class="panel" id="panel" hidden></div>
</div>
<script type="application/json" id="lineage">${payload}</script>
<script>
${js}
</script>
</body>
</html>
`;
}
