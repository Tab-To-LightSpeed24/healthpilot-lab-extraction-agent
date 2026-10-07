"""Basic API hardening: shared-secret auth, rate limiting, request ids,
security headers and log redaction.

Deliberately dependency-free. Auth is a single shared API key -- a stopgap
until real per-user accounts exist, not a substitute for them.
"""
import contextvars
import hmac
import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.core.config import settings

# ---------------------------------------------------------------- auth

def require_api_key(request: Request) -> None:
    """Router-level dependency. A no-op when API_KEY is unset, so local dev
    and the test suite keep working; enforced the moment a key is configured.
    CORS preflight (OPTIONS) is answered by the CORS middleware before routing,
    so it never reaches this check."""
    expected = settings.api_key
    if not expected:
        return
    supplied = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")


# --------------------------------------------------------- rate limiting

class SlidingWindowLimiter:
    """Per-process sliding-window counter keyed by client. Good enough to
    protect the LLM quota on one instance; with several instances each keeps
    its own counts, so a shared store (Redis) would be needed to enforce a
    global limit."""

    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float = 60.0) -> float:
        """Returns 0 if allowed, else seconds until the caller may retry."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return max(0.1, window - (now - q[0]))
            q.append(now)
            return 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce(request: Request, bucket: str, limit: int) -> None:
    if limit <= 0:
        return
    wait = limiter.check(f"{bucket}:{_client_key(request)}", limit)
    if wait:
        raise HTTPException(
            status_code=429,
            detail="Too many requests - please slow down and try again shortly.",
            headers={"Retry-After": str(int(wait) + 1)},
        )


def rate_limit_general(request: Request) -> None:
    _enforce(request, "general", settings.rate_limit_per_minute)


def rate_limit_uploads(request: Request) -> None:
    _enforce(request, "upload", settings.rate_limit_uploads_per_minute)


# ------------------------------------------------------ request id / logs

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),                      # Google API keys
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                        # sk-... style keys
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),           # bearer tokens
    re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"),
    re.compile(r"(?i)((?:api[_-]?key|key)=)[^&\s'\"]{8,}"),       # ?key=... in URLs
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    for secret in (settings.gemini_api_key, settings.api_key):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED]")
    return text


class LogSafetyFilter(logging.Filter):
    """Stamps every record with the current request id and redacts secrets
    from the message and exception text so keys can never reach the logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:  # never let logging break the app
            pass
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if not any(isinstance(f, LogSafetyFilter) for f in handler.filters):
            handler.addFilter(LogSafetyFilter())
        handler.setFormatter(logging.Formatter(LOG_FORMAT))


# ------------------------------------------------------------- middleware

def _is_docs_path(path: str) -> bool:
    return path.startswith(("/docs", "/redoc", "/openapi.json"))


async def security_middleware(request: Request, call_next):
    """Tags the request with an id and adds defensive headers to every response."""
    rid = request.headers.get("x-request-id", "")
    rid = rid if re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", rid) else new_request_id()
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    h = response.headers
    h["X-Request-ID"] = rid
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["X-Frame-Options"] = "DENY"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if not _is_docs_path(request.url.path):
        # JSON/file API: it never needs to run scripts or be framed.
        h["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    if request.headers.get("x-forwarded-proto", request.url.scheme) == "https":
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
