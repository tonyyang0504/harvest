"use strict";
// harvest frontend: a tiny hash-routed single page over the /api JSON endpoints.
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v) => (v === null || v === undefined || v === "" ? "—" : typeof v === "number" ? v.toLocaleString() : esc(typeof v === "object" ? JSON.stringify(v) : v));
// timestamps: "2026-10-01 12:41 UTC" (the full value in the tooltip), not raw ISO with microseconds
const when = (v) => { const m = /^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d)/.exec(String(v ?? "")); return m ? `<time datetime="${esc(v)}" title="${esc(v)}">${m[1]} ${m[2]} UTC</time>` : fmt(v); };
// a table cell: long values are clipped (the full text is in the tooltip) so one huge field cannot blow up the layout
const CLIP = 160;
const cell = (v) => {
  const s = v === null || v === undefined || v === "" ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > CLIP ? `<span title="${esc(s.slice(0, 2000))}">${esc(s.slice(0, CLIP))}…</span>` : fmt(v);
};
// only http(s) links are ever rendered as links: scraped data can carry javascript: or data: URLs
const safeUrl = (u) => { try { const x = new URL(String(u), location.href); return /^https?:$/.test(x.protocol) ? x.href : ""; } catch { return ""; } };
const link = (u, text) => { const h = safeUrl(u); return h ? `<a href="${esc(h)}" target="_blank" rel="noopener noreferrer">${text}</a>` : text; };
const enc = encodeURIComponent;
let timer = null;
let seq = 0; // render generation: a slow response for an old route never paints over a newer one
let painted = ""; // the last HTML painted into the view
let pollFailing = false;
const results = {}; // per project: the last schedule / watchdog result, shown until replaced

function token() { try { return localStorage.getItem("harvest_token") || ""; } catch { return ""; } }
function setToken(t) { try { t ? localStorage.setItem("harvest_token", t) : localStorage.removeItem("harvest_token"); } catch {} }

async function api(path, opts = {}) {
  let r;
  try { r = await fetch("/api" + path, { ...opts, headers: { "Content-Type": "application/json", Authorization: "Bearer " + token(), ...(opts.headers || {}) } }); }
  catch { throw new Error("cannot reach the harvest server (network error)"); }
  if (r.status === 401) {
    const had = !!token(), msg = had ? "wrong or expired admin token: sign in again" : "sign in first";
    setToken(""); route(); toast(msg); // the view is gone now, so say it here
    throw new Error(msg);
  }
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(errorText(body, r.status));
  return body;
}
function errorText(body, status) {
  if (body && typeof body === "object") {
    if (body.refused) return "refused: " + [].concat(body.refused).join("; ");
    if (body.message) return body.message;
    if (Array.isArray(body.detail)) return body.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    if (body.detail) return String(body.detail);
  }
  return (typeof body === "string" && body.trim()) || `request failed (HTTP ${status})`;
}
const post = (path, data = {}) => api(path, { method: "POST", body: JSON.stringify(data) });

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 4500); }
function chip(s) {
  const cls = { ok: "ok", enabled: "ok", pass: "ok", done: "ok", allowed: "ok", reviewed: "ok", no_clause: "ok", partial: "warn", running: "warn", queued: "warn", degraded: "warn",
    scaffolded: "warn", lane_detected: "warn", unknown: "warn", candidate: "", empty: "bad", error: "bad", failed: "bad", fail: "bad", none: "bad", lane_none: "bad",
    forbids: "bad", rejected: "bad", review_failed: "bad", review_stale: "bad", disallowed: "bad", timeout: "bad", stuck: "bad", lost: "bad", blocked: "bad" }[s] ?? "";
  return `<span class="chip ${cls}">${esc(s ?? "—")}</span>`;
}
function table(cols, rows, render, empty = "Nothing yet.") {
  if (!rows.length) return `<p class="muted empty">${esc(empty)}</p>`;
  return `<div class="tablewrap" tabindex="0" role="region" aria-label="${esc(cols.slice(0, 3).join(", "))} table"><table><thead><tr>${cols.map((c) => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead><tbody>${rows.map(render).join("")}</tbody></table></div>`;
}

