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
let wasDeveloping = false;

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
    const running = STATUS.scan.running || STATUS.develop.running;
    next = running ? 1500 : 15000;
    renderPills(); renderBanner(); renderScanCard();
    if (onTab("storage")) { renderFolders(); renderNasCard(); }
    if (onTab("develop") && (STATUS.develop.running || wasDeveloping)) loadDevelop();
    wasDeveloping = STATUS.develop.running;
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
  const dv = STATUS.develop;
  if (dv.running) html += `<button class="pill" data-go="develop"><span class="spinner"></span>Developing ${fmtN(dv.done)}${dv.total ? " of " + fmtN(dv.total) : ""}</button>`;
  else if (dv.enabled) html += `<button class="pill" data-go="develop"><span class="dot ok"></span>Developing on</button>`;
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
      <p class="hint">Pick the folder on your NAS that holds your CR2 photos. Photo Manager never changes or deletes your existing photos.</p>
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
      <p class="hint">Scanning only looks at your photos. Nothing is moved, changed or deleted.</p>`;
  }
  card.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.go)));
  const go = $("#scan-go");
  go && go.addEventListener("click", async () => {
    go.disabled = true;
    try { await api("scan", { method: "POST" }); wasRunning = true; } catch (e) { toast(e.message); }
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
      <p class="hint">Byte-for-byte identical files. You choose which copies move to quarantine — nothing is deleted.</p>
      <div class="row end"><button class="primary" data-go="quarantine">Review duplicates</button></div>`;

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
  el.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.go)));
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
  { key: "processed", label: "Developed photos", sub: "Where developed JPEGs are saved (when set to “separate folder” on the Develop tab). Created when needed." },
  { key: "quarantine", label: "Quarantine", sub: "Where duplicate copies you choose are moved — never deleted. Created when needed." },
];

