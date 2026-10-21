"use strict";
// All URLs are relative so the page works behind Home Assistant ingress.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (n) => Number(n || 0).toLocaleString("en-GB");
const fmtGB = (g) => (g >= 100 ? Math.round(g) : Number(g).toFixed(g >= 10 ? 1 : 2)).toLocaleString("en-GB");
const fmtSize = (g) => (g < 1 ? `${Math.round(g * 1024).toLocaleString("en-GB")} MB` : `${fmtGB(g)} GB`);
const plural = (n, one, many) => `${fmtN(n)} ${n === 1 ? one : many}`;
const fmtDate = (s) => (s ? s.slice(0, 10) : "—");
let STATUS = null;
let pollTimer = null;
let wasRunning = false;

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch("api/" + path, init);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

function toast(msg, ms = 4000) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
}

// ---- tabs -------------------------------------------------------------
const loaders = {};
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + name));
  try { localStorage.setItem("pm-tab", name); } catch { /* storage may be blocked */ }
  loaders[name] && loaders[name]();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
const onTab = (name) => $("#tab-" + name).classList.contains("on");

// ---- status ---------------------------------------------------------
async function loadStatus() {
  clearTimeout(pollTimer);
  let next = 15000;
  try {
    STATUS = await api("status");
    const running = STATUS.scan.running;
    next = running ? 1500 : 15000;
    renderPills(); renderBanner(); renderScanCard();
    if (onTab("storage")) { renderFolders(); renderNasCard(); }
    if (running && Date.now() - (loadStatus._reportAt || 0) > 3000) { loadStatus._reportAt = Date.now(); loadReport(); }
    if (wasRunning && !running) loadReport();
    wasRunning = running;
  } catch (e) { /* keep polling */ }
  pollTimer = setTimeout(loadStatus, next);
}

function renderPills() {
  const { nas, scan } = STATUS;
  const nasCls = !nas.healthy ? "bad" : nas.low_space ? "warn" : "ok";
  const nasTxt = nas.disk ? `${nas.name || "NAS"} · ${nas.disk.free_gb} GB free`
    : nas.needs_choice ? "Choose your NAS" : "No NAS";
  let html = `<button class="pill" data-go="storage"><span class="dot ${nasCls}"></span>${esc(nasTxt)}</button>`;
  if (scan.running) html += `<button class="pill" data-go="library"><span class="spinner"></span>Scanning</button>`;
  $("#pills").innerHTML = html;
  $("#pills").querySelectorAll("[data-go]").forEach((p) => p.addEventListener("click", () => showTab(p.dataset.go)));
}

function renderBanner() {
  const { nas } = STATUS;
  const banner = $("#banner");
  const open = !onTab("storage") ? ` <a href="#" id="banner-go">Open Storage</a>` : "";
  if (!nas.healthy) {
    banner.className = "banner";
    banner.innerHTML = esc(nas.problems.join(" · ")) + open;
  } else if (nas.low_space) {
    banner.className = "banner warn";
    banner.innerHTML = esc(nas.warnings.join(" · ")) + open;
  } else { banner.hidden = true; return; }
  banner.hidden = false;
  const g = $("#banner-go"); g && g.addEventListener("click", (e) => { e.preventDefault(); showTab("storage"); });
}