// Paint the view. A background refresh (quiet) that changes nothing leaves the DOM alone, and one that does change
// it keeps the keyboard focus, open <details> and scroll position, so polling never yanks the page from the user.
function paint(html, quiet) {
  if (quiet && html === painted) return false;
  const key = quiet ? focusKey(document.activeElement) : null;
  const open = quiet ? [...view.querySelectorAll("details")].map((d) => d.open) : [];
  const y = window.scrollY;
  view.innerHTML = html;
  painted = html;
  if (quiet) {
    view.querySelectorAll("details").forEach((d, i) => { if (open[i]) d.open = true; });
    if (key) { const el = [...view.querySelectorAll(key.sel)].find((e) => focusKey(e)?.id === key.id); if (el) el.focus({ preventScroll: true }); }
    window.scrollTo(0, y);
  }
  return true;
}
function focusKey(el) {
  if (!el || el === document.body || !view.contains(el)) return null;
  const attrs = ["data-act", "data-src", "data-proxy", "data-ua", "data-export", "data-page", "href", "name"].filter((a) => el.hasAttribute(a));
  if (!attrs.length) return null;
  return { sel: el.tagName.toLowerCase(), id: attrs.map((a) => a + "=" + el.getAttribute(a)).join("|") + "|" + (el.dataset.id || "") };
}
function errorState(title, msg) {
  return `<div class="panel error" role="alert"><h1>${esc(title)}</h1><p>${esc(msg)}</p><p><a href="#/">Back to projects</a> · <a href="${esc(location.hash || "#/")}" data-retry>Try again</a></p></div>`;
}

// ---------------------------------------------------------------- views
function loginView() {
  $("#logout").hidden = true;
  $("#crumbs").innerHTML = "";
  painted = "";
  view.innerHTML = `<div class="panel" style="max-width:460px;margin:40px auto">
    <h1>Sign in</h1><p class="muted" id="tokhelp">Paste the admin token (HARVEST_ADMIN_TOKEN, or the one printed when the server started).</p>
    <form id="login" class="row"><label for="tok" class="sr-only">Admin token</label><input id="tok" type="password" placeholder="admin token" autocomplete="current-password" aria-describedby="tokhelp" required style="flex:1"><button>Sign in</button></form></div>`;
  $("#login").onsubmit = async (e) => {
    e.preventDefault();
    setToken($("#tok").value.trim());
    try { await api("/me"); route(); } catch (err) { toast(err.message); }
  };
  $("#tok").focus();
}

async function homeView(my, quiet) {
  const [{ projects }, t] = await Promise.all([api("/projects"), api("/templates")]);
  if (my !== seq) return;
  $("#crumbs").innerHTML = "";
  if (quiet) return;
  paint(`
  <h1>Projects</h1><p class="muted">Enter a target and regions; agents find the sites, write and review scrapers, and collect clean data on a schedule.</p>
  <div class="panel"><h2 style="margin-top:0">New project</h2>
   <form id="newp" class="stack">
    <label>Name<input name="name" placeholder="used-cars-caucasus" pattern="[a-z0-9][a-z0-9_\\-]{1,62}" title="2-63 characters: lowercase letters, digits, - or _" required></label>
    <label>Target<input name="target" placeholder="used cars" required></label>
    <label>Regions<input name="regions" placeholder="KZ, GE  or  EU, GCC, Portugal" required></label>
    <label>Record type<select name="record_type">${t.templates.map((x) => `<option value="${esc(x.record_type)}">${esc(x.label)}</option>`).join("")}</select></label>
    <label>Extra fields<input name="fields" placeholder="comma-separated, optional"></label>
    <label>Max sources<input name="max_sources" type="number" value="30" min="1" required></label>
    <label>Cadence<select name="cadence">${["daily", "6h", "12h", "hourly", "weekly", "monthly", "manual"].map((c) => `<option>${c}</option>`).join("")}</select></label>
    <label>Report currency<input name="report_currency" value="USD" pattern="[A-Za-z]{3}" title="a 3-letter ISO currency code" maxlength="3" required></label>
    <div class="row" style="align-self:end"><button>Create</button></div>
   </form>
   <p class="muted" style="margin-bottom:0">Region groups: ${Object.keys(t.region_groups).map(esc).join(", ")}</p></div>
  <h2>Existing</h2>
  ${table(["Name", "Target", "Record type", "Regions", "Cadence"], projects, (p) =>
    `<tr><td><a href="#/p/${esc(enc(p.name))}">${esc(p.name)}</a></td><td>${esc(p.target)}</td><td>${esc(p.record_type)}</td><td>${esc((p.region_codes || p.regions || []).join(", "))}</td><td>${esc(p.cadence)}</td></tr>`,
    "No projects yet. Create one above.")}`, false);
  $("#newp").onsubmit = async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button"); btn.disabled = true;
    const f = Object.fromEntries(new FormData(e.target));
    const body = { ...f, report_currency: f.report_currency.toUpperCase(), regions: f.regions.split(/[,;]/).map((s) => s.trim()).filter(Boolean),
      fields: f.fields.split(",").map((s) => s.trim()).filter(Boolean), max_sources: +f.max_sources };
    try { await post("/projects", body); location.hash = `#/p/${enc(f.name)}`; } catch (err) { toast(err.message); } finally { btn.disabled = false; }
  };
}

