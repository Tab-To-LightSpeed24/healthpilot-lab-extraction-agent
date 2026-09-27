// No visible config UI -- the right backend is decided from where this page
// itself is being served, not typed in by whoever opens the site. Serving
// the frontend from a local dev server (127.0.0.1/localhost, any port)
// talks to the local backend; anywhere else (the deployed Vercel URL) talks
// to the live Render backend. A recruiter opening the deployed link sees
// working data immediately, with nothing to configure.
const LOCAL_API_BASE = "http://localhost:8000";
const DEPLOYED_API_BASE = "https://healthpilot-api-2b2m.onrender.com";
const IS_LOCAL_HOST = ["localhost", "127.0.0.1"].includes(window.location.hostname);

function getApiBase() {
  return IS_LOCAL_HOST ? LOCAL_API_BASE : DEPLOYED_API_BASE;
}

const uploadForm = document.getElementById("uploadForm");
const uploadBtn = document.getElementById("uploadBtn");
const uploadStatus = document.getElementById("uploadStatus");
const fileInput = document.getElementById("fileInput");
const fileLabel = document.getElementById("fileLabel");
const reportsList = document.getElementById("reportsList");
const statsStrip = document.getElementById("statsStrip");

const detailEmpty = document.getElementById("detailEmpty");
const detailContent = document.getElementById("detailContent");
const detailFilename = document.getElementById("detailFilename");
const detailMeta = document.getElementById("detailMeta");
const detailStatusBadge = document.getElementById("detailStatusBadge");
const observationCards = document.getElementById("observationCards");
const fhirJson = document.getElementById("fhirJson");
const cancelBtn = document.getElementById("cancelBtn");
const qualityChips = document.getElementById("qualityChips");

const CANCELLABLE_STATUSES = new Set(["pending", "processing"]);

let currentDocId = null;
let pollTimer = null;
let activeTab = "clinical";

fileInput.addEventListener("change", () => {
  const n = fileInput.files.length;
  fileLabel.textContent = n === 0
    ? "PDF, PNG, JPG, or TXT · select multiple for batch"
    : n === 1
      ? fileInput.files[0].name
      : `${n} files selected (batch)`;
});

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    activeTab = btn.dataset.tab;
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    document.getElementById("clinicalTab").classList.toggle("hidden", activeTab !== "clinical");
    document.getElementById("fhirTab").classList.toggle("hidden", activeTab !== "fhir");
    if (activeTab === "fhir" && currentDocId) loadFhirBundle(currentDocId);
  });
});

document.getElementById("copyFhirBtn").addEventListener("click", () => {
  navigator.clipboard.writeText(fhirJson.textContent).catch(() => {});
});

// Render's free tier spins the backend down after ~15 min idle; the first
// request after that can take 30-60s to wake it up. A plain fetch() with no
// timeout at all means a genuinely broken request (dropped connection, CORS
// preflight failing silently, DNS issue) hangs forever with zero feedback --
// exactly what looked like a stuck "Loading..." with no error. Every request
// now has an explicit timeout and always surfaces *something* to the user
// and the console, even when it fails.
const REQUEST_TIMEOUT_MS = 45000;

async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error(
        `Request timed out after ${REQUEST_TIMEOUT_MS / 1000}s (${url}). ` +
        `If the backend was idle, Render's free tier can take up to a minute to wake up -- try again.`
      );
    }
    // A fetch-level TypeError here almost always means the request never
    // reached the server at all (CORS rejection, DNS failure, offline).
    throw new Error(`Network error reaching ${url}: ${err.message}`);
  } finally {
    clearTimeout(timer);
  }
}

