// HealthPilot UI controller.
//
// Design rules that fix the old glitches:
//  * ONE source of truth (`state`); views are pure functions of it.
//  * DOM updates go through keyed reconciliation: untouched nodes (and the
//    inputs/focus/scroll inside them) are never rebuilt.
//  * Every network call is cancellable; responses for a report you've already
//    left are discarded (token check) instead of overwriting the current view.
//  * Switching reports is never blocked: an open edit form is just state, and
//    leaving it asks (non-blocking dialog) only if it has unsaved changes.

import { request, jsonBody, isAbort, apiBase, getApiKey, setApiKey } from "./api.js";
import { esc, icon, html, reconcile, fmtDateTime, fmtBytes, toast, openModal, closeTopModal, hasModal, confirmDialog, debounce } from "./ui.js";
import { DocumentViewer, kindOf } from "./viewer.js";
import { LivePanel } from "./live.js";

const $ = (id) => document.getElementById(id);
const els = {
  body: document.body, stats: $("stats"), netBar: $("netBar"),
  repList: $("repList"), repCount: $("repCount"), repSearch: $("repSearch"), repFilter: $("repFilter"),
  welcome: $("welcome"), wsContent: $("wsContent"), wsIco: $("wsIco"), wsTitle: $("wsTitle"), wsSub: $("wsSub"), wsTime: $("wsTime"),
  wsStatus: $("wsStatus"), wsActions: $("wsActions"), banners: $("banners"), paneSwitch: $("paneSwitch"),
  split: $("split"), splitter: $("splitter"), viewerBar: $("viewerBar"), viewerBody: $("viewerBody"),
  tabs: $("tabs"), cntResults: $("cntResults"), resBody: $("resBody"), resView: $("resView"), liveHost: $("liveHost"),
  soFar: $("soFar"), resTools: $("resTools"), addHost: $("addHost"), cards: $("cards"), resEmpty: $("resEmpty"),
  fhirWrap: $("fhirWrap"), fhirView: $("fhirView"), copyFhir: $("copyFhir"),
  navToggle: $("navToggle"), navBackdrop: $("navBackdrop"), uploadBtn: $("uploadBtn"), lookupBtn: $("lookupBtn"),
};

const ACTIVE = new Set(["pending", "processing"]);
const isActive = (s) => ACTIVE.has(s);
const OBS_FIELDS = [
  { key: "original_test_name", label: "Test name", wide: true },
  { key: "value", label: "Value" }, { key: "unit", label: "Unit" },
  { key: "reference_range", label: "Reference range" }, { key: "flag", label: "Flag" },
  { key: "specimen", label: "Specimen" }, { key: "method", label: "Method" }, { key: "timing", label: "Timing" },
];
const ACCEPT_EXT = [".pdf", ".docx", ".png", ".jpg", ".jpeg", ".txt"];
const MAX_UPLOAD = 15 * 1024 * 1024;

const state = {
  reports: [], reportsLoaded: false, listBusy: false, net: null,
  filter: "all", search: "",
  selectedId: null, detail: null, detailBusy: false, detailAbort: null, detailToken: 0, detailError: null,
  cache: new Map(),
  tab: "results", rf: "all", rq: "",
  edit: null,            // { id, values, original }
  add: null,             // { values }
  reviewId: null,
  selObs: null,
  seen: new Set(), seenReady: false,
  fhir: { id: null, sig: null, text: "" },
  pollTimer: null,
};