const TABS = ["overview", "census", "sources", "data", "runs", "quarantine", "jobs"];
const POLLED = ["overview", "census", "sources", "runs", "jobs"];

async function projectView(name, tab, my, quiet) {
  const base = `/projects/${enc(name)}`;
  const s = await api(base);
  if (my !== seq) return;
  $("#crumbs").innerHTML = `<a href="#/">projects</a> / ${esc(name)}`;
  if (!TABS.includes(tab)) tab = "overview";
  const tabs = `<nav class="tabs" aria-label="project sections">${TABS.map((t) => `<a href="#/p/${esc(enc(name))}/${t}" class="${t === tab ? "on" : ""}" ${t === tab ? 'aria-current="page"' : ""}>${t}</a>`).join("")}</nav>`;
  const head = `<h1>${esc(s.project.target)}</h1><p class="muted">${esc(s.project.record_type)} · ${esc(s.project.region_codes.join(", "))} · languages ${esc(s.project.languages.join(", "))} · ${esc(s.project.cadence)}</p>${tabs}`;
  const body = await ({ overview, census: censusTab, sources, data, runs, quarantine, jobs }[tab])(name, s);
  if (my !== seq) return;
  if (paint(head + body, quiet)) bind(name, s);
  if (POLLED.includes(tab)) timer = setTimeout(() => route(true), 4000);
}

async function overview(name, s) {
  const st = s.by_status || {}, total = s.sources || 0;
  const live = total - (st.rejected || 0);
  const stages = [["candidates", live], ["lanes detected", live - (st.candidate || 0)], ["reviewed", (st.reviewed || 0) + (st.enabled || 0)], ["enabled", s.enabled]];
  const running = (s.jobs || []).filter((j) => j.status === "running" || j.status === "queued");
  const cs = s.census_state || {};
  return `
  <div class="grid">
    <div class="stat"><b>${fmt(total)}</b><span>sources found</span></div>
    <div class="stat"><b>${fmt(s.enabled)}</b><span>collecting</span></div>
    <div class="stat"><b>${fmt(s.records)}</b><span>records stored</span></div>
    <div class="stat"><b>${fmt(s.cross_source_duplicates)}</b><span>cross-source duplicates</span></div>
    <div class="stat"><b>${fmt(s.quarantined)}</b><span>quarantined rows</span></div>
    <div class="stat"><b>${fmt(running.length)}</b><span>jobs running</span></div>
  </div>
  ${running.length ? `<p class="muted" role="status">Working: ${running.map((j) => `${esc(j.kind)} (${esc(j.status)})`).join(", ")}</p>` : ""}
  <div class="panel" style="margin-top:16px"><h2 style="margin-top:0">Pipeline</h2>
   ${stages.map(([l, n]) => `<div class="row" style="margin:6px 0"><span style="width:130px">${l}</span><div class="bar" style="flex:1" role="progressbar" aria-label="${l}" aria-valuemin="0" aria-valuemax="${live}" aria-valuenow="${n}"><i style="width:${live ? Math.round((100 * n) / live) : 0}%"></i></div><span class="muted" style="width:60px;text-align:right">${fmt(n)}</span></div>`).join("")}
   <div class="row" style="margin-top:14px">
     <button data-act="census">Run census agent</button><button class="ghost" data-act="audit">Adversarial audit</button>
     <button class="ghost" data-act="detect" ${total ? "" : "disabled"}>Detect lanes</button><button class="ghost" data-act="run" ${s.enabled ? "" : "disabled title=\"approve a source first\""}>Run collection now</button>
     <button class="ghost" data-act="schedule">Write schedule</button><button class="ghost" data-act="watchdog">Watchdog</button>
   </div>
   ${cs.budget_reached || cs.deferred ? `<p class="muted budget" style="margin-bottom:0">${cs.budget_reached ? "Census budget reached" : "Census budget"} (${fmt(cs.max_sources)} sources, ${fmt(cs.deferred)} verified candidates deferred, ${fmt(cs.empty_cells)} empty cells).
     <button class="small" data-act="resume">Raise budget &amp; resume</button></p>` : ""}</div>
  ${resultPanel(name)}
  ${proxyPanel(s.proxy)}
  ${uaPanel(s.ua)}
  <h2>Recent runs</h2>${runsTable(s.recent_runs.slice(0, 8))}
  <h2>Alerts</h2>${table(["When", "Source", "Kind", "Message"], s.alerts, (a) => `<tr><td>${when(a.created_at)}</td><td>${fmt(a.source_id)}</td><td>${chip(a.kind)}</td><td>${cell(a.message)}</td></tr>`, "No alerts.")}`;
}

