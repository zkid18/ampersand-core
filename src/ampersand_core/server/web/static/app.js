/* Ampersand vault web view — vanilla JS, hash-routed.
 *
 * Three views:
 *   #/                  recent docs (paginated via cursor)
 *   #/search?q=...      BM25 search results
 *   #/doc/<id_prefix>   full doc with markdown rendered to HTML
 *
 * Auth: paste AMPERSAND_API_KEY once, store in localStorage, attach as
 * Bearer to every fetch. 401 → clear + reload.
 */

const KEY_STORAGE = "ampersand_api_key";
const PAGE_SIZE = 20;

const root = () => document.getElementById("root");

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Snippet from the server is HTML-ish: contains literal <mark>…</mark>.
// Escape everything, then re-promote the mark tags.
function snippetToSafeHtml(snippet) {
  if (!snippet) return "";
  return escapeHtml(snippet)
    .replace(/&lt;mark&gt;/g, "<mark>")
    .replace(/&lt;\/mark&gt;/g, "</mark>");
}

function getKey() {
  let k = localStorage.getItem(KEY_STORAGE);
  while (!k) {
    k = window.prompt("Paste your AMPERSAND_API_KEY (one-time):");
    if (k && k.trim()) {
      localStorage.setItem(KEY_STORAGE, k.trim());
      k = k.trim();
      break;
    }
    k = null;
  }
  return k;
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(opts.headers || {}),
      Authorization: "Bearer " + getKey(),
    },
  });
  if (r.status === 401) {
    localStorage.removeItem(KEY_STORAGE);
    location.reload();
    throw new Error("unauthorized");
  }
  return r;
}

function showLoading(label) {
  root().innerHTML = `<p class="muted">${escapeHtml(label || "loading…")}</p>`;
}

function showError(msg) {
  root().innerHTML = `<div class="error">${escapeHtml(msg)}</div>`;
}

function fmtDate(iso) {
  if (!iso) return "";
  return iso.slice(0, 10);
}

function shortId(id) {
  return id ? id.slice(0, 10) + "…" : "";
}

// ── views ──────────────────────────────────────────────────────────

let _recentCursor = null;
let _recentItems = [];
let _recentOrder = "updated";  // "updated" | "captured"

async function viewRecent(order) {
  _recentOrder = order === "captured" ? "captured" : "updated";
  showLoading(_recentOrder === "captured" ? "loading recently added…" : "loading recently updated…");
  _recentCursor = null;
  _recentItems = [];
  await loadRecentPage();
}

async function loadRecentPage() {
  let url = `/vault?limit=${PAGE_SIZE}&order=${_recentOrder}`;
  if (_recentCursor) url += `&cursor=${encodeURIComponent(_recentCursor)}`;
  let r;
  try {
    r = await api(url);
  } catch (e) {
    if (e.message !== "unauthorized") showError("failed to fetch recent: " + e.message);
    return;
  }
  if (!r.ok) {
    showError("failed to fetch recent: HTTP " + r.status);
    return;
  }
  const data = await r.json();
  _recentItems = _recentItems.concat(data.items || []);
  _recentCursor = data.next_cursor || null;
  renderRecent();
}

function renderRecent() {
  const heading = _recentOrder === "captured" ? "Recently added" : "Recently updated";
  if (!_recentItems.length) {
    root().innerHTML = `
      <div class="section-head"><h2>${heading}</h2></div>
      <p class="muted">vault is empty</p>`;
    return;
  }
  const dateField = _recentOrder === "captured" ? "captured_at" : "updated_at";
  const items = _recentItems
    .map((m) => {
      const title = escapeHtml(m.title || "(untitled)");
      const date = fmtDate(m[dateField] || m.captured_at);
      const tags = (m.tags || []).map(escapeHtml).join(", ");
      const meta = [date, tags].filter(Boolean).join(" · ");
      return `<li>
        <a class="title" href="#/doc/${escapeHtml(m.id)}">${title}</a>
        <div class="meta">${escapeHtml(meta)}</div>
      </li>`;
    })
    .join("");
  const more = _recentCursor
    ? `<button class="more-btn" id="more">load more</button>`
    : "";
  root().innerHTML = `
    <div class="section-head">
      <h2>${heading}</h2>
      <span class="count">${_recentItems.length}${_recentCursor ? "+" : ""}</span>
    </div>
    <ul class="doc-list">${items}</ul>
    ${more}
  `;
  const moreBtn = document.getElementById("more");
  if (moreBtn) {
    moreBtn.onclick = async () => {
      moreBtn.disabled = true;
      moreBtn.textContent = "loading…";
      await loadRecentPage();
    };
  }
}

async function viewSearch(query) {
  if (!query) return viewRecent();
  showLoading(`searching for ${query}…`);
  let r;
  try {
    r = await api("/vault/search", {
      method: "POST",
      body: JSON.stringify({ q: query, limit: 30, mode: "any" }),
    });
  } catch (e) {
    if (e.message !== "unauthorized") showError("search failed: " + e.message);
    return;
  }
  if (!r.ok) {
    showError("search failed: HTTP " + r.status);
    return;
  }
  const data = await r.json();
  const results = data.results || [];

  // Dedupe by doc_id, keep best section per doc
  const seen = new Set();
  const docs = [];
  for (const x of results) {
    if (seen.has(x.doc_id)) continue;
    seen.add(x.doc_id);
    docs.push(x);
  }

  if (!docs.length) {
    root().innerHTML = `
      <div class="section-head">
        <h2>Search</h2>
        <span class="count">0 for ${escapeHtml(query)}</span>
      </div>
      <p class="muted">no matches for <em>${escapeHtml(query)}</em></p>`;
    return;
  }

  const items = docs
    .map((r) => {
      const path = (r.section_path || []);
      // Collapse consecutive duplicates in the path
      const collapsed = [];
      for (const p of path) if (!collapsed.length || collapsed[collapsed.length - 1] !== p) collapsed.push(p);
      const title = escapeHtml(collapsed[collapsed.length - 1] || "(untitled)");
      const crumb = collapsed.length > 1 ? collapsed.slice(0, -1).map(escapeHtml).join(" › ") : "";
      const snippet = snippetToSafeHtml(r.snippet);
      return `<li>
        ${crumb ? `<div class="crumb">${crumb}</div>` : ""}
        <a class="title" href="#/doc/${escapeHtml(r.doc_id)}">${title}</a>
        <div class="snippet">${snippet}</div>
      </li>`;
    })
    .join("");

  root().innerHTML = `
    <div class="section-head">
      <h2>Search</h2>
      <span class="count">${docs.length} result${docs.length === 1 ? "" : "s"} for ${escapeHtml(query)}</span>
    </div>
    <ul class="doc-list">${items}</ul>
  `;
}