async function api(path, options = {}) {
  const url = `${getApiBase()}${path}`;
  let resp;
  try {
    resp = await fetchWithTimeout(url, options);
  } catch (err) {
    console.error("[api] request failed:", url, err);
    throw err;
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    console.error("[api] non-OK response:", url, resp.status, text);
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json();
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function pillClass(status) {
  return `pill-${status}`;
}

async function loadReports() {
  reportsList.innerHTML = '<p class="text-stone-400 text-xs">Loading...</p>';
  try {
    const reports = await api("/reports");
    renderStats(reports);

    if (reports.length === 0) {
      reportsList.innerHTML = '<p class="text-stone-400 text-xs">No reports uploaded yet.</p>';
      return;
    }
    reportsList.innerHTML = "";
    reports.forEach((r) => {
      const div = document.createElement("div");
      div.className = "flex items-center justify-between p-2.5 rounded-lg hover:bg-stone-50 cursor-pointer border " +
        (r.id === currentDocId ? "border-[#0d3a2e]/30 bg-[#0d3a2e]/5" : "border-transparent");
      const isCancelling = r.cancel_requested && r.status === "processing";
      const displayStatus = isCancelling ? "cancelling" : r.status;
      div.innerHTML = `
        <div class="truncate">
          <div class="truncate font-medium text-[13px]">${escapeHtml(r.filename)}</div>
          <div class="text-[11px] text-stone-400">${new Date(r.uploaded_at).toLocaleString()}</div>
        </div>
        <span class="status-pill ${pillClass(displayStatus)} text-[10px] px-2 py-0.5 rounded-full shrink-0 ml-2">${displayStatus}</span>
      `;
      div.addEventListener("click", () => selectReport(r.id));
      reportsList.appendChild(div);
    });
  } catch (e) {
    reportsList.innerHTML = `<p class="text-rose-600 text-xs">Failed to load reports: ${escapeHtml(e.message)}</p>`;
    statsStrip.innerHTML = "";
  }
}

function renderStats(reports) {
  const total = reports.length;
  const complete = reports.filter((r) => r.status === "complete").length;
  const processing = reports.filter((r) => (r.status === "pending" || r.status === "processing") && !r.cancel_requested).length;
  const failed = reports.filter((r) => r.status === "failed" || r.status === "cancelled").length;

  const cards = [
    { label: "Reports uploaded", value: total, accent: "#0d3a2e" },
    { label: "Fully processed", value: complete, accent: "#16a34a" },
    { label: "In progress", value: processing, accent: "#0ea5e9" },
    { label: "Failed / Cancelled", value: failed, accent: "#dc2626" },
  ];
  statsStrip.innerHTML = cards
    .map(
      (c) => `
    <div class="card p-4">
      <div class="text-2xl font-extrabold tracking-tight" style="color:${c.accent}">${c.value}</div>
      <div class="text-[11px] text-stone-500 mt-0.5">${c.label}</div>
    </div>`
    )
    .join("");
}

async function selectReport(docId) {
  currentDocId = docId;
  detailEmpty.classList.add("hidden");
  detailContent.classList.remove("hidden");
  await refreshDetail();
  if (activeTab === "fhir") loadFhirBundle(docId);
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const doc = await refreshDetail();
    if (doc && (doc.status === "complete" || doc.status === "failed" || doc.status === "cancelled")) {
      clearInterval(pollTimer);
      loadReports();
    }
  }, 2000);
}

function renderObservationCard(o) {
  const rowClass = `row-${o.mapping_status}`;
  const reviewable = o.mapping_status !== "confirmed" || o.mapping_stage !== "human_review";
  return `
    <div class="card ${rowClass} p-3.5" data-obs-id="${o.id}">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <div class="flex items-baseline gap-2 flex-wrap">
            <span class="font-semibold text-[13px]">${escapeHtml(o.original_test_name)}</span>
            ${o.normalized_test_name && o.normalized_test_name !== o.original_test_name
              ? `<span class="text-[11px] text-stone-400">&rarr; ${escapeHtml(o.normalized_test_name)}</span>`
              : ""}
          </div>
          <div class="text-lg font-bold mono mt-0.5">
            ${escapeHtml(o.value)} <span class="text-xs font-normal text-stone-500">${escapeHtml(o.unit)}</span>
            ${o.flag ? `<span class="text-rose-600 text-xs font-bold ml-1">${escapeHtml(o.flag)}</span>` : ""}
          </div>
          <div class="text-[11px] text-stone-500 mt-1 flex flex-wrap gap-x-3">
            ${o.reference_range ? `<span>Ref: ${escapeHtml(o.reference_range)}</span>` : ""}
            ${o.specimen ? `<span>Specimen: ${escapeHtml(o.specimen)}</span>` : ""}
            ${o.page_number ? `<span>Page ${o.page_number}</span>` : ""}
          </div>
        </div>
        <div class="text-right shrink-0">
          <span class="status-pill ${pillClass(o.mapping_status)} text-[10px] px-2 py-0.5 rounded-full" title="${escapeHtml(o.mapping_rationale)}">
            ${o.mapping_status.replace("_", " ")}
          </span>
          <div class="mono text-xs text-stone-500 mt-1.5">${escapeHtml(o.loinc_code) || "&mdash;"}</div>
          <div class="text-[10px] text-stone-400">${o.mapping_confidence != null ? "conf " + o.mapping_confidence.toFixed(2) : ""}</div>
          ${reviewable ? `<button class="review-toggle text-[11px] text-[#0d3a2e] underline mt-1.5">Review</button>` : ""}
        </div>
      </div>
      ${reviewable ? `
      <div class="review-form hidden mt-3 pt-3 border-t border-[#e7e4db] flex flex-wrap items-center gap-2">
        <input type="text" placeholder="LOINC code e.g. 718-7" class="review-code-input text-xs border border-[#e7e4db] rounded-lg px-2 py-1.5 w-40 focus:outline-none focus:ring-2 focus:ring-[#0d3a2e]/20" />
        <button class="review-confirm text-xs bg-[#0d3a2e] text-white px-2.5 py-1.5 rounded-lg hover:bg-[#0a2e25] font-medium">Confirm code</button>
        <button class="review-unmapped text-xs border border-[#e7e4db] px-2.5 py-1.5 rounded-lg hover:bg-stone-50">No code applies</button>
        <span class="review-status text-[11px] text-stone-400"></span>
      </div>` : ""}
    </div>
  `;
}

