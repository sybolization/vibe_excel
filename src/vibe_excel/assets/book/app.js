/* 样张：左侧「来源」托盘 + 玻璃 chrome，画布沿用现在的排法。 */
(() => {
  const V = JSON.parse(document.getElementById("book-data").textContent);
  /* 站点模式把「从哪来」写在 <body> 上：有上一级就在顶栏给返回入口。
     单页模式下这个属性不在，行为与以前完全一致。 */
  const HOME = document.body.dataset.home ?? "";
  const D = V.diagram;
  /* 画布只画骨架：文件 → sheet → 机器 → 产物。
     原料表并进它的 sheet（sheet 卡片上多一行「产出：表名」），
     中间产物并成机器→机器的一条边，孤儿表不入图。 */
  const DIAGRAM = D === null ? { nodes: [], edges: [] } : D;
  const rawById = new Map((DIAGRAM.nodes ?? []).map((n) => [n.id, n]));
  const R = reduceDiagram(D === null ? { nodes: [], edges: [] } : D);
  const N = R.nodes;
  const E = R.edges;
  const G = V.graph;
  const I = V.inputs;
  /* 页面是服务托管的，还是 build 出来的静态文件？
     静态文件没有后台，导入/删除只能置灰——把能力缺失说清楚，好过让人以为点了会生效。 */
  const RT = (() => {
    const el = document.getElementById("book-runtime");
    if (el === null) return { mode: "static" };
    try { return JSON.parse(el.textContent); } catch { return { mode: "static" }; }
  })();
  const LIVE = RT.mode === "live" && RT.editable === true;
  /* 运行态里的输入快照：约定放在 inputs 下，但也容忍直接把 state 摊平传进来——
     一旦这里判错，整条链路会被误判成「已移出」，所以两种形状都认。
     注意这是函数不是常量：每次操作后 RT.inputs 会被换成服务端的新快照。 */
  const ground = () => {
    if (!LIVE) return null;
    if (RT.inputs !== undefined && RT.inputs !== null) return RT.inputs;
    return RT.files !== undefined ? RT : null;
  };
  const q = new URLSearchParams(location.search);
  /* 主题：?t= 显式指定则听它；否则跟随系统。默认浅色。 */
  const THEME_KEY = "vibe-book-theme";
  const readTheme = () => {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch {
      // file:// 或隐私模式下存储可能被禁；读不到就当没存过
      return null;
    }
  };
  const storeTheme = (value) => {
    try {
      localStorage.setItem(THEME_KEY, value);
    } catch {
      // 存不下不影响本次使用，主题只活在这个页面里
    }
  };
  const applyTheme = (value) => {
    document.documentElement.dataset.theme = value === "dark" ? "dark" : "light";
  };
  const prefersDark = window.matchMedia !== undefined && window.matchMedia("(prefers-color-scheme: dark)").matches;
  /* 优先级：显式 ?t= > 上次手动选的 > 系统偏好。默认浅色。 */
  applyTheme(q.get("t") ?? readTheme() ?? (prefersDark ? "dark" : "light"));
  const toggleTheme = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
    storeTheme(next);
  };

  const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const num = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US"));
  const dur = (v) => (v === null || v === undefined ? "—" : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(1)} s`);
  const kb = (n) => (n === null || n === undefined
    ? "—"
    : n < 1024
      ? `${Math.round(n)} B`
      : n < 1048576
        ? `${Math.round(n / 1024)} KB`
        : `${Math.round(n / 1048576)} MB`);
  const short = (v) => (v ? String(v).slice(0, 8) : "—");

  /* 中文名表：标签 → 中文。没有就回落到标签本身。 */
  const cnOf = new Map();
  for (const n of N) if (n.name !== undefined && n.name !== "") cnOf.set(n.label, n.name);
  const show = (v) => cnOf.get(v) ?? v;
  const byId = new Map(N.map((n) => [n.id, n]));
  const idx = new Map(N.map((n, i) => [n.id, i]));
  const downs = new Map();
  const ups = new Map();
  for (const e of E) {
    if (!downs.has(e.from)) downs.set(e.from, []);
    downs.get(e.from).push(byId.get(e.to));
    if (!ups.has(e.to)) ups.set(e.to, []);
    ups.get(e.to).push(e.from);
  }

  /* 四种剪影，不再是一个宽度乘四份。文件竖着（纸是竖的）、sheet 窄（一页夹签）、
     表横（表是横的）、机器只是一枚环骑在线上。
     长宽比按各自的内容定，不追求统一——统一才是"长条"和"单调"的来源。 */
  const WID = { file: 176, sheet: 154, table: 156, compute: 150 };
  const HT = { file: 68, sheet: 60, table: 60, compute: 54 };
  const RING_CY = 8; // 环心距节点顶端的距离；连线从这里穿过
  const GAP = 58;
  const ROW = 40;


  function reduceDiagram(d) {
    const nodes = d.nodes ?? [];
    const edges = d.edges ?? [];
    const ins = new Map();
    const outs = new Map();
    for (const e of edges) {
      if (!ins.has(e.to)) ins.set(e.to, []);
      if (!outs.has(e.from)) outs.set(e.from, []);
      ins.get(e.to).push(e);
      outs.get(e.from).push(e);
    }

    const tables = nodes.filter((n) => n.kind === "table");
    const madeBy = new Map();     // 原料表 ← 哪个 sheet
    const writtenBy = new Map();  // 任何表 ← 哪台机器
    for (const t of tables) {
      const make = (ins.get(t.id) ?? []).find((e) => e.kind === "make");
      if (make !== undefined) madeBy.set(t.id, make.from);
      const write = (ins.get(t.id) ?? []).find((e) => e.kind === "write");
      if (write !== undefined) writtenBy.set(t.id, write.from);
    }

    const folded = new Set();
    const sheetOut = new Map();
    for (const t of tables) {
      if (madeBy.has(t.id)) {
        /* 原料表：并进 sheet */
        folded.add(t.id);
        sheetOut.set(madeBy.get(t.id), t);
        continue;
      }
      if (!writtenBy.has(t.id)) {
        /* 既没来源也没 writer：孤儿，不入图 */
        folded.add(t.id);
        continue;
      }
      const consumed = (outs.get(t.id) ?? []).some((e) => e.kind === "read");
      if (consumed) folded.add(t.id); // 中间产物：并成机器直连
    }

    const kept = nodes.filter((n) => !folded.has(n.id));
    const keptIds = new Set(kept.map((n) => n.id));

    /* 边：原样的保留；落在被收起表上的，把端点换掉 */
    const mapped = [];
    const push = (from, to, kind, run) => {
      if (from === to || !keptIds.has(from) || !keptIds.has(to)) return;
      mapped.push({ from, to, kind, run });
    };
    const writerOf = (tableId) => writtenBy.get(tableId) ?? null;
    const sheetOf = (tableId) => madeBy.get(tableId) ?? null;

    for (const e of edges) {
      switch (e.kind) {
        case "contain": // 文件 → sheet
        case "write":   // 机器 → 表（产物保留；中间/原料已被收起）
          push(e.from, e.to, e.kind, e.run);
          break;
        case "make":    // sheet → 原料表：收进 sheet 卡片，不成边
          break;
        case "read": {  // 表 → 机器：换成它的来源
          const from = sheetOf(e.from) ?? writerOf(e.from);
          if (from !== null) push(from, e.to, "read", e.run);
          break;
        }
        default:
          push(e.from, e.to, e.kind, e.run);
      }
    }

    /* 去重（同一个端点只留一条），并去掉被收起链条造成的重复 */
    const seen = new Set();
    const reducedEdges = [];
    for (const e of mapped) {
      const key = e.from + "\u0000" + e.to;
      if (seen.has(key)) {
        const prev = reducedEdges.find((x) => x.from + "\u0000" + x.to === key);
        if (prev !== undefined && e.run === true) prev.run = true;
        continue;
      }
      seen.add(key);
      reducedEdges.push(e);
    }

    /* 按收起的图重算 rank */
    const rank = new Map(kept.map((n) => [n.id, 0]));
    for (let pass = 0; pass < kept.length; pass += 1) {
      let changed = false;
      for (const e of reducedEdges) {
        const a = rank.get(e.from);
        const b = rank.get(e.to);
        if (a === undefined || b === undefined) continue;
        if (b < a + 1) {
          rank.set(e.to, a + 1);
          changed = true;
        }
      }
      if (!changed) break;
    }

    const reducedNodes = kept.map((n) => {
      const out = sheetOut.get(n.id);
      return {
        ...n,
        rank: rank.get(n.id) ?? 0,
        out: out === undefined ? null : { label: out.label, name: out.name ?? "" },
      };
    });

    return { nodes: reducedNodes, edges: reducedEdges };
  }

  function layout() {
    const rankW = new Map();
    for (const n of N) rankW.set(n.rank, Math.max(rankW.get(n.rank) ?? 0, WID[n.kind]));
    const ranks = [...rankW.keys()].sort((a, b) => a - b);
    const colX = new Map();
    let x = 0;
    for (const r of ranks) {
      colX.set(r, x);
      x += rankW.get(r) + GAP;
    }
    const parents = new Map();
    for (const e of E) {
      if (!parents.has(e.to)) parents.set(e.to, []);
      parents.get(e.to).push(e.from);
    }
    const pos = new Map();
    const mid = (ids) => {
      const ys = ids.map((id) => pos.get(id)).filter(Boolean).map((p) => p.y + p.h / 2);
      return ys.length === 0 ? null : ys.reduce((a, b) => a + b, 0) / ys.length;
    };
    for (const r of ranks) {
      const list = N.filter((n) => n.rank === r);
      const want = new Map(list.map((n) => [n.id, mid(parents.get(n.id) ?? [])]));
      list.sort((a, b) => {
        const wa = want.get(a.id);
        const wb = want.get(b.id);
        if (wa === null && wb === null) return 0;
        if (wa === null) return 1;
        if (wb === null) return -1;
        return wa - wb;
      });
      let y = 0;
      for (const n of list) {
        const w = WID[n.kind];
        const h = HT[n.kind];
        const wv = want.get(n.id);
        const yy = Math.max(y, wv === null ? y : wv - h / 2);
        pos.set(n.id, {
          x: colX.get(r) + (rankW.get(r) - w) / 2,
          y: yy,
          w,
          h,
          cy: n.kind === "compute" ? RING_CY : h / 2,
          /* 机器的"接线柱"是那枚环本身，不是这个隐形盒子的右边。
             连线接到环上，才像一颗珠子穿在线上，而不是悬空断掉。 */
          xl: n.kind === "compute" ? colX.get(r) + (rankW.get(r) - w) / 2 + w / 2 - 8 : undefined,
          xr: n.kind === "compute" ? colX.get(r) + (rankW.get(r) - w) / 2 + w / 2 + 8 : undefined,
        });
        y = yy + Math.max(ROW, h + 18);
      }
    }
    let minY = Infinity;
    let maxY = -Infinity;
    for (const p of pos.values()) {
      minY = Math.min(minY, p.y);
      maxY = Math.max(maxY, p.y + p.h);
    }
    for (const p of pos.values()) p.y -= minY;
    return { pos, width: x - GAP, height: maxY - minY };
  }

  const L = D === null ? { pos: new Map(), width: 0, height: 0 } : layout();

  /* 连线接到每个节点的"轴"上：卡片是垂直中线，机器是那枚环的圆心。 */
  const axisY = (p) => p.y + (p.cy === undefined ? p.h / 2 : p.cy);

  const edgePath = (a, b) => {
    const x1 = a.xr === undefined ? a.x + a.w : a.xr;
    const y1 = axisY(a);
    const x2 = b.xl === undefined ? b.x : b.xl;
    const y2 = axisY(b);
    if (Math.abs(y1 - y2) < 0.6) return `M${x1} ${y1}H${x2}`;
    const xm = Math.round((x1 + x2) / 2);
    return `M${x1} ${y1}H${xm}V${y2}H${x2}`;
  };

  const subOf = (n) => (n.kind === "file" ? kb(n.bytes) : n.rows === null ? "" : n.kind === "sheet" ? `${num(n.rows)} 行` : `${num(n.rows)}×${n.columns === null ? "?" : n.columns}`);

  function nodeHtml(n) {
    const p = L.pos.get(n.id);
    if (p === undefined) return "";
    const i = idx.get(n.id);
    const st = `left:${Math.round(p.x)}px;top:${Math.round(p.y)}px;width:${p.w}px;height:${p.h}px`;
    const cls = `nd ${n.kind === "file" ? "f" : n.kind === "sheet" ? "s" : n.kind === "compute" ? "c" : "t"}${n.run ? " run" : ""}${n.loose ? " loose" : ""}`;
    if (n.kind === "compute") {
      const tip = n.file === null ? n.label : `${n.label} · ${n.file.split(/[\\/]/).pop()}`;
      const cn = n.name === undefined || n.name === "" ? n.label : n.name;
      /* 中文名和标识符都要：名字在上，英文标识与耗时在下，用一条 · 串起来。
         还没有中文名时只留耗时，不然同一行会重复同一个词。 */
      /* 有中文名时，英文标识只放 tooltip：画布上要读的是「这一步做什么」，
         函数名在底栏的运行路径里已经有了，不必在每颗环下面再抄一遍。 */
      const meta = dur(n.elapsedMs);
      return `<div class="${cls}" style="${st}" data-i="${i}" title="${esc(tip)}"><span class="ring"></span><span class="cap"><span class="cn">${esc(cn)}</span><span class="meta">${esc(meta)}</span></span></div>`;
    }
    const cn = n.name === undefined || n.name === "" ? n.label : n.name;
    const tip = cn === n.label ? n.label : `${cn} · ${n.label}`;
    /* sheet 上多挂一行「造出哪张表」：原料表不再是卡片，但它不能凭空消失。 */
    const out = n.out === null || n.out === undefined ? "" : `<span class="out" title="${esc(n.out.label)}">→ ${esc(n.out.name === "" ? n.out.label : n.out.name)}</span>`;
    return `<div class="${cls}" style="${st}" data-i="${i}" title="${esc(tip)}"><span class="nm">${esc(cn)}</span><span class="x">${esc(subOf(n))}</span>${out}</div>`;
  }

  /* ---------- 画布 ---------- */
  const root = document.getElementById("root");
  const cv = document.createElement("div");
  cv.className = "cv";
  const layer = document.createElement("div");
  layer.className = "cvv";
  const paths = E.map((e) => {
    const a = L.pos.get(e.from);
    const b = L.pos.get(e.to);
    if (a === undefined || b === undefined) return "";
    const cls = e.run ? "run" : e.kind === "loose" ? "loose" : "";
    return `<path d="${edgePath(a, b)}" data-a="${idx.get(e.from)}" data-b="${idx.get(e.to)}"${cls === "" ? "" : ` class="${cls}"`}/>`;
  }).join("");
  layer.innerHTML = `<svg width="${Math.max(1, L.width)}" height="${Math.max(1, L.height)}">${paths}</svg>${N.map(nodeHtml).join("")}`;
  cv.appendChild(layer);
  root.appendChild(cv);
  for (const cls of ["t", "b", "l"]) {
    const edge = document.createElement("div");
    edge.className = `edge ${cls}`;
    root.appendChild(edge);
  }

  const view = { x: 0, y: 0, z: 1 };
  const apply = () => {
    layer.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.z})`;
    const pct = document.getElementById("pct");
    if (pct !== null) pct.textContent = `${Math.round(view.z * 100)}%`;
  };
  const BAND = { left: 264, top: 76, bottom: 268, right: 28 };
  /* 对话区要能改画布的可视带宽，也要能知道当前选中了哪个节点。 */
  window.__BOOK = {
    setGutter(px) {
      BAND.right = Math.round(px);
      fit();
    },
  };

  const fit = () => {
    const availW = cv.clientWidth - BAND.left - BAND.right;
    const availH = cv.clientHeight - BAND.top - BAND.bottom;
    view.z = Math.max(0.3, Math.min(1, availW / Math.max(1, L.width), availH / Math.max(1, L.height)));
    view.x = BAND.left + (availW - L.width * view.z) / 2;
    view.y = BAND.top + (availH - L.height * view.z) / 2;
    apply();
  };
  const zoomAt = (f, px, py) => {
    const next = Math.min(2, Math.max(0.3, view.z * f));
    if (next === view.z) return;
    view.x = px - (px - view.x) * (next / view.z);
    view.y = py - (py - view.y) * (next / view.z);
    view.z = next;
    apply();
  };
  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  let drag = null;
  cv.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    /* 命中在按下时就记下来：setPointerCapture 之后，pointerup 的 target
       会变成画布本身，那时候再 closest(".nd") 永远是 null——真人点永远不生效，
       合成事件因为 setPointerCapture 抛错反而"看起来能用"。 */
    drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false, hit: e.target.closest(".nd") };
    try { cv.setPointerCapture(e.pointerId); } catch { /* 合成事件没有真实指针 */ }
  });
  cv.addEventListener("pointermove", (e) => {
    if (drag === null) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (Math.hypot(dx, dy) > 3) {
      drag.moved = true;
      cv.classList.add("drag");
    }
    view.x = drag.vx + dx;
    view.y = drag.vy + dy;
    apply();
  });
  cv.addEventListener("pointerup", (e) => {
    const pending = drag;
    drag = null;
    cv.classList.remove("drag");
    if (pending === null || pending.moved) return;
    if (pending.hit !== null) {
      selectNode(N[Number(pending.hit.getAttribute("data-i"))]);
      return;
    }
    // 点空白处取消高亮，整张图回到常态
    selNode.id = null;
    applyLit(null, null);
    paintBar();
  });

  /* ---------- 顶栏 ---------- */
  const top = document.createElement("div");
  top.className = "glass fbar";
  const same = Boolean(V.currentRevision && V.lastRunRevision && V.currentRevision === V.lastRunRevision);
  top.innerHTML = `
    <div class="brand">${HOME === "" ? "" : `<a class="home" href="${esc(HOME)}" title="返回项目">←</a>`}<b>${esc(V.program === null ? "—" : V.program.name)}</b><span>${esc(V.session === null ? "" : V.session.draftBranch)}</span></div>
    <span class="rev">工作集 <b>${esc(short(V.currentRevision))}</b></span>
    <span class="rev">上次运行 <b>${esc(short(V.lastRunRevision))}</b></span>
    ${same || V.lastRunRevision === null ? "" : '<span class="chip note">数据已更新</span>'}
    <span class="rev">${V.revisions.length} 个数据版本 · ${V.runs.length} 次运行</span>
    <span class="grow"></span>
    <button class="iconbtn" data-act="theme" title="切换深色 / 浅色" aria-label="切换深色 / 浅色">
      <svg class="i-moon" viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M13.4 9.8A5.7 5.7 0 0 1 6.2 2.6a5.7 5.7 0 1 0 7.2 7.2z"/></svg>
      <svg class="i-sun" viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1"/></svg>
    </button>
    <button class="btn off" disabled>发布</button>`;

  /* ---------- 左侧托盘 ---------- */
  const files = (I === null ? [] : I.files) || [];
  const tray = document.createElement("div");
  tray.className = "tray";
  /* 托盘分两类来源：已在批次里的（画布上有它）、只在 ground-truth 里的（还没人处理）。 */
  const batchFiles = () => (I === null ? [] : I.files) || [];
  const groundFiles = () => {
    const state = ground();
    return state === null ? [] : state.files || [];
  };
  /* 批次里有、ground-truth 里已经没有的文件：页面据此把整条链路标灰，不靠会话状态。 */
  const staleFiles = () => {
    if (!LIVE) return new Set();
    const alive = new Set(groundFiles().filter((f) => !f.missing).map((f) => f.name));
    const batch = batchFiles().filter((f) => f.inBatch);
    // 目录里一个都不认得 → 是指错了 ground-truth，不是「五个文件全被移出」。
    // 宁可什么都不标灰，也不能把所有链路判成失效。
    if (batch.length > 0 && !batch.some((f) => alive.has(f.name))) return new Set();
    return new Set(batch.filter((f) => !alive.has(f.name)).map((f) => f.name));
  };
  const trayRows = () => {
    const stale = staleFiles();
    const inBatch = new Set(batchFiles().map((f) => f.name));
    const rows = [];
    batchFiles().forEach((f, i) => {
      const all = f.direct.concat(f.derived);
      const sheets = [...new Set(f.direct.map((r) => r.sheet).filter((s) => s !== null))];
      const meta = stale.has(f.name)
        ? "已移出输入 · 重建批次前不可用"
        : `${sheets.length} 个 sheet · ${all.length} 张表 · ${kb(f.bytes)}`;
      rows.push(`<div class="row${stale.has(f.name) ? " stale" : ""}" data-f="${i}">
        <span class="t"><span class="n" title="${esc(f.name)}">${esc(f.name)}</span><span class="m">${esc(meta)}</span></span>
        ${LIVE ? `<span class="del" data-del="${esc(f.name)}" title="移出这个文件">×</span>` : ""}
      </div>`);
    });
    if (LIVE) {
      for (const f of groundFiles()) {
        if (inBatch.has(f.name)) continue;
        rows.push(`<div class="row local" data-p="${esc(f.name)}">
          <span class="t"><span class="n" title="${esc(f.name)}">${esc(f.name)}</span><span class="m">${kb(f.bytes)} · 待处理</span></span>
          <span class="del" data-del="${esc(f.name)}" title="移出这个文件">×</span>
        </div>`);
      }
    }
    return rows.join("");
  };
  const paintTray = () => {
    const list = tray.querySelector(".list");
    if (list === null) return;
    list.innerHTML = trayRows();
    const total = new Set([...batchFiles().map((f) => f.name), ...groundFiles().map((f) => f.name)]);
    const count = tray.querySelector(".cnt");
    if (count !== null) count.textContent = `${total.size} 个文件`;
    const undo = tray.querySelector('[data-act="undo"]');
    if (undo !== null) {
      const state = ground();
      const ready = LIVE && state !== null && state.canUndo === true;
      undo.disabled = !ready;
      undo.title = ready ? `撤回：把 ${state.undoFile ?? "上一个文件"} 放回输入` : "没有可撤回的移出";
    }
  };
  tray.innerHTML = `
    <div class="hd"><b>来源</b><span class="cnt"></span>
      <button class="imp" title="${LIVE ? "导入文件（也可以直接拖进来）" : "需要从 start.ps1 启动服务才能导入"}"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M6 2.5v7M2.5 6h7"/></svg></button>
      <button class="fold" title="收起"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 2.5 4 6l3.5 3.5"/></svg></button>
    </div>
    <div class="list"></div>
    ${LIVE ? '<div class="tray-ft"><button class="tbtn" data-act="undo">撤回</button><button class="tbtn" data-act="rebuild">重建输入批次</button></div>' : ""}
    <input class="pick" type="file" multiple hidden>`;
  if (!LIVE) tray.querySelector(".imp").classList.add("off");

  /* ---------- 底栏 ---------- */
  const bar = document.createElement("div");
  bar.className = "glass bp";

  const stations = () => {
    if (G === null) return [];
    const list = [];
    for (const m of G.materialize) list.push({ label: show(m.table) });
    for (const n of G.nodes) {
      list.push({ label: show(n.fn) });
      for (const t of n.writes) list.push({ label: show(t) });
    }
    return list;
  };

  /* 每张表在 inputs 里的那一行：行×列、来自哪个 sheet、谁写出来的。 */
  const tableRows = new Map();
  if (V.inputs !== undefined && V.inputs !== null) {
    for (const f of V.inputs.files) {
      for (const r of f.direct.concat(f.derived)) tableRows.set(r.name, { ...r, fileName: f.name });
    }
    for (const r of V.inputs.orphans) if (!tableRows.has(r.name)) tableRows.set(r.name, r);
  }

  /* 选中的是"画布上的哪个节点"，不再是"第几个文件"：
     表、机器、文件都点得动，底栏跟着换内容。 */
  const selNode = { id: null };
  const sel = { value: 0 };

  function filePane(f) {
    const all = f.direct.concat(f.derived);
    const links = all.map((r) => `<div class="link"><span class="nm">${esc(show(r.name))}</span><span class="x">${esc(r.rows === null ? "—" : `${num(r.rows)}×${r.columns}`)}</span></div>`).join("");
    const sheets = [...new Set(f.direct.map((r) => r.sheet).filter((s) => s !== null))];
    return `
      <div class="pane" style="width:330px;flex:none">
        <div class="selh"><span class="nm">${esc(f.name)}</span><span class="st">${esc(kb(f.bytes))}</span></div>
        <div class="note">${f.direct.length} 张物化/导入 · ${f.derived.length} 张派生 · ${f.inBatch ? "在本次批次" : "不在本次批次"}</div>
        <div class="note">sheet：${esc(sheets.join(" · ") || "—")}</div>
        <div class="note">${esc(f.sha256 === null ? "没有 sha256 记录" : `sha256 ${f.sha256.slice(0, 16)}…`)}</div>
      </div>
      <div class="pane grow">
        <div class="ilabel">被物化成<span class="r">${all.length} 张</span></div>
        <div class="links2">${links || '<div class="note">这个文件还没有物化记录</div>'}</div>
      </div>`;
  }

  function linkRow(label, value) {
    return `<div class="link"><span class="nm">${esc(label)}</span><span class="x">${esc(value)}</span></div>`;
  }

  function nodePane(n) {
    /* sheet 现在是画布上的一等节点：它物化成哪张表，写在它自己身上。 */
    if (n.kind === "sheet") {
      const out = n.out === null || n.out === undefined ? null : n.out;
      const row = out === null ? undefined : tableRows.get(out.label);
      const owner = String(n.ref ?? "").split("\u0000")[0];
      return `
        <div class="pane fix">
          <div class="selh"><span class="nm">${esc(n.label)}</span><span class="st">sheet</span></div>
          <div class="note">${row === undefined ? "没有行数记录" : `${num(row.rows)} 行 × ${row.columns} 列`}</div>
          <div class="note">来自 ${esc(owner === "" ? "—" : owner)}</div>
          <div class="note">物化成 ${out === null ? "—" : `<span class="idm">${esc(out.label)}</span>`}</div>
        </div>
        <div class="pane grow">
          <div class="ilabel">下游<span class="r">${E.filter((e) => e.from === n.id).length} 条</span></div>
          <div class="links2">${E.filter((e) => e.from === n.id).map((e) => linkRow(show(byId.get(e.to).label), "")).join("") || '<div class="note">这张 sheet 没有被任何一步读</div>'}</div>
        </div>`;
    }
    if (n.kind === "compute") {
      /* 读写从原图上的边读：本次运行没跑到的步骤不在 G.nodes 里，
         只查那里面会让「这一步读写了什么」变成两个 0。 */
      const edgeTables = (edges, side) =>
        edges
          .map((e) => rawById.get(e[side]))
          .filter((x) => x !== undefined && x.kind === "table")
          .map((x) => x.label);
      const allEdges = DIAGRAM.edges ?? [];
      const g = G === null ? undefined : G.nodes.find((x) => x.fn === n.label);
      const reads = g === undefined ? edgeTables(allEdges.filter((e) => e.to === n.id && e.kind === "read"), "from") : g.reads;
      const writes = g === undefined ? edgeTables(allEdges.filter((e) => e.from === n.id && e.kind === "write"), "to") : g.writes;
      /* 这一行要不要写「上次」：只有没在本次运行里跑过时才提，平时不必解释。 */
      const when = n.run === true ? `本次运行跑过 · ${dur(n.elapsedMs)}` : `本次运行没跑这一步 · 上次 ${dur(n.elapsedMs)}`;
      /* 这步到底干什么：run.json 里有函数的文档注释，页面以前从来没读过。
         没有注释时就退回「文件:函数」，至少能定位到代码。 */
      const doc = g === undefined || g.title === null || g.title === undefined ? "" : String(g.title).trim();
      const where = g === undefined ? String(n.file ?? "") : `${String(g.file).split(/[\\/]/).pop()}:${g.fn}`;
      return `
        <div class="pane fix">
          <div class="selh"><span class="nm">${esc(show(n.label))}</span></div>
          <div class="note">${when}</div>
          <div class="note mono">${esc(where === "" ? "没有文件记录" : where)}</div>
          ${doc === "" ? "" : `<div class="note doc">${esc(doc)}</div>`}
        </div>
        <div class="pane grow">
          <div class="ilabel">读<span class="r">${reads.length} 张</span></div>
          <div class="links2">${reads.map((t) => linkRow(show(t), show(t) === t ? "" : t)).join("") || '<div class="note">不读任何表</div>'}</div>
          <div class="ilabel">写<span class="r">${writes.length} 张</span></div>
          <div class="links2">${writes.map((t) => linkRow(show(t), show(t) === t ? "" : t)).join("") || '<div class="note">不写表</div>'}</div>
        </div>`;
    }

    const row = tableRows.get(n.label);
    const from = row === undefined || row.fileName === undefined ? null : row.fileName;
    const sheet = row === undefined || row.sheet === undefined ? null : row.sheet;
    const writer = row === undefined || row.writer === undefined ? null : row.writer;
    const upstream = row === undefined ? [] : row.from;
    return `
      <div class="pane fix">
        <div class="selh"><span class="nm">${esc(show(n.label))}</span></div>
        <div class="note">${n.rows === null ? "行数未知" : `${num(n.rows)} 行`}${n.columns === null ? "" : ` × ${n.columns} 列`}</div>
        <div class="note">${from === null ? (n.loose === true ? "没有上游，孤立的表" : "派生表，上游见右") : `来自 ${esc(from)}${sheet === null ? "" : ` · sheet ${esc(sheet)}`}`}</div>
        <div class="note">${writer === null || writer === undefined ? "不是任何节点写的（物化/导入）" : `由 ${esc(show(writer.fn))} 写出 · ${dur(writer.elapsedMs)}`}</div>
      </div>
      <div class="pane grow">
        <div class="ilabel">上游<span class="r">${upstream.length} 张</span></div>
        <div class="links2">${upstream.map((t) => linkRow(show(t), show(t) === t ? "" : t)).join("") || '<div class="note">没有上游</div>'}</div>
      </div>`;
  }

  /* ---------- 诊断：底栏里的第二个视图 ----------
     以前它是左下角一块独立浮层，和来源栏共用左边缘，1080p 上必然互相盖住。
     现在收进底栏：头部一个计数开关，点开把内容区换成告警列表，再点回到选中。
     失败行带完整 stderr——出错时人会把那段整段复制去问 agent，砍一刀就是
     让 agent 少看一截现场。 */
  const ALERTS = Array.isArray(V.alerts) ? V.alerts : [];
  const failures = ALERTS.filter((item) => item.level === "error").length;
  /* 底栏现在有两个视图：「选中」和「诊断」。开关就是这一个布尔值。
     ?diag=1 可以直接打开：截图和排查用得上，与 ?t= / ?focus= 是同一套约定。 */
  const DIAG = { open: new URLSearchParams(location.search).get("diag") === "1" };

  const alertRow = (item) => {
    const where = item.node === null || item.node === undefined ? "<span></span>" : `<span class="dg-node">${esc(item.node)}</span>`;
    const head = `<span class="dg-dot${item.level === "error" ? " bad" : ""}"></span><span class="dg-text">${esc(item.text)}</span>${where}`;
    if (item.detail === null || item.detail === undefined || item.detail === "") {
      return `<div class="dg-row${item.node === null || item.node === undefined ? "" : " linkable"}"${item.node === null || item.node === undefined ? "" : ` data-node="${esc(item.node)}"`} title="${item.node === null || item.node === undefined ? "" : "点一下聚焦到这个节点"}">${head}</div>`;
    }
    /* 有完整错误文本：默认展开，别让人再点一次才看见哪里坏了 */
    return `<details class="dg-row bad" open data-node="${esc(item.node ?? "")}"><summary>${head}</summary><pre class="dg-err">${esc(item.detail)}</pre><button class="dg-copy" type="button">复制错误</button></details>`;
  };

  function alertPane() {
    if (ALERTS.length === 0) {
      return '<div class="pane grow"><div class="note">没有告警</div></div>';
    }
    const rows = ALERTS.map(alertRow).join("");
    /* 头部已经写着「诊断 · N」了，这里只在真的有失败时补一句分账：
       全是提示的时候再写一遍「N 条提示」是重复。 */
    const tail = failures > 0 ? `<div class="note">${failures} 条失败 · ${ALERTS.length - failures} 条提示</div>` : "";
    return `<div class="pane grow"><div class="dg-list">${rows}</div>${tail}</div>`;
  }

  function paintBar() {
    const f = files[sel.value];
    const selected = selNode.id === null ? null : N.find((n) => n.id === selNode.id) ?? null;
    const path = stations().map((s, i) => `${i === 0 ? "" : '<span class="sep">→</span>'}<button>${esc(s.label)}</button>`).join("");
    const meta = G === null ? "没有运行记录" : `运行 ${esc(G.runShort)} · ${G.identical === true ? "一致" : G.identical === false ? "有变化" : "—"} · ${dur(G.elapsedMs)}`;
    const chain = staleChain();
    const gone = selected === null
      ? false
      : selected.kind === "table"
        ? chain.tables.has(selected.label)
        : selected.kind === "compute"
          ? incidentTables(selected.id).some((t) => chain.tables.has(t))
          : false;
    const goneNote = gone ? '<div class="bp-note">来源已移出输入，重建批次前不可用</div>' : "";
    const diagButton = ALERTS.length === 0
      ? ""
      : `<button class="bp-diag${DIAG.open === true ? " on" : ""}${failures > 0 ? " bad" : ""}" title="${failures > 0 ? `${failures} 条失败` : "没有失败，只有提示"}">诊断 · ${ALERTS.length}</button>`;
    const detail = DIAG.open === true
      ? alertPane()
      : selected !== null && selected.kind !== "file"
        ? nodePane(selected)
        : f === undefined
          ? '<div class="pane grow"><div class="note">点画布上的任意一个节点，或者左边的文件</div></div>'
          : filePane(f);
    bar.innerHTML = `
      <div class="bp-hd">
        <div class="bp-path">${path}</div>
        <span class="bp-meta">${esc(meta)}</span>
        ${diagButton}
      <button class="bp-fold" title="收起/展开"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 7.5 6 4l3.5 3.5"/></svg></button></div>
      ${DIAG.open === true ? "" : goneNote}<div class="bp-bd">${detail}</div>`;
  }

  /* ---------- 选中与聚焦 ---------- */
  /* 点一个节点，点亮的是它自己那条链：上下游都算，其余退到背景。
     之前这里传的是 null，等于什么都没高亮——画布上那条蓝色运行路径永远不变。 */
  function chainOf(id) {
    const lit = new Set([id]);
    const down = [id];
    while (down.length > 0) {
      for (const next of downs.get(down.pop()) ?? []) {
        if (next === undefined || next === null || lit.has(next.id)) continue;
        lit.add(next.id);
        down.push(next.id);
      }
    }
    const up = [id];
    const seen = new Set([id]);
    while (up.length > 0) {
      for (const prev of ups.get(up.pop()) ?? []) {
        if (prev === undefined || prev === null || seen.has(prev)) continue;
        seen.add(prev);
        lit.add(prev);
        up.push(prev);
      }
    }
    return lit;
  }

  function applyLit(lit, selectedId) {
    // 边记的是节点下标而不是 id：id 里带 \u0000，塞进 HTML 属性后会被解析成 U+FFFD，
    // 读回来和内存里的 id 永远对不上——这就是「点谁都是同一条路径」的根因。
    const litIdx = lit === null ? null : new Set([...lit].map((id) => idx.get(id)));
    for (const el of layer.querySelectorAll(".nd")) {
      const node = N[Number(el.getAttribute("data-i"))];
      el.classList.toggle("dim", lit !== null && !lit.has(node.id));
      el.classList.toggle("sel", node !== undefined && node.id === selectedId);
    }
    for (const el of layer.querySelectorAll("path")) {
      const a = Number(el.getAttribute("data-a"));
      const b = Number(el.getAttribute("data-b"));
      const litEdge = litIdx === null || (litIdx.has(a) && litIdx.has(b));
      el.style.opacity = litEdge ? "1" : "0.14";
      // 选中的链用一条更实的线画出来：不然「蓝的那条」和「我点的这条」会被看成两回事
      el.classList.toggle("hot", lit !== null && litEdge);
    }
  }

  function selectNode(n) {
    if (n === undefined) return;
    selNode.id = n.id;
    /* 文件节点仍然走原来的文件面板；表 / 机器走节点面板。 */
    if (n.kind === "file") {
      const index = files.findIndex((f) => `f:${f.name}` === n.id);
      if (index >= 0) sel.value = index;
    }
    applyLit(chainOf(n.id), n.id);
    for (const el of tray.querySelectorAll(".row")) {
      const f = files[Number(el.getAttribute("data-f"))];
      el.classList.toggle("on", f !== undefined && (n.kind === "file" ? `f:${f.name}` === n.id : f.direct.concat(f.derived).some((r) => r.name === n.label)));
    }
    paintBar();
    if (typeof window.__ON_SELECT === "function") window.__ON_SELECT(n);
  }

  function select(value, focus) {
    sel.value = value;
    const f = files[value];
    const nodeId = f === undefined ? null : `f:${f.name}`;
    selNode.id = nodeId;
    // 点文件也走同一套上下沿：文件和它喂出来的每一张表都算同一条链
    const lit = focus && nodeId !== null ? chainOf(nodeId) : null;
    applyLit(lit, nodeId);
    for (const el of tray.querySelectorAll(".row")) {
      el.classList.toggle("on", Number(el.getAttribute("data-f")) === value);
    }
    paintBar();
    if (typeof window.__ON_SELECT === "function") window.__ON_SELECT(nodeId === null ? null : N.find((n) => n.id === nodeId) ?? null);
  }

  /* ---------- 缩放条 ---------- */
  const zbar = document.createElement("div");
  zbar.className = "glass fzoom";
  zbar.innerHTML = `<button data-z="out">−</button><span class="pct" id="pct">100%</span><button data-z="in">+</button><button class="fit" data-z="fit">适应</button>`;

  root.append(top, tray, zbar, bar);

  /* ---------- 面板可以拖：标题栏是把手 ---------- */
  const draggable = (panel, selector) => {
    /* 用事件委托而不是绑在手柄上：底栏的头部每次 paintBar 都会重建，
       绑在具体元素上的话，重绘一次就失效了。 */
    panel.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest(selector) === null || e.target.closest("button") !== null) return;
      const rect = panel.getBoundingClientRect();
      const from = { x: e.clientX, y: e.clientY, l: rect.left, t: rect.top };
      try { panel.setPointerCapture(e.pointerId); } catch { /* 合成事件里没有真实指针，忽略 */ }
      const move = (ev) => {
        const left = Math.max(4, Math.min(window.innerWidth - 120, from.l + ev.clientX - from.x));
        const top = Math.max(4, Math.min(window.innerHeight - 48, from.t + ev.clientY - from.y));
        panel.style.left = `${Math.round(left)}px`;
        panel.style.top = `${Math.round(top)}px`;
        panel.style.right = "auto";
        panel.style.bottom = "auto";
      };
      const done = () => {
        panel.removeEventListener("pointermove", move);
        panel.removeEventListener("pointerup", done);
      };
      panel.addEventListener("pointermove", move);
      panel.addEventListener("pointerup", done);
    });
  };
  draggable(tray, ".hd");
  draggable(bar, ".bp-hd");
  bar.addEventListener("click", (e) => {
    if (e.target.closest(".bp-diag") !== null) {
      DIAG.open = !DIAG.open;
      paintBar();
      return;
    }
    const copy = e.target.closest(".dg-copy");
    if (copy !== null) {
      /* 整段 stderr 原样复制：出错时人要做的是把它粘进对话，不是自己重打 */
      const pre = copy.parentElement === null ? null : copy.parentElement.querySelector(".dg-err");
      const text = pre === null ? "" : pre.textContent ?? "";
      const done = () => note("错误文本已复制");
      if (navigator.clipboard === undefined || text === "") {
        note("这个浏览器不让复制，请手动选中", "bad");
        return;
      }
      navigator.clipboard.writeText(text).then(done).catch(() => note("复制失败，请手动选中", "bad"));
      return;
    }
    const row = e.target.closest(".dg-row");
    if (row !== null) {
      const id = row.getAttribute("data-node") ?? "";
      const node = N.find((n) => n.id === id);
      if (node !== undefined) {
        DIAG.open = false;
        selectNode(node);
      }
      return;
    }
    if (e.target.closest(".bp-fold") === null) return;
    bar.classList.toggle("folded");
  });