function resultPanel(name) {
  const r = results[name];
  if (!r) return "";
  if (r.kind === "watchdog") {
    const w = r.data;
    return `<div class="panel result" id="watchdog-result"><h2 style="margin-top:0">Watchdog <span class="muted small">${when(r.at)}</span></h2>
      <p class="muted">${w.findings.length ? `${w.findings.length} finding(s), ${fmt(w.new_alerts)} new alert(s).` : "All sources healthy: no findings."}</p>
      ${w.findings.length ? table(["Source", "Finding", "Detail"], w.findings, (f) => `<tr><td>${fmt(f.source_id)}</td><td>${chip(f.kind)}</td><td>${cell(f.message)}</td></tr>`) : ""}</div>`;
  }
  const d = r.data, files = d.files || {};
  return `<div class="panel result" id="schedule-result"><h2 style="margin-top:0">Schedule <span class="muted small">${when(r.at)}</span></h2>
    <p class="muted">${esc(d.note || "")} Cadence: ${esc(d.cadence)}.</p>
    ${files.cron ? `<p>cron: <code>${esc(files.cron.path)}</code></p><pre>${esc(files.cron.content)}</pre><p class="muted">Install: <code>${esc(files.cron.install)}</code></p>` : ""}
    ${files.systemd ? `<p>systemd: <code>${esc(files.systemd.service)}</code>, <code>${esc(files.systemd.timer)}</code></p><p class="muted">Install: <code>${esc(files.systemd.install)}</code></p>` : ""}
    ${table(["Source", "Next due"], d.sources || [], (x) => `<tr><td>${fmt(x.source_id)}</td><td>${when(x.next_due)}</td></tr>`, "No enabled sources yet: nothing will run until one is approved.")}</div>`;
}

