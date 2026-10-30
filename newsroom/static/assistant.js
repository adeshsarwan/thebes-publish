/* AI assistant in the story editor sidebar.
 *
 * Sends the question with the story as it is on screen (unsaved edits included),
 * then polls until the answer arrives. Answers can carry text to add (inserted as
 * blocks where you were last writing) and research sources (added to the story's
 * source list). Nothing changes in the story until you click a button.
 */
(function () {
  "use strict";
  var panel = document.getElementById("ai-panel");
  if (!panel || !window.FH_EDITOR) return;
  var storyId = panel.getAttribute("data-story");
  var thread = document.getElementById("ai-thread");
  var formEl = document.getElementById("ai-ask");
  var q = document.getElementById("ai-q");
  var web = document.getElementById("ai-web");
  var send = document.getElementById("ai-send");
  var err = document.getElementById("ai-err");
  var ideas = document.getElementById("ai-ideas");
  var timer = null, shown = {};

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function busy(list) { return list.some(function (m) { return m.status === "queued" || m.status === "running"; }); }

  function renderMessage(m) {
    var box = el("div", "ai-msg");
    box.appendChild(el("div", "ai-q", m.question));
    var who = el("div", "ai-who", m.who + (m.research ? " · searched the web" : ""));
    box.appendChild(who);
    var a = el("div", "ai-a");
    if (m.status === "queued" || m.status === "running") {
      a.appendChild(el("div", "ai-wait", m.research ? "Searching and writing…" : "Thinking…"));
    } else if (m.status === "failed") {
      a.appendChild(el("div", "ai-fail", m.error || "Something went wrong. Try again."));
    } else {
      var reply = el("div", "ai-reply"); reply.innerHTML = m.reply_html;   // server-rendered, escaped markdown
      a.appendChild(reply);
      if (m.blocks && m.blocks.length) {
        var prev = el("div", "ai-add");
        prev.appendChild(el("div", "ai-add-h", "Text to add"));
        m.blocks.forEach(function (b) {
          var d = b.data || {}, n;
          if (b.type === "header") n = el("b", "ai-add-hd");
          else if (b.type === "list") { n = el(d.style === "ordered" ? "ol" : "ul"); (d.items || []).forEach(function (i) { var li = el("li"); li.innerHTML = i; n.appendChild(li); }); prev.appendChild(n); return; }
          else n = el("p");
          n.innerHTML = d.text || "";   // sanitised inline HTML from the server
          prev.appendChild(n);
        });
        a.appendChild(prev);
        var ins = el("button", "primary ai-btn", "Insert into story");
        ins.type = "button";
        ins.title = "Adds it after the block you were last writing in (or at the end)";
        ins.addEventListener("click", function () {
          window.FH_EDITOR.insertBlocks(JSON.parse(JSON.stringify(m.blocks)));
          var n = m.sources && m.sources.length ? window.FH_EDITOR.addSources(m.sources) : 0;
          ins.textContent = "Inserted" + (n ? " · " + n + " source" + (n > 1 ? "s" : "") + " added" : "");
          setTimeout(function () { ins.textContent = "Insert again"; }, 2500);
        });
        a.appendChild(ins);
      }
      if (m.sources && m.sources.length) {
        var ul = el("ul", "ai-src");
        m.sources.forEach(function (s) {
          var li = el("li"), link = el("a", "", s.title || s.name || s.url);
          link.href = s.url; link.target = "_blank"; link.rel = "noopener";
          li.appendChild(link); ul.appendChild(li);
        });
        a.appendChild(el("div", "ai-add-h", "Sources"));
        a.appendChild(ul);
        var add = el("button", "ai-btn", "Add sources to the story");
        add.type = "button";
        add.addEventListener("click", function () {
          var n = window.FH_EDITOR.addSources(m.sources);
          add.textContent = n ? n + " added" : "Already in the story";
        });
        a.appendChild(add);
      }
      if (m.blocks && m.blocks.length) a.appendChild(el("p", "meta ai-note", "Check names and figures before publishing; the checks run when you save."));
    }
    box.appendChild(a);
    return box;
  }

  function render(list) {
    var atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 40;
    thread.innerHTML = "";
    list.forEach(function (m) { thread.appendChild(renderMessage(m)); });
    thread.hidden = !list.length;
    ideas.hidden = list.length > 0;
    var fresh = list.some(function (m) { return m.status === "done" && !shown[m.id]; });
    list.forEach(function (m) { if (m.status === "done") shown[m.id] = true; });
    if (atBottom || fresh) thread.scrollTop = thread.scrollHeight;
    send.disabled = busy(list);
    clearTimeout(timer);
    if (busy(list)) timer = setTimeout(load, 2500);
  }

  function load() {
    fetch("/story/" + storyId + "/assistant.json", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) { render(d.messages || []); })
      .catch(function () { clearTimeout(timer); timer = setTimeout(load, 5000); });
  }

  formEl.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = q.value.trim();
    if (text.length < 3) { err.textContent = "Type what you'd like the AI to do."; return; }
    err.textContent = ""; send.disabled = true;
    var body = new URLSearchParams({ question: text, research: web.checked ? "1" : "0",
                                     state: JSON.stringify(window.FH_EDITOR.state()) });
    fetch("/story/" + storyId + "/assistant", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: body })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (!x.ok) { err.textContent = x.d.error || "Couldn't send that."; send.disabled = false; return; }
        q.value = ""; render(x.d.messages || []); thread.scrollTop = thread.scrollHeight;
      })
      .catch(function () { err.textContent = "Couldn't reach the desk. Check your connection."; send.disabled = false; });
  });
  q.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); formEl.requestSubmit(); }
  });
  Array.prototype.forEach.call(document.querySelectorAll(".ai-idea"), function (b) {
    b.addEventListener("click", function () { q.value = b.textContent; q.focus(); });
  });
  var opener = document.getElementById("ai-open");
  if (opener) opener.addEventListener("click", function () {
    panel.scrollIntoView({ block: "start", behavior: "smooth" });
    setTimeout(function () { q.focus(); }, 300);
  });
  load();
})();
