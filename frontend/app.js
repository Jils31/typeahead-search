// Frontend logic: debounced suggestions, keyboard nav, search submit, trending.
const $ = (id) => document.getElementById(id);
const qEl = $("q"), dd = $("dropdown"), statusEl = $("status"),
      sourceEl = $("source"), answerEl = $("answer"), trendingEl = $("trending");

let items = [];        // current suggestions
let active = -1;       // keyboard-highlighted index
let debounceTimer = null;

function mode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

// ---- suggestions (debounced) ----
qEl.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(fetchSuggestions, 150); // debounce: avoid a call per keystroke
});

document.querySelectorAll('input[name="mode"]').forEach(r =>
  r.addEventListener("change", fetchSuggestions));

async function fetchSuggestions() {
  const q = qEl.value.trim();
  if (!q) { hideDropdown(); sourceEl.textContent = "—"; sourceEl.className = "badge"; return; }
  statusEl.textContent = "loading…"; statusEl.className = "status";
  try {
    const res = await fetch(`/suggest?q=${encodeURIComponent(q)}&mode=${mode()}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    items = data.suggestions || [];
    active = -1;
    renderDropdown();
    const hit = data.source === "cache";
    sourceEl.textContent = data.source === "cache" ? `cache HIT · ${data.node}`
                         : data.source === "trie" ? `cache MISS → trie · ${data.node || ""}`
                         : data.source;
    sourceEl.className = "badge " + (hit ? "hit" : data.source === "trie" ? "miss" : "");
    statusEl.textContent = items.length ? "" : "no matches";
  } catch (e) {
    statusEl.textContent = "Error fetching suggestions: " + e.message;
    statusEl.className = "status err";
    hideDropdown();
  }
}

function renderDropdown() {
  if (!items.length) { hideDropdown(); return; }
  dd.innerHTML = items.map((it, i) =>
    `<div class="item ${i === active ? "active" : ""}" data-i="${i}">
       <span>${escapeHtml(it.query)}</span><span class="c">${it.count.toLocaleString()}</span>
     </div>`).join("");
  dd.style.display = "block";
  dd.querySelectorAll(".item").forEach(el =>
    el.addEventListener("mousedown", (ev) => { ev.preventDefault(); submit(items[+el.dataset.i].query); }));
}

function hideDropdown() { dd.style.display = "none"; items = []; active = -1; }

// ---- keyboard navigation ----
qEl.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, items.length - 1); renderDropdown(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, -1); renderDropdown(); }
  else if (e.key === "Enter") {
    e.preventDefault();
    submit(active >= 0 ? items[active].query : qEl.value.trim());
  } else if (e.key === "Escape") { hideDropdown(); }
});

$("go").addEventListener("click", () => submit(qEl.value.trim()));

// ---- submit search ----
async function submit(query) {
  if (!query) return;
  qEl.value = query;
  hideDropdown();
  try {
    const res = await fetch("/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    answerEl.style.display = "block";
    answerEl.style.color = "";
    answerEl.textContent = `${data.message}: "${query}"`;
    setTimeout(() => loadTrending(), 400); // reflect new activity soon
  } catch (e) {
    answerEl.style.display = "block";
    answerEl.style.color = "#ff6b6b";
    answerEl.textContent = "Error submitting search: " + e.message;
  }
}

// ---- trending panel ----
async function loadTrending() {
  try {
    const res = await fetch("/trending?n=10");
    const data = await res.json();
    const list = data.trending || [];
    trendingEl.innerHTML = list.length
      ? list.map((t, i) => `<div class="trend"><span>${i + 1}. ${escapeHtml(t.query)}</span><span class="s">${t.score}</span></div>`).join("")
      : '<div class="trend"><span>No activity yet — submit some searches.</span></div>';
  } catch (e) {
    trendingEl.innerHTML = `<div class="trend err"><span>trending unavailable</span></div>`;
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

document.addEventListener("click", (e) => { if (!dd.contains(e.target) && e.target !== qEl) hideDropdown(); });
loadTrending();
setInterval(loadTrending, 5000); // periodic refresh