function renderQualityChips(quality) {
  if (!quality || quality.total_observations === 0) {
    qualityChips.innerHTML = "";
    return;
  }
  const chips = [];
  if (quality.review_needed_ratio != null && quality.review_needed_ratio > 0) {
    chips.push(`<span class="px-2 py-1 rounded-full bg-amber-50 text-amber-800 border border-amber-200">${Math.round(quality.review_needed_ratio * 100)}% need review</span>`);
  }
  if (quality.possible_duplicate_test_names.length) {
    chips.push(`<span class="px-2 py-1 rounded-full bg-amber-50 text-amber-800 border border-amber-200">Possible duplicates: ${quality.possible_duplicate_test_names.map(escapeHtml).join(", ")}</span>`);
  }
  if (quality.low_confidence_extractions.length) {
    chips.push(`<span class="px-2 py-1 rounded-full bg-rose-50 text-rose-800 border border-rose-200">${quality.low_confidence_extractions.length} low-confidence extraction(s)</span>`);
  }
  qualityChips.innerHTML = chips.join("");
}

observationCards.addEventListener("click", async (e) => {
  const card = e.target.closest("[data-obs-id]");
  if (!card) return;
  const obsId = card.dataset.obsId;

  if (e.target.classList.contains("review-toggle")) {
    card.querySelector(".review-form").classList.toggle("hidden");
    return;
  }

  if (e.target.classList.contains("review-confirm") || e.target.classList.contains("review-unmapped")) {
    const statusEl = card.querySelector(".review-status");
    const isUnmapped = e.target.classList.contains("review-unmapped");
    const codeInput = card.querySelector(".review-code-input");
    const code = codeInput.value.trim();
    if (!isUnmapped && !code) {
      statusEl.textContent = "Enter a LOINC code first.";
      statusEl.className = "review-status text-[11px] text-rose-600";
      return;
    }
    statusEl.textContent = "Saving...";
    statusEl.className = "review-status text-[11px] text-stone-400";
    try {
      await api(`/observations/${obsId}/review`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(isUnmapped ? { loinc_code: null, mapping_status: "unmapped" } : { loinc_code: code }),
      });
      await refreshDetail();
    } catch (err) {
      statusEl.textContent = `Failed: ${err.message}`;
      statusEl.className = "review-status text-[11px] text-rose-600";
    }
  }
});

cancelBtn.addEventListener("click", async () => {
  if (!currentDocId) return;
  cancelBtn.disabled = true;
  cancelBtn.textContent = "Cancelling...";
  try {
    await api(`/reports/${currentDocId}/cancel`, { method: "POST" });
    await refreshDetail();
    await loadReports();
  } catch (e) {
    alert(`Cancel failed: ${e.message}`);
    cancelBtn.disabled = false;
    cancelBtn.textContent = "Cancel";
  }
});