/* ================= preferences ================= */
const prefs = {
  get(k, d) { try { const v = localStorage.getItem("hp." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("hp." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

/* ================= viewer & live panels ================= */
const viewer = new DocumentViewer({ bar: els.viewerBar, body: els.viewerBody });
const livePanel = new LivePanel(els.liveHost);

/* ================= helpers ================= */
function kindIcon(doc) {
  const k = kindOf(doc.content_type, doc.filename);
  return { pdf: ["pdf", "file"], image: ["img", "image"], docx: ["doc", "doc"], text: ["txt", "file"], other: ["txt", "file"] }[k];
}
function statusChip(doc) {
  if (doc.cancel_requested && doc.status === "processing") return `<span class="chip live">Cancelling</span>`;
  switch (doc.status) {
    case "pending": return `<span class="chip live">Queued</span>`;
    case "processing": return `<span class="chip live">Processing</span>`;
    case "complete": return `<span class="chip ok">Complete</span>`;
    case "failed": return `<span class="chip bad">Failed</span>`;
    case "cancelled": return `<span class="chip muted">Cancelled</span>`;
    default: return `<span class="chip muted">${esc(doc.status)}</span>`;
  }
}
function fmtDuration(s) {
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60), r = Math.round(s - m * 60);
  return r === 60 ? `${m + 1} min` : `${m} min ${String(r).padStart(2, "0")} s`;
}
const num = (v) => (v === null || v === undefined || v === "" ? null : v);
const norm = (v) => (v === null || v === undefined ? "" : String(v).trim());

function setNet(message) {
  state.net = message;
  els.netBar.hidden = !message;
  if (message) {
    els.netBar.innerHTML = `${icon("alert")}<span>Can't reach the server (<span class="mono">${esc(apiBase())}</span>). Showing the last data - retrying automatically. <button class="btn btn-sm btn-outline" data-act="retry-net">Retry now</button></span>`;
  }
}

/* ================= stats / header chrome ================= */
function renderChrome() {
  els.navToggle.innerHTML = icon("panel");
  els.lookupBtn.innerHTML = `${icon("search")}<span class="lbl-sm">LOINC lookup</span><kbd class="desktop-only">Ctrl K</kbd>`;
  els.uploadBtn.innerHTML = `${icon("upload")}<span class="lbl-sm">Upload</span>`;
  $("copyFhir").innerHTML = `${icon("copy")} Copy JSON`;
}
function renderStats() {
  const total = state.reports.length;
  const live = state.reports.filter((r) => isActive(r.status)).length;
  const bad = state.reports.filter((r) => r.status === "failed").length;
  const ok = state.reports.filter((r) => r.status === "complete").length;
  const sig = `${total}|${live}|${bad}|${ok}`;
  if (els.stats.dataset.sig === sig) return;
  els.stats.dataset.sig = sig;
  els.stats.innerHTML = `
    <span class="stat"><b>${total}</b> report${total === 1 ? "" : "s"}</span>
    <span class="stat ok hide-md"><span class="dot"></span><b>${ok}</b> done</span>
    <span class="stat live ${live ? "" : "hide-md"}"><span class="dot"></span><b>${live}</b> active</span>
    ${bad ? `<span class="stat bad"><span class="dot"></span><b>${bad}</b> failed</span>` : ""}`;
}

/* ================= sidebar ================= */
function visibleReports() {
  const q = state.search.trim().toLowerCase();
  return state.reports.filter((r) => {
    if (q && !r.filename.toLowerCase().includes(q)) return false;
    if (state.filter === "active") return isActive(r.status);
    if (state.filter === "done") return r.status === "complete";
    if (state.filter === "issues") return r.status === "failed" || r.status === "cancelled";
    return true;
  });
}
function repHtml(r) {
  const [cls, ic] = kindIcon(r);
  const active = isActive(r.status);
  const pct = r.num_pages ? Math.min(99, Math.round(((r.pages_done || 0) + 0.3) / r.num_pages * 100)) : null;
  const pages = r.num_pages ? `${r.num_pages} page${r.num_pages === 1 ? "" : "s"}` : "";
  const sub = active
    ? esc(r.current_step || "Waiting to start…")
    : [fmtDateTime(r.uploaded_at), pages].filter(Boolean).join(" · ");
  return `<button class="rep ${active ? "is-active" : ""}" data-id="${esc(r.id)}" aria-current="false" title="${esc(r.filename)}">
    <span class="ico ${cls}">${icon(ic)}</span>
    <span class="main"><span class="name">${esc(r.filename)}</span><span class="meta">${sub}</span></span>
    <span class="side">${statusChip(r)}${r.used_fallback ? `<span class="chip yellow nodot" title="Extracted without AI; accuracy may be lower">no AI</span>` : ""}</span>
    ${active ? `<span class="bar ${pct == null ? "indet" : ""}"><i style="${pct == null ? "" : `width:${pct}%`}"></i></span>` : ""}
  </button>`;
}
function renderSidebar() {
  els.repCount.textContent = state.reportsLoaded ? `${state.reports.length}` : "";
  const list = els.repList;
  if (!state.reportsLoaded) {
    if (!list.querySelector(".skel")) {
      list.innerHTML = Array.from({ length: 5 }, () => `<div class="rep" style="pointer-events:none"><span class="skel" style="width:36px;height:36px;border-radius:10px"></span><span class="main"><span class="skel" style="display:block;height:12px;width:70%"></span><span class="skel" style="display:block;height:9px;width:45%;margin-top:7px"></span></span></div>`).join("");
    }
    return;
  }
  list.querySelectorAll(".skel").forEach((n) => n.closest(".rep")?.remove());
  const items = visibleReports();
  reconcile(list, items, {
    key: (r) => r.id,
    sig: (r) => [r.status, r.pages_done, r.num_pages, r.current_step, r.used_fallback, r.filename, r.cancel_requested, r.uploaded_at].join("|"),
    render: (r) => html(repHtml(r)),
  });
  list.querySelector(".list-empty")?.remove();
  if (!items.length) {
    const msg = state.reports.length ? "No reports match your filter." : "No reports yet.";
    list.appendChild(html(`<div class="list-empty" style="padding:26px 12px;text-align:center;color:var(--muted);font-size:13px">${msg}</div>`));
  }
  markSelectedRep();
}
function markSelectedRep() {
  els.repList.querySelectorAll(".rep[data-id]").forEach((n) => n.setAttribute("aria-current", n.dataset.id === state.selectedId ? "true" : "false"));
}

/* ================= data loading ================= */
async function loadReports() {
  if (state.listBusy) return;
  state.listBusy = true;
  try {
    const list = await request("/reports");
    state.reports = list;
    state.reportsLoaded = true;
    setNet(null);
    renderStats(); renderSidebar();
    if (!state.selectedId) renderWelcome();
  } catch (err) {
    if (isAbort(err)) return;
    setNet(err.message);
    if (!state.reportsLoaded) { state.reportsLoaded = true; renderSidebar(); renderWelcome(); }
  } finally { state.listBusy = false; }
}

async function loadDetail(id, { poll = false } = {}) {
  if (poll && state.detailBusy) return;
  const ac = new AbortController();
  if (!poll) { state.detailAbort?.abort(); state.detailAbort = ac; }
  const token = poll ? state.detailToken : ++state.detailToken;
  state.detailBusy = true;
  try {
    const d = await request(`/reports/${encodeURIComponent(id)}`, { signal: ac.signal });
    if (token !== state.detailToken || id !== state.selectedId) return;
    applyDetail(d);
  } catch (err) {
    if (isAbort(err) || token !== state.detailToken) return;
    state.detailError = err;
    renderBanners();
    renderResults();
  } finally {
    if (token === state.detailToken) state.detailBusy = false;
  }
}

function applyDetail(d) {
  const prev = state.detail && state.detail.id === d.id ? state.detail : state.cache.get(d.id);
  state.detail = d;
  state.detailError = null;
  state.cache.set(d.id, d);
  while (state.cache.size > 12) state.cache.delete(state.cache.keys().next().value);

  // keep the sidebar row in sync immediately, without waiting for the list poll
  const row = state.reports.find((r) => r.id === d.id);
  if (row) Object.assign(row, { status: d.status, pages_done: d.pages_done, num_pages: d.num_pages, current_step: d.current_step, used_fallback: d.used_fallback, cancel_requested: d.cancel_requested, error_message: d.error_message });

  if (prev && isActive(prev.status) && !isActive(d.status)) {
    if (d.status === "complete") toast(`Finished: ${d.observations.length} result${d.observations.length === 1 ? "" : "s"} extracted from ${d.filename}`);
    else if (d.status === "failed") toast(`Processing failed for ${d.filename}`, { type: "err" });
    loadReports();
  }
  viewer.open({ id: d.id, content_type: d.content_type, filename: d.filename });
  renderStats(); renderSidebar();
  renderWorkspace();
  if (!state.seenReady) { d.observations.forEach((o) => state.seen.add(o.id)); state.seenReady = true; renderResults(); }
}

/* ================= selection / routing ================= */
function editDirty() {
  if (state.edit) {
    const { values, original } = state.edit;
    if (OBS_FIELDS.some((f) => norm(values[f.key]) !== norm(original[f.key]))) return true;
  }
  if (state.add && Object.values(state.add.values).some((v) => norm(v))) return true;
  return false;
}
async function confirmDiscard() {
  if (!editDirty()) return true;
  return confirmDialog({ title: "Discard unsaved changes?", body: "You have edits that haven't been saved. If you leave now they will be lost.", confirmText: "Discard", cancelText: "Keep editing", danger: true });
}
const hashFor = (id) => (id ? `#/r/${id}` : "#/");
function idFromHash() { const m = location.hash.match(/^#\/r\/([\w-]+)/); return m ? m[1] : null; }

async function selectReport(id, { fromHash = false } = {}) {
  if (id === state.selectedId) return;
  if (!(await confirmDiscard())) {
    history.replaceState(null, "", hashFor(state.selectedId));
    return;
  }
  state.edit = null; state.add = null; state.reviewId = null; state.selObs = null;
  state.selectedId = id;
  state.seen = new Set(); state.seenReady = false;
  state.fhir = { id: null, sig: null, text: "" };
  state.rf = "all"; state.rq = ""; state.tab = "results";
  state.detail = id ? state.cache.get(id) || null : null;
  state.detailError = null;
  state.detailAbort?.abort();
  state.detailBusy = false;
  state.detailToken++;
  if (!fromHash && idFromHash() !== id) history.pushState(null, "", hashFor(id));
  markSelectedRep();
  closeDrawer();
  if (state.detail) state.detail.observations.forEach((o) => state.seen.add(o.id)), (state.seenReady = true);
  els.cards.replaceChildren();
  els.cards.dataset.doc = id || "";
  renderWorkspace();
  if (id) {
    const meta = state.detail || state.reports.find((r) => r.id === id);
    if (meta) viewer.open({ id, content_type: meta.content_type, filename: meta.filename });
    loadDetail(id);
  }
  schedulePoll();
}

/* ================= polling ================= */
function schedulePoll() {
  clearTimeout(state.pollTimer);
  const selActive = state.detail && isActive(state.detail.status);
  const anyActive = state.reports.some((r) => isActive(r.status));
  const delay = selActive ? 1100 : anyActive ? 3000 : 15000;
  state.pollTimer = setTimeout(tick, delay);
}
async function tick() {
  if (!document.hidden) {
    const jobs = [loadReports()];
    if (state.selectedId && (state.detailError || !state.detail || isActive(state.detail.status))) jobs.push(loadDetail(state.selectedId, { poll: true }));
    await Promise.allSettled(jobs);
  }
  schedulePoll();
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(state.pollTimer); tick(); } });

/* ================= workspace ================= */
function renderWelcome() {
  if (state.selectedId) return;
  els.welcome.hidden = false;
  els.wsContent.hidden = true;
  document.title = "HealthPilot — Lab Extraction & LOINC Coding";
  const none = state.reportsLoaded && state.reports.length === 0;
  const sig = none ? "none" : state.reportsLoaded ? "pick" : "load";
  if (els.welcome.dataset.sig === sig) return;
  els.welcome.dataset.sig = sig;
  els.welcome.innerHTML = state.reportsLoaded ? `<div class="empty welcome" style="height:100%">
      <div><div class="art" style="margin:0 auto 18px">${icon(none ? "upload" : "file")}</div>
      <h3 style="font-size:19px">${none ? "Upload your first lab report" : "Select a report"}</h3>
      <p style="margin:0 auto 16px">${none
        ? "Drop a PDF, Word document, scan or text file. You'll see the original and the extracted results side by side."
        : "Choose a report from the list to view the original document next to its extracted results."}</p>
      <button class="btn btn-primary" data-act="upload">${icon("upload")} Upload reports</button>
      <p style="margin:16px auto 0;font-size:12px;color:var(--faint)">Supports PDF, DOCX, PNG, JPG and TXT &middot; up to 15 MB each</p></div></div>`
    : `<div class="empty" style="height:100%"><div class="spin"></div></div>`;
}

function renderWorkspace() {
  const id = state.selectedId;
  els.welcome.hidden = !!id;
  els.wsContent.hidden = !id;
  if (!id) { renderWelcome(); return; }
  const meta = state.detail || state.reports.find((r) => r.id === id);
  if (!meta) {
    els.wsTitle.textContent = "Loading…"; els.wsSub.textContent = ""; els.wsTime.hidden = true; els.wsStatus.innerHTML = ""; els.wsActions.innerHTML = "";
    renderBanners(); renderResults();
    return;
  }
  document.title = `${meta.filename} — HealthPilot`;
  const [cls, ic] = kindIcon(meta);
  const hSig = [meta.filename, meta.status, meta.num_pages, meta.uploaded_at, meta.cancel_requested, meta.processing_seconds, state.detail && state.detail.id === meta.id ? "d" : "l"].join("|");
  if (els.wsContent.dataset.hsig !== hSig) {
    els.wsContent.dataset.hsig = hSig;
    els.wsIco.innerHTML = `<span class="ico ${cls}">${icon(ic)}</span>`;
    els.wsTitle.textContent = meta.filename;
    els.wsTitle.title = meta.filename;
    const took = meta.processing_seconds;
    const timed = took != null && (meta.status === "complete" || meta.status === "failed");
    els.wsTime.hidden = !timed;
    els.wsTime.classList.toggle("bad", meta.status === "failed");
    const last = state.detail && state.detail.id === meta.id ? (state.detail.progress || []).slice(-1)[0] : null;
    els.wsTime.title = timed && last && /^Finished in/.test(last.msg) ? last.msg : "";
    els.wsTime.innerHTML = timed ? `${icon("clock")}${meta.status === "failed" ? "Stopped after" : "Processed in"} ${fmtDuration(took)}` : "";
    els.wsSub.textContent = [meta.num_pages ? `${meta.num_pages} page${meta.num_pages === 1 ? "" : "s"}` : null, `uploaded ${fmtDateTime(meta.uploaded_at)}`].filter(Boolean).join(" · ");
    els.wsStatus.innerHTML = statusChip(meta);
    const cancelling = meta.cancel_requested && meta.status === "processing";
    els.wsActions.innerHTML = `
      ${isActive(meta.status) ? `<button class="btn btn-warn btn-sm" data-act="cancel-doc" ${cancelling ? "disabled" : ""}>${icon("stop")}${cancelling ? "Cancelling…" : "Cancel"}</button>` : ""}
      <button class="btn btn-outline btn-sm" data-act="download">${icon("download")}<span class="lbl-sm">Original</span></button>`;
  }
  renderBanners();
  renderResults();
}

function renderBanners() {
  const d = state.detail;
  const parts = [];
  if (state.detailError && !d) parts.push(`<div class="banner red">${icon("alert")}<div class="txt"><b>Couldn't load this report.</b><span class="why">${esc(state.detailError.message)}</span> <button class="btn btn-sm btn-outline" data-act="retry-detail" style="margin-top:6px">${icon("refresh")} Retry</button></div></div>`);
  if (d) {
    if (d.used_fallback && d.status !== "failed") {
      parts.push(`<div class="banner yellow">${icon("alert")}<div class="txt"><b>Lower accuracy.</b> This report was read without AI, using a basic parser, so values may be wrong or missing. Check the highlighted rows, and use Edit, Delete or Add row to correct them.${d.fallback_reason ? `<span class="why">Why: ${esc(d.fallback_reason)}</span>` : ""}</div></div>`);
    }
    if (d.status === "failed") {
      parts.push(`<div class="banner red">${icon("alert")}<div class="txt"><b>Processing failed.</b><span class="why">${esc(d.error_message || "No further detail was recorded.")}</span></div></div>`);
    } else if (d.status === "complete" && d.error_message) {
      // Bracketed system notes ("[Recovered after an interrupted run; retrying.]") are not page problems.
      const msg = d.error_message.replace(/\s*\[[^\]]*\]/g, "").trim();
      if (msg) parts.push(`<div class="banner yellow">${icon("info")}<div class="txt"><b>Some pages couldn't be read.</b><span class="why">${esc(msg)}</span></div></div>`);
    }
  }
  const sig = parts.join("");
  if (els.banners.dataset.sig !== sig) { els.banners.dataset.sig = sig; els.banners.innerHTML = sig; }
}

/* ================= results pane ================= */
function filteredObs(d) {
  const q = state.rq.trim().toLowerCase();
  return d.observations.filter((o) => {
    if (q && ![o.original_test_name, o.normalized_test_name, o.loinc_code, o.value].some((v) => norm(v).toLowerCase().includes(q))) return false;
    if (state.rf === "review") return o.mapping_status !== "confirmed";
    if (state.rf === "flagged") return (o.validation_notes && o.validation_notes.length) || (o.extraction_source === "fallback" && !o.is_edited);
    if (state.rf === "edited") return o.is_edited;
    return true;
  });
}
function renderTabs() {
  els.tabs.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
  els.cntResults.textContent = state.detail ? state.detail.observations.length : "–";
}

function renderResults() {
  const d = state.detail;
  // While a document is processing the live console is the only view; the FHIR
  // tab returns once the output is ready.
  if (d && isActive(d.status)) state.tab = "results";
  renderTabs();
  els.tabs.querySelector('[data-tab="fhir"]').hidden = !!(d && isActive(d.status));
  const showRes = state.tab === "results", showFhir = state.tab === "fhir";
  els.resView.hidden = !showRes; els.fhirWrap.hidden = !showFhir;
  if (!d) {
    if (showRes) {
      els.liveHost.hidden = true; els.soFar.hidden = true; els.resTools.hidden = true; els.addHost.replaceChildren(); els.cards.replaceChildren();
      const skel = state.detailError ? "" : `<div class="cards">${Array.from({ length: 4 }, () => `<div class="skel" style="height:96px;border-radius:13px"></div>`).join("")}</div>`;
      els.resEmpty.dataset.sig = skel ? "skeleton" : "";   // own cache key so real content later replaces it
      els.resEmpty.innerHTML = skel;
    }
    return;
  }
  if (showFhir) { loadFhir(); return; }

  const active = isActive(d.status);
  els.liveHost.hidden = !active;
  if (active) livePanel.update(d);
  const n = d.observations.length;
  els.soFar.hidden = !(active && n);
  if (active && n) els.soFar.innerHTML = `Results so far <span class="chip nodot muted">${n}</span>`;
  renderTools(d, active);
  renderAddForm(d, active);
  const rows = filteredObs(d);
  renderCards(rows);
  // empty states
  let empty = "";
  if (!n && !active) {
    empty = d.status === "failed"
      ? `<div class="empty"><div class="art">${icon("alert")}</div><h3>No results were extracted</h3><p>The reason is shown above. You can add rows by hand with “Add row”.</p></div>`
      : `<div class="empty"><div class="art">${icon("list")}</div><h3>No results yet</h3><p>Nothing was extracted from this document. Use “Add row” to enter results manually.</p></div>`;
  } else if (n && !rows.length) {
    empty = `<div class="empty" style="padding:30px 16px"><div class="art" style="width:48px;height:48px">${icon("search")}</div><h3>No rows match</h3><p>Try a different search or filter.</p><button class="btn btn-outline btn-sm" data-act="clear-filters">Clear filters</button></div>`;
  }
  if (els.resEmpty.dataset.sig !== empty) { els.resEmpty.dataset.sig = empty; els.resEmpty.innerHTML = empty; }
}

function renderTools(d, active) {
  els.resTools.hidden = active || !d.observations.length;
  if (els.resTools.hidden) return;
  const obs = d.observations;
  const review = obs.filter((o) => o.mapping_status !== "confirmed").length;
  const flagged = obs.filter((o) => (o.validation_notes && o.validation_notes.length)).length;
  const edited = obs.filter((o) => o.is_edited).length;
  const sig = [obs.length, review, flagged, edited, state.rf].join("|");
  if (els.resTools.dataset.sig !== sig) {
    els.resTools.dataset.sig = sig;
    const keepVal = $("rq")?.value ?? state.rq;
    els.resTools.innerHTML = `
      <div class="sumline">
        <span class="chip nodot muted"><b>${obs.length}</b>&nbsp;row${obs.length === 1 ? "" : "s"}</span>
        ${review ? `<span class="chip warn">${review} need review</span>` : `<span class="chip ok">all codes confirmed</span>`}
        ${flagged ? `<span class="chip yellow">${flagged} flagged</span>` : ""}
        ${edited ? `<span class="chip nodot muted">${edited} edited</span>` : ""}
      </div>
      <div class="res-tools" style="border-bottom:0;background:transparent">
        <div class="search"><input class="input" id="rq" type="search" placeholder="Search results…" aria-label="Search results" autocomplete="off" value="${esc(keepVal)}"></div>
        <div class="seg" id="rfSeg" role="group" aria-label="Filter results">
          ${[["all", "All"], ["review", "Review"], ["flagged", "Flagged"], ["edited", "Edited"]].map(([k, l]) => `<button data-rf="${k}" aria-pressed="${state.rf === k}">${l}</button>`).join("")}
        </div>
        <button class="btn btn-outline btn-sm" data-act="add-row">${icon("plus")} Add row</button>
      </div>`;
    $("rq").insertAdjacentHTML("beforebegin", icon("search"));
  }
}

function fieldInputs(values) {
  return OBS_FIELDS.map((f) => `<label class="field ${f.wide ? "wide" : ""}">${f.label}<input class="input" data-field="${f.key}" type="text" value="${esc(values[f.key])}" autocomplete="off" ${f.key === "original_test_name" ? "required" : ""}></label>`).join("");
}
function renderAddForm(d, active) {
  const open = !!state.add && !active;
  if (!open) { if (els.addHost.firstChild) els.addHost.replaceChildren(); return; }
  if (els.addHost.firstChild) return;
  els.addHost.appendChild(html(`<div class="cards" style="padding-bottom:0"><div class="addcard"><h4>Add a row manually</h4>
    <div class="grid4">${fieldInputs(state.add.values)}</div>
    <div class="form-actions" style="margin-top:12px"><button class="btn btn-primary btn-sm" data-act="add-save">${icon("plus")} Add row</button><button class="btn btn-outline btn-sm" data-act="add-cancel">Cancel</button><span class="msg" data-msg></span></div></div></div>`));
  els.addHost.querySelector("input")?.focus();
}

function obsHtml(o, isNew) {
  const editing = state.edit?.id === o.id;
  const reviewing = state.reviewId === o.id;
  const fb = o.extraction_source === "fallback" && !o.is_edited;
  const multi = state.detail && state.detail.num_pages > 1 && o.page_number;
  const badges = [
    fb ? `<span class="chip yellow nodot" title="Read by the basic no-AI parser; please check">auto-parsed</span>` : "",
    o.extraction_source === "manual" ? `<span class="chip live nodot">added by you</span>` : "",
    o.is_edited && o.extraction_source !== "manual" ? `<span class="chip muted nodot">edited</span>` : "",
  ].join("");
  const sc = { confirmed: ["ok", "Confirmed"], needs_review: ["warn", "Needs review"], unmapped: ["bad", "Unmapped"] }[o.mapping_status] || ["muted", o.mapping_status];
  const notes = (o.validation_notes || []).length
    ? `<div class="notes">${o.validation_notes.map((n) => `<div class="row"><span>${esc(n)}</span></div>`).join("")}
        ${o.suggested_value ? `<button class="btn btn-outline btn-sm apply" data-act="apply" data-v="${esc(o.suggested_value)}">${icon("check")} Use ${esc(o.suggested_value)}</button>` : ""}</div>` : "";
  const edit = editing ? `<div class="form"><div class="grid4">${fieldInputs(state.edit.values)}</div>
      <div class="form-actions"><button class="btn btn-primary btn-sm" data-act="save-edit">${icon("check")} Save changes</button><button class="btn btn-outline btn-sm" data-act="cancel-edit">Cancel</button><span class="msg" data-msg></span></div></div>` : "";
  const review = reviewing ? `<div class="form"><div class="form-actions">
      <label class="field" style="flex:1 1 160px">LOINC code<input class="input" data-review-code placeholder="e.g. 718-7" autocomplete="off"></label>
      <button class="btn btn-primary btn-sm" data-act="review-confirm" style="align-self:flex-end">Confirm code</button>
      <button class="btn btn-outline btn-sm" data-act="review-unmapped" style="align-self:flex-end">No code applies</button>
      <button class="btn btn-ghost btn-sm" data-act="review-cancel" style="align-self:flex-end">Cancel</button><span class="msg" data-msg style="flex-basis:100%"></span></div></div>` : "";
  const reviewable = o.mapping_status !== "confirmed" || o.mapping_stage !== "human_review";
  return `<article class="obs s-${esc(o.mapping_status)} ${fb ? "fb" : ""} ${editing ? "editing" : ""} ${isNew ? "new" : ""}" data-obs="${esc(o.id)}" tabindex="-1">
    <div class="obs-top">
      <div class="obs-name">${esc(o.original_test_name)}${o.normalized_test_name && o.normalized_test_name !== o.original_test_name ? `<small>→ ${esc(o.normalized_test_name)}</small>` : ""}
        <div class="obs-badges">${badges}</div></div>
      <div class="obs-code"><span class="chip ${sc[0]}" title="${esc(o.mapping_rationale)}">${sc[1]}</span>
        <div class="code" style="margin-top:6px">${esc(o.loinc_code) || "—"}</div>
        ${o.mapping_confidence != null ? `<span class="conf">conf ${Number(o.mapping_confidence).toFixed(2)}</span>` : ""}</div>
    </div>
    <div class="obs-val"><span class="v">${esc(num(o.value) ?? "—")}</span>${o.unit ? `<span class="u">${esc(o.unit)}</span>` : ""}${o.flag ? `<span class="flag">${esc(o.flag)}</span>` : ""}</div>
    <div class="obs-meta">${o.reference_range ? `<span>Ref ${esc(o.reference_range)}</span>` : ""}${o.specimen ? `<span>Specimen ${esc(o.specimen)}</span>` : ""}${o.method ? `<span>${esc(o.method)}</span>` : ""}
      ${multi ? `<button class="pgjump" data-act="jump" title="Show page ${o.page_number} in the document">${icon("file")} p.${o.page_number}</button>` : ""}</div>
    ${notes}
    <div class="obs-actions">
      ${reviewable ? `<button class="btn btn-ghost btn-sm" data-act="review">${icon("book")} Review code</button>` : ""}
      <button class="btn btn-ghost btn-sm" data-act="edit">${icon("edit")} Edit</button>
      <button class="btn btn-ghost btn-sm btn-danger" data-act="delete">${icon("trash")} Delete</button>
    </div>${edit}${review}</article>`;
}
function obsSig(o) {
  return [o.original_test_name, o.normalized_test_name, o.value, o.unit, o.reference_range, o.specimen, o.method, o.flag, o.loinc_code, o.mapping_status,
    o.mapping_stage, o.mapping_confidence, o.is_edited, o.extraction_source, o.suggested_value, (o.validation_notes || []).join("|"),
    state.edit?.id === o.id, state.reviewId === o.id, state.detail?.num_pages > 1].join("␟");
}
function renderCards(rows) {
  if (els.cards.dataset.doc !== (state.selectedId || "")) { els.cards.replaceChildren(); els.cards.dataset.doc = state.selectedId || ""; }
  reconcile(els.cards, rows, {
    key: (o) => o.id,
    sig: obsSig,
    render: (o) => {
      const isNew = state.seenReady && !state.seen.has(o.id);
      state.seen.add(o.id);
      const el = html(obsHtml(o, isNew));
      if (state.selObs === o.id) el.classList.add("sel");
      return el;
    },
  });
  markSelectedObs();
}
function markSelectedObs() {
  els.cards.querySelectorAll(".obs").forEach((n) => n.classList.toggle("sel", n.dataset.obs === state.selObs));
}

/* ----- FHIR ----- */
async function loadFhir() {
  const d = state.detail;
  const sig = d.observations.map((o) => `${o.id}:${o.value}:${o.loinc_code}:${o.unit}`).join(",");
  els.fhirWrap.hidden = false;
  if (state.fhir.id === d.id && state.fhir.sig === sig) { els.fhirView.textContent = state.fhir.text; return; }
  if (state.fhir.id !== d.id) els.fhirView.textContent = "Loading…";
  try {
    const bundle = await request(`/reports/${encodeURIComponent(d.id)}/fhir`);
    if (state.selectedId !== d.id) return;
    state.fhir = { id: d.id, sig, text: JSON.stringify(bundle, null, 2) };
    if (state.tab === "fhir") els.fhirView.textContent = state.fhir.text;
  } catch (err) {
    if (!isAbort(err)) els.fhirView.textContent = `Failed to load the FHIR bundle: ${err.message}`;
  }
}

/* ================= card interactions ================= */
function openEdit(o) {
  const values = {}; OBS_FIELDS.forEach((f) => (values[f.key] = o[f.key] ?? ""));
  state.edit = { id: o.id, values, original: { ...values } };
  state.reviewId = null;
  renderCards(filteredObs(state.detail));
  els.cards.querySelector(`[data-obs="${CSS.escape(o.id)}"] input`)?.focus();
}
function closeEdit() { state.edit = null; renderCards(filteredObs(state.detail)); }
function msgEl(card) { return card?.querySelector("[data-msg]"); }
function setMsg(card, text, err) { const m = msgEl(card); if (m) { m.textContent = text; m.classList.toggle("err", !!err); } }

async function refreshNow() { await loadDetail(state.selectedId); }

async function onCardAction(act, card, btn) {
  const id = card?.dataset.obs;
  const o = state.detail?.observations.find((x) => x.id === id);
  if (act === "jump") { selectObs(id); return; }
  if (!o) return;
  if (act === "edit") {
    if (state.edit && state.edit.id !== id && editDirty() && !(await confirmDiscard())) return;
    return openEdit(o);
  }
  if (act === "cancel-edit") return closeEdit();
  if (act === "save-edit") {
    const { values, original } = state.edit;
    if (!norm(values.original_test_name)) return setMsg(card, "Test name is required.", true);
    const changes = {};
    OBS_FIELDS.forEach((f) => { if (norm(values[f.key]) !== norm(original[f.key])) changes[f.key] = norm(values[f.key]) || null; });
    if (!Object.keys(changes).length) return setMsg(card, "No changes to save.");
    btn.disabled = true; setMsg(card, "Saving…");
    try {
      await request(`/observations/${encodeURIComponent(id)}`, { method: "PATCH", ...jsonBody(changes) });
      state.edit = null;
      await refreshNow();
      toast("Changes saved");
    } catch (err) { btn.disabled = false; setMsg(card, `Couldn't save: ${err.message}`, true); }
    return;
  }
  if (act === "delete") {
    if (!(await confirmDialog({ title: "Delete this row?", body: `“${o.original_test_name}” will be removed from this report. This can't be undone.`, confirmText: "Delete", danger: true }))) return;
    try {
      await request(`/observations/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (state.edit?.id === id) state.edit = null;
      await refreshNow(); toast("Row deleted");
    } catch (err) { toast(`Delete failed: ${err.message}`, { type: "err" }); }
    return;
  }
  if (act === "apply") {
    try {
      await request(`/observations/${encodeURIComponent(id)}`, { method: "PATCH", ...jsonBody({ value: btn.dataset.v }) });
      await refreshNow(); toast(`Value updated to ${btn.dataset.v}`);
    } catch (err) { toast(`Couldn't apply: ${err.message}`, { type: "err" }); }
    return;
  }
  if (act === "review") { state.reviewId = state.reviewId === id ? null : id; if (state.reviewId) state.edit = null; renderCards(filteredObs(state.detail)); card.querySelector("[data-review-code]")?.focus(); return; }
  if (act === "review-cancel") { state.reviewId = null; renderCards(filteredObs(state.detail)); return; }
  if (act === "review-confirm" || act === "review-unmapped") {
    const unmapped = act === "review-unmapped";
    const code = card.querySelector("[data-review-code]").value.trim();
    if (!unmapped && !code) return setMsg(card, "Enter a LOINC code first.", true);
    setMsg(card, "Saving…");
    try {
      await request(`/observations/${encodeURIComponent(id)}/review`, { method: "PATCH", ...jsonBody(unmapped ? { loinc_code: null, mapping_status: "unmapped" } : { loinc_code: code }) });
      state.reviewId = null; await refreshNow(); toast("Mapping updated");
    } catch (err) { setMsg(card, `Couldn't save: ${err.message}`, true); }
  }
}
function selectObs(id) {
  state.selObs = id;
  markSelectedObs();
  const o = state.detail?.observations.find((x) => x.id === id);
  if (o?.page_number) viewer.goToPage(o.page_number);
  if (window.matchMedia("(max-width: 980px)").matches && o) { /* stay on results; page chip is an explicit jump */ }
}

els.cards.addEventListener("click", (e) => {
  const card = e.target.closest(".obs");
  if (!card) return;
  const btn = e.target.closest("[data-act]");
  if (btn) { e.stopPropagation(); onCardAction(btn.dataset.act, card, btn); return; }
  if (e.target.closest("input,label,textarea,.form,.notes")) return;
  selectObs(card.dataset.obs);
  if (window.matchMedia("(max-width: 980px)").matches === false && state.detail?.num_pages > 1) { /* jump already handled */ }
});
els.cards.addEventListener("input", (e) => {
  const inp = e.target.closest("[data-field]");
  if (!inp || !state.edit) return;
  state.edit.values[inp.dataset.field] = inp.value;
});
els.cards.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.edit) { e.stopPropagation(); closeEdit(); }
  if (e.key === "Enter" && e.target.matches("[data-field]") && state.edit) { e.preventDefault(); e.target.closest(".obs")?.querySelector('[data-act="save-edit"]')?.click(); }
  if (e.key === "Enter" && e.target.matches("[data-review-code]")) { e.preventDefault(); e.target.closest(".obs")?.querySelector('[data-act="review-confirm"]')?.click(); }
});

/* add form + toolbar delegation (they live outside #cards) */
els.addHost.addEventListener("input", (e) => { const i = e.target.closest("[data-field]"); if (i && state.add) state.add.values[i.dataset.field] = i.value; });
els.addHost.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.add) { state.add = null; renderResults(); }
  if (e.key === "Enter" && e.target.matches("[data-field]")) { e.preventDefault(); els.addHost.querySelector('[data-act="add-save"]')?.click(); }
});
async function addRow(btn) {
  const v = state.add.values;
  const msg = els.addHost.querySelector("[data-msg]");
  if (!norm(v.original_test_name)) { msg.textContent = "Test name is required."; msg.classList.add("err"); return; }
  const body = { document_id: state.selectedId };
  OBS_FIELDS.forEach((f) => { if (norm(v[f.key])) body[f.key] = norm(v[f.key]); });
  btn.disabled = true; msg.textContent = "Adding…"; msg.classList.remove("err");
  try {
    const created = await request("/observations", { method: "POST", ...jsonBody(body) });
    state.add = null;
    state.seen.delete(created.id);
    await refreshNow(); toast("Row added");
  } catch (err) { btn.disabled = false; msg.textContent = `Couldn't add: ${err.message}`; msg.classList.add("err"); }
}