// ---- library ----------------------------------------------------------
function renderScanCard() {
  const { nas, scan } = STATUS;
  const card = $("#scan-card");
  const photo = nas.folders.photo;
  if (!nas.path || (!nas.healthy && !photo.rel)) {
    card.innerHTML = `<div class="headline"><span class="dot bad"></span>Your NAS isn't ready</div>
      <p class="hint">${esc(nas.problems[0] || "Add your NAS in Settings → System → Storage.")}</p>
      <div class="row end"><button class="primary" data-go="storage">Open Storage</button></div>`;
  } else if (!photo.rel) {
    card.innerHTML = `<div class="headline"><span class="dot warn"></span>Choose your photo folder</div>
      <p class="hint">Pick the folder on your NAS that holds your CR2 photos. Photo Manager only reads it — it never changes or deletes anything there.</p>
      <div class="row end"><button class="primary" data-go="storage">Choose folder</button></div>`;
  } else {
    let status;
    if (scan.running) {
      const what = scan.phase === "hashing" ? `Checking for duplicates… ${fmtN(scan.hashed)} of ${fmtN(scan.to_hash)}`
        : scan.phase === "reading" ? `Reading photo details… ${fmtN(scan.files)} photos so far`
        : `Looking through folders… ${fmtN(scan.files)} photos found`;
      status = `<span class="spinner"></span> ${esc(what)}<div class="sm hint">${esc(scan.current || "")}</div>`;
    } else if (scan.error) status = `<span class="dot bad"></span> Last scan failed: ${esc(scan.error)}`;
    else if (scan.cancelled) status = `Last scan was cancelled ${ago(scan.finished)}`;
    else if (scan.finished) status = `Last scan ${ago(scan.finished)} · ${fmtN(scan.files)} photos` +
      (scan.errors ? ` · ${fmtN(scan.errors)} couldn't be read` : "");
    else status = "Not scanned yet";
    card.innerHTML = `<div class="row between">
        <div><div class="headline">Photo folder <code>${esc(photo.rel)}</code></div>
        <p class="hint">${status}</p></div>
        <div>${scan.running ? `<button class="ghost" id="scan-cancel">Cancel</button>`
          : `<button class="primary" id="scan-go" ${nas.ready ? "" : "disabled"}>${scan.finished ? "Scan again" : "Scan now"}</button>`}</div>
      </div>
      <p class="hint">Read-only: scanning only looks at your photos. Nothing is moved, changed or deleted.</p>`;
  }
  card.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.go)));
  const go = $("#scan-go");
  go && go.addEventListener("click", async () => {
    go.disabled = true;
    try { await api("scan", { method: "POST" }); } catch (e) { toast(e.message); }
    loadStatus();
  });
  const cancel = $("#scan-cancel");
  cancel && cancel.addEventListener("click", async () => { try { await api("scan/cancel", { method: "POST" }); toast("Cancelling…"); } catch (e) { toast(e.message); } });
}

function barList(items, nameKey, valKey) {
  const max = Math.max(1, ...items.map((i) => i[valKey]));
  return `<div class="barlist">` + items.map((i) =>
    `<div class="brow"><span class="bname" title="${esc(i[nameKey])}">${esc(i[nameKey])}</span>` +
    `<span class="bbar"><i style="width:${Math.max(2, Math.round(100 * i[valKey] / max))}%"></i></span>` +
    `<span class="bnum">${fmtN(i[valKey])}</span></div>`).join("") + `</div>`;
}

