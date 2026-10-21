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
let wasUploading = false;
let wasTkBusy = false;

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
    const running = STATUS.scan.running || STATUS.develop.running || STATUS.upload.running || STATUS.takeout.scanning || STATUS.takeout.job;
    next = running ? 1500 : 15000;
    renderPills(); renderBanner(); renderScanCard();
    if (onTab("storage")) { renderFolders(); renderNasCard(); }
    if (onTab("develop") && (STATUS.develop.running || wasDeveloping)) loadDevelop();
    wasDeveloping = STATUS.develop.running;
    const tkBusy = STATUS.takeout.scanning || STATUS.takeout.job;
    if (onTab("google") && (STATUS.upload.running || wasUploading || tkBusy || wasTkBusy)) loadGoogle();
    wasUploading = STATUS.upload.running; wasTkBusy = tkBusy;
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
  if (STATUS.takeout.scanning) html += `<button class="pill" data-go="google"><span class="spinner"></span>Reading Takeout</button>`;
  else if (STATUS.takeout.job) html += `<button class="pill" data-go="google"><span class="spinner"></span>Uploading selection</button>`;
  const up = STATUS.upload;
  if (up.running) html += `<button class="pill" data-go="google"><span class="spinner"></span>Uploading ${fmtN(up.done)}${up.total ? " of " + fmtN(up.total) : ""}</button>`;
  if (!dv.running && dv.enabled) html += `<button class="pill" data-go="develop"><span class="dot ok"></span>Developing on</button>`;
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
    $("#picker-new").hidden = ["photo", "scope", "takeout"].includes(kind);
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
      $("#picker-info").textContent = kind === "takeout"
        ? "Choose the folder that holds your Takeout .zip files (or the extracted Takeout folder), then press Use this folder."
        : kind === "photo" || kind === "scope"
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
    const why = !dt.available ? esc(dt.error || "darktable isn't available.") : "";
    run = `<div class="row between"><div><div class="headline">${plural(c.pending, "photo", "photos")} waiting${scoped ? ` <span class="scope-tag">only ${esc(d.scope)}</span>` : ""}</div>
        <p class="hint">${last || "Nothing has been developed yet."}</p></div>
        <button class="primary" id="dv-run" ${dt.available && c.pending ? "" : "disabled"}>Develop now</button></div>
      ${why ? `<p class="hint">${why}</p>` : ""}`;
  }

  const fails = d.failed.length ? `<div class="sec"><div class="row between"><h3>Couldn't develop (${fmtN(c.failed)})</h3>
      <button class="ghost" id="dv-retry">Retry failed</button></div>
      <ul class="fails">${d.failed.slice(0, 10).map((f) => `<li><code>${esc(f.rel)}</code><span class="sm">${esc(f.error || "")} · ${f.will_retry ? `attempt ${f.attempts} of 3, will retry` : "gave up after 3 attempts"}</span></li>`).join("")}</ul>
      ${d.failed.length > 10 ? `<p class="hint">Showing 10 of ${fmtN(d.failed.length)}.</p>` : ""}</div>` : "";

  $("#develop").innerHTML = `
    <div class="card">
      <label class="switch"><input type="checkbox" id="dv-enable" ${d.enabled ? "checked" : ""}><span class="track"></span>
        Automatic overnight developing is ${d.enabled ? "on" : "off"}</label>
      <p class="hint">Converts your CR2 files to JPEG with darktable. This switch is <b>off</b> until you turn it on; when on, developing runs by itself between <b>${win}</b>. <b>Develop now</b> below always works, whatever this switch says. A new JPEG never replaces a file that already exists, and your CR2 files are never touched.</p>
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

  if (!DV_SET_DONE) renderDvSettings();
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


// ---- thumbnails ---------------------------------------------------------------------
const showThumbs = () => { try { return localStorage.getItem("pm-thumbs") !== "0"; } catch { return true; } };
const thumbImg = (src) => `<img class="thumb" loading="lazy" alt="" data-src="${esc(src)}">`;
const bigImg = (src) => `<img class="big" loading="lazy" alt="" data-src="${esc(src)}">`;
function wireImages(root) {
  root.querySelectorAll("img[data-src]").forEach((img) => {
    img.addEventListener("error", () => { img.removeAttribute("src"); img.classList.add("none"); img.alt = "No preview"; img.style.visibility = "hidden"; }, { once: true });
    img.addEventListener("click", (e) => {
      if (img.classList.contains("none")) return;
      e.preventDefault(); e.stopPropagation();
      $("#lightbox-img").src = img.dataset.src; $("#lightbox").hidden = false;
    });
    img.src = img.dataset.src;
  });
}
$("#lightbox").addEventListener("click", () => { $("#lightbox").hidden = true; $("#lightbox-img").removeAttribute("src"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#lightbox").click(); });
$("#show-thumbs").checked = showThumbs();
$("#show-thumbs").addEventListener("change", (e) => {
  try { localStorage.setItem("pm-thumbs", e.target.checked ? "1" : "0"); } catch { /* ignore */ }
  renderDups(); loadQuarantine();
});

// ---- google photos ----------------------------------------------------------------------
let GP = null, gpAuth = null, GPSEG = "upload";
async function loadGoogle() {
  try { GP = await api("google"); } catch (e) { $("#google").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  if (GPSEG === "find" && $("#gp-body") && $("#tk-status")) { await loadTakeoutState(); return; }   // keep the results as they are
  renderGoogle();
}
async function gpPost(path, body, msg) {
  try { const r = await api("google/" + path, { method: "POST", body: body || {} }); if (msg) toast(msg); return r; }
  catch (e) { toast(e.message); return null; }
  finally { await loadStatus(); await loadGoogle(); }
}
function renderGoogle() {
  if (!GP) return;
  $("#google").innerHTML = `<div class="seg"><button class="chip ${GPSEG === "upload" ? "on" : ""}" data-seg="upload">Upload</button>
    <button class="chip ${GPSEG === "find" ? "on" : ""}" data-seg="find">Find photos not in Google Photos</button></div><div id="gp-body"></div>`;
  $("#google").querySelectorAll("[data-seg]").forEach((b) => b.addEventListener("click", () => { GPSEG = b.dataset.seg; renderGoogle(); }));
  if (GPSEG === "find") { renderFind($("#gp-body")); return; }
  renderUpload($("#gp-body"));
}
function renderUpload(box) {
  const g = GP, st = g.state, c = g.counts;
  if (!g.configured) {
    box.innerHTML = `<div class="card"><div class="headline"><span class="dot warn"></span>Google Photos isn't set up yet</div>
      <p class="hint">Photo Manager adds your developed JPEGs to Google Photos (no albums). It can only <b>add</b> photos — it can't see, change or delete anything already there. One-off setup, about five minutes:</p>
      <ol class="steps">
        <li>In the <b>Google Cloud console</b> create a project and turn on the <b>Photos Library API</b>.</li>
        <li>Under <b>APIs &amp; Services → OAuth consent screen</b>, set it up as <b>External</b> and add yourself as a test user. Then press <b>Publish app</b> (you can ignore the “unverified” warning for your own use) — otherwise Google ends the sign-in after 7 days.</li>
        <li>Under <b>Credentials</b> create an <b>OAuth client ID</b> of type <b>Desktop app</b>.</li>
        <li>Copy its client ID and secret into this add-on's <b>Configuration</b> tab (<code>google_client_id</code>, <code>google_client_secret</code>), save, and restart the add-on.</li>
      </ol></div>`;
    return;
  }
  if (!g.connected) {
    box.innerHTML = `<div class="card"><div class="headline"><span class="dot warn"></span>${g.needs_reconnect ? "Google sign-in expired" : "Not connected to Google Photos"}</div>
      <p class="hint">Photo Manager only gets permission to <b>add</b> photos.</p>
      ${gpAuth ? `<ol class="steps">
        <li><a href="${esc(gpAuth)}" target="_blank" rel="noopener noreferrer">Open Google's sign-in page</a> and approve access.</li>
        <li>Google then sends you to a page that <b>won't load</b> (it starts with <code>http://localhost</code>). That's expected — copy that page's full address from the address bar.</li>
        <li>Paste it here:<div class="row" style="margin-top:6px"><input id="gp-paste" placeholder="http://localhost/?state=…&code=…" autocomplete="off"><button class="primary" id="gp-finish">Finish</button></div></li></ol>`
      : `<div class="row end"><button class="primary" id="gp-connect">Connect Google Photos</button></div>`}</div>`;
    const cn = $("#gp-connect");
    cn && cn.addEventListener("click", async () => { const r = await gpPost("auth-url"); if (r) { gpAuth = r.url; renderGoogle(); } });
    const fin = $("#gp-finish");
    fin && fin.addEventListener("click", async () => {
      const r = await gpPost("connect", { redirect_url: $("#gp-paste").value });
      if (r) { gpAuth = null; toast("Connected to Google Photos"); renderGoogle(); }
    });
    return;
  }
  let run;
  if (st.running) {
    const pct = st.total ? Math.round(100 * st.done / st.total) : 0;
    run = `<div class="headline"><span class="spinner"></span>Uploading ${fmtN(st.done)} of ${fmtN(st.total)}</div>
      <div class="progress"><i style="width:${pct}%"></i></div><p class="hint">${esc(st.current || "")}${st.failed ? ` · ${fmtN(st.failed)} failed` : ""}</p>
      <div class="row end"><button class="ghost" id="gp-stop">Stop</button></div>`;
  } else {
    const last = st.finished ? `Last run ${ago(st.finished)}: ${plural(st.done, "photo", "photos")} uploaded` + (st.failed ? `, ${fmtN(st.failed)} failed` : "") + (st.stop_text ? ` · ${esc(st.stop_text)}` : "") + (st.error ? ` · ${esc(st.error)}` : "") : "Nothing has been uploaded yet.";
    run = `<div class="row between"><div><div class="headline">${plural(c.pending, "developed photo", "developed photos")} waiting</div><p class="hint">${last}</p></div>
      <button class="primary" id="gp-run" ${c.pending ? "" : "disabled"}>Upload now</button></div>
      ${c.pending ? "" : `<p class="hint">Photos appear here once they have been developed on the Develop tab.</p>`}`;
  }
  const fails = g.failed.length ? `<div class="sec"><div class="row between"><h3>Couldn't upload (${fmtN(c.failed)})</h3><button class="ghost" id="gp-retry">Retry failed</button></div>
    <ul class="fails">${g.failed.slice(0, 10).map((f) => `<li><code>${esc(f.rel)}</code><span class="sm">${esc(f.error || "")} · ${f.will_retry ? `attempt ${f.attempts} of 3, will retry` : "gave up after 3 attempts"}</span></li>`).join("")}</ul></div>` : "";
  box.innerHTML = `<div class="card">
      <label class="switch"><input type="checkbox" id="gp-auto" ${g.enabled ? "checked" : ""}><span class="track"></span>Automatic upload is ${g.enabled ? "on" : "off"}</label>
      <p class="hint">This switch is <b>off</b> until you turn it on. When on, each newly developed JPEG is added to Google Photos after developing finishes. <b>Upload now</b> below always works. Only JPEGs Photo Manager developed are sent, each one once, with no albums. Nothing is ever deleted — from your NAS or from Google.</p>
      <p class="hint"><span class="dot ok"></span> Connected to Google Photos · ${fmtN(c.uploaded)} uploaded so far</p></div>
    <div class="card">${run}${fails}</div>
    <div class="row end"><button class="ghost" id="gp-disc">Disconnect</button></div>
    <p class="note">Google only lets apps see photos they uploaded themselves, so Photo Manager can't tell whether a photo is already in your library from another source — it only avoids sending the same JPEG twice.</p>`;
  $("#gp-auto").addEventListener("change", (e) => gpPost("settings", { upload: e.target.checked }, e.target.checked ? "Automatic upload on" : "Automatic upload off"));
  const go = $("#gp-run"); go && go.addEventListener("click", async () => { go.disabled = true; await gpPost("run"); wasUploading = true; });
  const stop = $("#gp-stop"); stop && stop.addEventListener("click", () => gpPost("cancel", {}, "Stopping after the current photo…"));
  const retry = $("#gp-retry"); retry && retry.addEventListener("click", () => gpPost("retry-failed", {}, "Will try again"));
  $("#gp-disc").addEventListener("click", () => { if (confirm("Disconnect Google Photos? Photos already uploaded stay there.")) gpPost("disconnect", {}, "Disconnected"); });
}
loaders.google = () => { loadStatus(); loadGoogle(); };

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
    const thumb = showThumbs() ? thumbImg(`api/thumb?rel=${encodeURIComponent(f.rel)}`) : "";
    return `<label class="gfile ${isKeep ? "keep" : ""} ${thumb ? "has-thumb" : ""}"><input type="checkbox" data-rel="${esc(f.rel)}" ${s.ticked.has(f.rel) ? "checked" : ""}>${thumb}
      <span class="path">${esc(f.rel)}<span class="sm">${esc(fmtDate(f.taken))} · ${(f.size / 1048576).toFixed(1)} MB</span></span>${crc}</label>`;
  }).join("");
  const keepNote = n && keep ? `Keeping <code>${esc(keep.rel)}</code>` : "Tick the copies to move to quarantine.";
  const keepPic = keep ? `<div class="keeppic">${bigImg(`api/thumb?rel=${encodeURIComponent(keep.rel)}`)}<span class="sm">The copy that stays<br><code>${esc(keep.rel)}</code></span></div>` : "";
  el.innerHTML = `<div class="ghead"><span>${g.count} identical copies</span><span class="sm hint" style="margin:0">${(g.size / 1048576).toFixed(1)} MB each</span></div>
    ${keepPic}${rows}
    <p class="hint">${keepNote}${n === g.count ? ` <b class="danger">Untick at least one — a copy has to stay.</b>` : ""}</p>
    ${s.msg ? `<div class="gmsg ${s.msg.cls}">${esc(s.msg.text)}</div>` : ""}
    <div class="gactions">
      <button class="ghost" data-act="auto">Keep first, tick the rest</button>
      <button class="ghost" data-act="verify" ${n && n < g.count ? "" : "disabled"}>Check checksums</button>
      <button class="primary" data-act="move" ${v && v.all_match && n && n < g.count ? "" : "disabled"}>Move ${n || ""} to quarantine</button>
    </div>`;
  wireImages(el);
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
    q.items.map((i) => `<div class="qitem"><div class="lead">${i.present && showThumbs() ? thumbImg(`api/thumb?q=${i.id}`) : ""}<div><code>${esc(i.rel)}</code><span class="sm">Copy of ${esc(i.keep_rel)} · CRC32 ${esc(i.crc32 || "")}${i.present ? "" : " · no longer in the quarantine folder"}</span></div></div>
      <button class="ghost" data-restore="${i.id}" ${i.present ? "" : "disabled"}>Restore</button></div>`).join("") + `</div>`;
  wireImages($("#qlist"));
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


