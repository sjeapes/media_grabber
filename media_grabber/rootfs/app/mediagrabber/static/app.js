"use strict";
// All URLs are relative so the page works behind Home Assistant ingress.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let STATUS = null;

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch("api/" + path, init);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

function toast(msg, ms = 3500) {
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
function inFuture(ts) {
  if (!ts) return "";
  const s = ts - Date.now() / 1000;
  if (s < 90) return "in a moment";
  if (s < 5400) return "in " + Math.round(s / 60) + " min";
  return "in " + Math.round(s / 3600) + " h";
}

// ---- tabs -------------------------------------------------------------
const loaders = {};
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + name));
  try { localStorage.setItem("mg-tab", name); } catch { /* storage may be blocked */ }
  if (name !== "browse") browserPause();
  loaders[name] && loaders[name]();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---- status ---------------------------------------------------------
async function loadStatus() {
  try { STATUS = await api("status"); } catch (e) { return; }
  const { nas, vpn, counts } = STATUS;
  const nasCls = !nas.healthy ? "bad" : nas.low_space ? "warn" : "ok";
  const nasTxt = nas.disk ? `${nas.mode === "local" ? "HA disk" : nas.name || "NAS"} · ${nas.disk.free_gb} GB free`
    : nas.needs_choice ? "Choose storage" : "Storage";
  const vpnCls = !vpn.configured ? "" : vpn.uk ? "ok" : "bad";
  const vpnTxt = !vpn.configured ? "VPN not set" : vpn.uk ? `UK VPN · ${vpn.exit.city || "GB"}` : `VPN ${vpn.exit.country || "down"}`;
  $("#pills").innerHTML =
    `<button class="pill" data-go="storage"><span class="dot ${nasCls}"></span>${esc(nasTxt)}</button>` +
    `<button class="pill" data-go="vpn"><span class="dot ${vpnCls}"></span>${esc(vpnTxt)}</button>`;
  $("#pills").querySelectorAll("[data-go]").forEach((p) => p.addEventListener("click", () => showTab(p.dataset.go)));

  const active = (counts.running || 0) + (counts.queued || 0);
  $("#qbadge").hidden = !active; $("#qbadge").textContent = active;

  const banner = $("#banner");
  if (!nas.healthy) {
    banner.className = "banner";
    banner.innerHTML = "Downloads paused — " + esc(nas.problems.join(" · ")) +
      (!$("#tab-storage").classList.contains("on") ? ` <a href="#" id="banner-go">Open Storage</a>` : "");
    banner.hidden = false;
    const g = $("#banner-go"); g && g.addEventListener("click", (e) => { e.preventDefault(); showTab("storage"); });
  } else if (nas.low_space || nas.mode === "local") {
    banner.className = "banner warn"; banner.textContent = nas.warnings.join(" · "); banner.hidden = !nas.warnings.length;
  } else banner.hidden = true;

  const idx = STATUS.index;
  $("#index-note").innerHTML = STATUS.iplayer_block ? esc(STATUS.iplayer_block)
    : idx.running ? `<span class="spinner"></span> Updating the iPlayer programme list — searches may be incomplete for a few minutes.`
    : `Programme list updated ${ago(idx.last_ok)}${idx.last_error ? " · last error: " + esc(idx.last_error) : ""} · <a href="#" id="idx-refresh">refresh now</a>`;
  const rl = $("#idx-refresh");
  rl && rl.addEventListener("click", async (e) => { e.preventDefault(); try { await api("iplayer/refresh", { method: "POST" }); toast("Refreshing programme list…"); loadStatus(); } catch (err) { toast(err.message); } });
  $("#pvr-next").textContent = STATUS.next_pvr ? `Checks every ${STATUS.options.pvr_interval_hours} h · next ${inFuture(STATUS.next_pvr)}` : "";
  if ($("#tab-storage").classList.contains("on")) renderStorage(nas);
  if ($("#tab-vpn").classList.contains("on")) renderVpn(vpn);
}

// ---- iPlayer --------------------------------------------------------
$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#q").value.trim(); if (!q) return;
  const btn = e.submitter; btn.disabled = true;
  $("#results").innerHTML = `<div class="empty"><span class="spinner"></span> Searching… (the first search builds the programme list and can take a few minutes)</div>`;
  try {
    const rows = await api(`iplayer/search?q=${encodeURIComponent(q)}&type=${$("#qtype").value}`);
    renderResults(rows, q);
  } catch (err) { $("#results").innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
  finally { btn.disabled = false; }
});