async function loadReport() {
  let rep;
  try { rep = await api("report"); } catch (e) { $("#report").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const el = $("#report");
  if (rep.total_files === null) { el.innerHTML = ""; return; }
  if (rep.total_files === 0) { el.innerHTML = `<div class="empty">No CR2 photos found in that folder. Check you picked the right one on the Storage tab.</div>`; return; }
  const d = rep.duplicates;
  const partial = rep.state.running ? `<div class="banner warn">Scan in progress — these figures will keep changing.</div>` : "";
  const stats = [
    ["CR2 photos", fmtN(rep.total_files), ""],
    ["Total size", fmtSize(rep.total_gb), ""],
    ["Earliest", fmtDate(rep.first), "sm"],
    ["Latest", fmtDate(rep.last), "sm"],
    ["Folders", fmtN(rep.folder_count), ""],
    ["With camera JPEG", fmtN(rep.with_jpeg), ""],
  ].map(([k, v, c]) => `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${esc(v)}</div></div>`).join("");

  let dup;
  if (rep.state.running && rep.state.phase !== "hashing" && !d.groups) dup = `<p class="hint">Duplicates are checked at the end of the scan.</p>`;
  else if (!d.groups) dup = `<div class="headline"><span class="dot ok"></span>No exact duplicates found</div>`;
  else dup = `<div class="headline"><span class="dot warn"></span>${fmtN(d.groups)} duplicate ${d.groups === 1 ? "group" : "groups"} — ${fmtN(d.extra_files)} extra ${d.extra_files === 1 ? "copy" : "copies"}, ${fmtSize(d.reclaimable_gb)} could be freed</div>
      <p class="hint">Byte-for-byte identical files. Nothing is deleted — duplicates will be handled when you review photos in a later version.</p>` +
      d.top.map((g) => `<details class="dup"><summary>${g.count} copies · ${g.size_mb} MB each</summary><ul>${g.files.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></details>`).join("") +
      (d.groups > d.top.length ? `<p class="hint">Showing the ${d.top.length} biggest groups.</p>` : "");

  const notes = [];
  if (rep.dated_from_file) notes.push(`${plural(rep.dated_from_file, "photo has", "photos have")} no readable capture time and ${rep.dated_from_file === 1 ? "is" : "are"} listed as "Unknown" by year.`);
  if (rep.state.errors) notes.push(`${plural(rep.state.errors, "file or folder", "files or folders")} couldn't be read.`);

  el.innerHTML = partial + `<div class="stat-grid">${stats}</div>
    <div class="card"><h3>Duplicates</h3>${dup}</div>
    <div class="card"><h3>By year</h3>${barList(rep.years.map((y) => ({ name: y.year || "Unknown", n: y.files })), "name", "n")}</div>
    <div class="card"><h3>Cameras</h3>${barList(rep.cameras.map((c) => ({ name: c.name, n: c.files })), "name", "n")}</div>
    <div class="card"><h3>Busiest folders</h3>${barList(rep.folders.map((f) => ({ name: f.path, n: f.files })), "name", "n")}
      ${rep.folder_count > rep.folders.length ? `<p class="hint">Showing ${rep.folders.length} of ${fmtN(rep.folder_count)} folders.</p>` : ""}</div>
    ${notes.length ? `<p class="note">${notes.map(esc).join(" ")}</p>` : ""}`;
}
loaders.library = () => { loadStatus(); loadReport(); };

// ---- storage ------------------------------------------------------
let OPTIONS = null;
async function loadNas() {
  try { OPTIONS = await api("storage/options"); } catch (e) { $("#nas").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  renderNasCard();
}

function renderNasCard() {
  if (!STATUS || !OPTIONS) return;
  const { nas } = STATUS;
  const o = OPTIONS, n = o.candidates.length;
  const d = nas.disk;
  const cls = !nas.healthy ? "bad" : nas.low_space ? "warn" : "ok";
  const head = nas.path && !nas.error
    ? `<div class="headline"><span class="dot ${cls}"></span>${esc(nas.name)} ${nas.healthy ? "is connected" : "has a problem"}</div>`
    : `<div class="headline"><span class="dot bad"></span>${nas.needs_choice ? "Choose which NAS to use" : "No NAS selected"}</div>`;
  const meter = d ? `<div class="meter ${d.used_pct > 95 ? "bad" : d.used_pct > 85 ? "warn" : ""}"><i style="width:${d.used_pct}%"></i></div>
    <p class="hint">${fmtGB(d.free_gb)} GB free of ${fmtGB(d.total_gb)} GB (${d.used_pct}% used)</p>` : "";
  const problems = nas.problems.length ? `<ul class="problems">${nas.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "";
  const kv = nas.path ? `<dl class="kv"><dt>Mounted at</dt><dd><code>${esc(nas.path)}</code></dd>` +
    (nas.source ? `<dt>Source</dt><dd>${esc(nas.source)} (${esc(nas.fstype)})</dd>` : "") + `</dl>` : "";
  let chooser = "";
  if (n === 0) chooser = `<p class="hint">Add your NAS in Home Assistant under Settings → System → Storage → Add network storage (usage: Media), then press Re-scan.</p>`;
  else if (n === 1 && o.choice === "auto") chooser = `<p class="hint">Found automatically from Home Assistant's network storage.</p>`;
  else {
    const opt = (val, title, sub) => `<label class="opt ${o.choice === val ? "sel" : ""}"><input type="radio" name="nas-choice" value="${esc(val)}" ${o.choice === val ? "checked" : ""}><span><b>${esc(title)}</b><span class="sm">${sub}</span></span></label>`;
    chooser = `<form id="nas-form" style="margin-top:12px">` +
      opt("auto", "Automatic", n === 1 ? `Use “${esc(o.candidates[0].name)}”` : `${n} found — pick one below`) +
      o.candidates.map((c) => opt(c.id, c.name, `${esc(c.usage)} storage · ${esc(c.fstype)} from ${esc(c.source)}${c.free_gb != null ? ` · ${c.free_gb} GB free` : " · not responding"}`)).join("") +
      `<div class="row end"><button class="primary">Use this NAS</button></div></form>`;
  }
  $("#nas").innerHTML = `<div class="card">${head}${problems}${meter}${kv}${chooser}</div>`;
  const form = $("#nas-form");
  form && form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const choice = (form.querySelector("input:checked") || {}).value;
    if (!choice) return;
    try { await api("storage/select", { method: "POST", body: { choice } }); toast("NAS saved"); await loadStatus(); await loadNas(); } catch (err) { toast(err.message); }
  });
}

const FOLDER_ROWS = [
  { key: "photo", label: "Photo folder", sub: "Where your CR2 photos are. Read-only — sub-folders are included." },
  { key: "processed", label: "Developed photos", sub: "Where JPEGs will be saved. Used from the next version; created when needed." },
  { key: "quarantine", label: "Quarantine", sub: "Where rejected photos will be moved, never deleted. Used from a later version." },
];

