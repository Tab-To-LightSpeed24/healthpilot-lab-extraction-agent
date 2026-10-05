// Shared UI helpers: escaping, icons, keyed DOM reconciliation, toasts, modals.

export function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const PATHS = {
  upload: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M17 8l-5-5-5 5 M12 3v12",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.3-4.3",
  x: "M18 6 6 18 M6 6l12 12",
  left: "m15 18-6-6 6-6",
  right: "m9 18 6-6-6-6",
  up: "m18 15-6-6-6 6",
  down: "m6 9 6 6 6-6",
  plus: "M12 5v14 M5 12h14",
  edit: "M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z",
  trash: "M3 6h18 M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6 M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
  check: "M20 6 9 17l-5-5",
  alert: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3 M12 9v4 M12 17h.01",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M12 16v-4 M12 8h.01",
  file: "M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z M14 2v6h6 M16 13H8 M16 17H8 M10 9H8",
  image: "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z M8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z M21 15l-5-5L5 21",
  zoomIn: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.3-4.3 M11 8v6 M8 11h6",
  zoomOut: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.3-4.3 M8 11h6",
  rotate: "M21 12a9 9 0 1 1-3-6.7L21 8 M21 3v5h-5",
  download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  fit: "M8 3H5a2 2 0 0 0-2 2v3 M21 8V5a2 2 0 0 0-2-2h-3 M3 16v3a2 2 0 0 0 2 2h3 M16 21h3a2 2 0 0 0 2-2v-3",
  menu: "M4 6h16 M4 12h16 M4 18h16",
  panel: "M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z M9 3v18",
  copy: "M8 8h12v12H8z M16 8V4H4v12h4",
  sparkle: "m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3z",
  book: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z M4 19.5V21h16",
  external: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6 M15 3h6v6 M10 14 21 3",
  stop: "M5 5h14v14H5z",
  refresh: "M3 12a9 9 0 0 1 15.5-6.3L21 8 M21 3v5h-5 M21 12a9 9 0 0 1-15.5 6.3L3 16 M3 21v-5h5",
  doc: "M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z M14 2v6h6 M8 13h8 M8 17h5",
  activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  code: "m16 18 6-6-6-6 M8 6l-6 6 6 6",
  list: "M8 6h13 M8 12h13 M8 18h13 M3 6h.01 M3 12h.01 M3 18h.01",
};

export function icon(name, cls = "") {
  return `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${PATHS[name] || ""}"/></svg>`;
}

export function html(markup) {
  const t = document.createElement("template");
  t.innerHTML = markup.trim();
  return t.content.firstElementChild;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
export function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
  return isNaN(d) ? "" : timeFmt.format(d);
}
export function fmtClock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour12: false });
}
export function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

/** Keyed list reconciliation: updates only nodes whose signature changed,
 *  keeps untouched nodes (and their focus/scroll/input state) in place, and
 *  moves nodes only when order differs. This is what replaces the old
 *  "innerHTML = everything" refresh that caused flicker and lost state. */
export function reconcile(container, items, { key, sig, render }) {
  container.querySelectorAll(":scope > :not([data-key])").forEach((n) => n.remove());
  const existing = new Map();
  for (const ch of Array.from(container.children)) existing.set(ch.dataset.key, ch);
  const seen = new Set();
  let prev = null;
  for (const item of items) {
    const k = String(key(item));
    const s = String(sig(item));
    seen.add(k);
    let node = existing.get(k);
    if (!node) {
      node = render(item, null);
      node.dataset.key = k;
      node.dataset.sig = s;
    } else if (node.dataset.sig !== s) {
      const fresh = render(item, node);
      if (fresh !== node) {
        node.replaceWith(fresh);
        node = fresh;
      }
      node.dataset.key = k;
      node.dataset.sig = s;
    }
    const expected = prev ? prev.nextElementSibling : container.firstElementChild;
    if (node !== expected) container.insertBefore(node, expected);
    prev = node;
  }
  for (const [k, node] of existing) if (!seen.has(k)) node.remove();
}

/* ---------- toasts ---------- */
let toastHost;
export function toast(message, { type = "ok", ms = 3600 } = {}) {
  toastHost ||= document.getElementById("toasts");
  const el = html(`<div class="toast ${type === "err" ? "err" : ""}" role="status">${icon(type === "err" ? "alert" : "check")}<span>${esc(message)}</span></div>`);
  toastHost.appendChild(el);
  const close = () => { el.classList.add("out"); setTimeout(() => el.remove(), 260); };
  const timer = setTimeout(close, ms);
  el.addEventListener("click", () => { clearTimeout(timer); close(); });
  while (toastHost.children.length > 4) toastHost.firstElementChild.remove();
}

/* ---------- modals ---------- */
const modalStack = [];
export function openModal(node, { onClose, initialFocus } = {}) {
  const backdrop = html(`<div class="backdrop"></div>`);
  backdrop.appendChild(node);
  const lastFocus = document.activeElement;
  let closed = false;
  const api = {
    el: node,
    close(result) {
      if (closed) return;
      closed = true;
      const i = modalStack.indexOf(api);
      if (i >= 0) modalStack.splice(i, 1);
      backdrop.remove();
      if (lastFocus && lastFocus.focus) lastFocus.focus();
      onClose && onClose(result);
    },
  };
  node.setAttribute("role", "dialog");
  node.setAttribute("aria-modal", "true");
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) api.close(); });
  document.body.appendChild(backdrop);
  modalStack.push(api);
  setTimeout(() => (initialFocus ? initialFocus() : node.querySelector("input,button,[tabindex]"))?.focus?.(), 30);
  return api;
}
export function closeTopModal() {
  const top = modalStack[modalStack.length - 1];
  if (top) { top.close(); return true; }
  return false;
}
export function hasModal() { return modalStack.length > 0; }

export function confirmDialog({ title, body, confirmText = "Confirm", cancelText = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    const node = html(`<div class="modal" style="width:min(440px,100%)">
      <div class="modal-head"><h3>${esc(title)}</h3></div>
      <div class="modal-body"><p class="lead" style="margin:0">${esc(body)}</p></div>
      <div class="modal-foot">
        <button class="btn btn-outline" data-r="0">${esc(cancelText)}</button>
        <button class="btn ${danger ? "btn-warn" : "btn-primary"}" data-r="1">${esc(confirmText)}</button>
      </div></div>`);
    const m = openModal(node, { onClose: (r) => resolve(r === true), initialFocus: () => node.querySelector('[data-r="0"]') });
    node.addEventListener("click", (e) => {
      const b = e.target.closest("[data-r]");
      if (b) m.close(b.dataset.r === "1");
    });
  });
}

export function debounce(fn, ms) {
  let t;
  const wrapped = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}