function renderResults(rows, q) {
  if (!rows.length) { $("#results").innerHTML = `<div class="empty">Nothing available for “${esc(q)}”. iPlayer only lists what's currently on the service.</div>`; return; }
  $("#results").innerHTML = rows.map((r, i) => `
    <article class="prog">
      ${r.thumbnail ? `<img loading="lazy" src="${esc(r.thumbnail)}" alt="" onerror="this.remove()">` : ""}
      <div class="body">
        <div class="name">${esc(r.name)}</div>
        <div class="ep">${esc(r.episode)}</div>
        <div class="meta">${esc(r.channel)}${r.minutes ? " · " + r.minutes + " min" : ""}${r.available ? " · " + esc(r.available.slice(0, 10)) : ""}</div>
        <div class="desc">${esc(r.desc)}</div>
      </div>
      <div class="acts">
        <button class="primary" data-i="${i}" data-act="get">Download</button>
        <button class="ghost" data-i="${i}" data-act="series" title="Record every new episode">+ Series</button>
      </div>
    </article>`).join("");
  $("#results").querySelectorAll("button[data-i]").forEach((b) => b.addEventListener("click", async () => {
    const r = rows[+b.dataset.i];
    try {
      if (b.dataset.act === "get") {
        await api("iplayer/download", { method: "POST", body: { target: r.pid, title: `${r.name} — ${r.episode}` } });
        b.textContent = "Queued ✓"; b.disabled = true; loadStatus();
      } else {
        await api("pvr", { method: "POST", body: { name: r.name, query: r.name, type: r.type === "radio" ? "radio" : "tv" } });
        b.textContent = "Added ✓"; b.disabled = true; toast(`Every new “${r.name}” episode will be recorded`);
      }
    } catch (err) { toast(err.message); }
  }));
}

$("#pid-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("iplayer/download", { method: "POST", body: { target: $("#pid").value, series: $("#pid-series").checked, subtitles: $("#pid-subs").checked } });
    $("#pid").value = ""; toast("Added to the queue"); loadStatus();
  } catch (err) { toast(err.message); }
});

// ---- links ----------------------------------------------------------
$("#link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("link/download", { method: "POST", body: { url: $("#url").value, audio_only: $("#link-audio").checked, playlist: $("#link-playlist").checked } });
    $("#url").value = ""; toast("Added to the queue"); loadStatus();
  } catch (err) { toast(err.message); }
});

// ---- queue ------------------------------------------------------------
async function loadJobs() {
  let jobs;
  try { jobs = await api("jobs"); } catch { return; }
  if (!jobs.length) { $("#jobs").innerHTML = `<div class="empty">Nothing downloading. Search iPlayer or paste a link to start.</div>`; return; }
  const open = new Set([...document.querySelectorAll("#jobs details[open]")].map((d) => d.dataset.id));
  $("#jobs").innerHTML = jobs.map((j) => {
    const info = j.status === "running"
      ? [j.progress ? j.progress.toFixed(1) + "%" : "starting", j.speed, j.eta && "ETA " + j.eta].filter(Boolean).join(" · ")
      : j.status === "queued" ? (j.retry_at && j.retry_at > Date.now() / 1000 ? `retrying ${inFuture(j.retry_at)}` : "waiting") : `${j.status === "done" ? "finished" : j.status} ${ago(j.finished)}`;
    const kind = j.kind === "iplayer" ? "iPlayer" : "Link";
    return `<div class="job">
      <div class="head"><div class="title">${esc(j.title)}</div><span class="st ${j.status}">${j.status}</span></div>
      <div class="info">${kind} · ${esc(info)}${j.attempt > 1 ? ` · attempt ${j.attempt} of 3` : ""}${j.files.length ? " · " + esc(j.files[j.files.length - 1].split("/").pop()) : ""}</div>
      ${j.status === "running" ? `<div class="bar"><i style="width:${Math.max(2, j.progress || 0)}%"></i></div>` : ""}
      ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
      ${j.status !== "failed" && j.past_errors && j.past_errors.length ? `<div class="info">Earlier attempt: ${esc(j.past_errors[j.past_errors.length - 1])}</div>` : ""}
      <div class="row end">
        ${["queued", "running"].includes(j.status) ? `<button class="ghost danger" data-cancel="${j.id}">Cancel</button>` : ""}
        ${["failed", "cancelled"].includes(j.status) ? `<button class="ghost" data-retry="${j.id}">Retry</button>` : ""}
      </div>
      ${j.log.length ? `<details class="log" data-id="${j.id}" ${open.has(j.id) ? "open" : ""}><summary>Log</summary><pre>${esc(j.log.join("\n"))}</pre></details>` : ""}
    </div>`;
  }).join("");
  $("#jobs").querySelectorAll("[data-cancel]").forEach((b) => b.addEventListener("click", async () => { await api(`jobs/${b.dataset.cancel}/cancel`, { method: "POST" }); loadJobs(); }));
  $("#jobs").querySelectorAll("[data-retry]").forEach((b) => b.addEventListener("click", async () => { try { await api(`jobs/${b.dataset.retry}/retry`, { method: "POST" }); loadJobs(); } catch (e) { toast(e.message); } }));
}
$("#clear-jobs").addEventListener("click", async () => { await api("jobs/clear", { method: "POST" }); loadJobs(); });
loaders.queue = loadJobs;

