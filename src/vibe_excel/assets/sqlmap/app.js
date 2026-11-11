/* 两级血缘界面。
   一级：输入表按"原始文件"分框（一个文件里的 n 张 sheet 框在一起）→ 输出表；
   二级：点输出表进来，点一列只看构成它的来源列与连线。
   纯 DOM + SVG，无依赖；深链接 #/out/<输出表>/<列名> 可直接打开某一列。 */
(function () {
  "use strict";

  var data = JSON.parse(document.getElementById("lineage").textContent);
  var PALETTE = ["#0369a1", "#0e7490", "#4d7c0f", "#b45309", "#9333ea", "#be123c", "#0f766e", "#7c2d12", "#1d4ed8", "#a16207"];
  var index = new Map();
  data.tables.forEach(function (t, i) { index.set(t.id, i); });
  var byId = new Map();
  data.tables.forEach(function (t) { byId.set(t.id, t); });
  var inputs = data.tables.filter(function (t) { return t.kind === "input"; });
  var outputs = data.tables.filter(function (t) { return t.kind === "output"; });

  function group(list, key) {
    var map = new Map();
    list.forEach(function (item) {
      var k = item[key];
      if (!map.has(k)) map.set(k, []);
      map.get(k).push(item);
    });
    return map;
  }
  var edgesByOutput = group(data.columns, "output");
  var filtersByOutput = group(data.filters, "output");

  /* 哪张输入表真的喂了输出（只算列映射，行过滤不算"喂数"）。 */
  var usedBy = new Map();
  data.columns.forEach(function (edge) {
    edge.from.forEach(function (ref) {
      if (ref.table === edge.output) return;
      if (!usedBy.has(ref.table)) usedBy.set(ref.table, new Set());
      usedBy.get(ref.table).add(edge.output);
    });
  });

  function colorOf(id) { return PALETTE[(index.get(id) || 0) % PALETTE.length]; }
  function fmt(n) { return n === null || n === undefined ? "—" : Number(n).toLocaleString("zh-CN"); }
  function h(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = text;
    return el;
  }
  function svgEl(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    return el;
  }
  function cssEscape(value) { return String(value).replace(/"/g, '\\"'); }

  var view1 = document.getElementById("view1");
  var view2 = document.getElementById("view2");
  var lanes = document.getElementById("lanes");
  var stage1 = document.getElementById("stage1");
  var svg1 = document.getElementById("wires1");
  var detailHead = document.getElementById("detail-head");
  var detail = document.getElementById("detail");
  var stage2 = document.getElementById("stage2");
  var svg2 = document.getElementById("wires2");
  var filtersBox = document.getElementById("filters");
  var panel = document.getElementById("panel");

  var state = { output: null, column: null, showAll: new Set(), showUnused: false };

  /* ---------------- 一级：原始文件 → 表 ---------------- */

  /** 按原始文件分组，保持声明顺序；没有 source 的表归到最后一组。 */
  function fileGroups(list) {
    var groups = [];
    var at = new Map();
    list.forEach(function (t) {
      var file = t.source && t.source.file ? t.source.file : "";
      var key = file || "\u0000none";
      if (!at.has(key)) {
        at.set(key, groups.length);
        groups.push({ file: file, tables: [] });
      }
      groups[at.get(key)].tables.push(t);
    });
    return groups;
  }

  function tableCard(t) {
    var card = h("div", "card input");
    card.dataset.table = t.id;
    var row = h("div", "crow");
    var dot = h("span", "dot");
    dot.style.background = colorOf(t.id);
    row.append(dot, h("span", "name", t.label));
    if (t.source && t.source.sheet) row.append(h("span", "sheet", t.source.sheet));
    card.append(row);
    card.append(h("div", "meta", fmt(t.rows) + " 行 · " + t.columns.length + " 列"));
    if (t.source && t.source.file) card.title = t.source.file + (t.source.sheet ? " / " + t.source.sheet : "");
    return card;
  }

  function fileBox(g) {
    var box = h("div", "filebox");
    var head = h("div", "filehead");
    head.append(h("span", "fname", g.file || "无原始文件"));
    if (g.tables.length > 1) head.append(h("span", "fcount", g.tables.length + " 张表"));
    box.append(head);
    var body = h("div", "fbody");
    g.tables.forEach(function (t) { body.append(tableCard(t)); });
    box.append(body);
    return box;
  }

  function outputCard(t) {
    var card = h("div", "card output");
    card.dataset.table = t.id;
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    var row = h("div", "crow");
    row.append(h("span", "name", t.label), h("span", "go", "→"));
    card.append(row);
    var mapped = (edgesByOutput.get(t.id) || []).length;
    var meta = fmt(t.rows) + " 行 · " + t.columns.length + " 列";
    if (mapped < t.columns.length) meta += " · 未标注 " + (t.columns.length - mapped);
    card.append(h("div", "meta", meta));
    card.addEventListener("click", function () { openOutput(card, t.id); });
    card.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        openOutput(card, t.id);
      }
    });
    return card;
  }

  /** 点输出表：先播一段打开动效，再进二级（减少动态偏好时直接跳）。 */
  function openOutput(card, id) {
    if (card.dataset.opening === "1") return;
    card.dataset.opening = "1";
    card.classList.add("opening");
    var go = function () { location.hash = "#/out/" + encodeURIComponent(id); };
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) return go();
    setTimeout(go, 170);
  }

  function renderLevel1() {
    view1.hidden = false;
    view2.hidden = true;
    panel.hidden = true;

    var wired = inputs.filter(function (t) { return usedBy.has(t.id); });
    var rest = inputs.filter(function (t) { return !usedBy.has(t.id); });
    var shown = state.showUnused ? inputs : wired;

    var inZone = h("section", "zone in");
    var inHead = h("div", "zone-head");
    var groups = fileGroups(shown);
    var fileCount = groups.filter(function (g) { return g.file; }).length;
    inHead.append(h("b", null, "输入表"), h("span", "sub", shown.length + " 张 · " + fileCount + " 个原始文件"));
    if (rest.length > 0) {
      inHead.append(h("span", "gap"));
      var toggle = h("button", "ghost", state.showUnused ? "只看参与输出的" : "显示全部 " + inputs.length + " 张");
      toggle.type = "button";
      toggle.addEventListener("click", function () {
        state.showUnused = !state.showUnused;
        renderLevel1();
      });
      inHead.append(toggle);
    }
    inZone.append(inHead);

    var inBody = h("div", "zone-body");
    groups.forEach(function (g) { inBody.append(fileBox(g)); });
    if (!state.showUnused && rest.length > 0) {
      var line = h("div", "unused");
      line.append(h("span", "ulabel", "未参与任何列映射"));
      rest.forEach(function (t) {
        var chip = h("span", "uchip");
        chip.append(document.createTextNode(t.label + " "), h("i", null, fmt(t.rows) + " 行"));
        line.append(chip);
      });
      inBody.append(line);
    }
    inZone.append(inBody);

    var outZone = h("section", "zone out");
    var outHead = h("div", "zone-head");
    outHead.append(h("b", null, "输出表"), h("span", "sub", outputs.length + " 张 · 点击进入"));
    outZone.append(outHead);
    var outBody = h("div", "zone-body");
    outputs.forEach(function (t) { outBody.append(outputCard(t)); });
    outZone.append(outBody);

    lanes.replaceChildren(inZone, outZone);
    requestAnimationFrame(drawTableEdges);
  }

  function rectIn(el, host) {
    var a = el.getBoundingClientRect();
    var b = host.getBoundingClientRect();
    return { left: a.left - b.left, right: a.right - b.left, top: a.top - b.top, bottom: a.bottom - b.top, w: a.width, h: a.height };
  }

  function curve(x1, y1, x2, y2) {
    var dx = Math.max(48, Math.abs(x2 - x1) * 0.42);
    return "M" + x1 + "," + y1 + " C" + (x1 + dx) + "," + y1 + " " + (x2 - dx) + "," + y2 + " " + x2 + "," + y2;
  }

  /** 某输入表喂给某输出表的列名（只看列映射）。 */
  function refsFrom(inputId, outputId) {
    var cols = new Set();
    (edgesByOutput.get(outputId) || []).forEach(function (edge) {
      edge.from.forEach(function (ref) {
        if (ref.table === inputId) cols.add(ref.column);
      });
    });
    return cols;
  }

  function drawTableEdges() {
    svg1.replaceChildren();
    if (view1.hidden) return;
    outputs.forEach(function (out) {
      var target = lanes.querySelector('[data-table="' + out.id + '"]');
      if (!target) return;
      var tb = rectIn(target, stage1);
      inputs.forEach(function (input) {
        var cols = refsFrom(input.id, out.id);
        if (cols.size === 0) return;
        var src = lanes.querySelector('[data-table="' + input.id + '"]');
        if (!src) return;
        var sb = rectIn(src, stage1);
        var box = src.closest(".filebox");
        var x1 = box ? rectIn(box, stage1).right : sb.right;
        var y1 = sb.top + sb.h / 2;
        var path = svgEl("path", { d: curve(x1, y1, tb.left, tb.top + tb.h / 2), class: "wire" });
        path.style.stroke = colorOf(input.id);
        path.style.opacity = "0.52";
        svg1.append(path);
        var label = svgEl("text", { x: x1 + 9, y: y1 - 6 });
        label.textContent = cols.size + " 列";
        svg1.append(label);
      });
    });
  }

  /* ---------------- 二级：列 → 列 ---------------- */
  function relevantRefs(outputId, columnName) {
    var edges = edgesByOutput.get(outputId) || [];
    var picked = columnName ? edges.filter(function (e) { return e.name === columnName; }) : edges;
    var map = new Map();
    picked.forEach(function (edge) {
      edge.from.forEach(function (ref) {
        if (ref.table === outputId) return;
        if (!map.has(ref.table)) map.set(ref.table, new Set());
        map.get(ref.table).add(ref.column);
      });
    });
    return map;
  }

  function sourceCard(t, used, showAll, outputId) {
    var card = h("div", "card input");
    card.dataset.table = t.id;
    var row = h("div", "crow");
    var dot = h("span", "dot");
    dot.style.background = colorOf(t.id);
    row.append(dot, h("span", "name", t.label));
    if (t.source && t.source.sheet) row.append(h("span", "sheet", t.source.sheet));
    card.append(row);
    card.append(h("div", "meta", fmt(t.rows) + " 行 · " + t.columns.length + " 列"));

    var visible = showAll ? t.columns : t.columns.filter(function (c) { return used.has(c); });
    var toggle = h("button", "ghost", showAll ? "只看相关 " + used.size + " 列" : "全部 " + t.columns.length + " 列");
    toggle.type = "button";
    toggle.style.marginTop = "4px";
    toggle.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (showAll) state.showAll.delete(t.id); else state.showAll.add(t.id);
      renderLevel2(outputId, state.column);
    });
    card.append(toggle);

    var cols = h("div", "cols");
    visible.forEach(function (col) {
      var line = h("div", "col" + (used.has(col) ? "" : " dim"));
      line.dataset.table = t.id;
      line.dataset.col = col;
      line.append(h("span", "name", col));
      if (used.has(col)) line.append(h("span", "tag", "相关"));
      line.addEventListener("click", function () { showSourcePanel(t.id, col); });
      cols.append(line);
    });
    card.append(cols);
    return card;
  }

  function targetCard(out, selected, outputId) {
    var card = h("div", "card output");
    card.style.cursor = "default";
    card.dataset.table = out.id;
    var row = h("div", "crow");
    var dot = h("span", "dot");
    dot.style.background = colorOf(out.id);
    row.append(dot, h("span", "name", out.label));
    card.append(row);

    var edges = edgesByOutput.get(out.id) || [];
    var byName = new Map();
    edges.forEach(function (e) { byName.set(e.name, e); });
    var cols = h("div", "cols");
    out.columns.forEach(function (col) {
      var edge = byName.get(col);
      var cls = "col target";
      if (selected === col) cls += " hot";
      else if (selected) cls += " dim";
      var line = h("div", cls);
      line.dataset.table = out.id;
      line.dataset.col = col;
      line.append(h("span", "name", col));
      if (!edge) line.append(h("span", "tag", "未标注"));
      else if (edge.from.length === 0) line.append(h("span", "tag", "常量"));
      else if (edge.from.some(function (r) { return r.table === out.id; })) line.append(h("span", "tag self", "含本表列"));
      else line.append(h("span", "tag", edge.from.length + " 源"));
      line.addEventListener("click", function () {
        var next = state.column === col ? null : col;
        location.hash = "#/out/" + encodeURIComponent(outputId) + (next ? "/" + encodeURIComponent(next) : "");
      });
      cols.append(line);
    });
    card.append(cols);
    return card;
  }

  function renderFilters(outputId) {
    var filters = filtersByOutput.get(outputId) || [];
    if (filters.length === 0) { filtersBox.hidden = true; return; }
    filtersBox.hidden = false;
    filtersBox.replaceChildren(h("div", "ftitle", "行从哪来"));
    filters.forEach(function (filter) {
      var box = h("div", "filter");
      var name = h("div", "fname");
      name.append(document.createTextNode(filter.name));
      name.append(h("span", "tag", filter.kind === "rows" ? "行过滤" : "分组键"));
      box.append(name, h("div", "fformula", filter.formula));
      box.addEventListener("click", function () { showFilterPanel(outputId, filter); });
      filtersBox.append(box);
    });
  }

  function renderLevel2(outputId, columnName) {
    var out = byId.get(outputId);
    if (!out) return renderLevel1();
    state.output = outputId;
    state.column = columnName || null;
    view1.hidden = true;
    view2.hidden = false;

    detailHead.replaceChildren();
    detailHead.append(h("h2", null, out.label));
    detailHead.append(h("div", "meta", fmt(out.rows) + " 行 · " + out.columns.length + " 列"));

    var relevant = relevantRefs(outputId, state.column);
    var sources = h("div", "zone");
    var sourceHead = h("div", "zone-head");
    sourceHead.append(h("b", null, "来源列"));
    sources.append(sourceHead);
    if (relevant.size === 0) {
      var allEdges = edgesByOutput.get(outputId) || [];
      var picked = state.column ? allEdges.filter(function (e) { return e.name === state.column; }) : [];
      var selfRefs = [];
      picked.forEach(function (e) {
        e.from.forEach(function (ref) {
          if (ref.table === outputId) selfRefs.push(ref.column);
        });
      });
      sources.append(
        h("div", "meta", selfRefs.length > 0
          ? "这一列不读其它表，只依赖本表的 " + selfRefs.join("、")
          : "没有来源列：常量，或映射还没写"),
      );
    }
    relevant.forEach(function (used, tableId) {
      var t = byId.get(tableId);
      if (!t) return;
      sources.append(sourceCard(t, used, state.showAll.has(tableId), outputId));
    });

    var target = h("div", "zone");
    var targetHead = h("div", "zone-head");
    targetHead.append(h("b", null, "输出列"));
    target.append(targetHead, targetCard(out, state.column, outputId));

    detail.replaceChildren(sources, target);
    renderFilters(outputId);
    requestAnimationFrame(drawColumnEdges);

    if (state.column) showColumnPanel(outputId, state.column, null);
    else panel.hidden = true;
  }

  function drawColumnEdges() {
    svg2.replaceChildren();
    if (view2.hidden || !state.column) return;
    var outputId = state.output;
    var column = state.column;
    var edge = (edgesByOutput.get(outputId) || []).filter(function (e) { return e.name === column; })[0];
    if (!edge) return;
    var targetEl = detail.querySelector('[data-table="' + outputId + '"][data-col="' + cssEscape(column) + '"]');
    if (!targetEl) return;
    var tb = rectIn(targetEl, stage2);
    edge.from.forEach(function (ref, i) {
      var srcEl = detail.querySelector('[data-table="' + ref.table + '"][data-col="' + cssEscape(ref.column) + '"]');
      if (!srcEl) return;
      var sb = rectIn(srcEl, stage2);
      var self = ref.table === outputId;
      var d = self
        ? "M" + sb.right + "," + (sb.top + sb.h / 2) + " C" + (sb.right + 38) + "," + (sb.top + sb.h / 2) + " " + (tb.right + 38) + "," + (tb.top + tb.h / 2) + " " + tb.right + "," + (tb.top + tb.h / 2)
        : curve(sb.right, sb.top + sb.h / 2, tb.left, tb.top + tb.h / 2);
      var hit = svgEl("path", { d: d, class: "hit" });
      hit.addEventListener("click", function () { showColumnPanel(outputId, column, i); });
      var path = svgEl("path", { d: d, class: "wire" });
      path.style.stroke = colorOf(ref.table);
      if (self) path.style.strokeDasharray = "4 3";
      svg2.append(path, hit);
    });
  }

  /* ---------------- 面板 ---------------- */
  function panelShell(title, where) {
    panel.hidden = false;
    panel.replaceChildren();
    var head = h("div", "phead");
    head.append(h("b", null, title));
    if (where) head.append(h("span", "where", where));
    var close = h("button", "close", "×");
    close.type = "button";
    close.addEventListener("click", function () { panel.hidden = true; });
    head.append(close);
    panel.append(head);
    return panel;
  }

  function addSources(parent, refs, hotIndex) {
    parent.append(h("div", "plabel", "来源列"));
    var wrap = h("div", "srcs");
    if (refs.length === 0) wrap.append(h("span", "src", "（无来源列：常量或未标注）"));
    refs.forEach(function (ref, i) {
      wrap.append(h("span", "src" + (i === hotIndex ? " hot" : ""), ref.table + "." + ref.column));
    });
    parent.append(wrap);
  }

  function showColumnPanel(outputId, column, hotIndex) {
    var edge = (edgesByOutput.get(outputId) || []).filter(function (e) { return e.name === column; })[0];
    panelShell(column, byId.get(outputId) ? byId.get(outputId).label : outputId);
    var body = h("div", "pbody");
    body.textContent = edge && edge.formula ? edge.formula : "（没有计算式：这一列要么是直通，要么映射还没写）";
    panel.append(body);
    if (edge && edge.note) {
      panel.append(h("div", "plabel", "说明"));
      panel.append(h("div", "note", edge.note));
    }
    addSources(panel, edge ? edge.from : [], hotIndex);
    var filters = filtersByOutput.get(outputId) || [];
    if (filters.length > 0) {
      panel.append(h("div", "plabel", "行约束"));
      filters.forEach(function (f) {
        panel.append(h("div", "note", f.name));
        panel.append(h("code", null, f.formula));
      });
    }
  }

  function showSourcePanel(tableId, column) {
    var hits = [];
    edgesByOutput.forEach(function (edges, outputId) {
      edges.forEach(function (edge) {
        edge.from.forEach(function (ref, i) {
          if (ref.table === tableId && ref.column === column) hits.push({ outputId: outputId, edge: edge, i: i });
        });
      });
    });
    panelShell(tableId + "." + column, hits.length + " 处引用");
    var body = h("div", "pbody");
    body.textContent = hits.length === 0
      ? "没有任何输出列引用它（可能只用在了过滤条件里）"
      : hits.map(function (hit) { return "→ " + byId.get(hit.outputId).label + "." + hit.edge.name; }).join("\n");
    panel.append(body);
    var targetCols = detail.querySelectorAll('[data-table="' + tableId + '"]');
    targetCols.forEach(function (el) { el.classList.toggle("hot", el.dataset.col === column); });
  }

  function showFilterPanel(outputId, filter) {
    panelShell(filter.name, byId.get(outputId) ? byId.get(outputId).label : outputId);
    var body = h("div", "pbody");
    body.textContent = filter.formula;
    panel.append(body);
    addSources(panel, filter.from, -1);
  }

  /* ---------------- 路由 ---------------- */
  function route() {
    var m = location.hash.match(/^#\/out\/([^/]+)(?:\/(.+))?$/);
    if (!m) return renderLevel1();
    var outputId = decodeURIComponent(m[1]);
    var column = m[2] ? decodeURIComponent(m[2]) : null;
    renderLevel2(outputId, column);
  }

  function redraw() {
    if (!view1.hidden) drawTableEdges();
    else drawColumnEdges();
  }

  var timer = null;
  window.addEventListener("resize", function () {
    clearTimeout(timer);
    timer = setTimeout(redraw, 80);
  });
  window.addEventListener("hashchange", route);

  var back = document.getElementById("back");
  if (back) back.addEventListener("click", function () { location.hash = ""; renderLevel1(); });

  var caseEl = document.getElementById("case");
  if (caseEl) {
    caseEl.textContent = data.case.title + " · AS OF " + (data.case.asOf || "—");
    caseEl.title = "血缘来源：" + data.case.provenance;
  }

  route();

  /* 字体晚到时卡片高度会变，连线要跟着重画一次。 */
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { redraw(); });
})();