/* ================= global click delegation ================= */
document.addEventListener("click", async (e) => {
  const rf = e.target.closest("[data-rf]");
  if (rf) { state.rf = rf.dataset.rf; els.resTools.dataset.sig = ""; renderResults(); return; }
  const tab = e.target.closest("[data-tab]");
  if (tab && tab.closest("#tabs")) { state.tab = tab.dataset.tab; renderResults(); return; }
  const rep = e.target.closest(".rep[data-id]");
  if (rep && els.repList.contains(rep)) { selectReport(rep.dataset.id); return; }
  const f = e.target.closest("#repFilter [data-f]");
  if (f) { state.filter = f.dataset.f; els.repFilter.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === f))); renderSidebar(); return; }
  const ps = e.target.closest("#paneSwitch [data-pane]");
  if (ps) { setPane(ps.dataset.pane); return; }
  const a = e.target.closest("[data-act]");
  if (!a || a.closest("#cards")) return;
  switch (a.dataset.act) {
    case "upload": openUpload(); break;
    case "add-row": state.add = state.add || { values: {} }; els.addHost.replaceChildren(); renderResults(); break;
    case "add-cancel": state.add = null; renderResults(); break;
    case "add-save": addRow(a); break;
    case "clear-filters": state.rf = "all"; state.rq = ""; els.resTools.dataset.sig = ""; renderResults(); break;
    case "retry-net": loadReports(); break;
    case "retry-detail": loadDetail(state.selectedId); break;
    case "download": downloadOriginal(); break;
    case "cancel-doc": cancelDoc(a); break;
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "rq") { state.rq = e.target.value; renderCards(filteredObs(state.detail)); renderResults(); }
});
els.repSearch.addEventListener("input", debounce(() => { state.search = els.repSearch.value; renderSidebar(); }, 120));
els.repList.addEventListener("keydown", (e) => {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  const items = Array.from(els.repList.querySelectorAll(".rep[data-id]"));
  const i = items.indexOf(document.activeElement);
  if (i < 0) return;
  e.preventDefault();
  items[Math.min(items.length - 1, Math.max(0, i + (e.key === "ArrowDown" ? 1 : -1)))].focus();
});