// ---- shared image helpers --------------------------------------------------------------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const thumbSrc = (it, size) => (it.developed ? `api/thumb?dev=${encodeURIComponent(it.rel)}` : `api/thumb?rel=${encodeURIComponent(it.rel)}`) + (size ? `&size=${size}` : "");
function loadImgs(root) {
  root.querySelectorAll("img[data-src]").forEach((img) => {
    img.addEventListener("error", () => { img.style.visibility = "hidden"; }, { once: true });
    img.src = img.dataset.src;
  });
}
function insideScope(rel) {   // a folder picked on the NAS -> relative to the photo folder, or null
  const photo = STATUS.nas.folders.photo.rel;
  if (rel === photo) return "";
  if (rel.startsWith(photo + "/")) return rel.slice(photo.length + 1);
  toast("Pick a folder inside your photo folder"); return null;
}
async function pickInPhotoFolder(title, current) {
  const photo = STATUS.nas.folders.photo.rel;
  const rel = await pickFolder({ title, kind: "scope", start: photo + (current ? "/" + current : "") });
  return rel === null ? null : insideScope(rel);
}

// ---- viewer (full-screen, developed vs camera preview) ----------------------------------
const VIEW = { items: [], i: 0, mode: "dev" };
function openViewer(items, i) {
  VIEW.items = items; VIEW.i = i; VIEW.mode = items[i].developed ? "dev" : "raw";
  $("#viewer").hidden = false; showViewer();
}
function showViewer() {
  const it = VIEW.items[VIEW.i]; if (!it) return;
  const dev = it.developed && VIEW.mode === "dev";
  const enc = encodeURIComponent(it.rel);
  const img = $("#viewer-img"); img.style.visibility = "visible";
  img.src = dev ? `api/thumb?dev=${enc}&size=l` : `api/thumb?rel=${enc}&size=l`;
  $("#viewer-title").textContent = it.rel;
  $("#viewer-info").textContent = `${VIEW.i + 1} / ${VIEW.items.length}` + (it.taken ? " · " + it.taken : "") + (it.uploaded ? " · in Google Photos" : "");
  const m = $("#viewer-mode"); m.hidden = !it.developed;
  m.textContent = dev ? "Developed · tap for camera preview" : it.developed ? "Camera preview · tap for developed" : "Camera preview";
  $("#viewer-prev").hidden = VIEW.i === 0; $("#viewer-next").hidden = VIEW.i >= VIEW.items.length - 1;
}
const stepViewer = (d) => { const n = VIEW.i + d; if (n >= 0 && n < VIEW.items.length) { VIEW.i = n; if (!VIEW.items[n].developed) VIEW.mode = "raw"; showViewer(); } };
$("#viewer-prev").addEventListener("click", () => stepViewer(-1));
$("#viewer-next").addEventListener("click", () => stepViewer(1));
$("#viewer-x").addEventListener("click", () => { $("#viewer").hidden = true; $("#viewer-img").removeAttribute("src"); });
$("#viewer-mode").addEventListener("click", () => { VIEW.mode = VIEW.mode === "dev" ? "raw" : "dev"; showViewer(); });
$("#viewer-img").addEventListener("error", () => { $("#viewer-img").style.visibility = "hidden"; $("#viewer-info").textContent = "No preview available for this photo"; });
document.addEventListener("keydown", (e) => {
  if ($("#viewer").hidden) return;
  if (e.key === "ArrowLeft") stepViewer(-1); else if (e.key === "ArrowRight") stepViewer(1); else if (e.key === "Escape") $("#viewer-x").click();
});