/* ---- 自检（只在 ?selftest 时跑）：拖动和收起是不是真的接上了 ---- */
/* 点一个节点：合成 pointerdown/up，走和真人点击同一条路径 */
const clickNode = (n) => {
  if (n === undefined || n === null) return null;
  const i = N.findIndex((x) => x.id === n.id);
  if (i < 0) return null;
  const el = cv.querySelector(`.nd[data-i="${i}"]`);
  if (el === null) return null;
  const r = el.getBoundingClientRect();
  const opts = { bubbles: true, pointerId: 9, isPrimary: true, button: 0, clientX: r.left + 8, clientY: r.top + 8 };
  el.dispatchEvent(new PointerEvent("pointerdown", opts));
  /* 抬起打在画布上，不是节点上——这是 setPointerCapture 之后的真实 target。 */
  cv.dispatchEvent(new PointerEvent("pointerup", opts));
  return N[i];
};

const wantSel = new URLSearchParams(location.search).get("sel");
if (wantSel !== null) {
  setTimeout(() => clickNode(N.find((x) => x.label === wantSel)), 300);
}

if (location.search.includes("selftest")) {
  setTimeout(() => {
    const out = [];
    const hd = tray.querySelector(".hd");
    const r = hd.getBoundingClientRect();
    const before = tray.getBoundingClientRect().left;
    hd.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 7, isPrimary: true, button: 0, clientX: r.left + 8, clientY: r.top + 8 }));
    tray.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 7, isPrimary: true, clientX: r.left + 88, clientY: r.top + 48 }));
    tray.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerId: 7, isPrimary: true, clientX: r.left + 88, clientY: r.top + 48 }));
    const after = tray.getBoundingClientRect().left;
    out.push(`drag dx=${Math.round(after - before)}`);

    tray.querySelector(".hd .fold").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    out.push(`trayFolded=${tray.classList.contains("collapsed")}`);
    tray.querySelector(".hd .fold").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    out.push(`trayUnfolded=${!tray.classList.contains("collapsed")}`);

    /* 诊断是底栏的第二个视图：点开要有行，再点要回到选中 */
    const diagBtn = bar.querySelector(".bp-diag");
    if (diagBtn === null) {
      out.push("diag=skip(no-alerts)");
    } else {
      diagBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      out.push(`diagOpen=${bar.querySelectorAll(".dg-row").length}`);
      bar.querySelector(".bp-diag").dispatchEvent(new MouseEvent("click", { bubbles: true }));
      out.push(`diagClosed=${bar.querySelectorAll(".dg-row").length === 0}`);
    }

    /* 表名被省略号截掉是这次要修的病：选中一个节点后逐个量一遍 */
    const clipped = Array.from(bar.querySelectorAll(".link .nm"))
      .filter((el) => el.scrollWidth > el.clientWidth + 1).length;
    out.push(`clipped=${clipped}/${bar.querySelectorAll(".link .nm").length}`);

    bar.querySelector(".bp-fold").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    out.push(`folded=${bar.classList.contains("folded")}`);
    bar.querySelector(".bp-fold").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    out.push(`unfolded=${!bar.classList.contains("folded")}`);

    /* 服务模式下拖放会真的写盘：自检不该往别人的数据里塞文件，直接跳过。 */
    if (LIVE) {
      out.push("imported=skip(live)");
    } else {
      const file = new File(["x"], "新导入_测试.xlsx", { type: "application/vnd.ms-excel" });
      const dt = new DataTransfer();
      dt.items.add(file);
      tray.dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer: dt }));
      out.push(`imported=${tray.querySelectorAll(".row.local").length}`);
    }

    /* 字体到底是不是鸿蒙：家族名对不对，以及这个家族能不能画出这几个汉字。
       CJK 的 advance 都是 1em，量宽度分不出家族，只能问 @font-face 本身。 */
    const nmEl = bar.querySelector(".link .nm");
    const sample = nmEl === null ? "原始可用" : nmEl.textContent;
    out.push(`family=${nmEl === null ? "?" : getComputedStyle(nmEl).fontFamily.split(",")[0]}`);
    out.push(`canRender=${document.fonts.check('12.5px "HarmonyOS Sans SC"', sample)}`);
    const yahei = [...document.fonts].length;
    out.push(`faces=${yahei}`);

    /* 点一个表节点、一个机器节点，看底栏换没换 */
    const tableNode = clickNode(N.find((n) => n.kind === "table" && n.label === "site_board"));
    out.push(`tableSel=${tableNode === null ? "miss" : bar.textContent.includes(show("site_board"))}`);
    out.push(`tableRowsShown=${/\d[\d,]* 行/.test(bar.textContent)}`);
    const computeNode = clickNode(N.find((n) => n.kind === "compute" && n.label === "clean_positions"));
    out.push(`computeSel=${computeNode === null ? "miss" : bar.textContent.includes(show("clean_positions"))}`);
    out.push(`hasReads=${bar.textContent.includes("读")}`);
    out.push(`paneKind=${bar.querySelector(".bp-bd").textContent.trim().slice(0, 18)}`);

    /* 选中必须真的改变高亮。边原本用节点 id 定位，而 id 里的 \u0000 写进 HTML 属性
       会被解析成 U+FFFD，读回来永远匹配不上——于是点谁都是同一条蓝线。 */
    const hotOf = () => layer.querySelectorAll("path.hot").length;
    const dimOf = () => layer.querySelectorAll(".nd.dim").length;
    clickNode(N.find((n) => n.kind === "table" && n.label === "site_board"));
    const firstHl = `${dimOf()}/${hotOf()}`;
    clickNode(N.find((n) => n.kind === "table" && n.label === "population"));
    const secondHl = `${dimOf()}/${hotOf()}`;
    out.push(`hl=${firstHl}->${secondHl}`);
    out.push(`hlChanged=${firstHl !== secondHl && hotOf() > 0}`);

    /* 点一个「还没被处理」的文件：提示要能指导下一步，而不是解释内部概念 */
    const waiting = tray.querySelector("[data-p]");
    if (waiting !== null) {
      waiting.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      const toast = document.getElementById("toast");
      out.push(`waiting=${toast === null ? "miss" : toast.textContent.trim().slice(0, 40)}`);
    }

    /* 对话区：收起 / 展开 / 列表 / 拖宽 */
    const st = () => window.__CHATSTATE();
    const pick = (s) => document.querySelector(s);
    const chatBefore = st();
    pick(".chat .fold").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const chatFolded = st();
    pick(".chat-rail").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const chatReopened = st();
    pick(".chat .list").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    out.push(`chat=${chatBefore.convs}conv/${chatBefore.blocks}blk/${chatBefore.md}md/${chatBefore.tools}tool`);
    out.push(`fold=${chatFolded.open === false && chatFolded.visible === false}/${chatFolded.gutter}`);
    out.push(`reopen=${chatReopened.open === true}/${chatReopened.gutter}`);
    out.push(`list=${st().view === "list"}/rows=${document.querySelectorAll(".chat .conv").length}`);
    const grip = pick(".chat .chat-grip");
    const gr = grip.getBoundingClientRect();
    grip.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 3, isPrimary: true, button: 0, clientX: gr.left + 4, clientY: gr.top + 40 }));
    grip.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 3, isPrimary: true, clientX: gr.left + 4 - 80, clientY: gr.top + 40 }));
    grip.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerId: 3, isPrimary: true, clientX: gr.left + 4 - 80, clientY: gr.top + 40 }));
    out.push(`dragw=${st().width}`);

    /* 主题开关：点一下要真的翻转，图标跟着换 */
    const themeBtn = pick('.iconbtn[data-act="theme"]');
    const themeBefore = document.documentElement.dataset.theme;
    themeBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    out.push(`theme=${themeBefore}->${document.documentElement.dataset.theme}`);
    out.push(`themeIcon=${getComputedStyle(themeBtn.querySelector(".i-moon")).display === "none" ? "sun" : "moon"}`);

    const box = document.createElement("div");
    box.id = "selftest";
    box.textContent = "RESULT " + out.join(" | ");
    document.body.appendChild(box);
  }, 400);
}


  /* ---------- 与服务的接口 ---------- */
  const storeOf = () => {
    const state = ground();
    return state !== null && typeof state.store === "string" ? state.store : "";
  };
  const postJson = async (url, payload) => {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({ ok: false, error: "服务没有返回 JSON" }));
    if (data.ok !== true) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  };
  const applyState = (state) => {
    if (state === undefined || state === null) return;
    RT.inputs = state;
    const el = document.getElementById("book-runtime");
    if (el !== null) {
      // 对话区读的是这份运行态：写回去，移出/撤回的事件才留得住
      try { el.textContent = JSON.stringify({ ...RT, inputs: state }); } catch { /* 序列化失败就别动它 */ }
    }
    paintTray();
    paintStale();
    if (typeof window.__CHAT === "object" && window.__CHAT !== null && typeof window.__CHAT.refresh === "function") {
      window.__CHAT.refresh();
    }
  };
  const note = (text, tone) => {
    const el = document.getElementById("toast");
    if (el === null) return;
    el.textContent = text;
    el.className = tone === "bad" ? "toast bad on" : "toast on";
    clearTimeout(note.timer);
    note.timer = setTimeout(() => { el.className = "toast"; }, 4200);
  };

  /* ---------- 导入：上传落进 ground-truth，再由 setup 收进批次 ---------- */
  const upload = async (fileList) => {
    for (const file of fileList) {
      if (!/\.(xlsx|xls)$/i.test(file.name)) {
        note(`只接受 .xlsx / .xls：${file.name}`, "bad");
        continue;
      }
      try {
        const response = await fetch("/api/inputs/upload", {
          method: "POST",
          headers: {
            "Content-Type": "application/octet-stream",
            "X-Filename": encodeURIComponent(file.name),
            "X-Store": storeOf(),
          },
          body: await file.arrayBuffer(),
        });
        const data = await response.json();
        if (data.ok !== true) throw new Error(data.error || "上传失败");
        applyState(data.state);
        note(`已导入 ${file.name}`);
      } catch (error) {
        note(`导入失败：${error.message}`, "bad");
      }
    }
  };
  tray.querySelector(".pick").addEventListener("change", (e) => {
    if (LIVE) upload(e.target.files);
    e.target.value = "";
  });
  tray.querySelector(".imp").addEventListener("click", () => {
    if (!LIVE) { note("导入需要从 start.ps1 启动服务", "bad"); return; }
    tray.querySelector(".pick").click();
  });
  tray.addEventListener("dragover", (e) => {
    if (!LIVE) return;
    e.preventDefault();
    tray.classList.add("drop");
  });
  tray.addEventListener("dragleave", () => tray.classList.remove("drop"));
  tray.addEventListener("drop", (e) => {
    e.preventDefault();
    tray.classList.remove("drop");
    if (!LIVE) {
      // 静态页没有后台：不说一句的话，拖进来像是什么都没发生
      note("导入需要从 start.ps1 启动服务", "bad");
      return;
    }
    if (e.dataTransfer !== null) upload(e.dataTransfer.files);
  });

  /* ---------- 移出：贴行确认，绝不替用户按下去 ---------- */
  let asking = null;
  const closeAsk = () => {
    if (asking !== null) { asking.remove(); asking = null; }
  };
  const askRemove = (anchor, name) => {
    closeAsk();
    if (!LIVE) { note("移出需要从 start.ps1 启动服务", "bad"); return; }
    const file = batchFiles().find((f) => f.name === name) ?? null;
    const tables = file === null ? [] : file.direct.concat(file.derived);
    const box = document.createElement("div");
    box.className = "ask";
    box.innerHTML = `
      <div class="ask-h">移出输入</div>
      <div class="ask-b">${esc(name)}</div>
      ${file === null ? "" : `<div class="ask-n">它物化出 ${tables.length} 张表；移出后这些链路会标记为不可用，下次重建批次才生效。</div>`}
      <div class="ask-f"><button class="tbtn" data-ask="cancel">取消</button><button class="tbtn go" data-ask="ok">移出</button></div>
      <div class="ask-e"></div>`;
    document.body.appendChild(box);
    const r = anchor.getBoundingClientRect();
    const size = box.getBoundingClientRect();
    let left = r.right + 10;
    if (left + size.width > window.innerWidth - 12) left = Math.max(12, r.left - size.width - 10);
    const top = Math.min(Math.max(12, r.top - 6), Math.max(12, window.innerHeight - size.height - 12));
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
    asking = box;
    box.querySelector('[data-ask="cancel"]').addEventListener("click", closeAsk);
    box.querySelector('[data-ask="ok"]').addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      box.querySelector(".ask-e").textContent = "";
      try {
        // 只报是哪个项目、哪个文件：物化出哪些表、牵动哪些步骤，由服务端算，
        // 这样包里的链路和页面上变灰的链路必定是同一份。
        const data = await postJson("/api/inputs/delete", {
          name,
          programId: V.program === null || V.program === undefined ? "" : V.program.id,
          store: storeOf(),
        });
        closeAsk();
        applyState(data.state);
        note(`已移出 ${name}`);
      } catch (error) {
        box.querySelector(".ask-e").textContent = error.message;
        button.disabled = false;
      }
    });
  };
  window.addEventListener("pointerdown", (e) => {
    if (asking !== null && !asking.contains(e.target) && !e.target.closest("[data-del]")) closeAsk();
  }, true);
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAsk();
  });

  /* ---------- 撤回 / 重建批次 ---------- */
  const TRAY_KEY = "vibe-excel-tray";
  const readTray = () => {
    try {
      return JSON.parse(localStorage.getItem(TRAY_KEY) || "{}") || {};
    } catch {
      return {};
    }
  };
  const saveTray = (patch) => {
    try {
      localStorage.setItem(TRAY_KEY, JSON.stringify({ ...readTray(), ...patch }));
    } catch {
      /* 隐私模式：记不住就算了 */
    }
  };
  const syncTray = () => {
    const folded = tray.classList.contains("collapsed");
    const btn = tray.querySelector(".hd .fold");
    if (btn !== null) {
      btn.title = folded ? "展开来源" : "收起来源";
      btn.classList.toggle("flip", folded);
    }
  };
  if (readTray().collapsed === true) tray.classList.add("collapsed");
  syncTray();

  tray.addEventListener("click", async (e) => {
    if (e.target.closest(".hd .fold") !== null) {
      const folded = tray.classList.toggle("collapsed");
      saveTray({ collapsed: folded });
      syncTray();
      return;
    }
    const del = e.target.closest("[data-del]");
    if (del !== null) {
      e.stopPropagation();
      askRemove(del.closest(".row"), del.getAttribute("data-del"));
      return;
    }
    const action = e.target.closest("[data-act]");
    if (action === null) return;
    const kind = action.getAttribute("data-act");
    if (action.disabled) return;
    try {
      if (kind === "undo") {
        const data = await postJson("/api/inputs/undo", { store: storeOf() });
        applyState(data.state);
        note(`已撤回：${data.event.file} 放回输入`);
      } else if (kind === "rebuild") {
        action.disabled = true;
        note("正在重建输入批次……");
        const data = await postJson("/api/inputs/rebuild", { store: storeOf() });
        applyState(data.state);
        note(data.ok ? "输入批次已重建，需要重新运行" : `重建失败：${data.failure || "看终端输出"}`, data.ok ? undefined : "bad");
        action.disabled = false;
      }
    } catch (error) {
      note(`操作失败：${error.message}`, "bad");
      action.disabled = false;
    }
  });

  /* 被移出的文件带出整条链：表沿着「谁读它 → 谁写它」一路传染，
     机器只要碰到其中一张就跟它一起变灰。 */
  const incidentTables = (id) => {
    const out = [];
    for (const e of E) {
      const other = e.from === id ? e.to : e.to === id ? e.from : null;
      if (other === null) continue;
      const n = byId.get(other);
      if (n !== undefined && n.kind === "table") out.push(n.label);
    }
    return out;
  };
  const staleChain = () => {
    const touched = staleFiles();
    const tables = new Set();
    const fns = new Set();
    if (touched.size > 0) {
      for (const f of batchFiles()) {
        if (!touched.has(f.name)) continue;
        for (const r of f.direct.concat(f.derived)) tables.add(r.name);
      }
      if (G !== null) {
        let growing = true;
        while (growing) {
          growing = false;
          for (const n of G.nodes) {
            if (fns.has(n.fn) || !n.reads.some((t) => tables.has(t))) continue;
            fns.add(n.fn);
            for (const t of n.writes) tables.add(t);
            growing = true;
          }
        }
      }
    }
    return { files: touched, tables, fns };
  };
  const paintStale = () => {
    const { files, tables, fns } = staleChain();
    for (const el of layer.querySelectorAll("[data-i]")) {
      const node = N[Number(el.getAttribute("data-i"))];
      if (node === undefined) continue;
      let hit = false;
      if (node.kind === "table") hit = tables.has(node.label);
      else if (node.kind === "sheet") hit = node.out !== null && node.out !== undefined && tables.has(node.out.label);
      else if (node.kind === "compute") hit = fns.size > 0 && incidentTables(node.id).some((t) => tables.has(t));
      else if (node.kind === "file") hit = files.has(String(node.id).replace(/^f:/, ""));
      el.classList.toggle("stale", hit);
    }
  };

  root.addEventListener("click", (e) => {
    if (e.target.closest('[data-act="theme"]') !== null) {
      toggleTheme();
      return;
    }
    const row = e.target.closest("[data-f]");
    if (row !== null) {
      select(Number(row.getAttribute("data-f")), true);
      return;
    }
    const pending = e.target.closest("[data-p]");
    if (pending !== null) {
      const name = pending.getAttribute("data-p");
      select(batchFiles().findIndex((f) => f.name === name), true);
      // 这句要能指导下一步：文件在，但还没人处理过它
      note(`${name} 还没被处理：先在对话里说清它是什么表、怎么用，再重建批次`);
      return;
    }
    const z = e.target.closest("[data-z]");
    if (z === null) return;
    const kind = z.getAttribute("data-z");
    if (kind === "fit") fit();
    else zoomAt(kind === "in" ? 1.2 : 1 / 1.2, cv.clientWidth / 2, cv.clientHeight / 2);
  });

  fit();
  const zq = q.get("z");
  if (zq !== null) {
    view.z = Number(zq);
    view.x = BAND.left;
    view.y = q.get("y") === null ? 0 : Number(q.get("y"));
    apply();
  }

  paintTray();
  paintStale();
  const start = files.findIndex((f) => f.inBatch);
  select(start < 0 ? 0 : start, q.get("focus") === "1");
  const wanted = q.get("node");
  if (wanted !== null) {
    const picked = N.find((n) => n.id === wanted || n.label === wanted);
    if (picked !== undefined) selectNode(picked);
  }
  window.addEventListener("resize", fit);
})();