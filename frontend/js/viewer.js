// Document viewer: PDF (pdf.js), Word (docx-preview), images, plain text.
// Race-safe: every open() bumps a token; any async step that finishes after the
// user has moved to another document checks the token and bails out.

import { request, isAbort } from "./api.js";
import { icon, esc, html } from "./ui.js";

const VENDOR = (f) => new URL(`../vendor/${f}`, import.meta.url).href;
const DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const MIN_ZOOM = 0.25, MAX_ZOOM = 4, STEP = 1.2;
const BLOB_CACHE_MAX = 6;

let pdfjsPromise;
function loadPdfjs() {
  pdfjsPromise ||= import(VENDOR("pdf.min.mjs")).then((m) => {
    m.GlobalWorkerOptions.workerSrc = VENDOR("pdf.worker.min.mjs");
    return m;
  });
  return pdfjsPromise;
}
const scriptCache = new Map();
function loadScript(src) {
  if (!scriptCache.has(src)) {
    scriptCache.set(src, new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = src; s.onload = res; s.onerror = () => rej(new Error(`Failed to load ${src}`));
      document.head.appendChild(s);
    }));
  }
  return scriptCache.get(src);
}
async function loadDocx() {
  await loadScript(VENDOR("jszip.min.js"));
  await loadScript(VENDOR("docx-preview.min.js"));
  return window.docx;
}

export function kindOf(contentType = "", filename = "") {
  const ct = contentType.toLowerCase();
  const name = filename.toLowerCase();
  if (ct === "application/pdf" || name.endsWith(".pdf")) return "pdf";
  if (ct.startsWith("image/") || /\.(png|jpe?g)$/.test(name)) return "image";
  if (ct === DOCX_TYPE || name.endsWith(".docx")) return "docx";
  if (ct.startsWith("text/") || name.endsWith(".txt")) return "text";
  return "other";
}
const KIND_LABEL = { pdf: "PDF", image: "Image", docx: "Word", text: "Text", other: "File" };