async function refreshDetail() {
  try {
    const doc = await api(`/reports/${currentDocId}`);
    detailFilename.textContent = doc.filename;
    detailMeta.textContent = `${doc.num_pages} page(s) &middot; uploaded ${new Date(doc.uploaded_at).toLocaleString()}${doc.error_message ? " &middot; " + doc.error_message : ""}`.replace(/&middot;/g, "·");

    const isCancelling = doc.cancel_requested && doc.status === "processing";
    const displayStatus = isCancelling ? "cancelling" : doc.status;
    detailStatusBadge.textContent = displayStatus;
    detailStatusBadge.className = `status-pill text-xs px-2.5 py-1 rounded-full ${pillClass(displayStatus)}`;

    if (doc.status === "cancelled") {
      cancelBtn.classList.add("hidden");
    } else if (isCancelling) {
      cancelBtn.classList.remove("hidden");
      cancelBtn.disabled = true;
      cancelBtn.textContent = "Cancelling...";
    } else if (CANCELLABLE_STATUSES.has(doc.status)) {
      cancelBtn.classList.remove("hidden");
      cancelBtn.disabled = false;
      cancelBtn.textContent = "Cancel";
    } else {
      cancelBtn.classList.add("hidden");
    }

    renderQualityChips(doc.quality);

    observationCards.innerHTML = doc.observations.length
      ? doc.observations.map(renderObservationCard).join("")
      : '<p class="text-stone-400 text-sm py-8 text-center">No observations extracted yet.</p>';

    return doc;
  } catch (e) {
    detailMeta.textContent = `Error loading report: ${e.message}`;
    return null;
  }
}

async function loadFhirBundle(docId) {
  fhirJson.textContent = "Loading...";
  try {
    const bundle = await api(`/reports/${docId}/fhir`);
    fhirJson.textContent = JSON.stringify(bundle, null, 2);
  } catch (e) {
    fhirJson.textContent = `Failed to load FHIR bundle: ${e.message}`;
  }
}

uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const files = Array.from(fileInput.files);
  if (files.length === 0) return;
  uploadBtn.disabled = true;
  uploadStatus.textContent = files.length > 1 ? `Uploading ${files.length} files as a batch...` : "Uploading...";
  try {
    const formData = new FormData();
    const isBatch = files.length > 1;
    files.forEach((f) => formData.append(isBatch ? "files" : "file", f));

    const url = `${getApiBase()}${isBatch ? "/reports/batch" : "/reports"}`;
    const resp = await fetchWithTimeout(url, { method: "POST", body: formData });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      console.error("[upload] non-OK response:", url, resp.status, text);
      throw new Error(`${resp.status}: ${text}`);
    }
    const result = await resp.json();
    const docs = isBatch ? result : [result];
    uploadStatus.textContent = `Queued ${docs.length} document(s).`;
    fileInput.value = "";
    fileLabel.textContent = "PDF, PNG, JPG, or TXT · select multiple for batch";
    await loadReports();
    await selectReport(docs[0].id);
  } catch (err) {
    console.error("[upload] failed:", err);
    uploadStatus.textContent = `Upload failed: ${err.message}`;
  } finally {
    uploadBtn.disabled = false;
  }
});

const loincSearchInput = document.getElementById("loincSearchInput");
const loincSearchResults = document.getElementById("loincSearchResults");
let loincDebounce = null;

loincSearchInput.addEventListener("input", () => {
  clearTimeout(loincDebounce);
  const q = loincSearchInput.value.trim();
  if (q.length < 2) {
    loincSearchResults.innerHTML = "";
    return;
  }
  loincDebounce = setTimeout(async () => {
    try {
      const results = await api(`/loinc/search?q=${encodeURIComponent(q)}`);
      loincSearchResults.innerHTML = results
        .map(
          (r) => `
        <div class="p-2 rounded-lg border border-[#e7e4db] flex justify-between gap-2">
          <span class="truncate">${escapeHtml(r.long_common_name)}</span>
          <span class="mono text-stone-400 shrink-0">${escapeHtml(r.loinc_num)}</span>
        </div>`
        )
        .join("") || '<p class="text-stone-400 text-xs">No matches.</p>';
    } catch (e) {
      loincSearchResults.innerHTML = `<p class="text-rose-600 text-xs">${escapeHtml(e.message)}</p>`;
    }
  }, 300);
});

const backendStatusBanner = document.getElementById("backendStatusBanner");

async function checkBackendHealth() {
  try {
    await fetchWithTimeout(`${getApiBase()}/health`);
    backendStatusBanner.classList.add("hidden");
  } catch (err) {
    console.error("[health] backend unreachable:", err);
    backendStatusBanner.textContent = `Backend unreachable (${getApiBase()}): ${err.message}`;
    backendStatusBanner.classList.remove("hidden");
  }
}

checkBackendHealth();
loadReports();
