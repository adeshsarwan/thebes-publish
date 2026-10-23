/* Story Hub images panel: stock photos (Unsplash, Pexels), AI images (Nano Banana) and uploads.
 * Used on the story page and in the block editor's sidebar. Everything external is put in the
 * page with textContent / attributes, never as HTML. */
(function () {
  "use strict";
  var boot = JSON.parse(document.getElementById("img-data").textContent);
  var panel = document.getElementById("img-panel");
  var body = panel.querySelector(".img-body");
  var sid = boot.story_id, editor = boot.mode === "editor";
  var state = boot.state, pollTimer = null, pending = null;   // pending: what to do with the next image added (editor picker)
  var KIND = { unsplash: "Unsplash", pexels: "Pexels", gemini: "AI image", upload: "Upload" };
  var ROLE = { hero: "Main image", inline: "In the story", gallery: "Not used yet" };

  function el(tag, cls, attrs, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (attrs) for (var k in attrs) if (attrs[k] !== null && attrs[k] !== undefined) n.setAttribute(k, attrs[k]);
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function button(text, cls, fn, title) {
    var b = el("button", cls || "", { type: "button", title: title || null }, text);
    b.addEventListener("click", function (ev) { ev.preventDefault(); fn(b); });
    return b;
  }
  function select(name, options, value) {
    var s = el("select", "", { name: name, "aria-label": name });
    options.forEach(function (o) {
      var v = Array.isArray(o) ? o[0] : o, t = Array.isArray(o) ? o[1] : o;
      var op = el("option", "", { value: v }, t); if (String(v) === String(value)) op.selected = true; s.appendChild(op);
    });
    return s;
  }
  function thumb(img) {
    var sizes = img.sizes || {}, best = null;
    Object.keys(sizes).forEach(function (w) { if (+w >= 320 && (!best || +w < +best)) best = w; });
    return best ? sizes[best] : (img.thumb || img.url);
  }
  function toast(msg, bad) {
    var t = panel.querySelector(".img-toast") || el("div", "img-toast", { role: "status" });
    t.textContent = msg; t.classList.toggle("bad", !!bad);
    if (!t.parentNode) panel.insertBefore(t, body);
    clearTimeout(t._h); t._h = setTimeout(function () { t.textContent = ""; }, bad ? 8000 : 4000);
  }

  // ------------------------------------------------------------------ server
  function post(url, data) {
    return fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(data || {}) })
      .then(answer);
  }
  function answer(r) {
    return r.json().catch(function () { return { error: "The server sent an unexpected answer (" + r.status + ")." }; })
      .then(function (j) { if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status)); return j; });
  }
  function refresh() {
    return fetch("/story/" + sid + "/images.json", { credentials: "same-origin" }).then(answer)
      .then(function (s) { state = s; render(); return s; })
      .catch(function (e) { toast(e.message, true); });
  }
  function poll() {
    clearTimeout(pollTimer);
    if (state.busy) pollTimer = setTimeout(function () { refresh().then(function () { poll(); }); }, 4000);
  }

  // ------------------------------------------------------------------ list
  function render() {
    body.innerHTML = "";
    var st = state.status;
    if (state.can_manage) {
      var bar = el("div", "img-actions");
      bar.appendChild(button("Find stock photo", "primary", function () { openDialog("stock"); }));
      bar.appendChild(button("Generate with AI", "", function () { openDialog("generate"); },
        st.generate ? null : "Needs the Gemini key and R2 storage on the server"));
      bar.appendChild(button("Upload", "", function () { openDialog("upload"); }, st.storage ? null : "Needs R2 storage on the server"));
      body.appendChild(bar);
    }
    if (state.live && state.site_differs) {
      var sync = el("div", "img-note warn", null, "The live story shows different images. ");
      if (state.can_sync) sync.appendChild(button("Update the live story's images", "", function (b) {
        b.disabled = true;
        post("/story/" + sid + "/images/sync").then(function (j) { toast(j.count + " image(s) are now on the live story."); refresh(); })
          .catch(function (e) { b.disabled = false; toast(e.message, true); });
      }));
      else sync.appendChild(document.createTextNode("An editor or publisher can send them to the site."));
      body.appendChild(sync);
    } else if (!state.live && !editor) {
      body.appendChild(el("p", "meta", null, "The main image and in-story images go to the site when the story is published."));
    }
    var imgs = state.images || [];
    if (!imgs.length) body.appendChild(el("p", "meta", null, "No images yet." + (state.can_manage ? " Search stock photos first; generate an illustration if nothing fits." : "")));
    var list = el("div", "img-list");
    imgs.forEach(function (img) { list.appendChild(card(img)); });
    body.appendChild(list);
    poll();
  }

  function card(img) {
    var c = el("div", "img-card s-" + img.status + " r-" + img.role);
    var pic = el("div", "img-pic");
    if (img.status === "ready" && img.url) pic.appendChild(el("img", "", { src: thumb(img), alt: img.alt || "", loading: "lazy" }));
    else pic.appendChild(el("div", "img-ph", null, img.status === "draft" ? "Prompt" : img.status === "failed" ? "Failed" : "Generating…"));
    if (img.status === "queued" || img.status === "generating") pic.classList.add("busy");
    c.appendChild(pic);
    var info = el("div", "img-info");
    var head = el("div", "img-head");
    head.appendChild(el("span", "img-badge role", null, img.status === "ready" ? ROLE[img.role] : img.status === "draft" ? "Not generated" : img.status));
    head.appendChild(el("span", "img-badge", null, KIND[img.provider] || img.kind));
    info.appendChild(head);
    if (img.prompt && img.kind === "generated") info.appendChild(el("div", "img-prompt", null, img.prompt));
    if (img.credit && img.kind !== "generated") info.appendChild(creditLine(img));
    if (img.status === "failed" && img.error) info.appendChild(el("div", "img-err", null, img.error));
    if (state.can_manage) info.appendChild(controls(img));
    c.appendChild(info);
    return c;
  }

  function controls(img) {
    var box = el("div", "img-ctl");
    if (img.status === "draft" || img.status === "failed") {
      var ta = el("textarea", "", { rows: "3", "aria-label": "Prompt" }); ta.value = img.prompt || "";
      var style = select("style", boot.styles, img.style), aspect = select("aspect", boot.aspects, img.aspect || boot.default_aspect);
      box.appendChild(ta);
      var row = el("div", "img-row"); row.appendChild(style); row.appendChild(aspect); box.appendChild(row);
      var r2 = el("div", "img-row");
      r2.appendChild(button(img.status === "failed" ? "Try again" : "Generate", "primary", function (b) {
        b.disabled = true;
        post("/story/" + sid + "/images/generate", { prompt: ta.value, style: style.value, aspect: aspect.value, draft_id: img.id, count: 1 })
          .then(function () { toast("Generating. It takes about 10–30 seconds."); refresh(); })
          .catch(function (e) { b.disabled = false; toast(e.message, true); });
      }, "About $0.05 with " + boot.model));
      r2.appendChild(button("Remove", "link", function () { remove(img); }));
      box.appendChild(r2);
      return box;
    }
    if (img.status !== "ready") return box;
    var roles = [["hero", "Main image"], ["inline", "In the story"], ["gallery", "Not used"]];
    var role = select("role", roles, img.role);
    var row1 = el("div", "img-row"); row1.appendChild(role);
    var pos = null;
    if (!boot.blocks && boot.sections.length) {
      pos = select("position", [["", "After the first section"]].concat(boot.sections.map(function (s) { return [s[0], "After “" + s[1] + "”"]; })), img.position || "");
      pos.hidden = img.role !== "inline";
      role.addEventListener("change", function () { pos.hidden = role.value !== "inline"; });
      row1.appendChild(pos);
    }
    box.appendChild(row1);
    var alt = el("input", "", { type: "text", placeholder: "Alt text: what the image shows", "aria-label": "Alt text" }); alt.value = img.alt || "";
    var cap = el("input", "", { type: "text", placeholder: "Caption (optional)", "aria-label": "Caption" }); cap.value = img.caption || "";
    box.appendChild(alt); box.appendChild(cap);
    var credit = null;
    if (img.kind === "upload") {
      credit = el("input", "", { type: "text", placeholder: "Credit, e.g. Photo: Jane Doe / Agency", "aria-label": "Credit" });
      credit.value = img.credit || ""; box.appendChild(credit);
    }
    var row2 = el("div", "img-row");
    var save = button("Save", "", function (b) {
      var data = { role: role.value, alt: alt.value, caption: cap.value };
      if (pos) data.position = pos.value;
      if (credit) data.credit = credit.value;
      b.disabled = true;
      post("/images/" + img.id + "/update", data).then(function () { toast("Saved."); refresh(); })
        .catch(function (e) { b.disabled = false; toast(e.message, true); });
    });
    save.disabled = true;
    [role, alt, cap, credit, pos].forEach(function (f) { if (f) f.addEventListener(f.tagName === "SELECT" ? "change" : "input", function () { save.disabled = false; save.classList.add("primary"); }); });
    row2.appendChild(save);
    if (editor && window.FH_EDITOR && window.FH_EDITOR.insertImage) {
      row2.appendChild(button("Insert in story", "", function () {
        insert(Object.assign({}, img, { alt: alt.value, caption: cap.value }), pending || {}); pending = null;
      }, "Adds it as an image block where you were last typing"));
    }
    row2.appendChild(button("Remove", "link", function () { remove(img); }));
    box.appendChild(row2);
    return box;
  }

  var HOME = { unsplash: ["Unsplash", "https://unsplash.com/?utm_source=story_hub&utm_medium=referral"], pexels: ["Pexels", "https://www.pexels.com"] };
  // "Photo by <photographer> on <Unsplash>", both linked, as the photo sites ask
  function creditLine(img) {
    var cr = el("div", "img-credit"), home = HOME[img.provider];
    if (home && img.photographer) {
      cr.appendChild(document.createTextNode("Photo by "));
      cr.appendChild(img.photographer_url ? el("a", "", { href: img.photographer_url, target: "_blank", rel: "noopener" }, img.photographer) : document.createTextNode(img.photographer));
      cr.appendChild(document.createTextNode(" on "));
      cr.appendChild(el("a", "", { href: home[1], target: "_blank", rel: "noopener" }, home[0]));
    } else if (img.credit_url) cr.appendChild(el("a", "", { href: img.credit_url, target: "_blank", rel: "noopener" }, img.credit));
    else cr.textContent = img.credit || "";
    return cr;
  }
  function remove(img) {
    post("/images/" + img.id + "/remove").then(function () { toast("Removed from the story."); refresh(); })
      .catch(function (e) { toast(e.message, true); });
  }
  function insert(img, where) {
    window.FH_EDITOR.insertImage({ url: img.url, alt: img.alt || "", caption: img.caption || "", credit: img.credit || "",
      credit_url: img.credit_url || "", provider: img.provider, photographer: img.photographer || "",
      photographer_url: img.photographer_url || "", image_id: img.id, width: img.width || undefined, height: img.height || undefined,
      sizes: img.sizes || {} }, where);
    closeDialog();
  }

  // ------------------------------------------------------------------ dialog
  var dlg = null;
  function closeDialog() { if (dlg && dlg.open) dlg.close(); }
  function openDialog(tab) {
    if (!dlg) {
      dlg = el("dialog", "img-dialog", { "aria-label": "Add images" });
      document.body.appendChild(dlg);
      dlg.addEventListener("click", function (ev) { if (ev.target === dlg) closeDialog(); });
      dlg.addEventListener("close", function () { if (pending && !pending.keep) pending = null; });
    }
    dlg.innerHTML = "";
    var top = el("div", "img-dtop");
    var tabs = el("div", "img-tabs", { role: "tablist" });
    var pane = el("div", "img-pane");
    var defs = [["stock", "Stock photos"], ["generate", "Generate with AI"], ["upload", "Upload"]];
    if (pending) defs.unshift(["story", "This story's images"]);
    defs.forEach(function (d) {
      var t = button(d[1], "img-tab" + (d[0] === tab ? " on" : ""), function () { openDialog(d[0]); });
      t.setAttribute("role", "tab"); tabs.appendChild(t);
    });
    top.appendChild(tabs);
    top.appendChild(button("✕", "img-x", closeDialog, "Close"));
    dlg.appendChild(top); dlg.appendChild(pane);
    ({ story: storyTab, stock: stockTab, generate: generateTab, upload: uploadTab }[tab] || stockTab)(pane);
    if (!dlg.open) dlg.showModal();
  }

  function afterAdd(id) {
    // in the editor picker, put the new image straight into the story
    refresh().then(function () {
      if (!pending) return;
      var img = (state.images || []).filter(function (i) { return i.id === id; })[0];
      if (img && img.status === "ready") { var w = pending; pending = null; insert(img, w); }
    });
  }

  function storyTab(pane) {
    var ready = (state.images || []).filter(function (i) { return i.status === "ready"; });
    if (!ready.length) { pane.appendChild(el("p", "meta", null, "No finished images yet: find a stock photo, generate one or upload one.")); return; }
    var grid = el("div", "img-grid");
    ready.forEach(function (img) {
      var c = el("div", "img-res");
      c.appendChild(el("img", "", { src: thumb(img), alt: img.alt || "", loading: "lazy" }));
      c.appendChild(img.credit ? creditLine(img) : el("div", "img-credit", null, KIND[img.provider] || ""));
      c.appendChild(button("Insert", "primary", function () { var w = pending; pending = null; insert(img, w); }));
      grid.appendChild(c);
    });
    pane.appendChild(grid);
  }

  function stockTab(pane) {
    var st = state.status;
    var form = el("form", "img-search");
    var q = el("input", "", { type: "search", name: "q", placeholder: "Search Unsplash and Pexels, e.g. family budget kitchen", "aria-label": "Search" });
    q.value = stockTab.last || boot.query || "";
    var provs = [["unsplash", "Unsplash"], ["pexels", "Pexels"]].filter(function (x) { return st[x[0]]; });
    var prov = select("provider", (provs.length > 1 ? [["all", "Unsplash + Pexels"]] : []).concat(provs.length ? provs : [["all", "Stock"]]),
      stockTab.prov || (provs.length > 1 ? "all" : (provs[0] || ["all"])[0]));
    var orient = select("orientation", [["landscape", "Landscape"], ["portrait", "Portrait"], ["square", "Square"], ["any", "Any shape"]], stockTab.orient || "landscape");
    form.appendChild(q); form.appendChild(prov); form.appendChild(orient);
    form.appendChild(el("button", "primary", { type: "submit" }, "Search"));
    pane.appendChild(form);
    if (!st.unsplash && !st.pexels) pane.appendChild(el("p", "img-note warn", null, "Stock search isn't set up on this server: add UNSPLASH_ACCESS_KEY to its settings."));
    pane.appendChild(el("p", "meta", null, "Photos are shown from " + (provs.length === 1 ? provs[0][1] : "the photo site") + " with a credit to the photographer, as the licence asks. Free to use; no fee."));
    var errs = el("div", "img-err"), grid = el("div", "img-grid"), more = el("div", "img-more");
    pane.appendChild(errs); pane.appendChild(grid); pane.appendChild(more);
    var page = 1;
    function run(reset) {
      if (reset) { page = 1; grid.innerHTML = ""; }
      stockTab.last = q.value; stockTab.prov = prov.value; stockTab.orient = orient.value;
      errs.textContent = ""; more.innerHTML = ""; more.appendChild(el("span", "meta", null, "Searching…"));
      var url = "/story/" + sid + "/images/search?" + new URLSearchParams({ q: q.value, provider: prov.value, orientation: orient.value, page: page });
      fetch(url, { credentials: "same-origin" }).then(answer).then(function (j) {
        more.innerHTML = "";
        var e = Object.keys(j.errors || {}).map(function (k) { return k + ": " + j.errors[k]; });
        errs.textContent = e.join(" · ");
        if (!j.results.length && page === 1) grid.appendChild(el("p", "meta", null, "Nothing found. Try simpler words: a scene, not a headline."));
        j.results.forEach(function (p) { grid.appendChild(result(p)); });
        if (j.results.length) more.appendChild(button("More results", "", function () { page += 1; run(false); }));
      }).catch(function (e) { more.innerHTML = ""; errs.textContent = e.message; });
    }
    form.addEventListener("submit", function (ev) { ev.preventDefault(); if (q.value.trim()) run(true); });
    if (q.value.trim() && (st.unsplash || st.pexels)) run(true);
    setTimeout(function () { q.focus(); }, 30);
  }
  function result(p) {
    var c = el("div", "img-res");
    var a = el("a", "", { href: p.source_url, target: "_blank", rel: "noopener", title: "Open on " + KIND[p.provider] });
    a.appendChild(el("img", "", { src: p.thumb, alt: p.alt || "", loading: "lazy" }));
    c.appendChild(a);
    var cl = creditLine(p); if (p.width) cl.appendChild(document.createTextNode(" · " + p.width + "×" + p.height));
    c.appendChild(cl);
    var row = el("div", "img-row");
    function add(role, b) {
      b.disabled = true;
      post("/story/" + sid + "/images/stock", { provider: p.provider, provider_id: p.provider_id, role: role, alt: p.alt || "" })
        .then(function (j) { b.textContent = "Added"; toast("Added to the story's images."); afterAdd(j.id); })
        .catch(function (e) { b.disabled = false; toast(e.message, true); });
    }
    if (pending) row.appendChild(button("Insert", "primary", function (b) { add(editor ? "inline" : "gallery", b); }));
    else {
      row.appendChild(button("Main image", "primary", function (b) { add("hero", b); }));
      row.appendChild(button(boot.blocks ? "Add" : "In the story", "", function (b) { add(boot.blocks ? "gallery" : "inline", b); }));
    }
    c.appendChild(row);
    return c;
  }

  function generateTab(pane) {
    var st = state.status;
    if (!st.generate) {
      pane.appendChild(el("p", "img-note warn", null, "Generating isn't set up on this server yet: it needs the Gemini key and Cloudflare R2 storage (" +
        (st.storage ? "storage is ready" : "R2 keys missing") + "). You can still save prompts for later."));
    }
    var ta = el("textarea", "", { rows: "4", placeholder: "Describe the scene: who or what, where, mood. Avoid text, logos and real people.", "aria-label": "Prompt" });
    ta.value = generateTab.last || boot.prompt || "";
    var style = select("style", boot.styles, boot.styles[0]);
    var aspect = select("aspect", boot.aspects, boot.default_aspect);
    var counts = []; for (var i = 1; i <= boot.max_variations; i++) counts.push([i, i === 1 ? "1 image" : i + " versions"]);
    var count = select("count", counts, 1);
    var role = select("role", [["hero", "Main image"], ["inline", "In the story"], ["gallery", "Decide later"]], "gallery");
    pane.appendChild(ta);
    var row = el("div", "img-row"); [style, aspect, count, role].forEach(function (x) { row.appendChild(x); }); pane.appendChild(row);
    var cost = el("p", "meta");
    function updateCost() { cost.textContent = "About $" + (0.05 * count.value).toFixed(2) + " with " + boot.model + ". Results appear in the list in 10–30 seconds."; }
    count.addEventListener("change", updateCost); updateCost(); pane.appendChild(cost);
    var row2 = el("div", "img-row");
    var go = button("Generate", "primary", function (b) {
      generateTab.last = ta.value; b.disabled = true;
      post("/story/" + sid + "/images/generate", { prompt: ta.value, style: style.value, aspect: aspect.value, count: count.value, role: role.value })
        .then(function () { toast("Generating…"); closeDialog(); refresh(); })
        .catch(function (e) { b.disabled = false; toast(e.message, true); });
    });
    go.disabled = !st.generate;
    row2.appendChild(go);
    row2.appendChild(button("Save prompt for later", "", function (b) {
      generateTab.last = ta.value; b.disabled = true;
      post("/story/" + sid + "/images/prompt", { prompt: ta.value, style: style.value, aspect: aspect.value })
        .then(function () { toast("Prompt saved on the story."); closeDialog(); refresh(); })
        .catch(function (e) { b.disabled = false; toast(e.message, true); });
    }));
    pane.appendChild(row2);
  }

  function uploadTab(pane) {
    var st = state.status;
    if (!st.storage) { pane.appendChild(el("p", "img-note warn", null, "Uploads need Cloudflare R2 storage: add the R2 keys to the server's settings.")); return; }
    var file = el("input", "", { type: "file", accept: "image/jpeg,image/png,image/webp,image/gif", "aria-label": "Image file" });
    var alt = el("input", "", { type: "text", placeholder: "Alt text: what the image shows" });
    var credit = el("input", "", { type: "text", placeholder: "Credit, e.g. Photo: Jane Doe / Agency (only use images you have the rights to)" });
    var role = select("role", [["gallery", "Decide later"], ["hero", "Main image"], ["inline", "In the story"]], pending ? "inline" : "gallery");
    [file, alt, credit, role].forEach(function (x) { pane.appendChild(x); });
    pane.appendChild(el("p", "meta", null, "JPEG, PNG, WebP or GIF up to " + boot.max_upload_mb + " MB. Saved as WebP in several sizes."));
    pane.appendChild(button("Upload", "primary", function (b) {
      var f = file.files && file.files[0];
      if (!f) { toast("Choose a file first.", true); return; }
      if (f.size > boot.max_upload_mb * 1048576) { toast("That file is over " + boot.max_upload_mb + " MB.", true); return; }
      b.disabled = true; b.textContent = "Uploading…";
      fetch("/story/" + sid + "/images/upload?" + new URLSearchParams({ role: role.value, alt: alt.value, credit: credit.value }), {
        method: "POST", credentials: "same-origin", body: f,
        headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(f.name) } })
        .then(answer).then(function (j) { toast("Uploaded."); if (!pending) closeDialog(); afterAdd(j.id); })
        .catch(function (e) { b.disabled = false; b.textContent = "Upload"; toast(e.message, true); });
    }));
  }

  // the block editor's "Image" block asks us for a picture
  document.addEventListener("fh-pick-image", function (ev) {
    if (!state.can_manage) return;
    pending = ev.detail || {};
    var ready = (state.images || []).some(function (i) { return i.status === "ready"; });
    openDialog(ready ? "story" : "stock");
  });

  render();
})();