async function cancelDoc(btn) {
  btn.disabled = true;
  try { await request(`/reports/${encodeURIComponent(state.selectedId)}/cancel`, { method: "POST" }); toast("Cancel requested"); await loadDetail(state.selectedId); loadReports(); }
  catch (err) { btn.disabled = false; toast(`Couldn't cancel: ${err.message}`, { type: "err" }); }
}
async function downloadOriginal() {
  const d = state.detail; if (!d) return;
  try {
    const blob = viewer.blobs.get(d.id) || await request(`/reports/${encodeURIComponent(d.id)}/file`, { as: "blob", timeout: 120000 });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = d.filename; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  } catch (err) { toast(`Download failed: ${err.message}`, { type: "err" }); }
}
els.copyFhir.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(els.fhirView.textContent); toast("FHIR JSON copied"); } catch { toast("Couldn't access the clipboard", { type: "err" }); }
});

/* ================= layout: sidebar, panes, splitter ================= */
const narrow = () => window.matchMedia("(max-width: 980px)").matches;
function closeDrawer() { els.body.classList.remove("sidebar-open"); }
els.navToggle.addEventListener("click", () => {
  if (narrow()) els.body.classList.toggle("sidebar-open");
  else { const c = els.body.classList.toggle("sidebar-collapsed"); prefs.set("collapsed", c); }
});
els.navBackdrop.addEventListener("click", closeDrawer);
function setPane(p) {
  els.wsContent.dataset.pane = p;
  els.paneSwitch.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.pane === p)));
}
(function initSplit() {
  const clamp = (v) => Math.min(72, Math.max(28, v));
  const apply = (v) => document.documentElement.style.setProperty("--split", `${clamp(v)}%`);
  apply(prefs.get("split", 50));
  let drag = false;
  const move = (clientX) => {
    const r = els.split.getBoundingClientRect();
    apply(((clientX - r.left) / r.width) * 100);
  };
  els.splitter.addEventListener("pointerdown", (e) => { drag = true; els.splitter.classList.add("drag"); els.splitter.setPointerCapture(e.pointerId); els.body.style.userSelect = "none"; });
  els.splitter.addEventListener("pointermove", (e) => { if (drag) move(e.clientX); });
  const end = (e) => { if (!drag) return; drag = false; els.splitter.classList.remove("drag"); els.body.style.userSelect = ""; prefs.set("split", parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--split"))); };
  els.splitter.addEventListener("pointerup", end);
  els.splitter.addEventListener("pointercancel", end);
  els.splitter.addEventListener("dblclick", () => { apply(50); prefs.set("split", 50); });
  els.splitter.addEventListener("keydown", (e) => {
    const cur = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--split")) || 50;
    if (e.key === "ArrowLeft") { apply(cur - 3); e.preventDefault(); }
    if (e.key === "ArrowRight") { apply(cur + 3); e.preventDefault(); }
    prefs.set("split", parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--split")));
  });
})();

/* ================= upload ================= */
function fileProblem(f) {
  const ext = "." + f.name.split(".").pop().toLowerCase();
  if (!ACCEPT_EXT.includes(ext)) return `Unsupported type (${ext})`;
  if (f.size > MAX_UPLOAD) return `Over the 15 MB limit (${fmtBytes(f.size)})`;
  if (f.size === 0) return "File is empty";
  return null;
}
function openUpload(initial = []) {
  let files = [...initial];
  let busy = false;
  const node = html(`<div class="modal"><div class="modal-head"><h3>Upload reports</h3><button class="btn btn-ghost btn-icon btn-sm" data-x aria-label="Close">${icon("x")}</button></div>
    <div class="modal-body"><p class="lead">Add one or more files. They're queued and processed in the background - you can watch progress live.</p>
      <label class="drop" data-drop tabindex="0">${icon("upload")}<b>Drop files here or click to browse</b><span>PDF, DOCX, PNG, JPG, TXT &middot; up to 15 MB each</span>
        <input type="file" multiple hidden accept="${ACCEPT_EXT.join(",")}"></label>
      <div class="files" data-files></div><div class="form-actions" style="margin-top:10px"><span class="msg err" data-err></span></div></div>
    <div class="modal-foot"><button class="btn btn-outline" data-x>Cancel</button><button class="btn btn-primary" data-go disabled>Process files</button></div></div>`);
  const m = openModal(node, { initialFocus: () => node.querySelector("[data-drop]") });
  const input = node.querySelector("input[type=file]");
  const list = node.querySelector("[data-files]");
  const go = node.querySelector("[data-go]");
  const draw = () => {
    list.innerHTML = files.map((f, i) => { const p = fileProblem(f); return `<div class="file ${p ? "bad" : ""}"><span class="ico">${icon("file")}</span><span class="fn" title="${esc(f.name)}">${esc(f.name)}</span><span class="fs">${p ? esc(p) : fmtBytes(f.size)}</span><button class="btn btn-ghost btn-icon btn-sm" data-rm="${i}" aria-label="Remove ${esc(f.name)}">${icon("x")}</button></div>`; }).join("");
    const ok = files.length && files.every((f) => !fileProblem(f));
    go.disabled = !ok || busy;
    go.textContent = files.length > 1 ? `Process ${files.length} files` : "Process file";
  };
  const add = (fl) => { files.push(...Array.from(fl)); node.querySelector("[data-err]").textContent = ""; draw(); };
  input.addEventListener("change", () => { add(input.files); input.value = ""; });
  const drop = node.querySelector("[data-drop]");
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { e.stopPropagation(); add(e.dataTransfer.files); });
  node.addEventListener("click", (e) => {
    if (e.target.closest("[data-x]")) m.close();
    const rm = e.target.closest("[data-rm]"); if (rm) { files.splice(Number(rm.dataset.rm), 1); draw(); }
  });
  go.addEventListener("click", async () => {
    busy = true; draw(); go.textContent = "Uploading…";
    const fd = new FormData();
    try {
      let created;
      if (files.length === 1) { fd.append("file", files[0]); created = [await request("/reports", { method: "POST", body: fd, timeout: 120000 })]; }
      else { files.forEach((f) => fd.append("files", f)); created = await request("/reports/batch", { method: "POST", body: fd, timeout: 180000 }); }
      m.close();
      toast(`Queued ${created.length} document${created.length === 1 ? "" : "s"} for processing`);
      await loadReports();
      selectReport(created[0].id);
    } catch (err) { busy = false; node.querySelector("[data-err]").textContent = err.message; draw(); }
  });
  draw();
}
els.uploadBtn.addEventListener("click", () => openUpload());

/* page-wide drag & drop */
(function initGlobalDrop() {
  let depth = 0, overlay = null;
  const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
  window.addEventListener("dragenter", (e) => { if (!hasFiles(e) || hasModal()) return; depth++; if (!overlay) { overlay = html(`<div class="dropall"><div>Drop files to upload</div></div>`); document.body.appendChild(overlay); } });
  window.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth && overlay) { overlay.remove(); overlay = null; } });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; overlay?.remove(); overlay = null;
    if (!hasModal() && e.dataTransfer.files.length) openUpload(Array.from(e.dataTransfer.files));
  });
})();