const mb = (b) => (b ? (b / 1048576).toFixed(b < 1048576 ? 2 : 1) + " MB" : "0 MB");
function proxyPanel(px) {
  if (!px) return "";
  const d = px.decision || {}, t = px.today || {}, by = px.usage_7d_by_source || {};
  const pct = t.daily_bytes ? Math.min(100, Math.round((100 * (t.used_bytes || 0)) / t.daily_bytes)) : 0;
  return `<div class="panel" style="margin-top:16px" id="proxy-panel"><h2 style="margin-top:0">Residential proxy</h2>
   <p class="muted">Used only after an IP-level block (403/401 without a challenge page, a connection reset, a persistent 429), for sources whose robots and terms allow collection. Never for captchas, bot challenges, logins or paywalls.</p>
   <div class="row"><span>${px.pool_configured ? "pool configured" : "no pool configured (HARVEST_PROXY_FILE / HARVEST_PROXY_URL)"}</span>
     <span>project: ${chip(d.enabled ? "enabled" : "off")}</span>${d.reason ? `<span class="muted decision">${esc(d.reason)} · ${when(d.at)} · ${esc(d.by || "")}</span>` : ""}
     <span style="flex:1"></span>
     ${d.enabled ? `<button class="small ghost" data-proxy="off">Switch off</button>` : `<button class="small" data-proxy="on">Switch on…</button>`}</div>
   <div class="row" style="margin:8px 0"><span style="width:130px">today (bytes)</span><div class="bar" style="flex:1"><i style="width:${pct}%"></i></div>
     <span class="muted">${mb(t.used_bytes)} / ${mb(t.daily_bytes)} · ${fmt(t.used_requests)} / ${fmt(t.daily_requests)} requests</span></div>
   ${table(["Source", "Requests (7 d)", "Bytes (7 d)"], Object.entries(by), ([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v.requests)}</td><td class="num">${mb(v.bytes)}</td></tr>`, "No proxied traffic in the last 7 days.")}</div>`;
}

function uaPanel(ua) {
  if (!ua) return "";
  const d = ua.decision || {};
  return `<div class="panel" style="margin-top:16px" id="ua-panel"><h2 style="margin-top:0">Browser user agent</h2>
   <p class="muted">Sends a desktop Chrome user agent instead of harvest's own, for sites that refuse non-browser agents. Only the header changes: challenges, captchas, logins and paywalls still stop a source; robots.txt is checked for harvest and for *, the stricter wins.</p>
   <div class="row"><span>project: ${chip(d.enabled ? "enabled" : "off")}</span>${d.reason ? `<span class="muted decision">${esc(d.reason)} · ${when(d.at)} · ${esc(d.by || "")}</span>` : ""}
     <span style="flex:1"></span>${d.enabled ? `<button class="small ghost" data-ua="off">Switch off</button>` : `<button class="small" data-ua="on">Switch on…</button>`}</div></div>`;
}

function runsTable(rows) {
  return table(["Started", "Source", "Status", "UA", "Pages", "Stored", "Quarantined", "Complete", "Write", "Error"], rows, (r) =>
    `<tr><td>${when(r.started_at)}</td><td>${fmt(r.source_id)}</td><td>${chip(r.status)}</td><td>${fmt(r.ua_mode)}</td><td class="num">${fmt(r.pages)}</td><td class="num">${fmt(r.rows_stored)}</td>
     <td class="num">${fmt(r.rows_quarantined)}</td><td>${r.finished_at || r.status !== "running" ? (r.complete ? "yes" : "no") : "…"}</td><td>${fmt(r.write_mode)}</td><td>${cell(r.error || r.stopped)}</td></tr>`, "No runs yet.");
}

async function censusTab(name) {
  const [plan, g] = await Promise.all([api(`/projects/${enc(name)}/census/plan`), api(`/projects/${enc(name)}/census/gaps`)]);
  const angles = Object.keys(plan.angles);
  const matrix = g.matrix || {};
  return `<p class="muted">The census plan: for every region, its languages, currency and ccTLD, and the search angles the census agent works through. Cells with 0 sources are still to search.</p>
  <h2>Plan</h2>
  ${table(["Region", "Name", "Currency", "Languages", "ccTLD", "Sources", "Angles to do"], plan.regions, (r) =>
    `<tr><td><b>${esc(r.region)}</b></td><td>${esc(r.name)}</td><td>${esc(r.currency)}</td><td>${esc((r.languages || []).join(", "))}</td><td>${esc(r.cctld)}</td><td class="num">${fmt(r.sources_so_far)}</td><td>${esc((r.todo_angles || []).join(", ") || "none")}</td></tr>`, "No regions.")}
  <h2>Coverage</h2>
  <p class="muted">${fmt(g.sources)} sources · dry streak ${fmt(g.dry_streak)} · ${g.saturated ? "saturated" : "not saturated"}${g.budget_reached ? " · budget reached" : ""} · ${fmt((g.empty_cells || []).length)} empty cells</p>
  ${table(["Region", ...angles], Object.entries(matrix), ([r, row]) => `<tr><td><b>${esc(r)}</b></td>${angles.map((a) => `<td class="num ${row[a] ? "" : "zero"}">${fmt(row[a] || 0)}</td>`).join("")}</tr>`)}
  ${(g.deferred || []).length ? `<h2>Deferred by the budget</h2>${table(["Domain", "URL"], g.deferred, (d) => `<tr><td>${esc(d.domain)}</td><td>${link(d.url, esc(d.url))}</td></tr>`)}` : ""}
  <h2>Angles</h2>${table(["Angle", "What to search"], Object.entries(plan.angles), ([a, d]) => `<tr><td>${esc(a)}</td><td>${esc(d)}</td></tr>`)}
  <h2>Queries</h2>${plan.regions.map((r) => `<details><summary>${esc(r.region)} · ${esc(r.name)}</summary>${table(["Angle", "Queries"], Object.entries(r.queries || {}), ([a, q]) => `<tr><td>${esc(a)}</td><td>${q.map(esc).join("<br>")}</td></tr>`)}</details>`).join("")}`;
}

// a reason: short enough -> plain text; longer -> a short label that opens (keyboard too) to the full text
function why(text, label, cls = "") {
  if (!text) return "";
  const s = String(text);
  if (!label && s.length <= 34) return `<div class="muted why ${cls}">${esc(s)}</div>`;
  const head = s.split(/[;(:]/)[0].trim();
  const short = label || (head.length <= 30 ? head : head.slice(0, 30).replace(/\s+\S*$/, "")) + "…";
  return `<details class="why ${cls}"><summary>${esc(short)}</summary><div>${esc(s)}</div></details>`;
}

async function sources(name) {
  const { sources } = await api(`/projects/${enc(name)}/sources`);
  const html = `<p class="muted">Approve a source once its review passes. A source is never collected while its lane is none (robots, terms, login, captcha or paywall).</p>` +
    table(["Source", "Regions", "Status", "Lane", "Robots · terms · review", "Records", "Proxy", "UA", "Actions"], sources, (x) => {
      const laneOk = x.lane && x.lane !== "none", rejected = x.status === "rejected", id = esc(x.id);
      const px = x.proxy || {}, ua = x.ua || {};
      return `<tr data-source="${id}">
      <td class="src">${link(x.url, esc(x.name || x.id))}<div class="muted">${id}</div></td>
      <td>${esc((x.regions || []).join(", "))}</td><td class="status">${chip(x.status)}</td>
      <td class="lane">${chip(x.lane)}${why(x.lane_reason)}</td>
      <td class="policy"><div class="chips">${chip(x.robots_status)}${chip(x.terms_status)}<span class="review">${chip(x.review_verdict)}</span></div></td>
      <td class="num">${fmt(x.records)}</td>
      <td class="proxy">${chip(px.switched_on ? "enabled" : "off")}${px.via_proxy ? why("lane via proxy") : px.ip_block ? why("IP-blocked") : ""}
        ${px.switched_on && px.eligible === false ? why(px.why || "gate closed", "not used", "gate") : ""}
        <div>${px.switched_on ? `<button class="small ghost" data-proxy="off" data-id="${id}">Off</button>` : `<button class="small ghost" data-proxy="on" data-id="${id}" ${rejected ? "disabled" : ""}>On…</button>`}</div></td>
      <td class="ua">${chip(ua.switched_on ? "browser" : "own")}
        ${ua.switched_on && ua.eligible === false ? why(ua.why || "gate closed", "not used", "gate") : ""}
        <div>${ua.switched_on ? `<button class="small ghost" data-ua="off" data-id="${id}">Own UA</button>` : `<button class="small ghost" data-ua="on" data-id="${id}" ${rejected ? "disabled" : ""}>Browser UA…</button>`}</div></td>
      <td class="act"><div class="actions">
        <button class="small ghost" data-src="detect" data-id="${id}" ${rejected ? "disabled" : ""}>Detect</button>
        <button class="small ghost" data-src="scaffold" data-id="${id}" ${laneOk && !x.module_exists && !rejected ? "" : "disabled"}>Scaffold</button>
        <button class="small ghost" data-src="build" data-id="${id}" ${laneOk && !rejected ? "" : "disabled"}>Build (agent)</button>
        <button class="small ghost" data-src="review" data-id="${id}" ${x.module_exists && !rejected ? "" : "disabled"}>Review</button>
        ${x.enabled ? `<button class="small ghost" data-src="disable" data-id="${id}">Disable</button>` : `<button class="small" data-src="approve" data-id="${id}" ${x.review_verdict === "pass" && laneOk && !rejected ? "" : "disabled"}>Approve</button>`}
        <button class="small ghost" data-src="reject" data-id="${id}" ${rejected ? "disabled" : ""}>Reject</button></div></td></tr>`;
    }, "No sources yet. Run the census agent, or add candidates through the API.");
  // the Actions column stays pinned to the right edge while the rest of the table scrolls under it
  return html.replace('<div class="tablewrap"', '<div class="tablewrap sources"').replace('<th scope="col">Actions</th>', '<th scope="col" class="act">Actions</th>');
}

// record columns: the title and money first, then whatever this page of rows actually fills (an all-empty column is
// noise), at most 14; the long description and internals stay in the export
const HIDDEN = ["extra", "fingerprint", "description", "project", "uid", "data"];
const FIRST = ["title", "price", "currency", "price_report"];
const LAST = ["source_id", "url"];
function recordColumns(rows) {
  if (!rows.length) return [];
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r)))].filter((k) => !HIDDEN.includes(k) && rows.some((r) => r[k] !== null && r[k] !== undefined && r[k] !== ""));
  const mid = keys.filter((k) => !FIRST.includes(k) && !LAST.includes(k));
  const pick = [...FIRST.filter((k) => keys.includes(k)), ...mid];
  return [...pick.slice(0, 14 - LAST.filter((k) => keys.includes(k)).length), ...LAST.filter((k) => keys.includes(k))];
}
// years and ids are not quantities: no thousands separators ("2015", not "2,015")
const field = (c, v) => (typeof v === "number" && /(^|_)(year|id|sku|gtin|zip|postcode)$/i.test(c) ? esc(String(v))
  : /(_at|_seen)$/.test(c) ? when(v) : cell(v));

let dataState = { project: "", filters: "", offset: 0, distinct: false };
async function data(name) {
  if (dataState.project !== name) dataState = { project: name, filters: "", offset: 0, distinct: false };
  const q = new URLSearchParams({ limit: 50, offset: dataState.offset, distinct: dataState.distinct });
  if (dataState.filters) q.set("filters", dataState.filters);
  let res;
  try { res = await api(`/projects/${enc(name)}/records?${q}`); } catch (err) { res = { rows: [], total: 0, error: err.message }; }
  const cols = recordColumns(res.rows);
  const empty = res.error ? "No records: the query failed." : dataState.filters ? "No records match these filters." : "No records yet. Approve a source and run a collection.";
  return `<div class="panel"><form id="qf" class="row">
      <label for="qfilters" class="sr-only">Filters (JSON)</label>
      <input id="qfilters" name="filters" placeholder='filters JSON, e.g. {"price_report": {"lte": 20000}, "city": {"contains": "tbilisi"}}' value="${esc(dataState.filters)}" style="flex:1;min-width:0">
      <label class="row" style="color:var(--ink)"><input type="checkbox" name="distinct" ${dataState.distinct ? "checked" : ""} style="width:auto"> one per duplicate group</label>
      <button>Query</button></form>
      <div class="row" style="margin-top:10px"><span class="muted" id="qcount">${fmt(res.total)} records</span>${res.error ? `<span class="error-text" role="alert">${esc(res.error)}</span>` : ""}
      <span style="flex:1"></span><button class="ghost small" data-export="csv" ${res.total ? "" : "disabled"}>Export CSV</button><button class="ghost small" data-export="jsonl" ${res.total ? "" : "disabled"}>JSONL</button><button class="ghost small" data-export="parquet" ${res.total ? "" : "disabled"}>Parquet</button></div></div>` +
    table(cols, res.rows, (r) => `<tr>${cols.map((c) => `<td${typeof r[c] === "number" ? ' class="num"' : ' dir="auto"'}>${c === "url" && r[c] ? link(r[c], "link") : field(c, r[c])}</td>`).join("")}</tr>`, empty) +
    `<div class="row" style="margin-top:10px"><button class="ghost small" data-page="-1" ${dataState.offset ? "" : "disabled"}>Previous</button><span class="muted">${res.total ? `${dataState.offset + 1}–${Math.min(dataState.offset + 50, res.total)} of ${fmt(res.total)}` : ""}</span><button class="ghost small" data-page="1" ${dataState.offset + 50 < res.total ? "" : "disabled"}>Next</button></div>`;
}

async function runs(name) { const r = await api(`/projects/${enc(name)}/runs?limit=100`); return runsTable(r.runs); }
async function quarantine(name) {
  const r = await api(`/projects/${enc(name)}/quarantine?limit=200`);
  return `<p class="muted">Rows that failed cleaning (a required field missing, a value outside its sanity bounds, a category conflict). They are kept here, never stored as records.</p>` +
    table(["When", "Source", "Reasons", "Raw row"], r.quarantine, (q) => `<tr><td>${when(q.created_at)}</td><td>${fmt(q.source_id)}</td><td>${fmt((q.reasons || []).join("; "))}</td><td><code class="raw">${esc(JSON.stringify(q.raw).slice(0, 240))}</code></td></tr>`, "Nothing quarantined.");
}
async function jobs(name) {
  const r = await api(`/projects/${enc(name)}/jobs`);
  return table(["Created", "Kind", "Status", "Params", "Result"], r.jobs, (j) => `<tr><td>${when(j.created_at)}</td><td>${fmt(j.kind)}</td><td>${chip(j.status)}</td>
    <td><code>${esc(JSON.stringify(j.params || {}).slice(0, 160))}</code></td><td>${j.result || j.error ? `<details><summary>view</summary><pre>${esc(JSON.stringify(j.result ?? j.error, null, 1) || "")}</pre></details>` : "—"}</td></tr>`, "No jobs yet.");
}

function bind(name, s) {
  const base = `/projects/${enc(name)}`;
  view.querySelectorAll("[data-retry]").forEach((a) => a.onclick = (e) => { e.preventDefault(); route(); });
  view.querySelectorAll("[data-act]").forEach((b) => b.onclick = async () => {
    const act = b.dataset.act;
    b.disabled = true;
    try {
      if (act === "census" || act === "audit") await post(`${base}/census/run`, { kind: act });
      else if (act === "detect") await post(`${base}/sources/detect`);
      else if (act === "run") await post(`${base}/run`, {});
      else if (act === "schedule") { results[name] = { kind: "schedule", at: new Date().toISOString().slice(0, 19), data: await post(`${base}/schedule`, {}) }; toast("schedule snippets written"); route(true); return; }
      else if (act === "watchdog") { const r = await post(`${base}/watchdog`); results[name] = { kind: "watchdog", at: new Date().toISOString().slice(0, 19), data: r }; toast(`watchdog: ${r.findings.length} finding(s)`); route(true); return; }
      else if (act === "resume") {
        const cur = s.census_state?.max_sources || 0;
        const ans = prompt(`Raise the census budget from ${cur} sources to:`, String(cur + 10));
        if (ans === null) return;
        const n = Number(ans.trim());
        if (!Number.isInteger(n) || n <= cur) { toast(`enter a whole number above ${cur}`); return; }
        const r = await post(`${base}/census/resume`, { max_sources: n, start_agent: true });
        toast(`budget ${r.max_sources}: ${r.readded.length} deferred re-added, census agent queued`); route(true); return;
      }
      toast(act + " started"); route(true);
    } catch (err) { toast(err.message); } finally { b.disabled = false; }
  });
  view.querySelectorAll("[data-src]").forEach((b) => b.onclick = async () => {
    const id = b.dataset.id, act = b.dataset.src;
    try {
      if (act === "reject") { const reason = prompt("Why reject " + id + "?"); if (!reason || !reason.trim()) return; await post(`${base}/sources/${enc(id)}/reject`, { reason: reason.trim() }); }
      else if (act === "build") await post(`${base}/sources/${enc(id)}/build`, {});
      else if (act === "scaffold") { const r = await post(`${base}/sources/${enc(id)}/scaffold`); toast(r.written ? `scaffold written: ${id} (a starting template; build or edit it, then review)` : `scaffold: ${r.message || "module already exists"}`); route(true); return; }
      else await post(`${base}/sources/${enc(id)}/${act}`);
      toast(`${act}: ${id}`); route(true);
    } catch (err) { toast(err.message); }
  });
  view.querySelectorAll("[data-proxy]").forEach((b) => b.onclick = async () => {
    const on = b.dataset.proxy === "on", sid = b.dataset.id || null;
    const what = sid ? "source " + sid : "the whole project";
    const reason = prompt(on ? `Operator decision: why switch the residential proxy on for ${what}? (at least a short sentence; recorded with today's date)` : `Why switch it off for ${what}? (optional)`);
    if (reason === null || (on && !reason.trim())) return;
    try { await post(`${base}/proxy`, { enabled: on, reason: reason.trim() || null, source_id: sid }); toast(`proxy ${on ? "on" : "off"}: ${what}`); route(true); } catch (err) { toast(err.message); }
  });
  view.querySelectorAll("[data-ua]").forEach((b) => b.onclick = async () => {
    const on = b.dataset.ua === "on", sid = b.dataset.id || null;
    const what = sid ? "source " + sid : "the whole project";
    const reason = prompt(on ? `Operator decision: why send a browser user agent for ${what}? (at least a short sentence; recorded with today's date)` : `Why switch back to harvest's own UA for ${what}? (optional)`);
    if (reason === null || (on && !reason.trim())) return;
    try { await post(`${base}/ua`, { enabled: on, reason: reason.trim() || null, source_id: sid }); toast(`browser UA ${on ? "on" : "off"}: ${what}`); route(true); } catch (err) { toast(err.message); }
  });
  const qf = $("#qf");
  if (qf) qf.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(qf), filters = f.get("filters").trim();
    if (filters) { try { const v = JSON.parse(filters); if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error("must be a JSON object"); } catch (err) { toast("filters: " + err.message); return; } }
    dataState = { project: name, filters, offset: 0, distinct: !!f.get("distinct") }; route();
  };
  view.querySelectorAll("[data-page]").forEach((b) => b.onclick = () => { dataState.offset = Math.max(0, dataState.offset + 50 * +b.dataset.page); route(); });
  view.querySelectorAll("[data-export]").forEach((b) => b.onclick = async () => {
    const fmtName = b.dataset.export;
    const q = new URLSearchParams({ format: fmtName, distinct: dataState.distinct }); if (dataState.filters) q.set("filters", dataState.filters);
    b.disabled = true;
    try {
      const r = await fetch(`/api${base}/export?${q}`, { headers: { Authorization: "Bearer " + token() } });
      if (!r.ok) { let body; try { body = await r.json(); } catch { body = null; } toast(errorText(body, r.status) || "export failed"); return; }
      const href = URL.createObjectURL(await r.blob()); const a = document.createElement("a"); a.href = href; a.download = `${name}.${fmtName}`;
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(href), 10000);
      toast(`exported ${fmtName}`);
    } catch (err) { toast("export failed: " + err.message); } finally { b.disabled = false; }
  });
}

