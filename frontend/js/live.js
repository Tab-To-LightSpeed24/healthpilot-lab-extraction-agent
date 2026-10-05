// Live processing console: derives a human phase + progress from the server's
// progress feed, and appends feed lines incrementally (no re-render flicker).

import { esc, fmtClock, html } from "./ui.js";

export const STEPS = ["Queued", "Opening", "Extracting", "Matching", "Done"];
const MARKS = { info: "›", warn: "!", error: "✕" };

/** Pure function (also unit-tested in the browser): turns a document detail
 *  into {phase, stepIndex, title, subtitle, pct}. */
export function derivePhase(doc) {
  const feed = doc.progress || [];
  const last = feed.length ? feed[feed.length - 1].msg : "";
  const total = doc.num_pages || 0;
  const done = doc.pages_done || 0;
  const status = doc.status;

  if (status === "complete") return { phase: "done", step: 4, title: "Extraction complete", subtitle: last || "All pages processed", pct: 100, tone: "done" };
  if (status === "failed") return { phase: "failed", step: Math.max(1, feedStep(feed)), title: "Processing failed", subtitle: doc.error_message || last, pct: pctOf(done, total, last), tone: "error" };
  if (status === "cancelled") return { phase: "cancelled", step: Math.max(1, feedStep(feed)), title: "Processing cancelled", subtitle: last || doc.error_message, pct: pctOf(done, total, last), tone: "error" };
  if (status === "pending") return { phase: "queued", step: 0, title: "Waiting in the queue", subtitle: "A worker will pick this document up in a moment…", pct: null, tone: "queued" };

  const step = feedStep(feed);
  const titles = ["Waiting in the queue", "Opening your document", "Extracting results", "Matching LOINC codes", "Finishing up"];
  const page = total ? ` · page ${Math.min(done + 1, total)} of ${total}` : "";
  return {
    phase: ["queued", "opening", "extracting", "matching", "done"][step],
    step,
    title: titles[step] + (step >= 2 ? page : ""),
    subtitle: last || "Starting…",
    pct: total ? pctOf(done, total, last) : null,
    tone: "running",
  };
}

function feedStep(feed) {
  // The most recent message of the CURRENT page decides the step; multi-page
  // documents cycle Extracting -> Matching for each page.
  for (let i = feed.length - 1; i >= 0; i--) {
    const m = feed[i].msg;
    if (/matching \d+ row/i.test(m)) return 3;
    if (/Page \d+\/\d+:|AI unavailable|turned off/i.test(m)) return 2;
    if (/Opened /i.test(m)) return 1;
    if (/Picked up/i.test(m)) return 1;
  }
  return 1;
}
function pctOf(done, total, last) {
  if (!total) return null;
  let within = 0.1;
  if (/matching \d+ row/i.test(last)) within = 0.8;
  else if (/found|flagged/i.test(last)) within = 0.6;
  else if (/sending|reading|running OCR/i.test(last)) within = 0.3;
  return Math.max(1, Math.min(99, Math.round(((done + within) / total) * 100)));
}

export class LivePanel {
  /** Console shown only while a document is processing. */
  constructor(host) {
    this.host = host;
    this.docId = null;
    this.count = 0;
    this.root = html(`<div class="console queued">
      <div class="live-head"><div class="orb"></div>
        <div class="live-title"><b data-title></b><span data-sub></span></div>
        <div class="live-pct" data-pct></div></div>
      <div class="pbar indet" data-bar><i></i></div>
      <div class="steps" data-steps>${STEPS.map((s) => `<span class="step"><i></i>${s}</span>`).join("")}</div>
      <div class="feed" data-feed role="log" aria-live="polite"></div>
      <p class="live-note" data-note></p></div>`);
    host.replaceChildren(this.root);
    this.feed = this.root.querySelector("[data-feed]");
  }

  update(doc) {
    if (doc.id !== this.docId) {
      this.docId = doc.id;
      this.count = 0;
      this.feed.replaceChildren();
    }
    const p = derivePhase(doc);
    const r = this.root;
    r.classList.toggle("queued", p.tone === "queued");
    r.classList.toggle("done", p.tone === "done" || p.tone === "error");
    r.querySelector("[data-title]").textContent = p.title;
    r.querySelector("[data-sub]").textContent = p.subtitle || "";
    const pct = p.pct;
    r.querySelector("[data-pct]").textContent = pct == null ? "" : `${pct}%`;
    const bar = r.querySelector("[data-bar]");
    bar.classList.toggle("indet", pct == null && p.tone !== "done");
    bar.firstElementChild.style.width = pct == null ? "" : `${pct}%`;
    r.querySelectorAll("[data-steps] .step").forEach((el, i) => {
      el.classList.toggle("done", i < p.step || p.tone === "done");
      el.classList.toggle("on", i === p.step && p.tone !== "done");
    });
    const note = r.querySelector("[data-note]");
    note.textContent = doc.used_fallback && doc.status !== "failed"
      ? "Using local extraction - results will be flagged for your review."
      : p.tone === "queued" ? "Large or scanned documents can take a little longer." : "";

    // Append only lines we haven't rendered yet.
    const feed = doc.progress || [];
    const nearBottom = this.feed.scrollHeight - this.feed.scrollTop - this.feed.clientHeight < 40;
    this.feed.querySelector(".last")?.classList.remove("last");
    for (let i = this.count; i < feed.length; i++) {
      const e = feed[i];
      this.feed.appendChild(html(
        `<div class="line ${esc(e.level || "info")}"><time>${esc(fmtClock(e.t))}</time><span class="mk">${MARKS[e.level] || MARKS.info}</span><span class="m">${esc(e.msg)}</span></div>`));
    }
    this.count = feed.length;
    if (!feed.length) {
      if (!this.feed.firstElementChild) this.feed.appendChild(html(`<div class="line ph"><time></time><span class="mk">›</span><span class="m">Waiting for the first event…</span></div>`));
    } else {
      this.feed.querySelector(".ph")?.remove();
    }
    this.feed.lastElementChild?.classList.add("last");
    if (nearBottom) this.feed.scrollTop = this.feed.scrollHeight;
  }
}