/* ================= LOINC lookup ================= */
function openLookup() {
  if (hasModal()) return;
  const node = html(`<div class="modal cmd"><div class="modal-head"><h3>LOINC terminology lookup</h3><button class="btn btn-ghost btn-icon btn-sm" data-x aria-label="Close">${icon("x")}</button></div>
    <div class="modal-body"><div class="search"><input class="input" data-q placeholder="Search by name or code, e.g. glucose, TSH, 2345-7" autocomplete="off" aria-label="Search LOINC"></div>
    <div class="results-list" data-res><div class="empty" style="padding:26px 12px"><p style="margin:0">Type at least 2 characters to search ~62,000 official LOINC laboratory codes.</p></div></div></div></div>`);
  const m = openModal(node, { initialFocus: () => node.querySelector("[data-q]") });
  const input = node.querySelector("[data-q]"), res = node.querySelector("[data-res]");
  let ac;
  const run = debounce(async () => {
    const q = input.value.trim();
    ac?.abort();
    if (q.length < 2) { res.innerHTML = `<div class="empty" style="padding:26px 12px"><p style="margin:0">Type at least 2 characters to search.</p></div>`; return; }
    ac = new AbortController();
    res.innerHTML = `<div class="empty" style="padding:26px"><div class="spin"></div></div>`;
    try {
      const rows = await request(`/loinc/search?q=${encodeURIComponent(q)}`, { signal: ac.signal });
      res.innerHTML = rows.length ? rows.map((r) => `<button class="res" data-code="${esc(r.loinc_num)}" title="Click to copy the code"><span class="c">${esc(r.loinc_num)}</span><span class="n">${esc(r.long_common_name)}<small>${esc([r.system, r.component, r.example_units].filter(Boolean).join(" · "))}</small></span></button>`).join("")
        : `<div class="empty" style="padding:26px 12px"><p style="margin:0">No LOINC codes match “${esc(q)}”.</p></div>`;
    } catch (err) { if (!isAbort(err)) res.innerHTML = `<div class="empty" style="padding:22px 12px"><p style="margin:0;color:var(--rose)">${esc(err.message)}</p></div>`; }
  }, 220);
  input.addEventListener("input", run);
  node.addEventListener("click", async (e) => {
    if (e.target.closest("[data-x]")) return m.close();
    const r = e.target.closest("[data-code]");
    if (r) { try { await navigator.clipboard.writeText(r.dataset.code); toast(`Copied ${r.dataset.code}`); } catch { toast(r.dataset.code); } }
  });
}
els.lookupBtn.addEventListener("click", openLookup);

