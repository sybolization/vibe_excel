/* book 的前端：一张可以拖动和缩放的图纸 + 一条底栏。只认 #book-data 里的视图模型，不碰磁盘。 */
(() => {
  const DATA = JSON.parse(document.getElementById("book-data").textContent);
  const root = document.getElementById("root");

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  const pad = (n) => String(n).padStart(2, "0");
  /** 设备里有些时间戳是文件名格式（2026-10-30T01-44-23-838Z），先掰回 ISO 再解析。 */
  const parseStamp = (iso) => {
    const text = String(iso).replace(/^(.*T\d{2})-(\d{2})-(\d{2})-(\d{3})Z$/, "$1:$2:$3.$4Z");
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  const fmtTime = (iso) => {
    if (!iso) return "—";
    const date = parseStamp(iso);
    if (date === null) return "—";
    const now = new Date();
    const sameYear = date.getFullYear() === now.getFullYear();
    const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
    return sameYear ? `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${hm}` : `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  };
  const fmtNum = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US"));
  const fmtDur = (ms) => (ms === null || ms === undefined ? "—" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
  const fmtBytes = (n) => {
    if (n === null || n === undefined) return "—";
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${Math.round(n / 1024)} KB`;
    return `${Math.round(n / 1048576)} MB`;
  };
  const short = (value) => (value ? String(value).slice(0, 8) : "—");
  const baseName = (path) => (path ? String(path).split(/[\\/]/).pop() : null);

  const inputs = DATA.inputs;
  const graph = DATA.graph;
  const diagram = DATA.diagram;
  const nodes = diagram === null ? [] : diagram.nodes;
  const edges = diagram === null ? [] : diagram.edges;
  const nodeIndex = new Map(nodes.map((node, index) => [node.id, index]));
  const dbRef = baseName(inputs === null ? null : inputs.dbPath) ?? "db";

  /* ---------------- 索引 ---------------- */
  const tableRows = new Map();
  for (const file of inputs === null ? [] : inputs.files) {
    for (const row of file.direct.concat(file.derived)) tableRows.set(row.name, { row, file });
  }
  for (const row of inputs === null ? [] : inputs.orphans) {
    if (!tableRows.has(row.name)) tableRows.set(row.name, { row, file: null });
  }
  const fileRows = new Map((inputs === null ? [] : inputs.files).map((file) => [file.name, file]));
  const nodeByFn = new Map();
  for (const node of graph === null ? [] : graph.nodes) nodeByFn.set(node.fn, node);

  const state = { sel: null, tab: "run", chips: [], text: "" };

  /* ---------------- 排版 ---------------- */
  const WID = { file: 196, sheet: 156, table: 196, compute: 176 };
  const HT = { file: 44, sheet: 28, table: 28, compute: 40 };
  const ROW = 46;
  const GAP = 48;

  function layout(view) {
    const rankWidth = new Map();
    for (const node of view.nodes) rankWidth.set(node.rank, Math.max(rankWidth.get(node.rank) ?? 0, WID[node.kind]));
    const ranks = [...rankWidth.keys()].sort((a, b) => a - b);
    const colX = new Map();
    let cursorX = 0;
    for (const rank of ranks) {
      colX.set(rank, cursorX);
      cursorX += (rankWidth.get(rank) ?? 0) + GAP;
    }
    const width = Math.max(0, cursorX - GAP);

    const parents = new Map();
    for (const edge of view.edges) {
      if (!parents.has(edge.to)) parents.set(edge.to, []);
      parents.get(edge.to).push(edge.from);
    }

    const pos = new Map();
    for (const rank of ranks) {
      const list = view.nodes.filter((node) => node.rank === rank);
      const want = new Map();
      for (const node of list) {
        const ups = (parents.get(node.id) ?? []).map((id) => pos.get(id)).filter((item) => item !== undefined);
        want.set(node.id, ups.length === 0 ? null : ups.reduce((sum, item) => sum + item.y + item.h / 2, 0) / ups.length);
      }
      list.sort((a, b) => {
        const wa = want.get(a.id);
        const wb = want.get(b.id);
        if (wa === null && wb === null) return 0;
        if (wa === null) return 1;
        if (wb === null) return -1;
        return wa - wb;
      });
      let cursorY = 0;
      for (const node of list) {
        const w = WID[node.kind];
        const h = HT[node.kind];
        const probe = want.get(node.id);
        const y = Math.max(cursorY, probe === null ? cursorY : probe - h / 2);
        pos.set(node.id, { x: colX.get(rank) + (rankWidth.get(rank) - w) / 2, y, w, h });
        cursorY = y + Math.max(ROW, h + 14);
      }
    }

    let minY = Infinity;
    let maxY = -Infinity;
    for (const item of pos.values()) {
      minY = Math.min(minY, item.y);
      maxY = Math.max(maxY, item.y + item.h);
    }
    if (!Number.isFinite(minY)) {
      minY = 0;
      maxY = 0;
    }
    for (const item of pos.values()) item.y -= minY;
    return { pos, width, height: maxY - minY };
  }

  const L = diagram === null ? { pos: new Map(), width: 0, height: 0 } : layout(diagram);

  function edgePath(a, b) {
    const x1 = a.x + a.w;
    const y1 = a.y + a.h / 2;
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    if (Math.abs(y1 - y2) < 0.6) return `M${x1} ${y1}H${x2}`;
    const xm = Math.round((x1 + x2) / 2);
    return `M${x1} ${y1}H${xm}V${y2}H${x2}`;
  }

  /* ---------------- 节点 ---------------- */
  function subOf(node) {
    if (node.kind === "file") return fmtBytes(node.bytes);
    if (node.rows === null) return "";
    if (node.kind === "sheet") return `${fmtNum(node.rows)} 行`;
    return `${fmtNum(node.rows)}×${node.columns === null ? "?" : node.columns}`;
  }

  function nodeHtml(node, index) {
    const item = L.pos.get(node.id);
    if (item === undefined) return "";
    const cls = [
      "nd",
      node.kind === "file" ? "f" : node.kind === "sheet" ? "s" : node.kind === "compute" ? "c" : "t",
      node.run ? "run" : "",
      node.loose ? "loose" : "",
      state.sel === index ? "sel" : "",
    ].filter(Boolean).join(" ");
    const style = `left:${Math.round(item.x)}px;top:${Math.round(item.y)}px;width:${item.w}px;height:${item.h}px`;
    const menu = `<div class="menu"><button data-act="copy" data-i="${index}">复制引用</button><button data-act="add" data-i="${index}">加到聊天框</button></div>`;
    if (node.kind === "compute") {
      const dur = node.elapsedMs === null ? "" : `<span class="dur">${esc(fmtDur(node.elapsedMs))}</span>`;
      const loc = node.file === null ? "" : `<div class="lo">${esc(baseName(node.file))}</div>`;
      return `<div class="${cls}" style="${style}" data-i="${index}"><div class="hd"><span class="fn">${esc(node.label)}</span>${dur}</div>${loc}${menu}</div>`;
    }
    return `<div class="${cls}" style="${style}" data-i="${index}"><span class="nm" title="${esc(node.label)}">${esc(node.label)}</span><span class="x">${esc(subOf(node))}</span>${menu}</div>`;
  }

  /* ---------------- 画布 ---------------- */
  const view = { x: 0, y: 0, z: 1 };
  const cv = document.createElement("div");
  cv.className = "cv";

  const layer = document.createElement("div");
  layer.className = "cvv";
  if (diagram === null) {
    layer.innerHTML = `<div class="note" style="position:absolute;left:32px;top:32px">没有可画的来路：这个项目还没有输入记录</div>`;
  } else {
    const paths = edges.map((edge) => {
      const a = L.pos.get(edge.from);
      const b = L.pos.get(edge.to);
      if (a === undefined || b === undefined) return "";
      const cls = edge.run ? "run" : edge.kind === "loose" ? "loose" : "";
      return `<path d="${edgePath(a, b)}"${cls === "" ? "" : ` class="${cls}"`}/>`;
    }).join("");
    layer.innerHTML = `<svg width="${Math.max(1, L.width)}" height="${Math.max(1, L.height)}">${paths}</svg>${nodes.map(nodeHtml).join("")}`;
  }
  cv.appendChild(layer);

  function apply() {
    layer.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.z})`;
    const pct = document.getElementById("pct");
    if (pct !== null) pct.textContent = `${Math.round(view.z * 100)}%`;
  }

  /** 顶栏 52 + 底栏 248，剩下的才给图。 */
  const BAND = { top: 64, bottom: 264, side: 24 };

  function fit() {
    const availW = cv.clientWidth - BAND.side * 2;
    const availH = cv.clientHeight - BAND.top - BAND.bottom;
    const z = Math.min(1, availW / Math.max(1, L.width), availH / Math.max(1, L.height));
    view.z = Math.max(0.35, z);
    view.x = BAND.side + (availW - L.width * view.z) / 2;
    view.y = BAND.top + (availH - L.height * view.z) / 2;
    apply();
  }

  function zoomAt(factor, px, py) {
    const next = Math.min(2, Math.max(0.35, view.z * factor));
    if (next === view.z) return;
    view.x = px - (px - view.x) * (next / view.z);
    view.y = py - (py - view.y) * (next / view.z);
    view.z = next;
    apply();
  }

  cv.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = cv.getBoundingClientRect();
    zoomAt(Math.exp(-event.deltaY * 0.0015), event.clientX - rect.left, event.clientY - rect.top);
  }, { passive: false });

  let drag = null;
  cv.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    if (event.target.closest(".menu") !== null) return;
    drag = { x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, moved: false };
    cv.setPointerCapture(event.pointerId);
  });
  cv.addEventListener("pointermove", (event) => {
    if (drag === null) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) > 3) {
      drag.moved = true;
      cv.classList.add("drag");
    }
    if (!drag.moved) return;
    view.x = drag.vx + dx;
    view.y = drag.vy + dy;
    apply();
  });
  cv.addEventListener("pointerup", (event) => {
    const moved = drag === null ? true : drag.moved;
    drag = null;
    cv.classList.remove("drag");
    if (moved) return;
    if (event.target.closest(".menu") !== null) return;
    const node = event.target.closest(".nd");
    select(node === null ? null : Number(node.getAttribute("data-i")));
  });

  /* ---------------- 顶栏 / 底栏 ---------------- */
  const bar = document.createElement("div");
  bar.className = "bp";
  bar.innerHTML = `<div class="bp-hd" id="bphd"></div><div class="bp-bd" id="bpbd"></div><div class="bp-ft" id="bpft"></div>`;

  const topBar = document.createElement("div");
  topBar.className = "fbar";
  const sameRev = Boolean(DATA.currentRevision && DATA.lastRunRevision && DATA.currentRevision === DATA.lastRunRevision);
  const program = DATA.program === null ? "—" : DATA.program.name;
  const draft = DATA.session === null ? "" : DATA.session.draftBranch;
  topBar.innerHTML = `
    <div class="brand"><b>${esc(program)}</b><span>${esc(draft)}</span></div>
    <span class="rev">工作集 <b>${esc(short(DATA.currentRevision))}</b></span>
    <span class="rev">上次运行 <b>${esc(short(DATA.lastRunRevision))}</b></span>
    ${sameRev || DATA.lastRunRevision === null ? "" : '<span class="chip amber">数据已更新</span>'}
    <span class="rev">${DATA.revisions.length} 个数据版本 · ${DATA.runs.length} 次运行</span>
    <span class="grow"></span>
    <button class="btn off" disabled title="v1 只读">发布</button>`;

  const zoomBar = document.createElement("div");
  zoomBar.className = "fzoom";
  zoomBar.innerHTML = `<button data-zoom="out" title="缩小">−</button><span class="pct" id="pct">100%</span><button data-zoom="in" title="放大">+</button><button class="fit" data-zoom="fit">适应</button>`;

  const toastEl = document.createElement("div");
  toastEl.className = "toast";

  root.append(cv, topBar, zoomBar, bar, toastEl);

  /* ---------------- 文案 ---------------- */
  const rowText = (row) => (row === null || row.rows === null ? "—" : `${fmtNum(row.rows)} 行${row.columns === null ? "" : ` · ${row.columns} 列`}`);
  const usageText = (row) => {
    if (row === null || row.usage === null) return "没用到";
    return row.usage === "read" ? "读" : row.usage === "write" ? "写" : "读写";
  };

  function sampleHtml(sample, title) {
    if (sample === null) {
      return `<div class="pane grow"><div class="ilabel">${esc(title)}</div><div class="note">没有可读取的库</div></div>`;
    }
    const head = sample.columns.map((col) => `<th>${esc(col)}</th>`).join("");
    const body = sample.rows.map((cells) => `<tr>${cells.map((cell) => `<td>${esc(cell === null ? "" : cell)}</td>`).join("")}</tr>`).join("");
    return `
      <div class="pane grow">
        <div class="ilabel">${esc(title)}<span class="r">${esc(sample.name)}</span></div>
        <table class="tbl2"><thead><tr>${head}</tr></thead><tbody>${body}</tbody>
        <tfoot><tr><td colspan="${sample.columns.length}">${esc(fmtNum(sample.total))} 行 · ${esc(sample.columnTotal)} 列 · ${esc(sample.note)}</td></tr></tfoot></table>
      </div>`;
  }

  /** 本次运行的路径：物化表 →（节点 → 写出的表）… */
  function stations() {
    if (graph === null) return [];
    const list = [];
    const push = (id, label) => {
      const index = nodeIndex.get(id);
      if (index !== undefined) list.push({ index, label });
    };
    for (const item of graph.materialize) push(`t:${item.table}`, item.table);
    for (const node of graph.nodes) {
      const hit = nodes.find((item) => item.kind === "compute" && item.ref === node.fn) ?? null;
      if (hit !== null) push(hit.id, node.fn);
      for (const table of node.writes) push(`t:${table}`, table);
    }
    return list;
  }

  function headHtml() {
    const tabHtml = [
      ["run", "本次运行", false],
      ["sel", "选中", state.sel === null],
      ["chat", "会话", false],
    ].map(([id, label, off]) => `<button class="btab${state.tab === id ? " on" : ""}" data-tab="${id}"${off ? " disabled" : ""}>${label}</button>`).join("");
    const path = stations().map((item, index) => `${index === 0 ? "" : '<span class="sep">→</span>'}<button data-goto="${item.index}"${state.sel === item.index ? ' class="on"' : ""}>${esc(item.label)}</button>`).join("");
    const meta = graph === null
      ? "没有运行记录"
      : `运行 ${graph.runShort} · ${graph.identical === true ? "一致" : graph.identical === false ? "有变化" : "—"} · ${fmtDur(graph.elapsedMs)}`;
    return `${tabHtml}<div class="bp-path">${path}</div><span class="bp-meta">${esc(meta)}</span>`;
  }

  function runHtml() {
    if (graph === null) {
      return `<div class="pane grow"><div class="note">这次运行没有 run.json，画不出流程</div></div>`;
    }
    const reads = [...new Set(graph.nodes.flatMap((node) => node.reads))];
    const writes = [...new Set(graph.nodes.flatMap((node) => node.writes))];
    const steps = graph.nodes.reduce((sum, node) => sum + node.steps.length, 0);
    return `
      <div class="pane" style="width:460px;flex:none">
        <dl class="kv">
          <div><dt>一致</dt><dd class="${graph.identical === true ? "ok" : ""}">${graph.identical === null ? "—" : graph.identical ? "是 · 重跑输出未变" : "否 · 输出有变化"}</dd></div>
          <div><dt>代码</dt><dd>${esc(short(graph.codeCommit))}</dd></div>
          <div><dt>数据</dt><dd>${esc(short(graph.revision))}</dd></div>
          <div><dt>耗时</dt><dd>${esc(fmtDur(graph.elapsedMs))}</dd></div>
          <div><dt>节点</dt><dd>${graph.nodes.length} 个 · ${steps} 步</dd></div>
          <div><dt>as of</dt><dd>${esc(graph.asOf ?? "—")}</dd></div>
        </dl>
        <div class="iowrap"><span class="k">读</span><span class="v" title="${esc(reads.join(" · "))}">${esc(reads.join(" · ") || "—")}</span></div>
        <div class="iowrap"><span class="k">写</span><span class="v" title="${esc(writes.join(" · "))}">${esc(writes.join(" · ") || "—")}</span></div>
        <div class="note" style="margin-top:6px;white-space:normal">${esc(graph.entry)}</div>
      </div>
      ${sampleHtml(graph.sample, "产出")}`;
  }

  function tablePane(name) {
    const hit = tableRows.get(name) ?? null;
    const row = hit === null ? null : hit.row;
    const origin = row === null
      ? "库里有，但记录里追不到它是从哪来的"
      : row.kind === "materialize"
        ? `物化 · 运行 ${row.via}`
        : row.kind === "import"
          ? "导入 · 表里逐行记着 _source_file / _source_sheet"
          : row.kind === "derive"
            ? `派生 · ${row.writer === null ? row.via : row.writer.fn}（${row.from.join(" + ")}）`
            : "库里有，但记录里追不到它是从哪来的";
    const where = hit !== null && hit.file !== null ? `${hit.file.name}${row !== null && row.sheet !== null ? ` #${row.sheet}` : ""}` : "没有来源文件";
    const writer = row === null || row.writer === null ? "" : ` · 写出者 ${row.writer.fn}${row.writer.elapsedMs === null ? "" : ` · ${fmtDur(row.writer.elapsedMs)}`}`;
    return `
      <div class="pane" style="width:460px;flex:none">
        <div class="selh"><span class="nm">${esc(name)}</span><span class="st">${esc(rowText(row))}</span></div>
        <div class="note" title="${esc(where)}">${esc(where)}</div>
        <div class="note" title="${esc(origin)}">${esc(origin)}</div>
        <div class="note">本次运行：${row === null ? "—" : esc(usageText(row))}${esc(writer)}</div>
      </div>
      ${row === null ? `<div class="pane grow"><div class="note">没有这张表的取样</div></div>` : sampleHtml(row.sample, "取样")}`;
  }

  function filePane(name) {
    const file = fileRows.get(name) ?? null;
    if (file === null) return `<div class="pane grow"><div class="note">这个文件已经不在清单里</div></div>`;
    const all = file.direct.concat(file.derived);
    const list = all.map((row) => {
      const index = nodeIndex.get(`t:${row.name}`);
      return `<div class="link"${index === undefined ? "" : ` data-goto="${index}"`}>${esc(row.name)}<span class="x">${esc(rowText(row))}</span></div>`;
    }).join("");
    return `
      <div class="pane" style="width:460px;flex:none">
        <div class="selh"><span class="nm">${esc(name)}</span><span class="st">${esc(fmtBytes(file.bytes))}</span></div>
        <div class="note">${file.direct.length} 张物化/导入 · ${file.derived.length} 张派生${file.inBatch ? "" : " · 不在本次批次"}</div>
        <div class="note">${esc(file.sha256 === null ? "没有 sha256 记录" : `sha256 ${file.sha256.slice(0, 16)}…`)}</div>
      </div>
      <div class="pane grow">
        <div class="ilabel">变成的表<span class="r">${all.length} 张</span></div>
        ${list || '<div class="note">这个文件还没有物化记录</div>'}
      </div>`;
  }

  function sheetPane(ref) {
    const parts = ref.split("\u0000");
    const file = parts[0] ?? "";
    const sheet = parts[1] ?? "";
    const fileRow = fileRows.get(file) ?? null;
    const rows = fileRow === null ? [] : fileRow.direct.filter((row) => row.sheet === sheet);
    const list = rows.map((row) => {
      const index = nodeIndex.get(`t:${row.name}`);
      return `<div class="link"${index === undefined ? "" : ` data-goto="${index}"`}>${esc(row.name)}<span class="x">${esc(rowText(row))}</span></div>`;
    }).join("");
    return `
      <div class="pane" style="width:460px;flex:none">
        <div class="selh"><span class="nm">#${esc(sheet)}</span><span class="st">${rows.length} 张表</span></div>
        <div class="note" title="${esc(file)}">${esc(file)}</div>
      </div>
      <div class="pane grow">
        <div class="ilabel">这个 sheet 变成的表<span class="r">${rows.length}</span></div>
        ${list || '<div class="note">这个 sheet 没有物化记录</div>'}
      </div>`;
  }

  function computePane(index) {
    const node = nodes[index];
    if (node === undefined) return `<div class="pane grow"><div class="note">这个节点已经不在图里</div></div>`;
    const ups = edges.filter((edge) => edge.to === node.id).map((edge) => nodes[nodeIndex.get(edge.from) ?? -1]).filter((item) => item !== undefined);
    const downs = edges.filter((edge) => edge.from === node.id).map((edge) => nodes[nodeIndex.get(edge.to) ?? -1]).filter((item) => item !== undefined);
    const focused = nodeByFn.get(node.ref) ?? null;
    const steps = focused === null
      ? `<div class="note">这一步不在本次运行里，没有步骤记录</div>`
      : `<div class="steps">${focused.steps.map((step) => `
          <div class="step" data-fn="${esc(focused.fn)}" data-step="${esc(step.n)}" data-line="${esc(step.line)}">
            <span class="i">${esc(step.n)}</span><span class="tx">${esc(step.label)}</span><span class="ln">:${esc(step.line)}</span>
            <div class="menu"><button data-act="copy-step">复制引用</button><button data-act="add-step">加到聊天框</button></div>
          </div>`).join("")}</div>`;
    return `
      <div class="pane" style="width:460px;flex:none">
        <div class="selh"><span class="nm">${esc(node.label)}</span><span class="st">${esc(node.elapsedMs === null ? "" : fmtDur(node.elapsedMs))}</span></div>
        <div class="note">${esc(node.file === null ? "代码文件没有记录" : baseName(node.file))}${node.runShort === null ? "" : ` · 运行 ${esc(node.runShort)}`}</div>
        <div class="note" title="${esc(ups.map((item) => item.label).join(" · "))}">读 ${esc(ups.map((item) => item.label).join(" · ") || "—")}</div>
        <div class="note" title="${esc(downs.map((item) => item.label).join(" · "))}">写 ${esc(downs.map((item) => item.label).join(" · ") || "—")}</div>
      </div>
      <div class="pane grow"><div class="ilabel">步骤<span class="r">${focused === null ? "" : `${focused.steps.length} 步`}</span></div>${steps}</div>`;
  }

  function chatHtml() {
    if (DATA.transcript.length === 0) return `<div class="pane grow"><div class="note">这次运行没有对话记录</div></div>`;
    const hit = DATA.transcript.find((item) => item.id === DATA.focusEpisode);
    const episode = hit ?? DATA.transcript[DATA.transcript.length - 1];
    if (episode === undefined) return `<div class="pane grow"><div class="note">这次运行没有对话记录</div></div>`;
    return `
      <div class="pane grow">
        <div class="ilabel">会话<span class="r">${DATA.transcript.length} 段 · 工具调用 ${episode.toolCalls} 次 · 写入 ${episode.writes} 次 · ${esc(fmtTime(episode.start))}</span></div>
        <div class="line"><span class="who">我</span><span class="say" title="${esc(episode.user)}">${esc(episode.user || "（空）")}</span></div>
        <div class="line"><span class="who">模型</span><span class="say" title="${esc(episode.assistant)}">${esc(episode.assistant || "（这一段没有可见回复）")}</span></div>
      </div>`;
  }

  function bodyHtml() {
    if (state.tab === "chat") return chatHtml();
    if (state.tab === "sel") {
      if (state.sel === null) return `<div class="pane grow"><div class="note">点画布上的一个节点</div></div>`;
      const node = nodes[state.sel];
      if (node === undefined) return `<div class="pane grow"><div class="note">这个节点已经不在图里</div></div>`;
      if (node.kind === "table") return tablePane(node.ref);
      if (node.kind === "file") return filePane(node.ref);
      if (node.kind === "sheet") return sheetPane(node.ref);
      return computePane(state.sel);
    }
    return runHtml();
  }

  function footHtml() {
    const chips = state.chips.map((chip, index) => `<span class="ref">${esc(chip.label)}<button data-remove="${index}" title="移除">×</button></span>`).join("");
    const ready = state.chips.length > 0 || state.text.trim() !== "";
    return `<div class="box">${chips}<input id="compose" placeholder="${state.chips.length > 0 ? "补充一句…" : "把某一步加到聊天框…"}" value="${esc(state.text)}"></div>
      <button class="btn${ready ? " primary" : " off"}" data-act="copy-compose"${ready ? "" : " disabled"}>复制</button>`;
  }

  function paintHead() {
    document.getElementById("bphd").innerHTML = headHtml();
  }
  function paintBody() {
    document.getElementById("bpbd").innerHTML = bodyHtml();
  }
  function paintFoot() {
    document.getElementById("bpft").innerHTML = footHtml();
    const input = document.getElementById("compose");
    if (input !== null) input.addEventListener("input", (event) => { state.text = event.target.value; });
  }

  function select(index) {
    state.sel = index;
    for (const element of layer.querySelectorAll(".nd")) {
      element.classList.toggle("sel", Number(element.getAttribute("data-i")) === index);
    }
    if (index !== null) state.tab = "sel";
    else if (state.tab === "sel") state.tab = "run";
    paintHead();
    paintBody();
  }

  /* ---------------- 引用与动作 ---------------- */
  function refOf(index) {
    const node = nodes[index];
    if (node === undefined) return null;
    if (node.kind === "table") return { ref: `@${dbRef}:${node.ref}`, chip: node.ref };
    if (node.kind === "file") return { ref: `@${node.ref}`, chip: node.ref };
    if (node.kind === "sheet") {
      const parts = node.ref.split("\u0000");
      return { ref: `@${parts[0] ?? ""}#${parts[1] ?? ""}`, chip: `#${parts[1] ?? ""}` };
    }
    return { ref: node.file === null ? `@${node.label}` : `@${baseName(node.file)}`, chip: node.label };
  }

  function toast(message) {
    toastEl.textContent = message;
    toastEl.classList.add("on");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => toastEl.classList.remove("on"), 1600);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制");
    } catch {
      window.prompt("复制这段：", text);
    }
  }

  function addChip(chip) {
    if (!state.chips.some((item) => item.ref === chip.ref)) state.chips.push(chip);
    paintFoot();
    toast("已加到聊天框");
  }

  root.addEventListener("click", (event) => {
    const target = event.target;

    const remove = target.closest("[data-remove]");
    if (remove !== null) {
      state.chips.splice(Number(remove.getAttribute("data-remove")), 1);
      paintFoot();
      return;
    }

    const goto = target.closest("[data-goto]");
    if (goto !== null) {
      select(Number(goto.getAttribute("data-goto")));
      return;
    }

    const tab = target.closest("[data-tab]");
    if (tab !== null) {
      state.tab = tab.getAttribute("data-tab");
      paintHead();
      paintBody();
      return;
    }

    const zoomBtn = target.closest("[data-zoom]");
    if (zoomBtn !== null) {
      const kind = zoomBtn.getAttribute("data-zoom");
      if (kind === "fit") fit();
      else zoomAt(kind === "in" ? 1.2 : 1 / 1.2, cv.clientWidth / 2, cv.clientHeight / 2);
      return;
    }

    const act = target.closest("[data-act]");
    if (act === null) return;
    const name = act.getAttribute("data-act");

    if (name === "copy-compose") {
      const line = state.chips.map((chip) => chip.ref).join(" ");
      void copyText([line, state.text].filter((part) => part && part.trim() !== "").join(" ").trim());
      return;
    }

    if (name === "copy-step" || name === "add-step") {
      const step = act.closest(".step");
      if (step === null) return;
      const fn = step.getAttribute("data-fn");
      const line = step.getAttribute("data-line");
      const focused = nodeByFn.get(fn);
      const ref = `@${focused === undefined || focused === null ? fn : baseName(focused.file)}:${line}`;
      if (name === "copy-step") void copyText(ref);
      else addChip({ ref, label: `${fn} · 步骤 ${step.getAttribute("data-step")}` });
      return;
    }

    const raw = act.getAttribute("data-i");
    if (raw === null) return;
    const found = refOf(Number(raw));
    if (found === null) return;
    if (name === "copy") void copyText(found.ref);
    else if (name === "add") addChip(found);
  });

  /* ---------------- 起来 ---------------- */
  paintHead();
  paintBody();
  paintFoot();
  fit();
  // 选中可以带在 URL 上：#i=12
  const initial = /^#i=(\d+)$/.exec(location.hash);
  if (initial !== null) {
    const index = Number(initial[1]);
    if (nodes[index] !== undefined) select(index);
  }
  window.addEventListener("resize", fit);
})();
