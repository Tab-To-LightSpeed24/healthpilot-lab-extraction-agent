// API client. Every call can be cancelled with an AbortSignal, which is how
// the UI guarantees a slow response for report A never lands on report B.

const LOCAL_API_BASE = "http://localhost:8000";
const DEPLOYED_API_BASE = "https://healthpilot-api-2b2m.onrender.com";
export const IS_LOCAL_HOST = ["localhost", "127.0.0.1"].includes(window.location.hostname);
// Dev convenience: on localhost only, `?api=http://localhost:8010` (or the
// `hp.api` localStorage key) points the UI at another backend. Ignored on any
// non-local host so a crafted link can't redirect a deployed user's data.
const LOCAL_OVERRIDE = (() => {
  if (!IS_LOCAL_HOST) return null;
  let v = null;
  try { v = new URLSearchParams(window.location.search).get("api") || localStorage.getItem("hp.api"); } catch { /* storage blocked */ }
  return v && /^http:\/\/(localhost|127\.0\.0\.1):\d+$/.test(v) ? v : null;
})();
export const apiBase = () => LOCAL_OVERRIDE || (IS_LOCAL_HOST ? LOCAL_API_BASE : DEPLOYED_API_BASE);

const DEFAULT_TIMEOUT_MS = 45000;

export class ApiError extends Error {
  constructor(message, { status = 0, url = "", aborted = false } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
    this.aborted = aborted;
  }
}

export const isAbort = (err) => err && (err.aborted || err.name === "AbortError");

export async function request(path, { method = "GET", headers, body, signal, timeout = DEFAULT_TIMEOUT_MS, as = "json" } = {}) {
  const url = `${apiBase()}${path}`;
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  const onAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", onAbort, { once: true });
  }
  let resp;
  try {
    resp = await fetch(url, { method, headers, body, signal: controller.signal });
  } catch (err) {
    if (err.name === "AbortError") {
      if (timedOut) {
        throw new ApiError(
          `Request timed out after ${timeout / 1000}s (${url}). If the server was idle it can take up to a minute to wake up - try again.`,
          { url }
        );
      }
      throw new ApiError("aborted", { aborted: true, url });
    }
    console.error("[api] network error:", url, err);
    throw new ApiError(`Network error reaching ${url}: ${err.message}`, { url });
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener("abort", onAbort);
  }

  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    let detail = text;
    try {
      const j = JSON.parse(text);
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch { /* not JSON */ }
    console.error("[api] non-OK response:", url, resp.status, text);
    throw new ApiError(detail || `${resp.status} ${resp.statusText}`, { status: resp.status, url });
  }
  if (resp.status === 204 || as === "none") return null;
  try {
    if (as === "blob") return await resp.blob();
    if (as === "text") return await resp.text();
    return await resp.json();
  } catch (err) {
    if (controller.signal.aborted) throw new ApiError("aborted", { aborted: true, url });
    throw err;
  }
}

export const jsonBody = (obj) => ({ headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) });
