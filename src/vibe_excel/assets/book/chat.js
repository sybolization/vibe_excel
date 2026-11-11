/* 右侧对话区。
 *
 * 版面决定来自设计讨论：
 * - 活的面板：有输入框，消息级（不是轮次级摘要）
 * - 可拖宽：300 / 360 / 520
 * - 收起成 40px 窄条，开关在面板自己的标题栏
 * - 窄窗（<1200）默认收起；位置与宽度都不持久化
 * - Markdown 用 marked 渲染，但原生 HTML 一律转义；链接新窗口；图片不放行
 * - 逐条不显示时间，只在间隔超过 5 分钟处显示一次相对时间
 * - 单条超过 12 行折叠
 */
(() => {
  "use strict";

  /* 对话数据随视图模型一起内联在 #book-data 里，和画布同源。 */
  const VIEW = JSON.parse(document.getElementById("book-data").textContent);
  const CHAT = { conversations: VIEW.conversations ?? [] };
  const MIN_W = 300;
  const MAX_W = 520;
  const RAIL = 40;
  const GAP = 12;
  const TIME_GAP = 5 * 60 * 1000;

  const state = {
    open: window.innerWidth >= 1200,
    width: 360,
    view: "thread", // thread | list
    conv: 0,
    draft: null,
    unread: false,
    sending: false,
  };

  /* 标题取首条用户消息的第一行，可能带 Markdown 的 `#` */
  const cleanTitle = (t) => String(t ?? "").replace(/^[#\s]+/, "").trim();

  const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function relative(iso) {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "";
    const d = Date.now() - t;
    const min = Math.round(d / 60000);
    if (min < 1) return "刚刚";
    if (min < 60) return `${min} 分钟前`;
    const h = Math.round(min / 60);
    if (h < 24) return `${h} 小时前`;
    const day = Math.round(h / 24);
    return `${day} 天前`;
  }

  /* 原生 HTML 一律转义：只渲染 Markdown 结构本身。
     围栏代码和行内代码里的内容不转义（那是给人看的代码）。 */
  function escapeHtmlOutsideCode(src) {
    const parts = String(src ?? "").split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g);
    return parts.map((p, i) => (i % 2 === 1 ? p : p.replace(/</g, "&lt;").replace(/>/g, "&gt;"))).join("");
  }

  function sanitize(html) {
    const doc = new DOMParser().parseFromString(`<div id="r">${html}</div>`, "text/html");
    const root = doc.getElementById("r");
    if (root === null) return "";
    const kill = new Set(["script", "style", "iframe", "object", "embed", "link", "meta", "form", "input", "video", "audio"]);
    for (const el of [...root.querySelectorAll("*")]) {
      const tag = el.tagName.toLowerCase();
      if (kill.has(tag)) {
        el.remove();
        continue;
      }
      for (const attr of [...el.attributes]) {
        const name = attr.name.toLowerCase();
        if (name.startsWith("on")) el.removeAttribute(attr.name);
        if ((name === "href" || name === "src") && /^\s*javascript:/i.test(attr.value)) el.removeAttribute(attr.name);
      }
      if (tag === "a") {
        el.setAttribute("target", "_blank");
        el.setAttribute("rel", "noopener noreferrer");
      }
      if (tag === "img") {
        const span = doc.createElement("span");
        span.className = "imgref";
        span.textContent = `[图片] ${el.getAttribute("src") ?? ""}`;
        el.replaceWith(span);
      }
    }
    return root.innerHTML;
  }

  function renderMd(src) {
    const marked = window.marked;
    if (marked === undefined) return `<p>${esc(src)}</p>`;
    try {
      const raw = marked.parse(escapeHtmlOutsideCode(src), { gfm: true, breaks: true, async: false });
      return sanitize(raw);
    } catch (error) {
      return `<p>${esc(src)}</p>`;
    }
  }

  /* ---------- 面板骨架 ---------- */
  const panel = document.createElement("div");
  panel.className = "chat";
  panel.innerHTML = `
    <div class="chat-grip" title="拖动调整宽度"></div>
    <div class="chat-hd">
      <button class="chat-ic list" title="对话列表"><svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="M2 3.5h9M2 6.5h9M2 9.5h9"/></svg></button>
      <b class="chat-tt">项目对话</b>
      <button class="chat-ic plus" title="开始一段新对话"><svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><path d="M6.5 2.5v8M2.5 6.5h8"/></svg></button>
      <button class="chat-ic fold" title="收起对话区"><svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 2.5 8.5 6.5 5 10.5"/></svg></button>
    </div>
    <div class="chat-bd"></div>
    <div class="chat-ft">
      <button class="chat-quote" hidden></button>
      <div class="chat-box"><textarea rows="1" placeholder="问点什么…"></textarea></div>
      <div class="chat-act"><button class="chat-send" disabled>发送</button></div>
    </div>`;

  const rail = document.createElement("button");
  rail.className = "chat-rail";
  rail.title = "展开对话区";
  rail.innerHTML = `<span class="chat-ic-wrap"><svg width="14" height="14" viewBox="0 0 13 13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 2.5h9v7.5H6.5L4 12V10H2z"/></svg></span><span class="chat-dot" hidden></span>`;

  const body = panel.querySelector(".chat-bd");
  const input = panel.querySelector("textarea");
  const sendBtn = panel.querySelector(".chat-send");
  /* 停止按钮和发送同位置：跑起来的时候它才出现 */
  const stopBtn = document.createElement("button");
  stopBtn.className = "chat-send chat-stop";
  stopBtn.type = "button";
  stopBtn.textContent = "停止";
  stopBtn.hidden = true;
  sendBtn.after(stopBtn);
  const quoteBtn = panel.querySelector(".chat-quote");
  const title = panel.querySelector(".chat-tt");

  const current = () => {
    if (state.draft !== null) return state.draft;
    return CHAT.conversations[state.conv] ?? null;
  };

  /* ---------- 版面对外的两个约定 ---------- */
  /* 对话区占满整个右边：贴住窗口边、从顶到底。
     --cw 是它占掉的宽度，画布和底栏都按这个值让位。 */
  function gutter() {
    return state.open ? state.width : RAIL;
  }

  function applyLayout() {
    panel.style.width = `${state.width}px`;
    document.documentElement.style.setProperty("--cw", `${gutter()}px`);
    panel.hidden = !state.open;
    rail.hidden = state.open;
    if (typeof window.__BOOK?.setGutter === "function") window.__BOOK.setGutter(28 + gutter() + GAP);
  }

  /* ---------- 列表 ---------- */
  function paintList() {
    const rows = CHAT.conversations
      .map((c, i) => {
        const on = state.draft === null && i === state.conv ? " on" : "";
        return `<button class="conv${on}" data-i="${i}"><span class="ct">${esc(cleanTitle(c.title))}</span><span class="cm">${esc(relative(c.end || c.start))}</span></button>`;
      })
      .join("");
    const draft = state.draft === null ? "" : `<button class="conv draft on"><span class="ct">${esc(state.draft.title)}</span><span class="cm">草稿</span></button>`;
    body.innerHTML = `<div class="conv-hd">这个项目的 ${CHAT.conversations.length} 场对话</div>${draft}${rows}`;
  }

  /* ---------- 消息流 ---------- */
  /* 失败时别把整行染红：灰行照旧，行尾挂一枚徽标，红点 + 一句原因。 */
  const failLabel = (result) => {
    const code = /exit code (\d+)/i.exec(result) ?? /退出码\s*(\d+)/.exec(result);
    return code === null || code === undefined ? "失败" : `退出码 ${code[1]}`;
  };

  function toolBlock(b) {
    /* 一次调用就是一行灰字：名字和参数同色同粗细。
       打勾打叉是噪音——没出错就是常态，不需要宣布。
       跑着的时候带上 live：让人知道它还在动，而不是卡住了。 */
    const badge = b.ok ? "" : `<span class="fail"><span class="fd"></span>${esc(failLabel(b.result))}</span>`;
    const live = b.live === true;
    return `<details class="blk tool${live ? " live" : ""}"${live ? " open" : ""}><summary><span class="tn">${esc(b.name)}</span><span class="tb" title="${esc(b.brief)}">${esc(b.brief)}</span>${badge}</summary>${b.result === "" ? "" : `<pre>${esc(b.result)}</pre>`}</details>`;
  }

  /* 思考块：默认折叠，点开才看。它不属于「agent 说的话」，所以单独一块。 */
  function thinkBlock(b) {
    const first = String(b.text ?? "").trim().split("\n")[0].slice(0, 40);
    /* 正在想的这一段要展开、带流光——折叠着看不出它在动；想完自动收回去。 */
    const live = b.live === true;
    return `<details class="blk think${live ? " live" : ""}"${live ? " open" : ""}><summary><span class="tt">思考</span><span class="tp">${esc(first)}</span></summary><div class="thinkbody">${esc(b.text ?? "").replace(/\n/g, "<br>")}</div></details>`;
  }

  /* 服务端记在 store 里的输入变更事件：读页面的运行态，不靠这次会话的记忆。
     移出过的文件在刷新之后仍然看得见——历史不能因为关了页面就消失。 */
  const eventsHtml = () => {
    const el = document.getElementById("book-runtime");
    if (el === null) return "";
    let runtime = null;
    try { runtime = JSON.parse(el.textContent); } catch { return ""; }
    const rows = runtime && runtime.inputs && runtime.inputs.events ? runtime.inputs.events : [];
    if (!Array.isArray(rows) || rows.length === 0) return "";
    const items = rows
      .slice()
      .reverse()
      .map((e) => {
        const at = new Date(e.at);
        const hh = Number.isNaN(at.getTime())
          ? ""
          : `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")} `;
        const verb = e.kind === "remove" ? "移出" : e.kind === "undo" ? "撤回" : String(e.kind ?? "");
        return `<div class="blk ev"><span class="evd"></span><span class="evt">${esc(hh)}${esc(verb)} ${esc(e.file ?? "")}</span></div>`;
      })
      .join("");
    return `<div class="evsec"><div class="evhd">输入变更</div>${items}</div>`;
  };

  function paintThread() {
    const conv = current();
    if (conv === null) {
      // 没有对话时也要露出输入变更：这个项目的输入被动过，比"还没有对话"更该被看见
      body.innerHTML = eventsHtml() + `<div class="empty">还没有对话</div>`;
      return;
    }
    title.textContent = cleanTitle(conv.title);
    let last = null;
    const html = conv.blocks
      .map((b) => {
        const at = Date.parse(b.at);
        let stamp = "";
        if (!Number.isNaN(at) && (last === null || at - last > TIME_GAP)) {
          stamp = `<div class="stamp">${esc(relative(b.at))}</div>`;
          last = at;
        } else if (!Number.isNaN(at)) {
          last = at;
        }
        if (b.kind === "tool") return stamp + toolBlock(b);
        if (b.kind === "thinking") return stamp + thinkBlock(b);
  /* 输入变更这类系统事件：不冒充 agent 说的话，单独一种行。 */
        if (b.kind === "event") return stamp + `<div class="blk ev"><span class="evd"></span><span class="evt">${esc(b.text)}</span></div>`;
        if (b.kind === "user") return `${stamp}<div class="blk u"><div class="who">你</div><div class="body">${esc(b.text).replace(/\n/g, "<br>")}</div></div>`;
        return `${stamp}<div class="blk a"><div class="who">agent</div><div class="body md">${renderMd(b.text)}</div></div>`;
      })
      .join("");
    body.innerHTML =
      eventsHtml() + (html === "" ? `<div class="empty">这段对话没有消息</div>` : html);

    body.scrollTop = body.scrollHeight;
  }

  function paint() {
    const list = state.view === "list";
    panel.classList.toggle("listing", list);
    panel.querySelector(".chat-ft").hidden = list;
    panel.querySelector(".chat-hd b").textContent = list ? "对话" : current() === null ? "项目对话" : cleanTitle(current().title);
    if (list) paintList();
    else paintThread();
  }

  /* ---------- 交互 ---------- */
  panel.querySelector(".fold").addEventListener("click", () => {
    state.open = false;
    state.unread = false;
    applyLayout();
    paintRail();
  });
  panel.querySelector(".list").addEventListener("click", () => {
    state.view = state.view === "list" ? "thread" : "list";
    paint();
  });
  panel.querySelector(".plus").addEventListener("click", () => {
    state.draft = { id: "draft", title: "未命名对话", start: "", end: "", blocks: [] };
    state.view = "thread";
    paint();
    input.focus();
  });
  rail.addEventListener("click", () => {
    state.open = true;
    state.unread = false;
    state.view = "thread";
    applyLayout();
    paint();
  });
  body.addEventListener("click", (e) => {
    const row = e.target.closest(".conv");
    if (row === null) return;
    if (row.classList.contains("draft")) {
      state.view = "thread";
    } else {
      state.draft = null;
      state.conv = Number(row.getAttribute("data-i"));
      state.view = "thread";
    }
    paint();
  });

  function paintRail() {
    const dot = rail.querySelector(".chat-dot");
    dot.hidden = !state.unread;
  }

  /* 拖动宽度：300–520 */
  const grip = panel.querySelector(".chat-grip");
  grip.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const from = { x: e.clientX, w: state.width };
    try { grip.setPointerCapture(e.pointerId); } catch { /* 合成事件没有真实指针 */ }
    const move = (ev) => {
      const next = Math.max(MIN_W, Math.min(MAX_W, from.w - (ev.clientX - from.x)));
      state.width = next;
      applyLayout();
    };
    const done = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", done);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", done);
  });

  /* 输入 */
  const syncSend = () => {
    sendBtn.disabled = input.value.trim() === "";
    /* 正在跑的时候把发送换成停止：不然只能干等 */
    sendBtn.hidden = state.sending === true;
    stopBtn.hidden = state.sending !== true;
  };
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(180, input.scrollHeight)}px`;
    syncSend();
  });
  input.addEventListener("keydown", (e) => {
    /* 中文输入法里，回车是「确认候选词」，不是「发送」。
       不挡这一下，一句话会被发两遍——第二遍还是同样的文字。 */
    if (e.isComposing === true || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  sendBtn.addEventListener("click", send);
  stopBtn.addEventListener("click", stop);

  /* ?send=<一句话>：加载完直接把这句话发出去。
     排查和截图都用得上——手点一次没法脚本化，这里走的是同一个 send()。 */
  const autoSend = new URLSearchParams(location.search).get("send");
  if (autoSend !== null && autoSend.trim() !== "") {
    setTimeout(() => {
      input.value = autoSend;
      syncSend();
      send();
    }, 400);
  }

  /* 页面上下文：跟哪个项目说、状态在哪个 store、有没有后台 */
  const contextIds = () => {
    const book = JSON.parse(document.getElementById("book-data").textContent);
    const el = document.getElementById("book-runtime");
    let runtime = {};
    try { runtime = el === null ? {} : JSON.parse(el.textContent) || {}; } catch { runtime = {}; }
    const inputs = runtime.inputs ?? {};
    return {
      programId: book.program === null || book.program === undefined ? "" : book.program.id,
      store: typeof inputs.store === "string" ? inputs.store : "",
      live: runtime.mode === "live" && runtime.editable === true,
    };
  };

  /* 现在这段对话（草稿也算），发送要接在它后面，而不是另起一段 */
  const threadOf = () => {
    if (state.view === "list") return null;
    if (state.draft !== null) return state.draft;
    return CHAT.conversations[state.conv] ?? null;
  };

  /* 一段对话一条事件流：发出去的消息由服务端排队，回答从这条流里回来。 */
  let stream = null;
  let streamFor = "";

  const closeStream = () => {
    if (stream !== null) stream.close();
    stream = null;
    streamFor = "";
  };

  const openStream = (conversationId, handle) => {
    if (conversationId === "" || streamFor === conversationId) return;
    closeStream();
    streamFor = conversationId;
    stream = new EventSource(`/api/chat/events?conversationId=${encodeURIComponent(conversationId)}`);
    stream.onmessage = (message) => {
      let event = null;
      try { event = JSON.parse(message.data); } catch { return; }
      handle(event);
    };
    /* 流断了不重连：这一轮的结果已经落盘，刷新就能看到，不值得在这儿死撑 */
    stream.onerror = () => closeStream();
  };

  async function send() {
    const text = input.value.trim();
    if (text === "" || state.sending === true) return;
    const ids = contextIds();
    let thread = threadOf();
    if (thread === null) {
      thread = {
        id: "draft",
        title: cleanTitle(text),
        start: new Date().toISOString(),
        end: null,
        blocks: [],
      };
      CHAT.conversations.unshift(thread);
      state.draft = thread;
    }
    const isNew = thread.id === "" || thread.id === "draft";
    thread.blocks.push({ kind: "user", at: new Date().toISOString(), text });
    input.value = "";
    syncSend();

    const at = new Date().toISOString();
    const thought = { kind: "thinking", at, text: "", live: true };
    const answer = { kind: "assistant", at, text: "" };
    const calls = new Map();
    thread.blocks.push(thought, answer);
    state.sending = true;
    paint();
    if (!state.open) {
      state.unread = true;
      paintRail();
    }
    if (!ids.live) {
      thread.blocks.splice(-2, 2, { kind: "event", at, text: "发送需要从 start.ps1 启动服务" });
      state.sending = false;
      paint();
      return;
    }

    const liveNodes = () => ({
      think: [...body.querySelectorAll(".blk.think .thinkbody")].pop() ?? null,
      thinkHeader: [...body.querySelectorAll(".blk.think .tp")].pop() ?? null,
      answer: [...body.querySelectorAll(".blk.a .body")].pop() ?? null,
    });
    const settleThought = () => {
      if (thought.live !== true) return;
      thought.live = false;
      const node = [...body.querySelectorAll(".blk.think")].pop();
      if (node !== null && node !== undefined) {
        node.classList.remove("live");
        node.removeAttribute("open");
      }
    };
    let frame = 0;
    const repaint = () => {
      if (frame !== 0) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const nodes = liveNodes();
        if (thought.text !== "" && nodes.think !== null) {
          nodes.think.innerHTML = esc(thought.text).replace(/\n/g, "<br>");
          if (nodes.thinkHeader !== null) {
            nodes.thinkHeader.textContent = thought.text.trim().split("\n")[0].slice(0, 40);
          }
        }
        if (nodes.answer !== null) nodes.answer.innerHTML = renderMd(answer.text) || "…";
      });
    };
    const finish = () => {
      settleThought();
      state.sending = false;
      paint();
    };

    const handle = (event) => {
      if (event.type === "thinking") {
        thought.text += event.delta ?? "";
        repaint();
        return;
      }
      if (event.type === "call") {
        /* 工具调用真发生了就当场长一行：它干了什么，不该等回答完才知道 */
        const block = { kind: "tool", at, name: event.name ?? "?", brief: event.brief ?? "", ok: true, result: "", ms: null };
        calls.set(String(event.id ?? ""), block);
        thread.blocks.splice(thread.blocks.indexOf(answer), 0, block);
        settleThought();
        paint();
        return;
      }
      if (event.type === "progress") {
        /* 命令还在跑，中间输出先铺出来：长命令不该在页面上是一段空白 */
        const block = calls.get(String(event.id ?? ""));
        if (block !== undefined) {
          block.result = event.text ?? "";
          block.live = true;
          paint();
        }
        return;
      }
      if (event.type === "result") {
        const block = calls.get(String(event.id ?? ""));
        if (block !== undefined) {
          block.ok = String(event.ok ?? "1") !== "0";
          block.result = event.text ?? "";
          block.live = false;
          const ms = Number(event.ms);
          block.ms = Number.isFinite(ms) ? ms : null;
          paint();
        }
        return;
      }
      if (event.type === "text") {
        answer.text += event.delta ?? "";
        settleThought();
        repaint();
        return;
      }
      if (event.type === "done") {
        thread.end = new Date().toISOString();
        if (answer.text === "") thread.blocks.splice(thread.blocks.indexOf(answer), 1);
        if (thought.text === "") thread.blocks.splice(thread.blocks.indexOf(thought), 1);
        closeStream();
        finish();
        return;
      }
      if (event.type === "interrupted") {
        thread.blocks.splice(thread.blocks.indexOf(answer), 1);
        thread.blocks.splice(thread.blocks.indexOf(thought) + 1, 0, { kind: "event", at, text: "你按了停止" });
        if (thought.text === "") thread.blocks.splice(thread.blocks.indexOf(thought), 1);
        closeStream();
        finish();
        return;
      }
      if (event.type === "error") {
        thread.blocks.splice(thread.blocks.indexOf(answer), 1);
        thread.blocks.splice(thread.blocks.indexOf(thought) + 1, 0, { kind: "event", at, text: `没问成：${event.error ?? "未知原因"}` });
        if (thought.text === "") thread.blocks.splice(thread.blocks.indexOf(thought), 1);
        closeStream();
        finish();
      }
    };

    try {
      /* 注意顺序：先 POST 再挂流。服务端在 POST 里就建好了这段对话的执行状态，
         挂流时订阅得到，backlog 会把已经发生的事件补齐。 */

      const response = await fetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          programId: ids.programId,
          store: ids.store,
          conversationId: isNew ? "" : thread.id,
          new: isNew,
          text,
        }),
      });
      let data = null;
      try { data = await response.json(); } catch { data = null; }
      if (data === null || data.ok !== true) {
        throw new Error((data && data.error) || `HTTP ${response.status}`);
      }
      thread.id = data.conversationId || thread.id;
      openStream(thread.id, handle);
    } catch (error) {
      thread.blocks.splice(thread.blocks.indexOf(answer), 1);
      thread.blocks.splice(thread.blocks.indexOf(thought) + 1, 0, { kind: "event", at, text: `没问成：${error.message}` });
      if (thought.text === "") thread.blocks.splice(thread.blocks.indexOf(thought), 1);
      finish();
    }
  }

  async function stop() {
    const thread = threadOf();
    if (thread === null || thread.id === "" || thread.id === "draft") return;
    try {
      await fetch("/api/chat/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversationId: thread.id }),
      });
    } catch {
      /* 停不下来就算了：这一轮结果仍然会落盘 */
    }
  }

  /* 引用选中的节点 */
  let quoted = null;
  window.__ON_SELECT = (node) => {
    const usable = node !== null && node.kind !== "file";
    quoted = usable ? node : null;
    quoteBtn.hidden = !usable;
    if (usable) quoteBtn.textContent = `引用：${node.name === "" || node.name === undefined ? node.label : node.name}`;
  };
  quoteBtn.addEventListener("click", () => {
    if (quoted === null) return;
    const name = quoted.name === "" || quoted.name === undefined ? quoted.label : quoted.name;
    input.value = `${input.value === "" ? "" : `${input.value} `}@${name} `;
    input.focus();
    syncSend();
  });

  /* 未读点：只在面板收起、又有新内容到达时点亮（本轮没有流式数据，只把机制接上） */
  window.__CHAT = {
    unread(on = true) {
      state.unread = on;
      paintRail();
    },
    open() {
      state.open = true;
      applyLayout();
      paint();
    },
    /* 页面上的动作（移出/撤回）之后让对话区重新读一遍运行态 */
    refresh() {
      paint();
    },
  };

  document.body.append(panel, rail);
  const qp = new URLSearchParams(location.search).get("chat");
  if (qp === "list") state.view = "list";
  if (qp === "rail") state.open = false;
  applyLayout();
  paint();
  paintRail();

  /* 自检用：面板状态从外面读得到 */
  window.__CHATSTATE = () => ({
    open: state.open,
    width: state.width,
    view: state.view,
    convs: CHAT.conversations.length,
    visible: !panel.hidden,
    blocks: body.querySelectorAll(".blk").length,
    md: body.querySelectorAll(".md").length,
    tools: body.querySelectorAll(".tool").length,
    gutter: getComputedStyle(document.documentElement).getPropertyValue("--cw").trim(),
  });
  if (new URLSearchParams(location.search).has("unread")) window.__CHAT.unread(true);
})();