// Resolve an ID (full or prefix) to a real doc, then render it
async function viewDoc(idOrPrefix) {
  showLoading("loading doc…");
  let id = idOrPrefix;
  // Full ULID is 26 chars; if shorter, look up by prefix
  if (id.length < 26) {
    let r;
    try {
      r = await api(`/vault?limit=500`);
    } catch (e) {
      if (e.message !== "unauthorized") showError("lookup failed: " + e.message);
      return;
    }
    if (!r.ok) {
      showError("lookup failed: HTTP " + r.status);
      return;
    }
    const data = await r.json();
    const matches = (data.items || []).filter((m) => m.id.startsWith(id.toUpperCase()));
    if (!matches.length) {
      showError(`no doc found with id prefix "${id}"`);
      return;
    }
    if (matches.length > 1) {
      showError(`prefix "${id}" matches ${matches.length} docs — be more specific`);
      return;
    }
    id = matches[0].id;
  }

  let r;
  try {
    r = await api(`/vault/${encodeURIComponent(id)}`);
  } catch (e) {
    if (e.message !== "unauthorized") showError("fetch failed: " + e.message);
    return;
  }
  if (!r.ok) {
    showError("fetch failed: HTTP " + r.status);
    return;
  }
  const doc = await r.json();
  renderDoc(doc);
}

function renderDoc(doc) {
  const title = escapeHtml(doc.title || "(untitled)");
  const date = fmtDate(doc.captured_at);
  const tags = (doc.tags || []).map(escapeHtml).join(", ");
  const source = doc.source
    ? `<a href="${escapeHtml(doc.source)}" target="_blank" rel="noopener noreferrer">source</a>`
    : "";
  const author = doc.extra && doc.extra.author ? escapeHtml(doc.extra.author) : "";
  const metaBits = [date, author, tags, source].filter(Boolean).join(" · ");

  let bodyHtml = "";
  try {
    // marked is loaded from CDN; treat output as trusted markdown render of our own data
    bodyHtml = window.marked ? window.marked.parse(doc.body || "") : escapeHtml(doc.body || "");
  } catch (e) {
    bodyHtml = `<pre>${escapeHtml(doc.body || "")}</pre>`;
  }

  const isEmail = (doc.source || "").startsWith("email://");
  const deleteHint = isEmail
    ? "delete this doc and feed it back as a SKIP signal for the classifier"
    : "delete this doc";

  root().innerHTML = `
    <article class="doc">
      <div class="doc-toolbar">
        <button id="doc-delete" class="doc-delete" title="${escapeHtml(deleteHint)}">delete</button>
      </div>
      <h1>${title}</h1>
      <div class="doc-meta">${metaBits}</div>
      <div class="doc-body">${bodyHtml}</div>
    </article>
  `;

  document.getElementById("doc-delete").addEventListener("click", async () => {
    const confirmMsg = isEmail
      ? `Delete "${doc.title || doc.id}" and tell the classifier to skip future ones like it?`
      : `Delete "${doc.title || doc.id}"?`;
    if (!confirm(confirmMsg)) return;
    const r = await api(`/vault/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
    if (!r.ok && r.status !== 204) {
      showError("delete failed: HTTP " + r.status);
      return;
    }
    location.hash = "#/";
  });
}

// ── router ─────────────────────────────────────────────────────────

function highlightTab(name) {
  document.querySelectorAll(".tabs a").forEach((a) => {
    a.classList.toggle("active", a.dataset.tab === name);
  });
}

function dispatch() {
  const h = location.hash || "#/";
  if (h.startsWith("#/search")) {
    const qs = new URLSearchParams(h.split("?")[1] || "");
    const q = qs.get("q") || "";
    document.getElementById("q").value = q;
    highlightTab(null);
    viewSearch(q);
  } else if (h.startsWith("#/doc/")) {
    document.getElementById("q").value = "";
    highlightTab(null);
    viewDoc(decodeURIComponent(h.slice(6)));
  } else if (h.startsWith("#/added")) {
    document.getElementById("q").value = "";
    highlightTab("added");
    viewRecent("captured");
  } else {
    document.getElementById("q").value = "";
    highlightTab("updated");
    viewRecent("updated");
  }
}

addEventListener("hashchange", dispatch);

addEventListener("DOMContentLoaded", () => {
  document.getElementById("searchForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = document.getElementById("q").value.trim();
    if (q) location.hash = "#/search?q=" + encodeURIComponent(q);
    else location.hash = "#/";
  });
  document.getElementById("logout").onclick = () => {
    if (confirm("Forget the saved API key?")) {
      localStorage.removeItem(KEY_STORAGE);
      location.reload();
    }
  };
  // Trigger key prompt eagerly so the user knows what's happening
  getKey();
  dispatch();
});
