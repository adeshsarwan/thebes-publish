/* Story Hub block editor.
 *
 * A small Gutenberg-style editor with no dependencies. The DOM is the source of
 * truth while editing; on save it is serialised to Editor.js-compatible JSON
 * blocks and posted with the form. The server sanitises everything again.
 *
 * Blocks: paragraph, header (H2/H3), list (bulleted/numbered), quote, table, delimiter, image.
 * Insert with "/" in an empty paragraph, the + buttons, or markdown shortcuts
 * ("## ", "### ", "- ", "1. ", "> ", "---"). Select text for bold, italic and links.
 */
(function () {
  "use strict";
  var cfg = JSON.parse(document.getElementById("ed-data").textContent);
  var form = document.getElementById("editform");
  var root = document.getElementById("ed-blocks");
  var titleEl = document.getElementById("ed-headline");
  var dekEl = document.getElementById("ed-dek");
  var wcEl = document.getElementById("ed-wc");
  var msgEl = document.getElementById("ed-msg");
  var dirty = false, submitting = false;
  var DRAFT_KEY = "fh-draft-" + cfg.story_id + "-v" + cfg.version;

  var TYPES = [
    { type: "paragraph", label: "Paragraph", hint: "Body text", icon: "¶", keys: "paragraph text p body" },
    { type: "header", level: 2, label: "Heading", hint: "Section heading (H2)", icon: "H2", keys: "heading h2 title section" },
    { type: "header", level: 3, label: "Subheading", hint: "Smaller heading (H3)", icon: "H3", keys: "subheading h3 small" },
    { type: "list", style: "unordered", label: "Bulleted list", hint: "Points in any order", icon: "•", keys: "list bullet bulleted ul points" },
    { type: "list", style: "ordered", label: "Numbered list", hint: "Steps in order", icon: "1.", keys: "numbered ordered ol steps list" },
    { type: "quote", label: "Quote", hint: "A quotation with its source", icon: "“", keys: "quote blockquote citation" },
    { type: "table", label: "Table", hint: "Rows and columns of figures", icon: "▦", keys: "table grid figures numbers" },
    { type: "delimiter", label: "Divider", hint: "A line between parts", icon: "—", keys: "divider separator hr line delimiter" },
    { type: "image", label: "Image", hint: "A photo or illustration from the story's images", icon: "▣", keys: "image photo picture illustration figure" }
  ];

  // ------------------------------------------------------------------ helpers
  function el(tag, cls, attrs) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (attrs) for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  function editable(n, placeholder) {
    n.setAttribute("contenteditable", "true");
    if (placeholder) n.setAttribute("data-placeholder", placeholder);
    return n;
  }
  function textOf(n) { return (n.textContent || "").replace(/ /g, " "); }
  function tidy(n) { if (n && textOf(n).trim() === "" && !n.querySelector("li,td")) n.innerHTML = ""; }
  function blocks() { return Array.prototype.slice.call(root.children); }
  function blockOf(n) { while (n && n !== root) { if (n.classList && n.classList.contains("blk")) return n; n = n.parentNode; } return null; }
  function bodyOf(b) { return b.querySelector(".blk-body") || b.querySelector("td") || b.querySelector(".blk-hr") || b.querySelector(".blk-img"); }
  function markDirty() { dirty = true; scheduleBackup(); scheduleCount(); document.dispatchEvent(new Event("fh-editor-changed")); }

  function placeCaret(n, atEnd) {
    if (!n) return;
    // list items and table cells sit inside (or are) the editable host; focus the host, then place the caret in n
    var host = n.getAttribute("contenteditable") === "true" ? n : (n.closest ? n.closest("[contenteditable=true]") : null);
    if (!host) { if (n.focus) n.focus(); return; }
    host.focus();
    var r = document.createRange();
    r.selectNodeContents(n);
    r.collapse(!atEnd);
    var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  }
  function caretRange() { var s = window.getSelection(); return s.rangeCount ? s.getRangeAt(0) : null; }
  function caretAt(n, end) {
    var r = caretRange();
    if (!r || !r.collapsed || !n.contains(r.startContainer)) return false;
    var t = document.createRange(); t.selectNodeContents(n);
    if (end) t.setStart(r.endContainer, r.endOffset); else t.setEnd(r.startContainer, r.startOffset);
    return t.toString().replace(/​/g, "") === "" && !(end ? t.cloneContents().querySelector("br:not(:last-child)") : false);
  }
  function focusBlock(b, atEnd) {
    if (!b) return;
    var t = b.dataset.type, target;
    if (t === "list") { var lis = b.querySelectorAll("li"); target = atEnd ? lis[lis.length - 1] : lis[0]; placeCaret(target || bodyOf(b), atEnd); return; }
    if (t === "table") { var tds = b.querySelectorAll("td"); target = atEnd ? tds[tds.length - 1] : tds[0]; placeCaret(target, atEnd); return; }
    placeCaret(bodyOf(b), atEnd);
  }

  // ------------------------------------------------------------------ blocks
  function make(b) {
    var t = b.type, d = b.data || {};
    var w = el("div", "blk"); w.dataset.type = t;
    var body;
    if (t === "paragraph") {
      body = editable(el("div", "blk-body"), "Type / to add a block");
      body.innerHTML = d.text || "";
    } else if (t === "header") {
      var lvl = d.level === 3 ? 3 : 2;
      w.dataset.level = lvl;
      body = editable(el("div", "blk-body h" + lvl, { role: "heading", "aria-level": String(lvl) }), lvl === 2 ? "Heading" : "Subheading");
      body.innerHTML = d.text || "";
    } else if (t === "list") {
      w.dataset.style = d.style === "ordered" ? "ordered" : "unordered";
      body = editable(el(d.style === "ordered" ? "ol" : "ul", "blk-body"));
      var items = (d.items && d.items.length) ? d.items : [""];
      items.forEach(function (it) { var li = el("li"); li.innerHTML = (typeof it === "string" ? it : (it.content || "")) || "<br>"; body.appendChild(li); });
    } else if (t === "quote") {
      var q = el("blockquote", "blk-quote");
      body = editable(el("div", "blk-body"), "Quotation");
      body.innerHTML = d.text || "";
      var cap = editable(el("div", "blk-cap"), "Who said it (optional)");
      cap.innerHTML = d.caption || "";
      q.appendChild(body); q.appendChild(cap); w.appendChild(q);
    } else if (t === "delimiter") {
      body = el("div", "blk-hr", { tabindex: "0", role: "separator", "aria-label": "Divider" });
    } else if (t === "image") {
      var fig = el("figure", "blk-img", { tabindex: "0", contenteditable: "false", "aria-label": "Image" });
      w._img = {}; for (var k in d) if (k !== "caption" && k !== "alt") w._img[k] = d[k];
      var im = el("img", "", { src: d.url || "", alt: d.alt || "" });
      var alt = el("input", "blk-alt", { type: "text", placeholder: "Alt text: what the image shows (for screen readers and search)", "aria-label": "Alt text" });
      alt.value = d.alt || "";
      var icap = editable(el("div", "blk-cap"), "Caption (optional)");
      icap.innerHTML = d.caption || "";
      var cr = el("div", "blk-credit"); cr.textContent = d.credit || "";
      fig.appendChild(im); fig.appendChild(icap); fig.appendChild(cr); fig.appendChild(alt);
      w.appendChild(fig);
    } else if (t === "table") {
      var rows = (d.content && d.content.length) ? d.content : [["", "", ""], ["", "", ""], ["", "", ""]];
      var wrap = el("div", "blk-table"), tbl = el("table", d.withHeadings === false ? "" : "head");
      rows.forEach(function (r) { var tr = el("tr"); r.forEach(function (c) { var td = editable(el("td")); td.innerHTML = c || ""; tr.appendChild(td); }); tbl.appendChild(tr); });
      wrap.appendChild(tbl); w.appendChild(wrap);
    }
    if (body && t !== "quote") w.appendChild(body);
    w.insertBefore(toolbar(w), w.firstChild);
    var plus = el("button", "blk-plus", { type: "button", title: "Add a block below", "aria-label": "Add a block below" });
    plus.textContent = "+";
    plus.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
    plus.addEventListener("click", function () { openMenu(plus, { mode: "insert", after: w }); });
    w.insertBefore(plus, w.firstChild);
    return w;
  }

  function toolbar(w) {
    var bar = el("div", "blk-bar", { contenteditable: "false" });
    var t = w.dataset.type;
    var name = t === "header" ? (w.dataset.level === "3" ? "H3" : "H2") : t === "list" ? (w.dataset.style === "ordered" ? "Numbered" : "List") : (TYPES.filter(function (x) { return x.type === t; })[0] || {}).label;
    var lbl = el("span", "lbl"); lbl.textContent = name; bar.appendChild(lbl);
    function btn(txt, title, fn) {
      var b = el("button", "", { type: "button", title: title, "aria-label": title }); b.textContent = txt;
      b.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
      b.addEventListener("click", function () { fn(b); markDirty(); });
      bar.appendChild(b);
    }
    if (t === "table") {
      btn("+ Row", "Add a row", function () { tableRow(w, 1); });
      btn("+ Col", "Add a column", function () { tableCol(w, 1); });
      btn("− Row", "Remove the current row", function () { tableRow(w, -1); });
      btn("− Col", "Remove the current column", function () { tableCol(w, -1); });
      btn("Header", "Toggle header row", function () { w.querySelector("table").classList.toggle("head"); });
    }
    if (t === "image") btn("Swap", "Choose a different image", function () { document.dispatchEvent(new CustomEvent("fh-pick-image", { detail: { replace: w } })); });
    if (t !== "delimiter" && t !== "table" && t !== "image") btn("⇄", "Turn into another block", function (b) { openMenu(b, { mode: "convert", block: w }); });
    btn("↑", "Move up", function () { if (w.previousElementSibling) { root.insertBefore(w, w.previousElementSibling); focusBlock(w, true); } });
    btn("↓", "Move down", function () { if (w.nextElementSibling) { root.insertBefore(w.nextElementSibling, w); focusBlock(w, true); } });
    btn("✕", "Delete block", function () { removeBlock(w); });
    return bar;
  }

  function insertAfter(ref, b, focus) {
    var n = make(b);
    if (ref && ref.parentNode === root) root.insertBefore(n, ref.nextSibling); else root.appendChild(n);
    if (focus !== false) focusBlock(n, false);
    markDirty();
    return n;
  }
  function removeBlock(w, focusPrev) {
    var prev = w.previousElementSibling, next = w.nextElementSibling;
    w.remove();
    if (!root.children.length) insertAfter(null, { type: "paragraph", data: {} });
    else if (focusPrev !== false) focusBlock(prev || next, !!prev);
    markDirty();
  }
  function replaceBlock(w, b) {
    var n = make(b); root.replaceChild(n, w); focusBlock(n, true); markDirty(); return n;
  }

  function textHtml(w) {
    var t = w.dataset.type;
    if (t === "list") return Array.prototype.map.call(w.querySelectorAll("li"), function (li) { return li.innerHTML; }).filter(function (x) { return x.replace(/<br>/g, "").trim(); });
    var b = w.querySelector(".blk-body");
    return b ? [b.innerHTML] : [""];
  }
  function convert(w, spec) {
    var parts = textHtml(w);
    if (spec.type === "list") return replaceBlock(w, { type: "list", data: { style: spec.style, items: parts.length ? parts : [""] } });
    if (spec.type === "table" || spec.type === "delimiter") { insertAfter(w, { type: spec.type, data: {} }); return; }
    if (w.dataset.type === "list" && spec.type === "paragraph" && parts.length > 1) {
      var first = replaceBlock(w, { type: "paragraph", data: { text: parts[0] } }), ref = first;
      parts.slice(1).forEach(function (p) { ref = insertAfter(ref, { type: "paragraph", data: { text: p } }, false); });
      return;
    }
    var html = parts.join("<br>");
    replaceBlock(w, { type: spec.type, data: { text: html, level: spec.level } });
  }

  // tables
  function curCell(w) { var r = caretRange(); var n = r && r.startContainer; while (n && n !== w) { if (n.nodeName === "TD") return n; n = n.parentNode; } return null; }
  function tableRow(w, dir) {
    var tbl = w.querySelector("table"), cell = curCell(w);
    var tr = cell ? cell.parentNode : tbl.rows[tbl.rows.length - 1];
    if (dir > 0) {
      var n = el("tr");
      for (var i = 0; i < tr.cells.length; i++) n.appendChild(editable(el("td")));
      tr.parentNode.insertBefore(n, tr.nextSibling); placeCaret(n.cells[0]);
    } else if (tbl.rows.length > 1) { var prev = tr.previousElementSibling || tr.nextElementSibling; tr.remove(); placeCaret(prev.cells[0]); }
  }
  function tableCol(w, dir) {
    var tbl = w.querySelector("table"), cell = curCell(w);
    var idx = cell ? cell.cellIndex : tbl.rows[0].cells.length - 1;
    Array.prototype.forEach.call(tbl.rows, function (tr) {
      if (dir > 0) tr.insertBefore(editable(el("td")), tr.cells[idx + 1] || null);
      else if (tr.cells.length > 1) tr.cells[idx].remove();
    });
    var row = (cell && cell.parentNode) || tbl.rows[0];
    placeCaret(row.cells[Math.min(dir > 0 ? idx + 1 : idx, row.cells.length - 1)]);
  }

  // ------------------------------------------------------------------ menu (insert, "/" and convert)
  var menu = null;
  function closeMenu() { if (menu) { menu.el.remove(); menu = null; } }
  function openMenu(anchor, opts) {
    closeMenu();
    var pop = el("div", "ed-pop", { role: "listbox" });
    document.body.appendChild(pop);
    menu = { el: pop, opts: opts, active: 0, items: [] };
    renderMenu(opts.query || "");
    var rect = (opts.rect || anchor.getBoundingClientRect());
    pop.style.top = (window.scrollY + rect.bottom + 6) + "px";
    pop.style.left = Math.max(8, Math.min(window.scrollX + rect.left, window.scrollX + document.documentElement.clientWidth - 280)) + "px";
  }
  function renderMenu(q) {
    var pop = menu.el, opts = menu.opts;
    q = (q || "").toLowerCase().trim();
    var list = TYPES.filter(function (t) {
      if (opts.mode === "convert" && (t.type === "table" || t.type === "delimiter" || t.type === "image")) return false;
      return !q || t.label.toLowerCase().indexOf(q) >= 0 || t.keys.indexOf(q) >= 0;
    });
    menu.items = list; menu.active = Math.min(menu.active, Math.max(0, list.length - 1));
    pop.innerHTML = "";
    if (!list.length) { var none = el("div", "none"); none.textContent = "No matching block"; pop.appendChild(none); return; }
    list.forEach(function (t, i) {
      var b = el("button", "it" + (i === menu.active ? " on" : ""), { type: "button", role: "option" });
      var ic = el("i"); ic.textContent = t.icon; b.appendChild(ic);
      var tx = el("span"); tx.textContent = t.label; var sm = el("small"); sm.textContent = t.hint; tx.appendChild(sm); b.appendChild(tx);
      b.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
      b.addEventListener("click", function () { pick(t); });
      pop.appendChild(b);
    });
  }
  function pick(t) {
    var opts = menu.opts; closeMenu();
    if (t.type === "image") {
      var anchor = opts.mode === "slash" ? opts.block : (opts.after || null);
      if (opts.mode === "slash") { var sb = bodyOf(anchor); sb.innerHTML = ""; }
      document.dispatchEvent(new CustomEvent("fh-pick-image", { detail: { after: anchor, clear: opts.mode === "slash" ? anchor : null } }));
      return;
    }
    var spec = { type: t.type, data: { level: t.level, style: t.style } };
    if (opts.mode === "convert") { convert(opts.block, t); return; }
    if (opts.mode === "slash") {
      var w = opts.block;
      if (t.type === "paragraph") { var bd = bodyOf(w); bd.innerHTML = ""; placeCaret(bd); return; }
      var n = replaceBlock(w, spec);
      if (t.type === "delimiter") insertAfter(n, { type: "paragraph", data: {} });
      else focusBlock(n, false);
      return;
    }
    var nb = insertAfter(opts.after, spec);
    if (t.type === "delimiter") insertAfter(nb, { type: "paragraph", data: {} });
  }
  function menuKey(ev) {
    if (!menu) return false;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      var n = menu.items.length; if (!n) return true;
      menu.active = (menu.active + (ev.key === "ArrowDown" ? 1 : n - 1)) % n;
      renderMenu(menu.opts.mode === "slash" ? slashQuery(menu.opts.block) : "");
      return true;
    }
    if (ev.key === "Enter" || ev.key === "Tab") { if (menu.items.length) { ev.preventDefault(); pick(menu.items[menu.active]); return true; } }
    if (ev.key === "Escape") { ev.preventDefault(); closeMenu(); return true; }
    return false;
  }
  function slashQuery(w) { var t = textOf(bodyOf(w)); return t.charAt(0) === "/" ? t.slice(1) : null; }

  // ------------------------------------------------------------------ inline toolbar (bold, italic, link)
  var ink = el("div", "ed-ink", { role: "toolbar", "aria-label": "Text formatting" });
  document.body.appendChild(ink);
  var savedRange = null;
  function inkButtons() {
    ink.innerHTML = "";
    [["B", "Bold (⌘B)", function () { document.execCommand("bold"); }],
     ["I", "Italic (⌘I)", function () { document.execCommand("italic"); }],
     ["Link", "Add a link (⌘K)", function () { linkInput(); }],
     ["Unlink", "Remove link", function () { document.execCommand("unlink"); }]].forEach(function (x) {
      var b = el("button", "", { type: "button", title: x[1], "aria-label": x[1] }); b.textContent = x[0];
      if (x[0] === "I") b.style.fontStyle = "italic";
      b.addEventListener("mousedown", function (ev) { ev.preventDefault(); });
      b.addEventListener("click", function () { x[2](); markDirty(); });
      ink.appendChild(b);
    });
  }
  function linkInput() {
    var r = caretRange(); if (!r || r.collapsed) return;
    savedRange = r.cloneRange();
    ink.innerHTML = "";
    var inp = el("input", "", { type: "url", placeholder: "Paste a link and press Enter", "aria-label": "Link address" });
    ink.appendChild(inp); ink.style.display = "flex"; inp.focus();
    inp.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); restore(); hideInk(); }
      if (ev.key === "Enter") {
        ev.preventDefault();
        var url = inp.value.trim();
        if (url && !/^(https?:|mailto:)/i.test(url)) url = "https://" + url;
        restore();
        if (url) { document.execCommand("createLink", false, url); markDirty(); }
        hideInk();
      }
    });
    inp.addEventListener("blur", function () { setTimeout(function () { if (document.activeElement !== inp) hideInk(); }, 150); });
  }
  function restore() { if (savedRange) { var s = window.getSelection(); s.removeAllRanges(); s.addRange(savedRange); savedRange = null; } }
  function hideInk() { ink.style.display = "none"; inkButtons(); }
  inkButtons();
  document.addEventListener("selectionchange", function () {
    if (ink.contains(document.activeElement)) return;
    var r = caretRange();
    if (!r || r.collapsed || !root.contains(r.commonAncestorContainer)) { ink.style.display = "none"; return; }
    var rect = r.getBoundingClientRect();
    ink.style.display = "flex";
    ink.style.top = (window.scrollY + rect.top - 40) + "px";
    ink.style.left = Math.max(8, window.scrollX + rect.left) + "px";
  });

  // ------------------------------------------------------------------ typing
  var SHORTCUTS = [
    [/^#{2}\s$/, { type: "header", data: { level: 2 } }],
    [/^#{3}\s$/, { type: "header", data: { level: 3 } }],
    [/^[-*]\s$/, { type: "list", data: { style: "unordered" } }],
    [/^1[.)]\s$/, { type: "list", data: { style: "ordered" } }],
    [/^>\s$/, { type: "quote", data: {} }],
    [/^(---|___)$/, { type: "delimiter", data: {} }]
  ];
  // "![" in an empty paragraph (Markdown's image syntax) opens the image picker
  function imageShortcut(w, body) {
    if (textOf(body) !== "![") return false;
    body.innerHTML = "";
    document.dispatchEvent(new CustomEvent("fh-pick-image", { detail: { after: w, clear: w } }));
    return true;
  }
  root.addEventListener("input", function (ev) {
    markDirty();
    var w = blockOf(ev.target); if (!w) return;
    var body = bodyOf(w);
    if (w.dataset.type === "paragraph") {
      if (imageShortcut(w, body)) return;
      var t = textOf(body);
      for (var i = 0; i < SHORTCUTS.length; i++) {
        if (SHORTCUTS[i][0].test(t)) {
          var n = replaceBlock(w, SHORTCUTS[i][1]);
          if (SHORTCUTS[i][1].type === "delimiter") insertAfter(n, { type: "paragraph", data: {} }); else focusBlock(n, false);
          return;
        }
      }
      var q = slashQuery(w);
      if (q !== null && q.indexOf(" ") < 0 && q.length < 20) {
        if (!menu || menu.opts.block !== w) {
          var r = caretRange();
          openMenu(body, { mode: "slash", block: w, query: q, rect: r && r.getBoundingClientRect().height ? r.getBoundingClientRect() : body.getBoundingClientRect() });
        } else renderMenu(q);
      } else if (menu && menu.opts.mode === "slash") closeMenu();
    }
    if (w.dataset.type === "list" && !body.querySelector("li")) {
      var li = el("li"); li.innerHTML = body.innerHTML; body.innerHTML = ""; body.appendChild(li); placeCaret(li, true);
    }
    if (body && body.classList.contains("blk-body") && w.dataset.type !== "list") tidy(body);
  });

  function splitAtCaret(body) {
    var r = caretRange(); if (!r) return "";
    if (!r.collapsed) r.deleteContents();
    var tail = document.createRange();
    tail.setStart(r.endContainer, r.endOffset);
    tail.setEnd(body, body.childNodes.length);
    var frag = tail.extractContents(), tmp = el("div");
    tmp.appendChild(frag);
    tidy(body);
    var html = tmp.innerHTML;
    return tmp.textContent.trim() ? html : "";
  }

  root.addEventListener("keydown", function (ev) {
    if (ev.target.nodeName === "INPUT") { if (ev.key === "Enter") ev.preventDefault(); return; }
    if (menuKey(ev)) return;
    var w = blockOf(ev.target); if (!w) return;
    var t = w.dataset.type, target = ev.target;
    if (t === "image") {
      if (target.classList.contains("blk-cap")) {
        if (ev.key === "Enter") { ev.preventDefault(); insertAfter(w, { type: "paragraph", data: {} }); }
        return;
      }
      t = "delimiter";   // the picture itself behaves like a divider: Backspace removes it, Enter adds a paragraph
    }
    var mod = ev.metaKey || ev.ctrlKey;
    if (mod && (ev.key === "k" || ev.key === "K")) { ev.preventDefault(); linkInput(); return; }

    if (t === "delimiter") {
      if (ev.key === "Backspace" || ev.key === "Delete") { ev.preventDefault(); removeBlock(w); }
      else if (ev.key === "Enter") { ev.preventDefault(); insertAfter(w, { type: "paragraph", data: {} }); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); focusBlock(w.previousElementSibling, true); }
      else if (ev.key === "ArrowDown") { ev.preventDefault(); focusBlock(w.nextElementSibling, false); }
      return;
    }
    if (t === "table") {
      if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); var c = target.closest("td"); var nx = c && (c.nextElementSibling || (c.parentNode.nextElementSibling && c.parentNode.nextElementSibling.cells[0])); if (nx) placeCaret(nx, true); else insertAfter(w, { type: "paragraph", data: {} }); }
      return;
    }
    if (t === "list") {
      var li = target.nodeName === "LI" ? target : (caretRange() && caretRange().startContainer);
      while (li && li.nodeName !== "LI" && li !== w) li = li.parentNode;
      if (!li || li.nodeName !== "LI") return;
      if (ev.key === "Enter" && !ev.shiftKey && textOf(li).trim() === "") {
        ev.preventDefault();
        var list = li.parentNode; li.remove();
        var after = insertAfter(w, { type: "paragraph", data: {} });
        if (!list.querySelector("li")) { w.remove(); focusBlock(after); }
        return;
      }
      if (ev.key === "Backspace" && caretAt(li, false) && li === li.parentNode.firstElementChild) {
        ev.preventDefault();
        var items = textHtml(w);
        if (!textOf(li).trim() && items.length === 0) { replaceBlock(w, { type: "paragraph", data: {} }); return; }
        var p = insertAfter(w.previousElementSibling, { type: "paragraph", data: { text: li.innerHTML } });
        root.insertBefore(p, w); li.remove();
        if (!w.querySelector("li")) w.remove();
        placeCaret(bodyOf(p), false);
        return;
      }
      if (ev.key === "ArrowUp" && caretAt(li, false) && li === li.parentNode.firstElementChild && w.previousElementSibling) { ev.preventDefault(); focusBlock(w.previousElementSibling, true); }
      if (ev.key === "ArrowDown" && caretAt(li, true) && li === li.parentNode.lastElementChild && w.nextElementSibling) { ev.preventDefault(); focusBlock(w.nextElementSibling, false); }
      return;
    }
    // paragraph, header, quote (text or caption)
    var body = target.classList.contains("blk-cap") ? target : bodyOf(w);
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      if (t === "quote") { insertAfter(w, { type: "paragraph", data: {} }); return; }
      var rest = splitAtCaret(body);
      insertAfter(w, { type: "paragraph", data: { text: rest } });
      return;
    }
    if (ev.key === "Backspace" && caretAt(body, false) && !target.classList.contains("blk-cap")) {
      var prev = w.previousElementSibling;
      if (textOf(body).trim() === "") {
        if (t !== "paragraph") { ev.preventDefault(); replaceBlock(w, { type: "paragraph", data: {} }); return; }
        if (prev) { ev.preventDefault(); removeBlock(w); }
        return;
      }
      if (!prev) { if (t !== "paragraph") { ev.preventDefault(); convert(w, { type: "paragraph" }); } return; }
      if (prev.dataset.type === "delimiter") { ev.preventDefault(); prev.remove(); markDirty(); return; }
      if (prev.dataset.type === "paragraph" || prev.dataset.type === "header") {
        ev.preventDefault();
        var pb = bodyOf(prev), marker = el("span"); marker.id = "ed-caret";
        tidy(pb);
        pb.appendChild(marker);
        pb.insertAdjacentHTML("beforeend", body.innerHTML);
        w.remove();
        var m = document.getElementById("ed-caret"), rr = document.createRange();
        pb.focus(); rr.setStartBefore(m); rr.collapse(true); m.remove();
        var s = window.getSelection(); s.removeAllRanges(); s.addRange(rr);
        markDirty();
      }
      return;
    }
    if (ev.key === "ArrowUp" && caretAt(body, false) && w.previousElementSibling) { ev.preventDefault(); focusBlock(w.previousElementSibling, true); }
    if (ev.key === "ArrowDown" && caretAt(body, true) && w.nextElementSibling) { ev.preventDefault(); focusBlock(w.nextElementSibling, false); }
  });

  // Paste as clean text; blank lines start new paragraphs, new lines in a list start new items.
  function insertText(s) { document.execCommand("insertText", false, s); }
  document.addEventListener("paste", function (ev) {
    var t = ev.target.closest ? ev.target.closest("[contenteditable=true]") : null;
    if (!t || !(root.contains(t) || t === titleEl || t === dekEl)) return;
    ev.preventDefault();
    var text = (ev.clipboardData || window.clipboardData).getData("text/plain").replace(/\r/g, "");
    var w = blockOf(t);
    if (!w || t.nodeName === "TD" || t.classList.contains("blk-cap") || w.dataset.type === "header") { insertText(text.replace(/\s*\n\s*/g, " ")); return; }
    if (w.dataset.type === "list") {
      var lines = text.split(/\n+/).map(function (x) { return x.replace(/^\s*([-*•]|\d+[.)])\s+/, "").trim(); }).filter(Boolean);
      if (!lines.length) return;
      insertText(lines[0]);
      var li = caretRange().startContainer; while (li && li.nodeName !== "LI") li = li.parentNode;
      lines.slice(1).forEach(function (line) { var n = el("li"); n.textContent = line; li.parentNode.insertBefore(n, li.nextSibling); li = n; });
      if (li) placeCaret(li, true);
      markDirty(); return;
    }
    var paras = text.split(/\n\s*\n/).map(function (p) { return p.replace(/\s*\n\s*/g, " ").trim(); }).filter(Boolean);
    if (!paras.length) return;
    insertText(paras[0]);
    if (paras.length > 1) {
      var rest = splitAtCaret(bodyOf(w)), ref = w;
      paras.slice(1).forEach(function (p) { var d = el("div"); d.textContent = p; ref = insertAfter(ref, { type: "paragraph", data: { text: d.innerHTML } }, false); });
      if (rest) ref = insertAfter(ref, { type: "paragraph", data: { text: rest } }, false);
      focusBlock(ref, true);
    }
    markDirty();
  });

  [titleEl, dekEl].forEach(function (n) {
    n.addEventListener("input", function () { tidy(n); markDirty(); });
    n.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); if (n === titleEl) placeCaret(dekEl, true); else focusBlock(root.firstElementChild, false); }
    });
  });

  document.getElementById("ed-add").addEventListener("click", function (ev) {
    var last = root.lastElementChild;
    if (last && last.dataset.type === "paragraph" && !textOf(bodyOf(last)).trim()) { focusBlock(last); openMenu(ev.currentTarget, { mode: "insert", after: last }); return; }
    openMenu(ev.currentTarget, { mode: "insert", after: last });
  });
  document.addEventListener("mousedown", function (ev) { if (menu && !menu.el.contains(ev.target)) closeMenu(); });
  var addImg = document.getElementById("ed-add-img");
  if (addImg) addImg.addEventListener("click", function () {
    document.dispatchEvent(new CustomEvent("fh-pick-image", { detail: { after: null } }));
  });

  // ------------------------------------------------------------------ serialise, save, count, backup
  function serialise() {
    var out = [];
    blocks().forEach(function (w) {
      var t = w.dataset.type, body = bodyOf(w);
      if (t === "paragraph") out.push({ type: t, data: { text: body.innerHTML } });
      else if (t === "header") out.push({ type: t, data: { text: body.innerHTML, level: Number(w.dataset.level) || 2 } });
      else if (t === "list") {
        var items = [];
        Array.prototype.forEach.call(body.childNodes, function (n) {
          if (n.nodeName === "LI") items.push(n.innerHTML);
          else if ((n.textContent || "").trim()) items.push(n.nodeType === 3 ? n.textContent : n.innerHTML);
        });
        out.push({ type: t, data: { style: w.dataset.style, items: items } });
      } else if (t === "quote") out.push({ type: t, data: { text: body.innerHTML, caption: w.querySelector(".blk-cap").innerHTML } });
      else if (t === "delimiter") out.push({ type: t, data: {} });
      else if (t === "image") {
        var d = {}; for (var k in (w._img || {})) d[k] = w._img[k];
        d.caption = w.querySelector(".blk-cap").innerHTML; d.alt = w.querySelector(".blk-alt").value;
        out.push({ type: t, data: d });
      }
      else if (t === "table") {
        var tbl = w.querySelector("table");
        out.push({ type: t, data: { withHeadings: tbl.classList.contains("head"),
          content: Array.prototype.map.call(tbl.rows, function (tr) { return Array.prototype.map.call(tr.cells, function (td) { return td.innerHTML; }); }) } });
      }
    });
    return out;
  }
  function state() {
    var side = {};
    Array.prototype.forEach.call(document.querySelectorAll("[form=editform][name]"), function (n) { if (n.type !== "submit") side[n.name] = n.value; });
    return { headline: textOf(titleEl).trim(), dek: textOf(dekEl).trim(), blocks: serialise(), side: side };
  }
  function plain(html) { var d = el("div"); d.innerHTML = html.replace(/<br>/g, " "); return d.textContent || ""; }
  function words() {
    var s = state(), parts = [s.headline, s.dek];
    s.blocks.forEach(function (b) {
      var d = b.data;
      if (d.text) parts.push(plain(d.text)); if (d.caption) parts.push(plain(d.caption));
      (d.items || []).forEach(function (i) { parts.push(plain(i)); });
      (d.content || []).forEach(function (r) { r.forEach(function (c) { parts.push(plain(c)); }); });
    });
    return parts.join(" ").split(/\s+/).filter(Boolean).length;
  }
  var countTimer = null;
  function scheduleCount() { clearTimeout(countTimer); countTimer = setTimeout(updateCount, 250); }
  function updateCount() {
    var n = words();
    wcEl.textContent = n.toLocaleString() + " words";
    wcEl.classList.toggle("out", n < cfg.min_words || n > cfg.max_words);
    wcEl.title = "The quality checks expect " + cfg.min_words + "–" + cfg.max_words + " words";
  }
  var backupTimer = null;
  function scheduleBackup() {
    clearTimeout(backupTimer);
    backupTimer = setTimeout(function () {
      try { localStorage.setItem(DRAFT_KEY, JSON.stringify({ at: Date.now(), state: state() })); } catch (e) { /* storage unavailable */ }
    }, 800);
  }
  function clearBackup() { try { localStorage.removeItem(DRAFT_KEY); } catch (e) { /* ignore */ } }

  function load(s) {
    titleEl.textContent = s.headline || ""; dekEl.textContent = s.dek || "";
    root.innerHTML = "";
    (s.blocks && s.blocks.length ? s.blocks : [{ type: "paragraph", data: {} }]).forEach(function (b) { root.appendChild(make(b)); });
    if (s.side) for (var k in s.side) { var n = document.querySelector("[form=editform][name=" + k + "]"); if (n) n.value = s.side[k]; }
    renderSources();
    updateCount(); countSide();
  }

  form.addEventListener("submit", function (ev) {
    closeMenu();
    var s = state();
    if (!s.headline) { ev.preventDefault(); msgEl.textContent = "Add a headline before saving."; placeCaret(titleEl); return; }
    form.querySelector("[name=headline]").value = s.headline;
    form.querySelector("[name=dek]").value = s.dek;
    form.querySelector("[name=blocks]").value = JSON.stringify(s.blocks);
    dirty = false; submitting = true; clearBackup();
    Array.prototype.forEach.call(document.querySelectorAll("button[form=editform]"), function (b) { setTimeout(function () { b.disabled = true; }, 0); });
  });
  window.addEventListener("beforeunload", function (ev) { if (dirty) { ev.preventDefault(); ev.returnValue = ""; } });
  document.addEventListener("keydown", function (ev) {
    if ((ev.metaKey || ev.ctrlKey) && (ev.key === "s" || ev.key === "S")) { ev.preventDefault(); form.requestSubmit(document.getElementById("ed-save")); }
  });
  Array.prototype.forEach.call(document.querySelectorAll("[form=editform]"), function (n) { n.addEventListener("input", function () { markDirty(); countSide(); }); });

  function countSide() {
    [["meta_description", "ed-meta-count", 155, 160], ["seo_title", "ed-title-count", 60, 65]].forEach(function (x) {
      var f = document.querySelector("[name=" + x[0] + "][form=editform]"), c = document.getElementById(x[1]);
      if (f && c) { c.textContent = f.value.length + " / " + x[2]; c.classList.toggle("out", f.value.length > x[3]); }
    });
  }
  // "Keywords the sources use": click to add one to our keywords
  Array.prototype.forEach.call(document.querySelectorAll(".kw-add"), function (b) {
    b.addEventListener("click", function () {
      var box = document.querySelector("[name=seo_keywords][form=editform]"); if (!box) return;
      var have = box.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
      var kw = b.getAttribute("data-kw");
      if (have.map(function (x) { return x.toLowerCase(); }).indexOf(kw.toLowerCase()) < 0) have.push(kw);
      box.value = have.join(", "); b.disabled = true; markDirty();
    });
  });

  // ------------------------------------------------------------------ sources (listed at the end of the story)
  var srcBox = document.getElementById("ed-sources");
  var srcField = document.createElement("input");
  srcField.type = "hidden"; srcField.name = "sources_json"; srcField.setAttribute("form", "editform");
  srcField.value = JSON.stringify(cfg.sources || []);
  if (srcBox) srcBox.parentNode.insertBefore(srcField, srcBox);
  function getSources() { try { return JSON.parse(srcField.value || "[]"); } catch (e) { return []; } }
  function setSources(list) { srcField.value = JSON.stringify(list); renderSources(); markDirty(); }
  function renderSources() {
    if (!srcBox) return;
    var list = getSources();
    srcBox.innerHTML = "";
    var ul = el("ul", "ed-src-list");
    list.forEach(function (x, i) {
      var li = el("li");
      var a = el("a"); a.href = x.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = x.title || x.url;
      li.appendChild(a);
      if (x.name) li.appendChild(document.createTextNode(" · " + x.name));
      var rm = el("button", "ed-src-rm", { type: "button", title: "Remove this source" }); rm.textContent = "✕";
      rm.addEventListener("click", function () { var l = getSources(); l.splice(i, 1); setSources(l); });
      li.appendChild(rm); ul.appendChild(li);
    });
    if (!list.length) { var none = el("li", "meta"); none.textContent = "None yet"; ul.appendChild(none); }
    srcBox.appendChild(ul);
    var add = el("div", "ed-src-add");
    var url = el("input", "", { type: "url", placeholder: "https://www.gov.uk/…", "aria-label": "Source web address" });
    var name = el("input", "", { type: "text", placeholder: "Who published it, e.g. GOV.UK (optional)", "aria-label": "Source name" });
    var title = el("input", "", { type: "text", placeholder: "Page title (optional)", "aria-label": "Page title" });
    var btn = el("button", "", { type: "button" }); btn.textContent = "Add source";
    var err = el("div", "ed-src-err");
    btn.addEventListener("click", function () {
      var u = url.value.trim();
      if (!/^https?:\/\/[^\s\/]+\.[^\s]+$/.test(u)) { err.textContent = "Paste the page's full web address (https://…)."; return; }
      var l = getSources();
      if (l.some(function (x) { return x.url === u; })) { err.textContent = "That page is already a source."; return; }
      var host = u.replace(/^https?:\/\/(www\.)?/, "").split("/")[0];
      l.push({ url: u, name: name.value.trim() || host, title: title.value.trim() || u });
      setSources(l);
    });
    [url, name, title].forEach(function (f) { f.addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.preventDefault(); btn.click(); } }); });
    add.appendChild(url); add.appendChild(name); add.appendChild(title); add.appendChild(btn); add.appendChild(err);
    srcBox.appendChild(add);
  }

  // start
  load({ headline: cfg.headline, dek: cfg.dek, blocks: cfg.blocks });
  // Restore: the newest of the server autosave (any device) and this browser's backup
  var saved = null;
  try { saved = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch (e) { saved = null; }
  if (cfg.autosave && cfg.autosave.state) {
    var at = Date.parse(cfg.autosave.at.replace(" ", "T"));
    if (!saved || !(saved.at > at)) saved = { at: isNaN(at) ? Date.now() : at, state: cfg.autosave.state, outdated: cfg.autosave.outdated, server: true };
  }
  function sig(st) { return JSON.stringify([st.headline || "", st.dek || "", st.blocks || []]); }
  if (saved && saved.state && sig(saved.state) !== sig(state())) {
    var bar = document.getElementById("ed-restore");
    bar.querySelector("span").textContent = "You have unsaved changes from " + new Date(saved.at).toLocaleString() + "." +
      (saved.outdated ? " They were made on an older version, and someone has saved since: check before restoring." : "");
    bar.hidden = false;
    bar.querySelector("[data-act=restore]").addEventListener("click", function () { load(saved.state); markDirty(); bar.hidden = true; });
    bar.querySelector("[data-act=discard]").addEventListener("click", function () {
      clearBackup(); bar.hidden = true;
      post("/story/" + cfg.story_id + "/autosave", { discard: "1" });
    });
  }

  // Autosave to the server every minute while there are changes. It is not a version: no checks,
  // no status change. "Save draft" (or ⌘S) makes the version; the autosave is then cleared.
  var autoEl = document.createElement("span"); autoEl.className = "ed-auto"; autoEl.setAttribute("role", "status");
  wcEl.parentNode.insertBefore(autoEl, wcEl);
  var autoDirty = false, autoBusy = false;
  function post(url, data, keepalive) {
    return fetch(url, { method: "POST", credentials: "same-origin", keepalive: !!keepalive,
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(data) });
  }
  function autosave(keepalive) {
    if (!autoDirty || autoBusy || submitting) return;
    autoBusy = true; autoDirty = false;
    post("/story/" + cfg.story_id + "/autosave", { state: JSON.stringify(state()), base_version: cfg.version }, keepalive)
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function () { autoEl.textContent = "Autosaved " + new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); autoEl.classList.remove("bad"); })
      .catch(function () { autoDirty = true; autoEl.textContent = "Autosave failed; your changes are kept in this browser"; autoEl.classList.add("bad"); })
      .then(function () { autoBusy = false; });
  }
  root.addEventListener("input", function () { autoDirty = true; });
  [titleEl, dekEl].forEach(function (n) { n.addEventListener("input", function () { autoDirty = true; }); });
  Array.prototype.forEach.call(document.querySelectorAll("[form=editform]"), function (n) { n.addEventListener("input", function () { autoDirty = true; }); });
  document.addEventListener("fh-editor-changed", function () { autoDirty = true; });
  setInterval(function () { autosave(false); }, 60000);
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") autosave(true); });

  var lastBlock = null;
  root.addEventListener("focusin", function (ev) { lastBlock = blockOf(ev.target) || lastBlock; });
  // Called by the images panel: put an image block after `after` (or the block last worked on), or in place of `replace`.
  function insertImage(data, opts) {
    opts = opts || {};
    if (opts.replace && opts.replace.parentNode === root) {
      var old = opts.replace, keep = old.querySelector(".blk-cap").innerHTML, keepAlt = old.querySelector(".blk-alt").value;
      if (!data.caption && keep) data.caption = keep;
      if (!data.alt && keepAlt) data.alt = keepAlt;
      return replaceBlock(old, { type: "image", data: data });
    }
    var ref = opts.after || (lastBlock && lastBlock.parentNode === root ? lastBlock : root.lastElementChild);
    var n = insertAfter(ref, { type: "image", data: data }, false);
    if (opts.clear && opts.clear.parentNode === root && !textOf(bodyOf(opts.clear)).trim()) opts.clear.remove();
    if (!n.nextElementSibling) insertAfter(n, { type: "paragraph", data: {} }, false);
    n.scrollIntoView({ block: "center", behavior: "smooth" });
    var a = n.querySelector(".blk-alt"); if (a && !a.value) a.focus();
    return n;
  }
  root.addEventListener("input", function (ev) { if (ev.target.classList && ev.target.classList.contains("blk-alt")) markDirty(); });
  // Called by the AI assistant: put its blocks after the block last worked on (or at the end).
  // An empty paragraph there is replaced rather than left behind.
  function insertBlocks(list) {
    if (!list || !list.length) return null;
    var ref = lastBlock && lastBlock.parentNode === root ? lastBlock : root.lastElementChild, first = null;
    var empty = ref && ref.dataset.type === "paragraph" && !textOf(bodyOf(ref)).trim() ? ref : null;
    list.forEach(function (b) {
      ref = insertAfter(ref, b, false);
      ref.classList.add("ai-new");
      if (!first) first = ref;
    });
    if (empty) empty.remove();
    lastBlock = ref;
    setTimeout(function () { Array.prototype.forEach.call(root.querySelectorAll(".ai-new"), function (n) { n.classList.remove("ai-new"); }); }, 4000);
    first.scrollIntoView({ block: "center", behavior: "smooth" });
    return first;
  }
  // Called by the AI assistant: add its research sources to the story's source list (skipping ones already there).
  function addSources(list) {
    var have = getSources(), added = 0;
    (list || []).forEach(function (x) {
      if (!x || !x.url || have.some(function (h) { return h.url === x.url; })) return;
      have.push({ url: x.url, name: x.name || "", title: x.title || x.url }); added++;
    });
    if (added) setSources(have);
    return added;
  }
  window.FH_EDITOR = { serialise: serialise, state: state, insertImage: insertImage, insertBlocks: insertBlocks, addSources: addSources };
})();