function renderFolders() {
  if (!STATUS) return;
  const { nas } = STATUS;
  const usable = !!nas.path && !nas.error;
  $("#folders").innerHTML = `<div class="card">` + FOLDER_ROWS.map((r) => {
    const f = nas.folders[r.key];
    return `<div class="frow"><div class="val"><div class="lbl">${r.label}</div>
      <div>${f.rel ? `<code>${esc(f.rel)}</code>` : `<span class="sm">Not chosen yet</span>`}</div>
      <div class="sm">${r.sub}</div></div>
      <button class="ghost" data-pick="${r.key}" ${usable ? "" : "disabled"}>${f.rel ? "Change…" : "Choose…"}</button></div>`;
  }).join("") + `</div>`;
  $("#folders").querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => chooseFolder(b.dataset.pick)));
}

async function chooseFolder(key) {
  const row = FOLDER_ROWS.find((r) => r.key === key);
  const current = STATUS.nas.folders[key].rel;
  const rel = await pickFolder({ title: row.label, kind: key, start: key === "photo" ? current || "" : (current ? current.split("/").slice(0, -1).join("/") : "") });
  if (rel === null) return;
  try {
    await api("folders", { method: "POST", body: { [key]: rel } });
    toast(key === "photo" ? "Photo folder saved — press Scan now on the Library tab" : "Folder saved");
    await loadStatus();
  } catch (e) { toast(e.message); }
}
loaders.storage = () => { loadStatus(); loadNas(); renderFolders(); };
$("#nas-rescan").addEventListener("click", async () => { await loadStatus(); await loadNas(); toast("Checked"); });

// ---- folder picker ----------------------------------------------------------
function pickFolder({ title, kind, start }) {
  return new Promise((resolve) => {
    const root = $("#picker"), list = $("#picker-list"), ok = $("#picker-ok");
    const newname = $("#picker-newname");
    let cur = "", info = null;
    $("#picker-title").textContent = title;
    $("#picker-new").hidden = kind === "photo";
    newname.value = "";

    const target = () => {
      const name = newname.value.trim().replace(/[\\/]+/g, " ").trim();
      return name ? (cur ? cur + "/" + name : name) : cur;
    };
    const refreshOk = () => { ok.disabled = !target(); };
    const done = (val) => {
      root.hidden = true;
      ok.onclick = $("#picker-cancel").onclick = $("#picker-x").onclick = root.onclick = newname.oninput = null;
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => { if (e.key === "Escape") done(null); };

    async function go(rel, fallback = true) {
      list.innerHTML = `<div class="none"><span class="spinner"></span></div>`;
      try { info = await api("folders?rel=" + encodeURIComponent(rel)); }
      catch (e) {
        if (rel && fallback) return go("", false);
        list.innerHTML = `<div class="none">${esc(e.message)}</div>`; return;
      }
      cur = info.rel;
      const parts = cur ? cur.split("/") : [];
      let acc = "";
      $("#crumbs").innerHTML = `<button data-rel="" ${cur ? "" : "disabled"}>${esc(STATUS.nas.name || "NAS")}</button>` +
        parts.map((p, i) => { acc += (acc ? "/" : "") + p; const last = i === parts.length - 1;
          return `<span class="sep">/</span><button data-rel="${esc(acc)}" ${last ? "disabled" : ""}>${esc(p)}</button>`; }).join("");
      $("#crumbs").querySelectorAll("button:not([disabled])").forEach((b) => b.addEventListener("click", () => go(b.dataset.rel)));
      list.innerHTML = info.dirs.length
        ? info.dirs.map((d) => `<button data-dir="${esc(d)}">📁 ${esc(d)}</button>`).join("")
        : `<div class="none">No sub-folders here</div>`;
      list.querySelectorAll("[data-dir]").forEach((b) => b.addEventListener("click", () => go((cur ? cur + "/" : "") + b.dataset.dir)));
      $("#picker-info").textContent = kind === "photo"
        ? (info.is_root ? "Open the folder that holds your photos, then press Use this folder."
          : `${fmtN(info.cr2_here)} CR2 photo${info.cr2_here === 1 ? "" : "s"} directly in this folder (sub-folders are included automatically).`)
        : "";
      refreshOk();
    }

    ok.onclick = () => done(target());
    $("#picker-cancel").onclick = $("#picker-x").onclick = () => done(null);
    root.onclick = (e) => { if (e.target === root) done(null); };
    newname.oninput = refreshOk;
    document.addEventListener("keydown", onKey);
    root.hidden = false;
    go(start || "");
  });
}

// ---- start ------------------------------------------------------------------------
(function init() {
  let tab = "library";
  try { tab = localStorage.getItem("pm-tab") || "library"; } catch { /* ignore */ }
  if (!["library", "storage"].includes(tab)) tab = "library";
  showTab(tab);
  loadStatus();
  if (tab !== "library") loadReport();
})();