// ---- gallery ---------------------------------------------------------------------------------
const GAL = { filter: "all", days: 7, folder: "", q: "", items: [], total: 0, counts: null, loading: false };
try { GAL.filter = localStorage.getItem("pm-gal") || "all"; } catch { /* ignore */ }
function galBar() {
  const c = GAL.counts || { all: 0, recent: 0, developed: 0, undeveloped: 0 };
  const chip = (id, label, n) => `<button class="chip ${GAL.filter === id ? "on" : ""}" data-f="${id}">${label}${n != null ? ` (${fmtN(n)})` : ""}</button>`;
  $("#gal-bar").innerHTML = `<div class="chips">${chip("all", "All", c.all)}${chip("recent", "Recently developed", c.recent)}${chip("developed", "Developed", c.developed)}${chip("undeveloped", "Not developed", c.undeveloped)}</div>
    <div class="toolrow">
      ${GAL.filter === "recent" ? `<select id="gal-days" aria-label="How recent"><option value="1">Last day</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="365">Last year</option></select>` : ""}
      <button class="ghost" id="gal-folder">${GAL.folder ? "Folder: " + esc(GAL.folder) : "All folders"} ▾</button>
      ${GAL.folder ? `<button class="ghost" id="gal-folder-x" aria-label="Clear folder">✕</button>` : ""}
      <input id="gal-q" placeholder="Search file names" value="${esc(GAL.q)}" autocomplete="off" style="min-width:120px">
    </div>`;
  document.querySelectorAll("#gal-bar [data-f]").forEach((b) => b.addEventListener("click", () => { GAL.filter = b.dataset.f; try { localStorage.setItem("pm-gal", GAL.filter); } catch { /* ignore */ } loadGallery(); }));
  const days = $("#gal-days"); if (days) { days.value = String(GAL.days); days.addEventListener("change", () => { GAL.days = Number(days.value); loadGallery(); }); }
  $("#gal-folder").addEventListener("click", async () => { const r = await pickInPhotoFolder("Show only this folder", GAL.folder); if (r !== null) { GAL.folder = r; loadGallery(); } });
  const fx = $("#gal-folder-x"); fx && fx.addEventListener("click", () => { GAL.folder = ""; loadGallery(); });
  $("#gal-q").addEventListener("input", (e) => { clearTimeout(galBar._t); galBar._t = setTimeout(() => { GAL.q = e.target.value.trim(); loadGallery(false); }, 350); });
}
function galUrl(offset) {
  const p = new URLSearchParams({ filter: GAL.filter, days: GAL.days, folder: GAL.folder, q: GAL.q, offset, limit: 60 });
  return "gallery?" + p;
}
async function loadGallery(rebar = true) {
  if (!STATUS) await loadStatus();
  if (!STATUS.nas.folders.photo.rel) { $("#gal-bar").innerHTML = ""; $("#gal-grid").innerHTML = `<div class="empty" style="grid-column:1/-1">Choose your photo folder on the Storage tab and scan it first.</div>`; $("#gal-more").innerHTML = ""; return; }
  let r;
  try { r = await api(galUrl(0)); } catch (e) { $("#gal-grid").innerHTML = `<div class="empty" style="grid-column:1/-1">${esc(e.message)}</div>`; return; }
  GAL.items = r.items; GAL.total = r.total; GAL.counts = r.counts;
  if (rebar) galBar();
  renderGalGrid();
}
function cellHtml(it, i, extra = "") {
  return `<div class="cell ${extra}" data-i="${i}" data-rel="${esc(it.rel)}"><img alt="" loading="lazy" data-src="${esc(thumbSrc(it))}">
    <span class="badges">${it.developed ? `<span class="badge ok">Developed</span>` : ""}${it.uploaded ? `<span class="badge">Google</span>` : ""}</span></div>`;
}
function renderGalGrid() {
  const g = $("#gal-grid");
  if (!GAL.items.length) { g.innerHTML = `<div class="empty" style="grid-column:1/-1">${GAL.filter === "recent" ? "Nothing has been developed in that time." : "No photos match."}</div>`; $("#gal-more").innerHTML = ""; return; }
  g.innerHTML = GAL.items.map((it, i) => cellHtml(it, i)).join("");
  loadImgs(g);
  g.querySelectorAll(".cell").forEach((c) => c.addEventListener("click", () => openViewer(GAL.items, Number(c.dataset.i))));
  $("#gal-more").innerHTML = GAL.items.length < GAL.total ? `<button class="ghost" id="gal-more-btn">Show more (${fmtN(GAL.total - GAL.items.length)} more)</button>` : `<span class="sm hint">${fmtN(GAL.total)} photos</span>`;
  const m = $("#gal-more-btn");
  m && m.addEventListener("click", async () => {
    m.disabled = true;
    try { const r = await api(galUrl(GAL.items.length)); GAL.items = GAL.items.concat(r.items); GAL.total = r.total; } catch (e) { toast(e.message); }
    renderGalGrid();
  });
}
loaders.gallery = () => { loadStatus(); loadGallery(); };