function renderFolders() {
  if (!STATUS) return;
  const { nas } = STATUS;
  const usable = !!nas.path && !nas.error;
  $("#folders").innerHTML = `<div class="card">` + FOLDER_ROWS.map((r) => {
    const f = nas.folders[r.key];
    const unused = r.key === "processed" && nas.output_mode === "alongside";
    return `<div class="frow ${unused ? "off" : ""}"><div class="val"><div class="lbl">${r.label}</div>
      <div>${f.rel ? `<code>${esc(f.rel)}</code>` : `<span class="sm">Not chosen yet</span>`}</div>
      <div class="sm">${unused ? "Not used — JPEGs are saved next to each RAW file (change on the Develop tab)." : r.sub}</div></div>
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
    $("#picker-new").hidden = kind === "photo" || kind === "scope";
    newname.value = "";

    const target = () => {
      const name = newname.value.trim().replace(/[\\/]+/g, " ").trim();
      return name ? (cur ? cur + "/" + name : name) : cur;
    };
    const refreshOk = () => { ok.disabled = !target() && kind !== "scope"; };
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
      $("#picker-info").textContent = kind === "photo" || kind === "scope"
        ? (info.is_root ? "Open the folder you want, then press Use this folder."
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


// ---- develop ----------------------------------------------------------------------
let DEV = null;
async function loadDevelop() {
  try { DEV = await api("develop"); } catch (e) { $("#develop").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  renderDevelop();
}
async function devSettings(body, msg) {
  try { DEV = await api("develop/settings", { method: "POST", body }); if (msg) toast(msg); } catch (e) { toast(e.message); }
  await loadStatus(); await loadDevelop();
}
function renderDevelop() {
  if (!DEV || !STATUS) return;
  const d = DEV, st = d.state, c = d.counts, dt = d.darktable;
  const photoRel = STATUS.nas.folders.photo.rel;
  const alongside = d.output_mode === "alongside";
  const scoped = !!d.scope;
  const win = `${String(d.window.start).padStart(2, "0")}:00–${String(d.window.end).padStart(2, "0")}:00`;
  const ex = d.example ? `<p class="hint">Example: <code>${esc(d.example.raw)}</code> → <code>${esc(d.example.jpeg)}</code></p>` : "";

  let run;
  if (st.running) {
    const pct = st.total ? Math.round(100 * st.done / st.total) : 0;
    run = `<div class="headline"><span class="spinner"></span>${st.phase === "scanning" ? "Scanning for new photos…" : `Developing ${fmtN(st.done)} of ${fmtN(st.total)}`}</div>
      <div class="progress"><i style="width:${pct}%"></i></div>
      <p class="hint">${esc(st.current || "")}${st.failed ? ` · ${fmtN(st.failed)} failed` : ""}</p>
      <div class="row end"><button class="ghost" id="dv-stop">Stop</button></div>`;
  } else {
    let last = "";
    if (st.finished) {
      last = `Last run ${ago(st.finished)} (${st.trigger === "schedule" ? "overnight" : "manual"}): ${plural(st.done, "photo", "photos")} developed` +
        (st.failed ? `, ${fmtN(st.failed)} failed` : "") + (st.stop_text ? ` · ${esc(st.stop_text)}` : "") + (st.error ? ` · ${esc(st.error)}` : "");
    }
    const why = !d.enabled ? "Switch developing on first." : !dt.available ? esc(dt.error || "darktable isn't available.") : "";
    run = `<div class="row between"><div><div class="headline">${plural(c.pending, "photo", "photos")} waiting${scoped ? ` <span class="scope-tag">only ${esc(d.scope)}</span>` : ""}</div>
        <p class="hint">${last || "Nothing has been developed yet."}</p></div>
        <button class="primary" id="dv-run" ${d.enabled && dt.available && c.pending ? "" : "disabled"}>Develop now</button></div>
      ${why ? `<p class="hint">${why}</p>` : ""}`;
  }

  const fails = d.failed.length ? `<div class="sec"><div class="row between"><h3>Couldn't develop (${fmtN(c.failed)})</h3>
      <button class="ghost" id="dv-retry">Retry failed</button></div>
      <ul class="fails">${d.failed.slice(0, 10).map((f) => `<li><code>${esc(f.rel)}</code><span class="sm">${esc(f.error || "")} · ${f.will_retry ? `attempt ${f.attempts} of 3, will retry` : "gave up after 3 attempts"}</span></li>`).join("")}</ul>
      ${d.failed.length > 10 ? `<p class="hint">Showing 10 of ${fmtN(d.failed.length)}.</p>` : ""}</div>` : "";

  $("#develop").innerHTML = `
    <div class="card">
      <label class="switch"><input type="checkbox" id="dv-enable" ${d.enabled ? "checked" : ""}><span class="track"></span>
        Automatic developing is ${d.enabled ? "on" : "off"}</label>
      <p class="hint">Converts your CR2 files to JPEG with darktable. It is <b>off</b> until you switch it on. When on, it runs by itself between <b>${win}</b> and can also be started by hand. A new JPEG never replaces a file that already exists, and your CR2 files are never touched.</p>
      ${!dt.available ? `<p class="hint danger">${esc(dt.error || "darktable isn't available.")}</p>` : `<p class="hint">${esc(dt.version || "darktable")} · JPEG quality ${d.jpeg_quality}</p>`}
    </div>
    <div class="card">${run}${fails}</div>
    <div class="card"><h3>Where to save the JPEGs</h3>
      <label class="opt ${!alongside ? "sel" : ""}"><input type="radio" name="dv-mode" value="separate" ${!alongside ? "checked" : ""}><span><b>In a separate folder</b><span class="sm">Keeps your photo folder exactly as it is. Folder: <code>${esc(STATUS.nas.folders.processed.rel || "—")}</code> (change on the Storage tab)</span></span></label>
      <label class="opt ${alongside ? "sel" : ""}"><input type="radio" name="dv-mode" value="alongside" ${alongside ? "checked" : ""}><span><b>Next to each RAW file</b><span class="sm">Adds a <code>_developed.jpg</code> in the same folder as each CR2. Existing files are never changed or replaced.</span></span></label>
      ${ex}
    </div>
    <div class="card"><h3>Which photos</h3>
      <div class="row between"><div>${scoped ? `Only <code>${esc(d.scope)}</code> <span class="sm">(inside the photo folder)</span>` : "All photos in the photo folder"}</div>
        <div class="row"><button class="ghost" id="dv-scope" ${photoRel ? "" : "disabled"}>${scoped ? "Change…" : "Choose a folder…"}</button>
        ${scoped ? `<button class="ghost" id="dv-scope-clear">All photos</button>` : ""}</div></div>
      <p class="hint">Pick one folder to try things out on a few photos first. Applies to manual and overnight runs.</p>
      <label class="check"><input type="checkbox" id="dv-skip" ${d.skip_with_jpeg ? "checked" : ""}><span><b>Skip photos that already have a JPEG</b>
        <span class="sm">Leaves a CR2 alone when there is already a JPEG with the same name in its folder${c.with_camera_jpeg ? ` (${fmtN(c.with_camera_jpeg)} such photo${c.with_camera_jpeg === 1 ? "" : "s"} right now)` : ""}.</span></span></label>
      ${c.duplicates_skipped ? `<p class="hint">${plural(c.duplicates_skipped, "identical copy is", "identical copies are")} developed only once.</p>` : ""}
    </div>
    <p class="note">${fmtN(c.developed)} developed so far · ${fmtN(c.photos)} photos in the library. The overnight window is set in the add-on's Configuration tab.</p>`;

  $("#dv-enable").addEventListener("change", (e) => devSettings({ enabled: e.target.checked }, e.target.checked ? "Developing switched on" : "Developing switched off"));
  document.querySelectorAll("input[name=dv-mode]").forEach((r) => r.addEventListener("change", () => devSettings({ output_mode: r.value }, "Saved")));
  $("#dv-skip").addEventListener("change", (e) => devSettings({ skip_with_jpeg: e.target.checked }, "Saved"));
  const clear = $("#dv-scope-clear"); clear && clear.addEventListener("click", () => devSettings({ scope: "" }, "Developing all photos"));
  $("#dv-scope").addEventListener("click", async () => {
    const start = photoRel + (d.scope ? "/" + d.scope : "");
    const rel = await pickFolder({ title: "Develop only this folder", kind: "scope", start });
    if (rel === null) return;
    if (rel !== photoRel && !rel.startsWith(photoRel + "/")) { toast("Pick a folder inside your photo folder"); return; }
    devSettings({ scope: rel === photoRel ? "" : rel.slice(photoRel.length + 1) }, "Saved");
  });
  const go = $("#dv-run");
  go && go.addEventListener("click", async () => { go.disabled = true; try { await api("develop/run", { method: "POST" }); wasDeveloping = true; } catch (e) { toast(e.message); } await loadStatus(); loadDevelop(); });
  const stop = $("#dv-stop");
  stop && stop.addEventListener("click", async () => { try { await api("develop/cancel", { method: "POST" }); toast("Stopping after the current photo…"); } catch (e) { toast(e.message); } });
  const retry = $("#dv-retry");
  retry && retry.addEventListener("click", async () => { try { const r = await api("develop/retry-failed", { method: "POST" }); toast(`${plural(r.reset, "photo", "photos")} will be tried again`); } catch (e) { toast(e.message); } loadDevelop(); });
}
loaders.develop = () => { loadStatus(); loadDevelop(); };

// ---- quarantine ---------------------------------------------------------------------
const GROUPS = new Map();   // group id -> { g, ticked:Set, verified:null|{...}, msg }
let dupTotal = 0, dupLoaded = 0;
const GROUP_PAGE = 10;

async function loadDups(reset = true) {
  if (reset) { GROUPS.clear(); dupLoaded = 0; $("#dups").innerHTML = `<div class="empty"><span class="spinner"></span></div>`; }
  let r;
  try { r = await api(`duplicates?offset=${dupLoaded}&limit=${GROUP_PAGE}`); } catch (e) { $("#dups").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  dupTotal = r.total; dupLoaded += r.groups.length;
  r.groups.forEach((g) => GROUPS.set(g.id, { g, ticked: new Set(), verified: null, msg: null }));
  renderDups();
}
const keepOf = (s) => s.g.files.find((f) => !s.ticked.has(f.rel));

function renderDups() {
  const box = $("#dups");
  if (!dupTotal) { box.innerHTML = `<div class="empty">No duplicate photos found. Scan the library on the Library tab to check again.</div>`; return; }
  box.innerHTML = [...GROUPS.values()].map(({ g }) => `<div class="card group" data-gid="${esc(g.id)}"></div>`).join("") +
    (dupLoaded < dupTotal ? `<div class="row end"><button class="ghost" id="dups-more">Show more (${fmtN(dupTotal - dupLoaded)} more groups)</button></div>` : "");
  GROUPS.forEach((_, id) => renderGroup(id));
  const more = $("#dups-more"); more && more.addEventListener("click", () => loadDups(false));
}

function renderGroup(id) {
  const s = GROUPS.get(id);
  const el = document.querySelector(`.group[data-gid="${CSS.escape(id)}"]`);
  if (!s || !el) return;
  const { g } = s;
  const keep = keepOf(s);
  const n = s.ticked.size;
  const v = s.verified;
  const vf = (rel) => (v ? (rel === (v.keep && v.keep.rel) ? v.keep : v.files.find((f) => f.rel === rel)) : null);
  const rows = g.files.map((f) => {
    const isKeep = keep && f.rel === keep.rel;
    const x = vf(f.rel);
    let crc = "";
    if (x) {
      const ok = isKeep ? !x.problem : x.match;
      crc = x.problem && !x.crc32 ? `<span class="crc badv">${esc(x.problem)}</span>`
        : `<span class="crc ${isKeep ? "" : ok ? "good" : "badv"}">${esc(x.crc32 || "")} ${isKeep ? "" : ok ? "✓ same" : "✗ different"}</span>`;
    } else if (isKeep) crc = `<span class="tag">Stays</span>`;
    return `<label class="gfile ${isKeep ? "keep" : ""}"><input type="checkbox" data-rel="${esc(f.rel)}" ${s.ticked.has(f.rel) ? "checked" : ""}>
      <span class="path">${esc(f.rel)}<span class="sm">${esc(fmtDate(f.taken))} · ${(f.size / 1048576).toFixed(1)} MB</span></span>${crc}</label>`;
  }).join("");
  const keepNote = n && keep ? `Keeping <code>${esc(keep.rel)}</code>` : "Tick the copies to move to quarantine.";
  el.innerHTML = `<div class="ghead"><span>${g.count} identical copies</span><span class="sm hint" style="margin:0">${(g.size / 1048576).toFixed(1)} MB each</span></div>
    ${rows}
    <p class="hint">${keepNote}${n === g.count ? ` <b class="danger">Untick at least one — a copy has to stay.</b>` : ""}</p>
    ${s.msg ? `<div class="gmsg ${s.msg.cls}">${esc(s.msg.text)}</div>` : ""}
    <div class="gactions">
      <button class="ghost" data-act="auto">Keep first, tick the rest</button>
      <button class="ghost" data-act="verify" ${n && n < g.count ? "" : "disabled"}>Check checksums</button>
      <button class="primary" data-act="move" ${v && v.all_match && n && n < g.count ? "" : "disabled"}>Move ${n || ""} to quarantine</button>
    </div>`;
  el.querySelectorAll("input[type=checkbox]").forEach((cb) => cb.addEventListener("change", () => {
    cb.checked ? s.ticked.add(cb.dataset.rel) : s.ticked.delete(cb.dataset.rel);
    s.verified = null; s.msg = null;           // any change in the selection needs a fresh check
    renderGroup(id);
  }));
  el.querySelector('[data-act="auto"]').addEventListener("click", () => {
    s.ticked = new Set(g.files.slice(1).map((f) => f.rel)); s.verified = null; s.msg = null; renderGroup(id);
  });
  el.querySelector('[data-act="verify"]').addEventListener("click", async (e) => {
    const b = e.currentTarget; b.disabled = true; b.textContent = "Reading files…";
    try {
      s.verified = await api("quarantine/verify", { method: "POST", body: { keep: keepOf(s).rel, move: [...s.ticked] } });
      s.msg = s.verified.all_match ? { cls: "good", text: `✓ Checksums match (${s.verified.keep.crc32}) — safe to move.` }
        : { cls: "badv", text: "✗ The checksums don't all match, so these can't be moved." };
    } catch (err) { s.verified = null; s.msg = { cls: "badv", text: err.message }; }
    renderGroup(id);
  });
  el.querySelector('[data-act="move"]').addEventListener("click", async (e) => {
    const b = e.currentTarget; b.disabled = true;
    try {
      const r = await api("quarantine/move", { method: "POST", body: { keep: keepOf(s).rel, move: [...s.ticked] } });
      toast(r.errors.length ? `${r.moved.length} moved, ${r.errors.length} failed: ${r.errors[0].error}` : `${plural(r.moved.length, "photo", "photos")} moved to quarantine`);
    } catch (err) { s.msg = { cls: "badv", text: err.message }; s.verified = null; renderGroup(id); return; }
    loadDups(); loadQuarantine(); loadStatus();
  });
}

async function loadQuarantine() {
  let q;
  try { q = await api("quarantine"); } catch (e) { $("#qlist").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (!q.items.length) { $("#qlist").innerHTML = `<div class="empty">Nothing in quarantine.</div>`; return; }
  $("#qlist").innerHTML = `<div class="card"><p class="hint" style="margin-top:0">${plural(q.files, "file", "files")} in <code>${esc(q.folder || "")}</code> · ${fmtSize(q.bytes / 1073741824)}. Delete them yourself from the NAS when you're sure; use “Clear emptied” afterwards to tidy this list.</p>` +
    q.items.map((i) => `<div class="qitem"><div><code>${esc(i.rel)}</code><span class="sm">Copy of ${esc(i.keep_rel)} · CRC32 ${esc(i.crc32 || "")}${i.present ? "" : " · no longer in the quarantine folder"}</span></div>
      <button class="ghost" data-restore="${i.id}" ${i.present ? "" : "disabled"}>Restore</button></div>`).join("") + `</div>`;
  $("#qlist").querySelectorAll("[data-restore]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await api("quarantine/restore", { method: "POST", body: { id: Number(b.dataset.restore) } }); toast("Put back where it was"); loadDups(); }
    catch (e) { toast(e.message); }
    loadQuarantine(); loadStatus();
  }));
}
$("#dups-reload").addEventListener("click", () => loadDups());
$("#q-forget").addEventListener("click", async () => { try { const r = await api("quarantine/forget-missing", { method: "POST" }); toast(`${plural(r.forgotten, "entry", "entries")} cleared`); } catch (e) { toast(e.message); } loadQuarantine(); });
loaders.quarantine = () => { loadStatus(); loadDups(); loadQuarantine(); };

// ---- start ------------------------------------------------------------------------
(function init() {
  let tab = "library";
  try { tab = localStorage.getItem("pm-tab") || "library"; } catch { /* ignore */ }
  if (!["library", "develop", "quarantine", "storage"].includes(tab)) tab = "library";
  showTab(tab);
  loadStatus();
  if (tab !== "library") loadReport();
})();