// ---- series (PVR) ---------------------------------------------------
async function loadPvr() {
  const items = await api("pvr");
  $("#pvr").innerHTML = items.length ? items.map((i) => `
    <div class="list-item"><div><div><b>${esc(i.name)}</b></div>
      <div class="sm">matches “${esc(i.query)}” · ${i.type}${i.subtitles ? " · subtitles" : ""} · last checked ${ago(i.last_run)}</div></div>
      <button class="ghost danger" data-del="${i.id}">Remove</button></div>`).join("")
    : `<div class="empty">No series recordings yet. Add one above, or use “+ Series” on a search result.</div>`;
  $("#pvr").querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => { await api(`pvr/${b.dataset.del}`, { method: "DELETE" }); loadPvr(); }));
}
$("#pvr-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("pvr", { method: "POST", body: { query: $("#pvr-q").value, name: $("#pvr-q").value, type: $("#pvr-type").value, subtitles: $("#pvr-subs").checked } });
    $("#pvr-q").value = ""; loadPvr();
  } catch (err) { toast(err.message); }
});
$("#pvr-run").addEventListener("click", async () => { try { const r = await api("pvr/run", { method: "POST" }); toast(r.running ? "A series check is already running" : "Checking for new episodes — refreshing the iPlayer list first"); } catch (e) { toast(e.message); } });
loaders.series = loadPvr;

// ---- library ------------------------------------------------------
async function loadLibrary() {
  const files = await api("files");
  $("#library").innerHTML = files.length ? files.map((f) => `
    <div class="list-item"><div><div>${esc(f.name)}</div>
      <div class="sm">${f.source}${f.folder ? " / " + esc(f.folder) : ""} · ${f.size_mb >= 1024 ? (f.size_mb / 1024).toFixed(2) + " GB" : f.size_mb + " MB"} · ${ago(f.mtime)}</div></div></div>`).join("")
    : `<div class="empty">No downloads on the NAS yet.</div>`;
}
$("#lib-refresh").addEventListener("click", loadLibrary);
loaders.library = loadLibrary;