export class DocumentViewer {
  constructor({ bar, body, onNavigate }) {
    this.bar = bar;
    this.body = body;
    this.onNavigate = onNavigate || (() => {});
    this.token = 0;
    this.blobs = new Map();
    this.current = null;
    this.kind = null;
    this.zoom = 1;
    this.fit = true;
    this.page = 1;
    this.pageEls = [];
    this.rot = 0;
    this._resize = new ResizeObserver(() => this._onResize());
    this._resize.observe(this.body);
    this.body.addEventListener("scroll", () => this._onScroll(), { passive: true });
    this.bar.addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]");
      if (b) this._action(b.dataset.v);
    });
    this.bar.addEventListener("change", (e) => {
      if (e.target.matches("[data-v-page]")) this.goToPage(parseInt(e.target.value, 10) || 1);
    });
    this._renderBar(null);
  }

  /* ----- public ----- */
  async open(doc) {
    if (this.current && this.current.id === doc.id && this.status !== "error") return;
    this._teardown();
    const token = ++this.token;
    this.current = doc;
    this.kind = kindOf(doc.content_type, doc.filename);
    this.zoom = 1; this.fit = true; this.page = 1; this.rot = 0;
    this.status = "loading";
    this._renderBar(this.kind);
    this._state("loading");
    try {
      const blob = await this._fetchBlob(doc, token);
      if (token !== this.token) return;
      if (this.kind === "pdf") await this._openPdf(blob, token);
      else if (this.kind === "image") await this._openImage(blob, token);
      else if (this.kind === "docx") await this._openDocx(blob, token);
      else if (this.kind === "text") await this._openText(blob, token);
      else this._state("error", "This file type can't be previewed here.", true);
      if (token === this.token && this.kind !== "other") { this.status = "ready"; this._clearState(); }
    } catch (err) {
      if (isAbort(err) || token !== this.token) return;
      console.error("[viewer] failed to open", doc, err);
      this.status = "error";
      this._state("error", err.message || "Could not load the document.", true);
    }
  }

  goToPage(n) {
    const els = this.pageEls;
    if (!els.length) { this.onNavigate(1); return; }
    n = Math.min(Math.max(1, n), els.length);
    const el = els[n - 1];
    this.body.scrollTo({ top: Math.max(0, el.offsetTop - 14), behavior: "smooth" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1300);
    this.page = n;
    this._syncBar();
  }

  destroy() { this._teardown(); this._resize.disconnect(); }

  /* ----- internals ----- */
  _teardown() {
    this.token++;
    this.abort?.abort();
    this.abort = null;
    try { this.pdfTask?.destroy?.(); } catch { /* already gone */ }
    this.pdfTask = null;
    this.pdf = null;
    this.io?.disconnect(); this.io = null;
    if (this.objectUrl) { URL.revokeObjectURL(this.objectUrl); this.objectUrl = null; }
    this.pageEls = [];
    this.stage = null;
    this.img = null;
    this.body.replaceChildren();
    this.body.scrollTop = 0; this.body.scrollLeft = 0;
    this.current = null;
  }

  async _fetchBlob(doc, token) {
    if (this.blobs.has(doc.id)) {
      const b = this.blobs.get(doc.id);
      this.blobs.delete(doc.id); this.blobs.set(doc.id, b); // refresh LRU order
      return b;
    }
    this.abort = new AbortController();
    const blob = await request(`/reports/${encodeURIComponent(doc.id)}/file`, { as: "blob", signal: this.abort.signal, timeout: 120000 });
    if (token !== this.token) return blob;
    this.blobs.set(doc.id, blob);
    while (this.blobs.size > BLOB_CACHE_MAX) this.blobs.delete(this.blobs.keys().next().value);
    return blob;
  }

  _state(kind, message = "", withDownload = false) {
    this._clearState();
    const el = html(`<div class="vstate" data-vstate><div>${
      kind === "loading"
        ? `<div class="spin" style="margin:0 auto"></div><h3>Loading document…</h3><p>Fetching the original file</p>`
        : `<div class="empty-ico">${icon("alert", "")}</div><h3>Can't show this document</h3><p>${esc(message)}</p>${
            withDownload && this.current ? `<button class="btn btn-outline btn-sm" data-v="retry">${icon("refresh")} Try again</button>` : ""}`
    }</div></div>`);
    this.body.appendChild(el);
  }
  _clearState() { this.body.querySelectorAll("[data-vstate]").forEach((n) => n.remove()); }

  /* ----- toolbar ----- */
  _renderBar(kind) {
    const nav = kind === "pdf" || kind === "docx";
    const zoom = kind && kind !== "other";
    this.bar.innerHTML = `
      <span class="label">${icon(kind === "image" ? "image" : "file")} ${kind ? KIND_LABEL[kind] : "Document"}</span>
      ${nav ? `<div class="grp"><button class="btn btn-ghost btn-icon btn-sm" data-v="prev" aria-label="Previous page">${icon("up")}</button>
        <span class="pg"><input class="input" data-v-page value="1" inputmode="numeric" aria-label="Page number"> / <span data-v-total>–</span></span>
        <button class="btn btn-ghost btn-icon btn-sm" data-v="next" aria-label="Next page">${icon("down")}</button></div><span class="sep"></span>` : ""}
      ${zoom ? `<div class="grp"><button class="btn btn-ghost btn-icon btn-sm" data-v="out" aria-label="Zoom out">${icon("zoomOut")}</button>
        <span class="zoom" data-v-zoom>100%</span>
        <button class="btn btn-ghost btn-icon btn-sm" data-v="in" aria-label="Zoom in">${icon("zoomIn")}</button>
        <button class="btn btn-ghost btn-icon btn-sm" data-v="fit" aria-label="Fit to width" title="Fit to width">${icon("fit")}</button></div>` : ""}
      ${kind === "image" ? `<span class="sep"></span><button class="btn btn-ghost btn-icon btn-sm" data-v="rotate" aria-label="Rotate" title="Rotate 90°">${icon("rotate")}</button>` : ""}`;
    this._syncBar();
  }
  _syncBar() {
    const z = this.bar.querySelector("[data-v-zoom]");
    if (z) z.textContent = `${Math.round(this.zoom * 100)}%`;
    const inp = this.bar.querySelector("[data-v-page]");
    if (inp && document.activeElement !== inp) inp.value = this.page;
    const tot = this.bar.querySelector("[data-v-total]");
    if (tot) tot.textContent = this.pageEls.length || "–";
  }
  _action(a) {
    if (a === "retry") { const d = this.current; this.status = "retry"; this.current = null; if (d) this.open(d); return; }
    if (a === "prev") return this.goToPage(this.page - 1);
    if (a === "next") return this.goToPage(this.page + 1);
    if (a === "in") return this._setZoom(this.zoom * STEP);
    if (a === "out") return this._setZoom(this.zoom / STEP);
    if (a === "fit") { this.fit = true; return this._relayout(); }
    if (a === "rotate") { this.rot = (this.rot + 90) % 360; this.fit = true; return this._relayout(); }
  }
  _setZoom(z) {
    this.fit = false;
    this.zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
    this._relayout();
  }
  _fitZoom() {
    const w = this.body.clientWidth, h = this.body.clientHeight;
    if (this.kind === "pdf" && this.pageSizes) return Math.min(2, Math.max(MIN_ZOOM, (w - 36) / Math.max(...this.pageSizes.map((s) => s.w))));
    if (this.kind === "image" && this.img) {
      const swap = this.rot % 180 !== 0;
      const bw = swap ? this.img.naturalHeight : this.img.naturalWidth, bh = swap ? this.img.naturalWidth : this.img.naturalHeight;
      return Math.max(MIN_ZOOM, Math.min((w - 44) / bw, (h - 44) / bh, 1));
    }
    if (this.kind === "docx") return Math.min(1.4, Math.max(MIN_ZOOM, (w - 28) / (this.pageEls[0]?.offsetWidth || 794)));
    return this.zoom;
  }
  _onResize() {
    if (!this.current || this.status !== "ready" || !this.fit) return;
    clearTimeout(this._rt);
    this._rt = setTimeout(() => {
      // Ignore resizes that wouldn't visibly change the fit: re-laying out
      // cancels in-flight page renders, so a feedback loop (layout -> scrollbar
      // -> resize -> layout) would starve rendering forever.
      const z = this._fitZoom();
      if (Math.abs(z - this.zoom) / this.zoom > 0.012) this._relayout();
    }, 90);
  }
  _relayout() {
    if (this.kind === "pdf") this._layoutPdf();
    else if (this.kind === "image") this._layoutImage();
    else if (this.kind === "docx") this._layoutDocx();
    else if (this.kind === "text") this._layoutText();
    this._syncBar();
  }
  _onScroll() {
    if (!this.pageEls.length || this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      const mid = this.body.scrollTop + this.body.clientHeight * 0.35;
      let cur = 1;
      this.pageEls.forEach((el, i) => { if (el.offsetTop <= mid) cur = i + 1; });
      if (cur !== this.page) { this.page = cur; this._syncBar(); }
    });
  }

  /* ----- PDF ----- */
  async _openPdf(blob, token) {
    const pdfjs = await loadPdfjs();
    const data = new Uint8Array(await blob.arrayBuffer());
    if (token !== this.token) return;
    const task = pdfjs.getDocument({ data });
    this.pdfTask = task;
    const pdf = await task.promise;
    if (token !== this.token) { pdf.destroy(); return; }
    this.pdf = pdf;
    const sizes = [];
    for (let i = 1; i <= pdf.numPages; i++) {
      const p = await pdf.getPage(i);
      const vp = p.getViewport({ scale: 1 });
      sizes.push({ w: vp.width, h: vp.height });
      if (token !== this.token) return;
    }
    this.pageSizes = sizes;
    const wrap = html(`<div class="pdf-pages"></div>`);
    sizes.forEach((_, i) => {
      const el = html(`<div class="pdf-page loading" data-i="${i}"><span class="pn">${i + 1} / ${sizes.length}</span></div>`);
      wrap.appendChild(el);
      this.pageEls.push(el);
    });
    this._clearState();
    this.body.appendChild(wrap);
    this.io = new IntersectionObserver((entries) => entries.forEach((en) => { if (en.isIntersecting) this._renderPdfPage(en.target); }),
      { root: this.body, rootMargin: "700px 0px" });
    this.status = "ready";
    this._layoutPdf();
  }
  _layoutPdf() {
    if (!this.pdf) return;
    if (this.fit) this.zoom = this._fitZoom();
    this.io.disconnect();
    this.pageEls.forEach((el, i) => {
      const s = this.pageSizes[i];
      el.style.width = `${Math.floor(s.w * this.zoom)}px`;
      el.style.height = `${Math.floor(s.h * this.zoom)}px`;
      el._task?.cancel?.();
      el._task = null;
      el.querySelector("canvas")?.remove();
      el.dataset.scale = "";
      el.classList.add("loading");
      this.io.observe(el);
    });
    this._syncBar();
  }
  async _renderPdfPage(el) {
    const scaleKey = `${this.zoom.toFixed(3)}`;
    if (el.dataset.scale === scaleKey || el._busy) return;
    el._busy = true;
    const token = this.token;
    try {
      const i = Number(el.dataset.i);
      const page = await this.pdf.getPage(i + 1);
      if (token !== this.token) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const base = this.pageSizes[i];
      const outScale = Math.min(this.zoom * dpr, 4096 / base.w);
      const vp = page.getViewport({ scale: outScale });
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(vp.width); canvas.height = Math.floor(vp.height);
      const task = page.render({ canvasContext: canvas.getContext("2d"), viewport: vp, canvas });
      el._task = task;
      await task.promise;
      if (token !== this.token || el.dataset.scale === scaleKey) return;
      el.querySelector("canvas")?.remove();
      el.prepend(canvas);
      el.dataset.scale = scaleKey;
      el.classList.remove("loading");
    } catch (err) {
      if (err?.name !== "RenderingCancelledException") console.error("[viewer] page render failed", err);
    } finally {
      el._busy = false;
      // a zoom change during the render leaves a stale scale; re-run for it
      if (token === this.token && el.dataset.scale !== `${this.zoom.toFixed(3)}` && el.isConnected) this.io?.observe(el);
    }
  }

  /* ----- image ----- */
  async _openImage(blob, token) {
    this.objectUrl = URL.createObjectURL(blob);
    const img = new Image();
    img.src = this.objectUrl;
    img.alt = "Original document";
    await img.decode();
    if (token !== this.token) return;
    this.img = img;
    const stage = html(`<div class="img-stage"><div class="img-frame"></div></div>`);
    stage.firstElementChild.appendChild(img);
    this._clearState();
    this.body.appendChild(stage);
    this.status = "ready";
    this._layoutImage();
  }
  _layoutImage() {
    const img = this.img;
    if (!img) return;
    const nw = img.naturalWidth, nh = img.naturalHeight;
    const swap = this.rot % 180 !== 0;
    const bw = swap ? nh : nw, bh = swap ? nw : nh;
    if (this.fit) this.zoom = this._fitZoom();
    const frame = img.parentElement;
    frame.style.width = `${Math.round(bw * this.zoom)}px`;
    frame.style.height = `${Math.round(bh * this.zoom)}px`;
    img.style.width = `${Math.round(nw * this.zoom)}px`;
    img.style.height = `${Math.round(nh * this.zoom)}px`;
    img.style.transform = `translate(-50%, -50%) rotate(${this.rot}deg)`;
    this._syncBar();
  }

  /* ----- docx ----- */
  async _openDocx(blob, token) {
    const docx = await loadDocx();
    if (token !== this.token) return;
    const stage = html(`<div class="docx-stage"></div>`);
    this.body.appendChild(stage);
    await docx.renderAsync(blob, stage, undefined, { className: "docx", inWrapper: true, breakPages: true, useBase64URL: true, ignoreLastRenderedPageBreak: false });
    if (token !== this.token) return;
    this.stage = stage;
    this.pageEls = Array.from(stage.querySelectorAll("section.docx"));
    this._clearState();
    this.status = "ready";
    this._layoutDocx();
  }
  _layoutDocx() {
    if (!this.stage) return;
    this.stage.style.zoom = "";
    if (this.fit) this.zoom = this._fitZoom();
    this.stage.style.zoom = String(this.zoom);
    this._syncBar();
  }

  /* ----- text ----- */
  async _openText(blob, token) {
    const text = await blob.text();
    if (token !== this.token) return;
    const pre = html(`<pre class="txt-stage"></pre>`);
    pre.textContent = text;
    this.stage = pre;
    this._clearState();
    this.body.appendChild(pre);
    this.status = "ready";
    this._layoutText();
  }
  _layoutText() {
    if (!this.stage) return;
    if (this.fit) this.zoom = 1;
    this.stage.style.fontSize = `${12.5 * this.zoom}px`;
    this._syncBar();
  }
}