/* ================= keyboard ================= */
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openLookup(); return; }
  if (e.key === "Escape") { if (closeTopModal()) return; if (els.body.classList.contains("sidebar-open")) closeDrawer(); }
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !hasModal()) { e.preventDefault(); if (narrow()) els.body.classList.add("sidebar-open"); els.repSearch.focus(); }
});
window.addEventListener("hashchange", () => { const id = idFromHash(); if (id !== state.selectedId) selectReport(id, { fromHash: true }); });

/* ================= boot ================= */
async function init() {
  renderChrome();
  els.repSearch.insertAdjacentHTML("beforebegin", icon("search"));
  if (prefs.get("collapsed", false) && !narrow()) els.body.classList.add("sidebar-collapsed");
  renderSidebar(); renderWelcome();
  const wanted = idFromHash();
  await loadReports();
  if (wanted) selectReport(wanted, { fromHash: true });
  schedulePoll();
}
/* ---------- API key prompt (shown only when the server answers 401) ---------- */
let keyModalOpen = false;
window.addEventListener("hp:auth-required", () => {
  if (keyModalOpen) return;
  keyModalOpen = true;
  const node = html(`<div class="modal"><div class="modal-head"><h3>API key required</h3></div>
    <div class="modal-body"><p class="lead">${getApiKey() ? "That key was rejected." : "This server is protected."} Enter the API key to continue.</p>
      <label class="field">API key<input class="input" type="password" autocomplete="off" spellcheck="false" data-key></label>
      <div class="form-actions" style="margin-top:10px"><span class="msg err" data-err></span></div></div>
    <div class="modal-foot"><button class="btn btn-primary" data-go>Continue</button></div></div>`);
  const m = openModal(node, { onClose: () => { keyModalOpen = false; }, initialFocus: () => node.querySelector("[data-key]") });
  const input = node.querySelector("[data-key]");
  const go = () => {
    const v = input.value.trim();
    if (!v) { node.querySelector("[data-err]").textContent = "Enter a key."; return; }
    setApiKey(v); m.close(true); loadReports();
    if (state.selectedId) selectReport(state.selectedId, { fromHash: true });
  };
  node.querySelector("[data-go]").addEventListener("click", go);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
});

window.__hp = { state, viewer };   // handy for debugging in the console
init();