// ---- storage ------------------------------------------------------
function meter(pct) {
  const cls = pct >= 95 ? "bad" : pct >= 85 ? "warn" : "";
  return `<div class="meter ${cls}"><i style="width:${Math.min(100, pct || 0)}%"></i></div>`;
}
function renderStorage(nas) {
  const d = nas.disk;
  const cls = !nas.healthy ? "bad" : (nas.low_space || nas.mode === "local") ? "warn" : "ok";
  const head = !nas.healthy ? "Not ready — downloads are paused"
    : nas.low_space ? "Working, but space is low"
    : nas.mode === "local" ? "Saving to Home Assistant's disk" : "Healthy";
  const where = nas.mode === "local" ? "Home Assistant disk" : nas.source ? `${nas.fstype} from ${nas.source}` : "—";
  $("#nas").innerHTML = `<div class="card">
    <div class="headline"><span class="dot ${cls}"></span>${head}</div>
    ${nas.problems.length ? `<ul class="problems">${nas.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    ${nas.warnings.length && nas.healthy ? `<p class="hint">${esc(nas.warnings.join(" · "))}</p>` : ""}
    ${d ? `${meter(d.used_pct)}<div class="stat-grid">
      <div class="stat"><div class="k">Free</div><div class="v">${d.free_gb} GB</div></div>
      <div class="stat"><div class="k">Used</div><div class="v">${d.used_pct}%</div></div>
      <div class="stat"><div class="k">Total</div><div class="v">${d.total_gb} GB</div></div>
      <div class="stat"><div class="k">Write test</div><div class="v">${nas.probe && nas.probe.ok ? nas.probe.ms + " ms" : nas.probe && nas.probe.ok === false ? "failed" : "—"}</div></div>
    </div>` : ""}
    ${nas.path ? `<dl class="kv">
      <dt>Location</dt><dd>${esc(nas.name || "—")}</dd>
      <dt>Path</dt><dd>${esc(nas.path)}</dd>
      <dt>Mount</dt><dd>${esc(where)}</dd>
      ${nas.folders ? `<dt>iPlayer folder</dt><dd>${esc(nas.folders.iplayer)}</dd>
      <dt>YouTube folder</dt><dd>${esc(nas.folders.youtube)}</dd>` : ""}
      <dt>Last write test</dt><dd>${nas.probe && nas.probe.at ? ago(nas.probe.at) : "—"}</dd>
      <dt>Low-space warning</dt><dd>below ${nas.low_space_gb} GB${nas.mode === "local" ? ` · stops below ${nas.local_min_free_gb} GB` : ""}</dd>
    </dl>` : ""}</div>`;
  const h = nas.ha_disk;
  $("#hadisk").innerHTML = h && nas.mode !== "local" ? `<div class="card"><div class="headline">Home Assistant disk</div>
    ${meter(h.used_pct)}<p class="hint">${h.free_gb} GB free of ${h.total_gb} GB. Downloads aren't going here.</p></div>` : "";
  const v = STATUS && STATUS.versions;
  $("#versions").textContent = v ? `get_iplayer ${v.get_iplayer || "?"} · yt-dlp ${v["yt-dlp"] || "?"}${STATUS.ha_connected ? " · publishing sensors to Home Assistant" : ""}` : "";
}

async function loadLocation() {
  let o;
  try { o = await api("storage/options"); } catch (e) { $("#location").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const n = o.candidates.length;
  const ha = o.local.ha_disk;
  const opt = (value, title, sub, extra = "") => `
    <label class="opt ${o.choice === value ? "sel" : ""}">
      <input type="radio" name="loc" value="${esc(value)}" ${o.choice === value ? "checked" : ""}>
      <span><b>${title}</b><span class="sm">${sub}</span>${extra}</span>
    </label>`;
  const autoSub = n === 0 ? "No network storage found yet"
    : n === 1 ? `Uses your network storage “${esc(o.candidates[0].name)}”`
    : `${n} found — pick one below`;
  const html = [
    opt("auto", "Automatic", autoSub + " · follows Settings → System → Storage"),
    ...o.candidates.map((c) => opt(c.id, esc(c.name),
      `${esc(c.usage)} storage · ${esc(c.fstype)} from ${esc(c.source)}${c.free_gb != null ? ` · ${c.free_gb} GB free` : " · not responding"}`)),
    opt("local", "Home Assistant storage",
      `${esc(o.local.path)}${ha ? ` · ${ha.free_gb} GB free of ${ha.total_gb} GB` : ""}`,
      `<span class="sm warn-t">Shares the disk Home Assistant runs on. Downloads stop if free space drops below ${esc(o.local.min_free_gb)} GB.</span>`),
  ].join("");
  $("#location").innerHTML = `<form class="card" id="loc-form">${html}
    ${n === 0 ? `<p class="hint">Add your NAS in Home Assistant under Settings → System → Storage → Add network storage (usage: Media), then press Re-scan.</p>` : ""}
    <div class="row end"><button class="primary">Use this location</button></div></form>`;
  $("#loc-form").querySelectorAll("input[name=loc]").forEach((r) => r.addEventListener("change", () =>
    $("#loc-form").querySelectorAll(".opt").forEach((l) => l.classList.toggle("sel", l.querySelector("input").checked))));
  $("#loc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const choice = new FormData(e.target).get("loc");
    if (choice === "local" && !confirm("Save downloads to Home Assistant's own disk? It has limited space.")) return;
    try {
      const st = await api("storage/select", { method: "POST", body: { choice } });
      toast(st.healthy ? `Downloads will go to ${st.name}` : st.problems.join(" · "), 5000);
      await loadStatus(); loadLocation();
    } catch (err) { toast(err.message); }
  });
}
$("#loc-rescan").addEventListener("click", () => { loadLocation(); loadStatus(); });

$("#nas-check").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try { renderStorage(await api("nas/check", { method: "POST" })); } catch (err) { toast(err.message); }
  e.target.disabled = false;
});
loaders.storage = () => { loadLocation(); STATUS && renderStorage(STATUS.nas); };