async function route(quiet) {
  clearTimeout(timer);
  const my = ++seq;
  if (!token()) return loginView();
  $("#logout").hidden = false;
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch { return p; } });
  try {
    if (parts[0] === "p" && parts[1]) await projectView(parts[1], parts[2] || "overview", my, quiet);
    else await homeView(my, quiet);
    if (pollFailing && my === seq) toast("connection restored");
    pollFailing = false;
  } catch (err) {
    if (my !== seq || !token()) return;
    if (quiet) {
      // a background refresh failed (server restarting, network): say so once and keep the page; retry later
      if (!pollFailing) toast("connection problem: " + err.message + " (retrying)");
      pollFailing = true;
      timer = setTimeout(() => route(true), 8000);
      return;
    }
    $("#crumbs").innerHTML = parts[0] === "p" ? `<a href="#/">projects</a> / ${esc(parts[1] || "")}` : "";
    paint(errorState(parts[0] === "p" ? "Could not open this project" : "Could not load", err.message), false);
    bind(parts[1] || "", {});
    toast(err.message);
  }
}
$("#logout").onclick = () => { setToken(""); route(); };
window.addEventListener("hashchange", () => route());
window.addEventListener("storage", (e) => { if (e.key === "harvest_token") route(); }); // signing in or out in another tab
route();
