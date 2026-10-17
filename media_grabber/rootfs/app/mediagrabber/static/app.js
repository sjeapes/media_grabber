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
  loaders[name] && loaders[name]();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---- status ---------------------------------------------------------
async function loadStatus() {
  try { STATUS = await api("status"); } catch (e) { return; }
  const { nas, vpn, counts } = STATUS;
  const nasCls = !nas.healthy ? "bad" : nas.low_space ? "warn" : "ok";
  const nasTxt = nas.disk ? `NAS ${nas.disk.free_gb} GB free` : "NAS";
  const vpnCls = !vpn.configured ? "" : vpn.uk ? "ok" : "bad";
  const vpnTxt = !vpn.configured ? "VPN not set" : vpn.uk ? `UK VPN · ${vpn.exit.city || "GB"}` : `VPN ${vpn.exit.country || "down"}`;
  $("#pills").innerHTML =
    `<button class="pill" data-go="storage"><span class="dot ${nasCls}"></span>${esc(nasTxt)}</button>` +
    `<button class="pill" data-go="vpn"><span class="dot ${vpnCls}"></span>${esc(vpnTxt)}</button>`;
  $("#pills").querySelectorAll("[data-go]").forEach((p) => p.addEventListener("click", () => showTab(p.dataset.go)));

  const active = (counts.running || 0) + (counts.queued || 0);
  $("#qbadge").hidden = !active; $("#qbadge").textContent = active;

  const banner = $("#banner");
  if (!nas.healthy) { banner.className = "banner"; banner.textContent = "Downloads paused — " + nas.problems.join(" · "); banner.hidden = false; }
  else if (nas.low_space) { banner.className = "banner warn"; banner.textContent = nas.problems.join(" · "); banner.hidden = false; }
  else banner.hidden = true;

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
      : j.status === "queued" ? "waiting" : `${j.status === "done" ? "finished" : j.status} ${ago(j.finished)}`;
    const kind = j.kind === "iplayer" ? "iPlayer" : "Link";
    return `<div class="job">
      <div class="head"><div class="title">${esc(j.title)}</div><span class="st ${j.status}">${j.status}</span></div>
      <div class="info">${kind} · ${esc(info)}${j.files.length ? " · " + esc(j.files[j.files.length - 1].split("/").pop()) : ""}</div>
      ${j.status === "running" ? `<div class="bar"><i style="width:${Math.max(2, j.progress || 0)}%"></i></div>` : ""}
      ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
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
$("#pvr-run").addEventListener("click", async () => { try { const r = await api("pvr/run", { method: "POST" }); toast(`Queued ${r.queued} series check(s)`); } catch (e) { toast(e.message); } });
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
  const cls = !nas.healthy ? "bad" : nas.low_space ? "warn" : "ok";
  const head = !nas.healthy ? "Not ready — downloads are paused" : nas.low_space ? "Working, but space is low" : "Healthy";
  $("#nas").innerHTML = `<div class="card">
    <div class="headline"><span class="dot ${cls}"></span>${head}</div>
    ${nas.problems.length ? `<ul class="problems">${nas.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    ${d ? `${meter(d.used_pct)}<div class="stat-grid">
      <div class="stat"><div class="k">Free</div><div class="v">${d.free_gb} GB</div></div>
      <div class="stat"><div class="k">Used</div><div class="v">${d.used_pct}%</div></div>
      <div class="stat"><div class="k">Total</div><div class="v">${d.total_gb} GB</div></div>
      <div class="stat"><div class="k">Write test</div><div class="v">${nas.probe && nas.probe.ok ? nas.probe.ms + " ms" : nas.probe && nas.probe.ok === false ? "failed" : "—"}</div></div>
    </div>` : ""}
    <dl class="kv">
      <dt>Path</dt><dd>${esc(nas.path)}</dd>
      <dt>Mount</dt><dd>${nas.mounted ? esc(`${nas.fstype} from ${nas.source}`) : "not mounted"}</dd>
      <dt>iPlayer folder</dt><dd>${esc(nas.folders.iplayer)}</dd>
      <dt>YouTube folder</dt><dd>${esc(nas.folders.youtube)}</dd>
      <dt>Last write test</dt><dd>${nas.probe && nas.probe.at ? ago(nas.probe.at) : "—"}</dd>
      <dt>Low-space warning</dt><dd>below ${nas.low_space_gb} GB</dd>
    </dl></div>`;
  const h = nas.ha_disk;
  $("#hadisk").innerHTML = h ? `<div class="card"><div class="headline">Home Assistant disk</div>
    ${meter(h.used_pct)}<p class="hint">${h.free_gb} GB free of ${h.total_gb} GB. Downloads never go here — only to the NAS.</p></div>` : "";
  const v = STATUS && STATUS.versions;
  $("#versions").textContent = v ? `get_iplayer ${v.get_iplayer || "?"} · yt-dlp ${v["yt-dlp"] || "?"}${STATUS.ha_connected ? " · publishing sensors to Home Assistant" : ""}` : "";
}
$("#nas-check").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try { renderStorage(await api("nas/check", { method: "POST" })); } catch (err) { toast(err.message); }
  e.target.disabled = false;
});
loaders.storage = () => STATUS && renderStorage(STATUS.nas);

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