// ---- space (disk usage) ---------------------------------------------
const fmtB = (b) => b >= 1024 ** 4 ? (b / 1024 ** 4).toFixed(2) + " TB" : b >= 1024 ** 3 ? (b / 1024 ** 3).toFixed(1) + " GB"
  : b >= 1024 ** 2 ? Math.round(b / 1024 ** 2) + " MB" : Math.round(b / 1024) + " KB";
let usageTimer = null;
async function loadUsage() {
  let u;
  try { u = await api("usage"); } catch (e) { $("#usage").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const sel = $("#scan-target"), prev = sel.value;
  sel.innerHTML = u.targets.map((t) => `<option value="${esc(t.path)}">${esc(t.name)}${t.free_gb != null ? ` — ${t.free_gb} GB free` : ""}</option>`).join("");
  sel.value = prev || (u.state.path && u.targets.some((t) => t.path === u.state.path) ? u.state.path : u.default || u.targets[0]?.path || "");
  const st = u.state;
  $("#scan-go").textContent = st.running ? "Cancel" : "Scan";
  $("#scan-go").classList.toggle("danger", st.running);
  $("#scan-progress").innerHTML = st.running
    ? `<p class="note" style="margin-top:12px"><span class="spinner"></span> Scanning ${esc(st.path)} — ${st.files.toLocaleString()} files, ${fmtB(st.bytes)} so far${st.current ? ` · <span class="sm">${esc(st.current.slice(-60))}</span>` : ""}</p>`
    : st.error ? `<p class="problems">${esc(st.error)}</p>`
    : st.cancelled ? `<p class="hint">Last scan was cancelled.</p>` : "";
  clearTimeout(usageTimer);
  if (st.running && $("#tab-space").classList.contains("on")) usageTimer = setTimeout(loadUsage, 2000);
  if (!u.root) { $("#usage").innerHTML = st.running ? "" : `<div class="empty">No scan yet. Pick a location and press Scan.</div>`; return; }
  const d = st.disk, total = u.root.s;
  const hidden = d ? d.used_gb * 1024 ** 3 - total : 0;
  const rec = u.recycle_bins || [];
  $("#usage").innerHTML = `
    <div class="card">
      <div class="headline">${esc(st.path)}</div>
      <div class="stat-grid">
        <div class="stat"><div class="k">Found by scan</div><div class="v">${fmtB(total)}</div></div>
        ${d ? `<div class="stat"><div class="k">Storage used</div><div class="v">${d.used_gb} GB</div></div>
        <div class="stat"><div class="k">Free</div><div class="v">${d.free_gb} GB</div></div>` : ""}
        <div class="stat"><div class="k">Files</div><div class="v">${u.root.f.toLocaleString()}</div></div>
      </div>
      <p class="hint">Scanned ${ago(st.finished)}${st.errors ? ` · ${st.errors} items couldn't be read` : ""}.
      ${hidden > 5 * 1024 ** 3 ? ` About <b>${fmtB(hidden)}</b> of used space isn't visible from here — usually snapshots, the NAS recycle bin, or other shares/volumes on the same disk. Check those in your NAS's own storage manager.` : ""}</p>
      ${rec.length ? `<p class="banner warn" style="margin:10px 0 0">Recycle bin / snapshot folders: ${rec.map((r) => `${esc(r.path)} (${r.gb} GB)`).join(", ")} — empty these in the NAS's settings.</p>` : ""}
    </div>
    <div class="toolbar"><h2>Folders</h2><span class="sm note" style="margin:0">tap a folder to open it</span></div>
    <div class="card tree" id="tree"></div>
    <div class="toolbar"><h2>Largest files</h2></div>
    <div class="card"><div class="files">${(u.largest_files || []).map((f) => `<div class="frow"><span class="fp">${esc(f.path)}</span><b>${f.gb} GB</b></div>`).join("") || `<div class="empty">None</div>`}</div></div>`;
  renderTreeLevel($("#tree"), u.root, total, 0);
}
function renderTreeLevel(container, node, total, depth) {
  const rows = node.children.map((k) => `
    <div class="tnode" data-rel="${esc(k.rel)}">
      <button class="trow" ${k.has_children ? "" : "disabled"} style="padding-left:${10 + depth * 16}px">
        <span class="caret">${k.has_children ? "▸" : ""}</span>
        <span class="tname">${esc(k.name)}${k.flag === "recycle" ? ' <span class="tag">recycle bin</span>' : ""}</span>
        <span class="tsize">${fmtB(k.s)}</span>
        <span class="tbar"><i style="width:${Math.max(1, (100 * k.s) / total).toFixed(1)}%"></i></span>
        <span class="tpct">${((100 * k.s) / total).toFixed(1)}%</span>
      </button><div class="tkids" hidden></div></div>`).join("");
  const extra = [];
  if (node.own_files > 0) extra.push(["files directly in this folder", node.own_files]);
  if (node.small_folders > 0) extra.push(["smaller folders (under 50 MB each)", node.small_folders]);
  container.innerHTML = rows + extra.map(([n, s]) => `<div class="trow muted" style="padding-left:${26 + depth * 16}px">
      <span class="tname">${n}</span><span class="tsize">${fmtB(s)}</span><span class="tbar"><i style="width:${Math.max(1, (100 * s) / total).toFixed(1)}%"></i></span><span class="tpct">${((100 * s) / total).toFixed(1)}%</span></div>`).join("");
  container.querySelectorAll(":scope > .tnode > .trow:not([disabled])").forEach((b) => b.addEventListener("click", async () => {
    const holder = b.nextElementSibling, caret = b.querySelector(".caret");
    if (!holder.hidden) { holder.hidden = true; caret.textContent = "▸"; return; }
    if (!holder.dataset.loaded) {
      try { renderTreeLevel(holder, await api("usage/children?rel=" + encodeURIComponent(b.parentElement.dataset.rel)), total, depth + 1); holder.dataset.loaded = "1"; }
      catch (e) { toast(e.message); return; }
    }
    holder.hidden = false; caret.textContent = "▾";
  }));
}
$("#scan-go").addEventListener("click", async () => {
  try {
    if ($("#scan-go").textContent === "Cancel") await api("usage/cancel", { method: "POST" });
    else await api("usage/scan", { method: "POST", body: { path: $("#scan-target").value } });
  } catch (e) { toast(e.message); }
  setTimeout(loadUsage, 300);
});
loaders.space = loadUsage;

// ---- browse (remote browser over the UK VPN) -----------------------------
const B = { on: false, seq: 0, loop: 0, url: "", ctx: null, chain: Promise.resolve(), down: null, lastMove: 0, wheel: null };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function bMsg(t) { const m = $("#screen-msg"); m.innerHTML = t; m.hidden = !t; }
function bSize() { const r = $("#screen-wrap").getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; }
function bSend(o) { // keep input events in order
  B.chain = B.chain.then(() => fetch("api/browser/input", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(o) })).catch(() => {});
}
async function browserOpen() {
  if (B.on) return;
  B.on = true;
  if (!B.seq) bMsg(`<span class="spinner"></span> Starting the browser through the UK VPN…`);
  try { await api("browser/start", { method: "POST", body: bSize() }); }
  catch (e) { bMsg(esc(e.message)); B.on = false; return; }
  frameLoop(++B.loop); stateLoop(B.loop);
}
function browserPause() { B.on = false; }
async function frameLoop(id) {
  while (B.on && id === B.loop) {
    try {
      const r = await fetch("api/browser/frame?after=" + B.seq);
      if (r.status === 200) {
        B.seq = +r.headers.get("X-Seq") || B.seq + 1;
        const url = URL.createObjectURL(await r.blob());
        const img = $("#screen"), old = img.src;
        img.src = url; bMsg("");
        if (old.startsWith("blob:")) setTimeout(() => URL.revokeObjectURL(old), 1000);
      } else if (r.status !== 204) { await sleep(1500); B.on && api("browser/start", { method: "POST", body: bSize() }).catch((e) => bMsg(esc(e.message))); }
    } catch { await sleep(1500); }
  }
}
async function stateLoop(id) {
  while (B.on && id === B.loop) {
    try {
      const st = await api("browser/state");
      if (!st.running && st.error) bMsg(esc(st.error));
      $("#screen-load").hidden = !st.loading;
      if (document.activeElement !== $("#addr")) $("#addr").value = st.url && st.url !== "about:blank" ? st.url : "";
      if (st.url !== B.url && !st.loading) { B.url = st.url; refreshContext(); }
    } catch { /* keep trying */ }
    await sleep(1200);
  }
}
async function refreshContext() {
  try { B.ctx = await api("browser/context"); } catch { B.ctx = null; }
  const c = B.ctx || {};
  $("#add-prog").disabled = !c.can_add_programme;
  $("#add-series").disabled = !c.can_add_series;
  $("#add-prog").title = c.can_add_programme ? `Download “${c.title}”` : "Open an episode page to download it";
  $("#add-series").title = c.can_add_series ? `Record “${c.programme_name}”` : "Open a programme or episode page";
}
function bPoint(e) { // map a tap on the (letterboxed) picture to page coordinates
  const img = $("#screen"), r = img.getBoundingClientRect();
  const nw = img.naturalWidth || r.width, nh = img.naturalHeight || r.height;
  const k = Math.min(r.width / nw, r.height / nh);
  const ox = (r.width - nw * k) / 2, oy = (r.height - nh * k) / 2;
  const clamp = (v, max) => Math.max(0, Math.min(max - 1, v));
  return { x: Math.round(clamp((e.clientX - r.left - ox) / k, nw)), y: Math.round(clamp((e.clientY - r.top - oy) / k, nh)) };
}
const scr = $("#screen");
scr.addEventListener("contextmenu", (e) => e.preventDefault());
scr.addEventListener("pointerdown", (e) => {
  e.preventDefault(); scr.focus({ preventScroll: true });
  B.down = { type: e.pointerType, x: e.clientX, y: e.clientY, lx: e.clientX, ly: e.clientY, moved: false };
  scr.setPointerCapture(e.pointerId);
  if (e.pointerType === "mouse") bSend({ type: "down", ...bPoint(e), button: ["left", "middle", "right"][e.button] || "left" });
});
scr.addEventListener("pointermove", (e) => {
  const d = B.down;
  if (d && d.type !== "mouse") { // touch/pen drag = scroll
    const dx = d.lx - e.clientX, dy = d.ly - e.clientY;
    if (!d.moved && Math.hypot(e.clientX - d.x, e.clientY - d.y) < 8) return;
    d.moved = true; d.lx = e.clientX; d.ly = e.clientY;
    const p = bPoint(e), w = B.wheel || (B.wheel = { ...p, dx: 0, dy: 0 });
    w.dx += dx * 1.5; w.dy += dy * 1.5;
    if (!B.wheelT) B.wheelT = setTimeout(() => { bSend({ type: "wheel", ...B.wheel }); B.wheel = null; B.wheelT = null; }, 60);
    return;
  }
  const now = Date.now();
  if (now - B.lastMove > 120) { B.lastMove = now; bSend({ type: "move", ...bPoint(e) }); }
});
scr.addEventListener("pointerup", (e) => {
  const d = B.down; B.down = null;
  if (!d) return;
  if (d.type === "mouse") bSend({ type: "up", ...bPoint(e), button: ["left", "middle", "right"][e.button] || "left" });
  else if (!d.moved) bSend({ type: "click", ...bPoint(e) });
});
scr.addEventListener("wheel", (e) => {
  e.preventDefault();
  const k = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 400 : 1;
  bSend({ type: "wheel", ...bPoint(e), dx: e.deltaX * k, dy: e.deltaY * k });
}, { passive: false });
const SPECIAL = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "Home", "End", "PageUp", "PageDown", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown"]);
function bKey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (SPECIAL.has(e.key)) { e.preventDefault(); bSend({ type: "key", key: e.key }); }
  else if (e.key.length === 1 && e.target === scr) { e.preventDefault(); bSend({ type: "text", text: e.key }); }
}
scr.addEventListener("keydown", bKey);
const kbd = $("#kbd"); // phones: a hidden input brings up the on-screen keyboard
$("#kbd-btn").addEventListener("click", () => { kbd.value = ""; kbd.focus(); });
kbd.addEventListener("keydown", bKey);
kbd.addEventListener("input", (e) => { if (e.data) bSend({ type: "text", text: e.data }); kbd.value = ""; });
document.querySelectorAll("[data-nav]").forEach((b) => b.addEventListener("click", async () => {
  try { await api("browser/nav", { method: "POST", body: { action: b.dataset.nav } }); } catch (e) { toast(e.message); }
}));
$("#addr-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#addr").value.trim(); $("#addr").blur();
  try { await api("browser/nav", { method: "POST", body: { url: v } }); } catch (err) { toast(err.message); }
});
$("#add-prog").addEventListener("click", async () => {
  try { const r = await api("browser/add", { method: "POST", body: { mode: "programme" } }); toast(r.message); loadStatus(); }
  catch (e) { toast(e.message); }
});
$("#add-series").addEventListener("click", async () => {
  await refreshContext();
  const c = B.ctx || {};
  if (!c.can_add_series) return toast("Open a programme or episode page first");
  $("#series-name").value = c.programme_name || "";
  $("#series-existing-label").textContent = c.series_pid ? "Download all episodes available now" : "Download this episode now";
  $("#series-dlg").showModal();
});
$("#series-cancel").addEventListener("click", () => $("#series-dlg").close());
$("#series-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("browser/add", { method: "POST", body: {
      mode: "series", name: $("#series-name").value, download_existing: $("#series-existing").checked,
      record_new: $("#series-new").checked, subtitles: $("#series-subs").checked } });
    $("#series-dlg").close(); toast(r.message, 6000); loadStatus();
  } catch (err) { toast(err.message, 6000); }
});
let resizeT;
new ResizeObserver(() => {
  clearTimeout(resizeT);
  resizeT = setTimeout(() => { if (B.on) api("browser/resize", { method: "POST", body: bSize() }).catch(() => {}); }, 400);
}).observe($("#screen-wrap"));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) browserPause();
  else if ($("#tab-browse").classList.contains("on")) browserOpen();
});
loaders.browse = browserOpen;

// ---- VPN ------------------------------------------------------------
function renderVpn(v) {
  const x = v.exit || {};
  const cls = !v.configured ? "" : v.uk ? "ok" : "bad";
  const head = !v.configured ? "Not configured" : !v.running ? "Tunnel not running" : v.uk ? "Connected — UK exit" : `Connected, but exit is ${x.country || "unknown"}`;
  $("#vpn").innerHTML = `<div class="card">
    <div class="headline"><span class="dot ${cls}"></span>${esc(head)}</div>
    ${v.error ? `<ul class="problems"><li>${esc(v.error)}</li></ul>` : ""}
    ${x.error ? `<ul class="problems"><li>${esc(x.error)}</li></ul>` : ""}
    ${v.configured ? `<dl class="kv">
      <dt>Exit</dt><dd>${esc([x.city, x.country].filter(Boolean).join(", ") || "—")} ${x.ip ? "· " + esc(x.ip) : ""}</dd>
      <dt>Provider</dt><dd>${esc(x.org || "—")}</dd>
      <dt>Endpoint</dt><dd>${esc(v.endpoint || "—")}</dd>
      <dt>Checked</dt><dd>${x.at ? ago(x.at) : "—"}</dd>
      <dt>Used by</dt><dd>get_iplayer${STATUS && STATUS.options.vpn_for_youtube ? " and yt-dlp" : " only"}</dd>
    </dl>` : `<p class="hint">Paste a UK WireGuard config below to enable iPlayer downloads.</p>`}
    ${v.log && v.log.length ? `<details class="log"><summary>Tunnel log</summary><pre>${esc(v.log.join("\n"))}</pre></details>` : ""}
  </div>`;
}
$("#vpn-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { renderVpn(await api("vpn", { method: "POST", body: { config: $("#wg").value } })); $("#wg").value = ""; toast("Saved — connecting…"); setTimeout(loadStatus, 5000); }
  catch (err) { toast(err.message, 6000); }
});
$("#vpn-delete").addEventListener("click", async () => {
  if (!confirm("Remove the VPN config? iPlayer downloads will stop working.")) return;
  renderVpn(await api("vpn", { method: "DELETE" }));
});
$("#vpn-check").addEventListener("click", async () => { try { renderVpn(await api("vpn/check", { method: "POST" })); } catch (e) { toast(e.message); } });
loaders.vpn = () => STATUS && renderVpn(STATUS.vpn);

// ---- boot -------------------------------------------------------------
let start = "iplayer";
try { start = localStorage.getItem("mg-tab") || start; } catch { /* ignore */ }
loadStatus().then(() => showTab(document.getElementById("tab-" + start) ? start : "iplayer"));
setInterval(loadStatus, 10000);
setInterval(() => { if ($("#tab-queue").classList.contains("on")) loadJobs(); }, 2000);