// ---- darktable settings -----------------------------------------------------------------------------
let DV_SET_DONE = false;
const SIZES = [[0, "Full size"], [4000, "4000 px"], [3000, "3000 px"], [2048, "2048 px"], [1600, "1600 px"]];
function renderDvSettings() {
  if (!DEV) return;
  DV_SET_DONE = true;
  const o = DEV.dt_options, dt = DEV.dt;
  const sizes = SIZES.some(([v]) => v === dt.max_size) ? SIZES : [...SIZES, [dt.max_size, `${dt.max_size} px`]];
  const tpl = ["", ...DEV.templates];
  const opts = (list, sel, val = (x) => x.id, lab = (x) => x.label) => list.map((x) => `<option value="${esc(val(x))}" ${val(x) === sel ? "selected" : ""}>${esc(lab(x))}</option>`).join("");
  $("#develop-settings").innerHTML = `<div class="card"><h3>Look and quality</h3>
    <p class="hint" style="margin-top:0">These apply to photos developed from now on. Existing JPEGs are never replaced, so changing a setting doesn't touch earlier results.</p>
    ${o.presets.map((p) => `<label class="opt ${dt.preset === p.id ? "sel" : ""}"><input type="radio" name="dvs-preset" value="${p.id}" ${dt.preset === p.id ? "checked" : ""}><span><b>${esc(p.label)}</b><span class="sm">${esc(p.help)}</span></span></label>`).join("")}
    <div class="fgrid">
      <label>JPEG quality (50–100)<input id="dvs-quality" type="number" min="50" max="100" placeholder="${DEV.jpeg_quality} (add-on default)" value="${dt.quality ?? ""}"></label>
      <label>Longest side<select id="dvs-size">${sizes.map(([v, l]) => `<option value="${v}" ${v === dt.max_size ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label>Colour space<select id="dvs-colour">${opts(o.colours, dt.colour)}</select></label>
      <label>Rendering intent<select id="dvs-intent">${opts(o.intents, dt.intent)}</select></label>
      <label style="grid-column:1/-1">XMP template (optional)<select id="dvs-xmp">${opts(tpl.map((n) => ({ id: n, label: n || "None" })), dt.xmp_template)}</select></label>
    </div>
    <p class="hint">${DEV.templates.length ? "" : "No templates found. "}To apply your own look, export a <code>.xmp</code> from the darktable desktop app and put it in this add-on's config folder (<code>/addon_configs/…_photo_manager</code>); it appears in the list above. It is applied to every photo.</p>
    <label class="check"><input type="checkbox" id="dvs-hq" ${dt.hq ? "checked" : ""}><span><b>High-quality resampling</b><span class="sm">Slower but sharper when a size limit is set.</span></span></label>
    <div class="row end"><button class="ghost" id="dvs-reset">Reset to defaults</button><button class="primary" id="dvs-save">Save settings</button></div>
    <div class="sec"><h3>Try it on one photo</h3><p class="hint" style="margin-top:0">Develops a small preview with the settings above (even if not saved yet) and shows it next to the camera's own picture. Nothing is saved to your NAS.</p>
      <div class="row"><button class="ghost" id="dvs-try">Choose a photo…</button></div><div id="dv-try"></div></div></div>`;
  document.querySelectorAll("input[name=dvs-preset]").forEach((r) => r.addEventListener("change", () => {
    document.querySelectorAll("#develop-settings .opt").forEach((l) => l.classList.toggle("sel", l.querySelector("input").checked));
  }));
  $("#dvs-save").addEventListener("click", async () => {
    try { await api("develop/dt", { method: "POST", body: dvForm() }); toast("Settings saved"); } catch (e) { toast(e.message); return; }
    DV_SET_DONE = false; loadDevelop();
  });
  $("#dvs-reset").addEventListener("click", async () => {
    try { await api("develop/dt", { method: "POST", body: { ...o.defaults } }); toast("Back to the defaults"); } catch (e) { toast(e.message); return; }
    DV_SET_DONE = false; loadDevelop();
  });
  $("#dvs-try").addEventListener("click", async () => { const rel = await pickPhoto(); if (rel) dvTry(rel); });
}
function dvForm() {
  const q = $("#dvs-quality").value.trim();
  return { preset: (document.querySelector("input[name=dvs-preset]:checked") || {}).value, quality: q === "" ? null : Number(q),
    max_size: Number($("#dvs-size").value), hq: $("#dvs-hq").checked, colour: $("#dvs-colour").value, intent: $("#dvs-intent").value,
    xmp_template: $("#dvs-xmp").value };
}
function pickPhoto() {
  return new Promise(async (resolve) => {
    const root = $("#photopicker"), grid = $("#pp-grid");
    const done = (v) => { root.hidden = true; $("#pp-x").onclick = root.onclick = null; resolve(v); };
    $("#pp-x").onclick = () => done(null); root.onclick = (e) => { if (e.target === root) done(null); };
    grid.innerHTML = `<div class="none"><span class="spinner"></span></div>`; root.hidden = false;
    let r;
    try { r = await api("gallery?filter=all&limit=48"); } catch (e) { grid.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (!r.items.length) { grid.innerHTML = `<div class="empty" style="grid-column:1/-1">No photos yet — scan your library first.</div>`; return; }
    grid.innerHTML = r.items.map((it, i) => cellHtml({ ...it, developed: false }, i)).join("");
    loadImgs(grid);
    grid.querySelectorAll(".cell").forEach((c) => c.addEventListener("click", () => done(c.dataset.rel)));
  });
}
async function dvTry(rel) {
  const box = $("#dv-try");
  box.innerHTML = `<p class="hint"><span class="spinner"></span> Developing a preview of <code>${esc(rel)}</code>… this can take a minute.</p>`;
  try { await api("develop/preview", { method: "POST", body: { rel, dt: dvForm() } }); } catch (e) { box.innerHTML = `<p class="hint danger">${esc(e.message)}</p>`; return; }
  let s;
  for (let n = 0; n < 400; n++) { await sleep(1000); try { s = await api("develop/preview"); } catch { continue; } if (!s.running) break; }
  if (!s || s.running) { box.innerHTML = `<p class="hint danger">That took too long.</p>`; return; }
  if (s.error) { box.innerHTML = `<p class="hint danger">${esc(s.error)}</p>`; return; }
  const enc = encodeURIComponent(rel);
  box.innerHTML = `<div class="compare"><figure><img alt="" data-src="api/thumb?rel=${enc}&size=l"><figcaption>Camera's own preview</figcaption></figure>
    <figure><img alt="" data-src="api/develop/preview.jpg?t=${Date.now()}"><figcaption>darktable with these settings</figcaption></figure></div>`;
  wireImages(box);
}

// ---- find photos that aren't in Google Photos (Google Takeout) -----------------------------------------------
let TK = null;
const FIND = { folder: "", possible: false, items: [], total: 0, folders: [], foldersTotal: 0, counts: null, selFolders: new Set(), selRels: new Set() };
const dirOf = (rel) => (rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "");
function renderFind(box) {
  box.innerHTML = `<details class="card" id="tk-guide" ${TK && TK.counts.entries ? "" : "open"}><summary><b>How to get your Google Takeout export</b></summary>
    <ol class="steps">
      <li>Go to <a href="https://takeout.google.com" target="_blank" rel="noopener noreferrer">takeout.google.com</a> and sign in with the Google account that holds your photos.</li>
      <li>Press <b>Deselect all</b>, then tick only <b>Google Photos</b>. (Optional: under “All photo albums included” choose just the years or albums you want to check.)</li>
      <li>Press <b>Next step</b> and choose: <b>Send download link via email</b>, frequency <b>Export once</b>, file type <b>.zip</b>, size <b>50 GB</b> (fewer files).</li>
      <li>Press <b>Create export</b>. Google emails you when it's ready — from minutes to a couple of days for a big library. Download <b>all</b> the .zip parts (the links last 7 days).</li>
      <li>Copy the .zip files — <b>don't unzip them</b> — into a folder on your NAS (for example <code>Takeout</code>), then choose that folder below.</li>
    </ol>
    <p class="hint">Photo Manager reads only the small <code>.json</code> info files inside the zips (file name and date taken). It never extracts the photos and never changes the folder. The export is a snapshot: photos added to Google Photos afterwards aren't in it, so export again if your library has changed a lot. If you delete the zips, a re-scan forgets what it learned.</p></details>
    <div class="card"><div class="row between"><div><div class="headline">Takeout folder</div><p class="hint" id="tk-folder" style="margin-top:4px"></p></div>
      <div class="row"><button class="ghost" id="tk-choose">Choose…</button><button class="primary" id="tk-scan">Scan Takeout</button></div></div>
      <div id="tk-status"></div></div>
    <div id="tk-job"></div><div id="tk-results"></div>`;
  $("#tk-choose").addEventListener("click", async () => {
    const rel = await pickFolder({ title: "Your Takeout folder", kind: "takeout", start: TK && TK.folder || "" });
    if (rel === null) return;
    try { await api("takeout/folder", { method: "POST", body: { folder: rel } }); toast("Folder saved"); } catch (e) { toast(e.message); }
    loadTakeoutState();
  });
  $("#tk-scan").addEventListener("click", async () => {
    try { await api("takeout/scan", { method: "POST" }); wasTkBusy = true; FIND.needsLoad = true; } catch (e) { toast(e.message); }
    await loadStatus(); loadTakeoutState();
  });
  loadTakeoutState(true);
}
async function loadTakeoutState(first = false) {
  try { TK = await api("takeout"); } catch (e) { return; }
  const st = TK.state, c = TK.counts;
  $("#tk-folder").innerHTML = TK.folder ? `<code>${esc(TK.folder)}</code>${TK.folder_ok ? "" : ` <span class="danger">— not found on the NAS</span>`}` : "Not chosen yet";
  $("#tk-scan").disabled = !TK.folder || !TK.folder_ok || st.running;
  $("#tk-status").innerHTML = st.running
    ? `<p class="hint"><span class="spinner"></span> ${st.phase === "reading" ? "Reading" : "Looking at"} <code>${esc(st.current || "")}</code></p><div class="row end"><button class="ghost" id="tk-cancel">Stop</button></div>`
    : st.error ? `<p class="hint danger">${esc(st.error)}</p>`
    : c.entries ? `<p class="hint">${fmtN(c.entries)} photos known to Google from ${plural(c.sources, "file", "files")}${st.finished ? ` · scanned ${ago(st.finished)}` : ""}${st.errors ? ` · <span class="danger">${plural(st.errors, "file", "files")} couldn't be read (${esc((st.error_samples || [])[0] || "")})</span>` : ""}</p>`
    : st.finished ? `<p class="hint danger">No photo information was found in that folder. Check it holds the Takeout .zip files.</p>` : `<p class="hint">Not scanned yet.</p>`;
  const cancel = $("#tk-cancel"); cancel && cancel.addEventListener("click", () => api("takeout/cancel", { method: "POST" }));
  renderTkJob();
  if ((first || (!st.running && FIND.needsLoad)) && c.entries && !st.running) { FIND.needsLoad = false; loadFind(); }
  if (st.running) FIND.needsLoad = true;
  if (!c.entries) $("#tk-results").innerHTML = "";
}
function renderTkJob() {
  const j = TK.job, el = $("#tk-job");
  if (j.running) {
    const pct = j.total ? Math.round(100 * (j.phase === "developing" ? j.developed : j.to_develop + j.uploaded) / (j.total + j.to_develop)) : 0;
    el.innerHTML = `<div class="card"><div class="headline"><span class="spinner"></span>${j.phase === "developing" ? `Developing ${fmtN(j.to_develop)} photo${j.to_develop === 1 ? "" : "s"} first…` : `Uploading ${fmtN(j.total)} photo${j.total === 1 ? "" : "s"} to Google Photos…`}</div>
      <div class="progress"><i style="width:${Math.min(100, pct)}%"></i></div><div class="row end"><button class="ghost" id="tk-job-stop">Stop</button></div></div>`;
    $("#tk-job-stop").addEventListener("click", () => api("takeout/upload/cancel", { method: "POST" }));
  } else if (j.finished) {
    el.innerHTML = `<div class="card"><div class="headline"><span class="dot ${j.error || j.failed ? "warn" : "ok"}"></span>Last upload: ${plural(j.uploaded, "photo", "photos")} sent${j.failed ? `, ${fmtN(j.failed)} failed` : ""}</div>
      ${j.error ? `<p class="hint danger">${esc(j.error)}</p>` : ""}${j.stopped ? `<p class="hint">${esc(j.stopped)}</p>` : ""}</div>`;
  } else el.innerHTML = "";
}
async function loadFind(more = false) {
  const res = $("#tk-results");
  if (!more) res.innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
  let r;
  try { r = await api(`takeout/missing?${new URLSearchParams({ folder: FIND.folder, possible: FIND.possible ? 1 : 0, offset: more ? FIND.items.length : 0, limit: 60 })}`); }
  catch (e) { res.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  FIND.items = more ? FIND.items.concat(r.items) : r.items; FIND.total = r.total; FIND.folders = r.folders; FIND.foldersTotal = r.folders_total; FIND.counts = r.counts;
  if (!more) { FIND.selFolders.clear(); FIND.selRels.clear(); }
  renderFindResults();
}
function selCount() {
  let n = 0; const byDir = new Map(FIND.folders.map((f) => [f.path, f.count]));
  FIND.selFolders.forEach((d) => { n += byDir.get(d) || 0; });
  FIND.selRels.forEach((r) => { if (!FIND.selFolders.has(dirOf(r))) n += 1; });
  return n;
}
const isSel = (rel) => FIND.selRels.has(rel) || FIND.selFolders.has(dirOf(rel));
function renderFindResults() {
  const c = FIND.counts, res = $("#tk-results");
  const summary = `<div class="card"><div class="stat-grid" style="margin:0 0 10px">
      <div class="stat"><div class="k">Checked</div><div class="v">${fmtN(c.in_scope)}</div></div>
      <div class="stat"><div class="k">Already there</div><div class="v">${fmtN(c.uploaded)}</div></div>
      <div class="stat"><div class="k">Possibly there</div><div class="v">${fmtN(c.possibly)}</div></div>
      <div class="stat"><div class="k">Not found</div><div class="v">${fmtN(c.missing)}</div></div></div>
    <div class="toolrow"><button class="ghost" id="fd-folder">${FIND.folder ? "Only: " + esc(FIND.folder) : "All folders"} ▾</button>${FIND.folder ? `<button class="ghost" id="fd-folder-x">✕</button>` : ""}
      <label class="inline"><input type="checkbox" id="fd-possible" ${FIND.possible ? "checked" : ""}> Include “possibly there”</label></div>
    <p class="hint" style="margin-top:0">“Already there” means Google has a photo with the same name taken at about the same time (or Photo Manager uploaded it). “Possibly there” means only the time matches — often a different file, sometimes a burst.</p></div>`;
  if (!FIND.total) { res.innerHTML = summary + `<div class="empty">Nothing to upload — everything checked is already in Google Photos.</div>`; wireFindTop(); return; }
  const rows = FIND.folders.map((f) => `<label class="frow2"><input type="checkbox" data-dir="${esc(f.path)}" ${FIND.selFolders.has(f.path) ? "checked" : ""}><span>${f.path ? esc(f.path) : "<i>photo folder itself</i>"}</span><span class="sm">${fmtN(f.count)}</span></label>`).join("");
  res.innerHTML = summary + `<div class="card"><div class="row between"><h3 style="margin:0">Folders with photos to upload</h3><span><button class="ghost" id="fd-all">Select all</button> <button class="ghost" id="fd-none">Clear</button></span></div>
      <div class="frows">${rows}</div>${FIND.foldersTotal > FIND.folders.length ? `<p class="hint">Showing the ${FIND.folders.length} biggest of ${fmtN(FIND.foldersTotal)} folders.</p>` : ""}</div>
    <div class="grid" id="fd-grid">${FIND.items.map((it, i) => cellHtml({ ...it, uploaded: false }, i, isSel(it.rel) ? "sel" : "").replace('<span class="badges">', `<input type="checkbox" class="pick" ${isSel(it.rel) ? "checked" : ""} aria-label="Select"><span class="badges">${it.match === "possibly" ? `<span class="badge warn">Possibly there</span>` : ""}${it.developed ? "" : `<span class="badge">Not developed</span>`}`).replace('<span class="badge ok">Developed</span>', "")).join("")}</div>
    ${FIND.items.length < FIND.total ? `<div class="row end"><button class="ghost" id="fd-more">Show more (${fmtN(FIND.total - FIND.items.length)} more)</button></div>` : ""}
    <div class="selbar" id="fd-bar"></div>`;
  wireFindTop(); loadImgs(res);
  res.querySelectorAll("[data-dir]").forEach((cb) => cb.addEventListener("change", () => { cb.checked ? FIND.selFolders.add(cb.dataset.dir) : FIND.selFolders.delete(cb.dataset.dir); syncSel(); }));
  res.querySelectorAll("#fd-grid .cell").forEach((cell) => {
    const it = FIND.items[Number(cell.dataset.i)], pick = cell.querySelector(".pick");
    pick.addEventListener("click", (e) => e.stopPropagation());
    pick.addEventListener("change", () => {
      if (FIND.selFolders.has(dirOf(it.rel))) { pick.checked = true; toast("This folder is selected as a whole — untick the folder above to choose single photos"); return; }
      pick.checked ? FIND.selRels.add(it.rel) : FIND.selRels.delete(it.rel); syncSel();
    });
    cell.addEventListener("click", () => openViewer(FIND.items.map((x) => ({ ...x, uploaded: false })), Number(cell.dataset.i)));
  });
  $("#fd-all").addEventListener("click", () => {
    if (FIND.foldersTotal > FIND.folders.length) { FIND.items.forEach((it) => FIND.selRels.add(it.rel)); toast("Only the photos shown are selected — there are too many folders to select them all at once"); }
    else FIND.folders.forEach((f) => FIND.selFolders.add(f.path));
    syncSel(true);
  });
  $("#fd-none").addEventListener("click", () => { FIND.selFolders.clear(); FIND.selRels.clear(); syncSel(true); });
  const more = $("#fd-more"); more && more.addEventListener("click", () => loadFind(true));
  syncSel();
}
function wireFindTop() {
  $("#fd-folder").addEventListener("click", async () => { const r = await pickInPhotoFolder("Check only this folder", FIND.folder); if (r !== null) { FIND.folder = r; loadFind(); } });
  const x = $("#fd-folder-x"); x && x.addEventListener("click", () => { FIND.folder = ""; loadFind(); });
  $("#fd-possible").addEventListener("change", (e) => { FIND.possible = e.target.checked; loadFind(); });
}
function syncSel(full = false) {
  const res = $("#tk-results");
  if (full) res.querySelectorAll("[data-dir]").forEach((cb) => { cb.checked = FIND.selFolders.has(cb.dataset.dir); });
  res.querySelectorAll("#fd-grid .cell").forEach((cell) => {
    const rel = cell.dataset.rel, on = isSel(rel);
    cell.classList.toggle("sel", on); cell.querySelector(".pick").checked = on;
  });
  const n = selCount(), bar = $("#fd-bar"); if (!bar) return;
  const undeveloped = FIND.items.filter((it) => isSel(it.rel) && !it.developed).length;
  bar.innerHTML = `<span><b>${fmtN(n)}</b> selected${undeveloped ? ` · <span class="sm">some will be developed first</span>` : ""}</span>
    <button class="primary" id="fd-go" ${n && TK.google_connected && !TK.job.running ? "" : "disabled"}>Upload to Google Photos</button>
    ${!TK.google_connected ? `<span class="sm" style="width:100%">Connect Google Photos on the Upload tab first.</span>` : ""}`;
  const go = $("#fd-go");
  go && go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      await api("takeout/upload", { method: "POST", body: { rels: [...FIND.selRels], folders: [...FIND.selFolders], possible: FIND.possible } });
      toast("Started — developing (if needed) and uploading"); FIND.needsLoad = true; wasTkBusy = true;
    } catch (e) { toast(e.message); }
    await loadStatus(); loadTakeoutState();
  });
}

// ---- start ------------------------------------------------------------------------
(function init() {
  let tab = "library";
  try { tab = localStorage.getItem("pm-tab") || "library"; } catch { /* ignore */ }
  if (!["library", "gallery", "develop", "quarantine", "google", "storage"].includes(tab)) tab = "library";
  showTab(tab);
  loadStatus();
  if (tab !== "library") loadReport();
})();
