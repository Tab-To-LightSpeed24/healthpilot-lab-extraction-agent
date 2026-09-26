const API_BASE_KEY = "healthpilot_api_base";

function getApiBase() {
  return localStorage.getItem(API_BASE_KEY) || "http://localhost:8000";
}

function setApiBase(url) {
  localStorage.setItem(API_BASE_KEY, url.replace(/\/$/, ""));
}

const apiBaseInput = document.getElementById("apiBaseInput");
apiBaseInput.value = getApiBase();
document.getElementById("saveApiBase").addEventListener("click", () => {
  setApiBase(apiBaseInput.value.trim());
  loadReports();
});

const uploadForm = document.getElementById("uploadForm");
const uploadBtn = document.getElementById("uploadBtn");
const uploadStatus = document.getElementById("uploadStatus");
const fileInput = document.getElementById("fileInput");
const reportsList = document.getElementById("reportsList");

const detailEmpty = document.getElementById("detailEmpty");
const detailContent = document.getElementById("detailContent");
const detailFilename = document.getElementById("detailFilename");
const detailMeta = document.getElementById("detailMeta");
const detailStatusBadge = document.getElementById("detailStatusBadge");
const observationsBody = document.getElementById("observationsBody");

let currentDocId = null;
let pollTimer = null;

async function api(path, options = {}) {
  const resp = await fetch(`${getApiBase()}${path}`, options);
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json();
}

function statusBadgeClass(status) {
  return `badge-${status}`;
}

async function loadReports() {
  reportsList.innerHTML = '<p class="text-slate-400 text-xs">Loading...</p>';
  try {
    const reports = await api("/reports");
    if (reports.length === 0) {
      reportsList.innerHTML = '<p class="text-slate-400 text-xs">No reports uploaded yet.</p>';
      return;
    }
    reportsList.innerHTML = "";
    reports.forEach((r) => {
      const div = document.createElement("div");
      div.className = "flex items-center justify-between p-2 rounded hover:bg-slate-100 cursor-pointer border border-transparent " +
        (r.id === currentDocId ? "border-indigo-300 bg-indigo-50" : "");
      div.innerHTML = `
        <div class="truncate">
          <div class="truncate font-medium">${escapeHtml(r.filename)}</div>
          <div class="text-xs text-slate-400">${new Date(r.uploaded_at).toLocaleString()}</div>
        </div>
        <span class="text-xs px-2 py-0.5 rounded ${statusBadgeClass(r.status)}">${r.status}</span>
      `;
      div.addEventListener("click", () => selectReport(r.id));
      reportsList.appendChild(div);
    });
  } catch (e) {
    reportsList.innerHTML = `<p class="text-red-500 text-xs">Failed to load reports: ${escapeHtml(e.message)}</p>`;
  }
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function selectReport(docId) {
  currentDocId = docId;
  detailEmpty.classList.add("hidden");
  detailContent.classList.remove("hidden");
  await refreshDetail();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const doc = await refreshDetail();
    if (doc && (doc.status === "complete" || doc.status === "failed")) {
      clearInterval(pollTimer);
      loadReports();
    }
  }, 2000);
}

async function refreshDetail() {
  try {
    const doc = await api(`/reports/${currentDocId}`);
    detailFilename.textContent = doc.filename;
    detailMeta.textContent = `${doc.num_pages} page(s) · uploaded ${new Date(doc.uploaded_at).toLocaleString()}${doc.error_message ? " · " + doc.error_message : ""}`;
    detailStatusBadge.textContent = doc.status;
    detailStatusBadge.className = `text-xs px-2 py-1 rounded font-medium ${statusBadgeClass(doc.status)}`;

    observationsBody.innerHTML = "";
    doc.observations.forEach((o) => {
      const tr = document.createElement("tr");
      tr.className = "border-b border-slate-100";
      tr.innerHTML = `
        <td class="py-2 pr-2">${escapeHtml(o.original_test_name)}</td>
        <td class="py-2 pr-2">${escapeHtml(o.normalized_test_name)}</td>
        <td class="py-2 pr-2 font-mono">${escapeHtml(o.value)}${o.flag ? ` <span class="text-red-500 font-semibold">${escapeHtml(o.flag)}</span>` : ""}</td>
        <td class="py-2 pr-2">${escapeHtml(o.unit)}</td>
        <td class="py-2 pr-2 text-slate-500">${escapeHtml(o.reference_range)}</td>
        <td class="py-2 pr-2 text-slate-500">${escapeHtml(o.specimen)}</td>
        <td class="py-2 pr-2 font-mono">${escapeHtml(o.loinc_code)}</td>
        <td class="py-2 pr-2 text-slate-500">${escapeHtml(o.loinc_display)}</td>
        <td class="py-2 pr-2"><span class="px-2 py-0.5 rounded text-xs ${statusBadgeClass(o.mapping_status)}" title="${escapeHtml(o.mapping_rationale)}">${o.mapping_status.replace("_", " ")}</span></td>
        <td class="py-2 pr-2 text-slate-500">${o.mapping_confidence != null ? o.mapping_confidence.toFixed(2) : "—"}</td>
        <td class="py-2 pr-2 text-slate-500">${o.page_number ?? "—"}</td>
      `;
      observationsBody.appendChild(tr);
    });
    return doc;
  } catch (e) {
    detailMeta.textContent = `Error loading report: ${e.message}`;
    return null;
  }
}

uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;
  uploadBtn.disabled = true;
  uploadStatus.textContent = "Uploading...";
  try {
    const formData = new FormData();
    formData.append("file", file);
    const resp = await fetch(`${getApiBase()}/reports`, { method: "POST", body: formData });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`${resp.status}: ${text}`);
    }
    const doc = await resp.json();
    uploadStatus.textContent = `Uploaded. Processing document ${doc.id}...`;
    fileInput.value = "";
    await loadReports();
    await selectReport(doc.id);
  } catch (err) {
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
        <div class="p-2 rounded border border-slate-100 flex justify-between">
          <span>${escapeHtml(r.long_common_name)}</span>
          <span class="font-mono text-slate-400">${escapeHtml(r.loinc_num)}</span>
        </div>`
        )
        .join("") || '<p class="text-slate-400 text-xs">No matches.</p>';
    } catch (e) {
      loincSearchResults.innerHTML = `<p class="text-red-500 text-xs">${escapeHtml(e.message)}</p>`;
    }
  }, 300);
});

loadReports();
