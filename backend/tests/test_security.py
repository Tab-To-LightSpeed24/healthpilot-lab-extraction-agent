"""API hardening: shared-key auth, rate limiting, security headers, request
ids, log redaction and the prompt-injection guard. No network or LLM calls."""
import logging

import pytest

from app.core import security
from app.core.config import settings
from app.services import llm_client
from tests.test_api import client, _make_pdf_bytes  # noqa: F401


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", False)
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)
    monkeypatch.setattr(settings, "rate_limit_uploads_per_minute", 0)
    security.limiter.reset()
    yield
    security.limiter.reset()


# ------------------------------------------------------------------ auth

def test_auth_is_off_when_no_key_configured(client):
    assert client.get("/reports").status_code == 200


def test_requests_without_a_valid_key_are_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret-test-key")
    for headers in ({}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}):
        r = client.get("/reports", headers=headers)
        assert r.status_code == 401
        assert "key" in r.json()["detail"].lower()


def test_correct_key_is_accepted_on_every_router(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret-test-key")
    h = {"X-API-Key": "s3cret-test-key"}
    assert client.get("/reports", headers=h).status_code == 200
    assert client.get("/observations", headers=h).status_code == 200
    assert client.get("/loinc/search?q=glucose", headers=h).status_code == 200


def test_health_stays_public_even_when_auth_is_on(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret-test-key")
    assert client.get("/health").status_code == 200


def test_upload_requires_the_key_too(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret-test-key")
    files = {"file": ("a.pdf", _make_pdf_bytes("WBC 6.8"), "application/pdf")}
    assert client.post("/reports", files=files).status_code == 401


def test_cors_preflight_does_not_need_the_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret-test-key")
    r = client.options("/reports", headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-api-key",
    })
    assert r.status_code == 200
    assert "x-api-key" in r.headers["access-control-allow-headers"].lower()


def test_rejection_still_carries_cors_headers(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret-test-key")
    r = client.get("/reports", headers={"Origin": "https://app.example.com"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin")


# ---------------------------------------------------------- rate limiting

def test_upload_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_uploads_per_minute", 2)
    statuses = []
    for i in range(4):
        r = client.post("/reports", files={"file": (f"r{i}.pdf", _make_pdf_bytes("WBC 6.8"), "application/pdf")})
        statuses.append(r.status_code)
    assert statuses[:2] == [201, 201]
    assert statuses[2:] == [429, 429]
    assert int(r.headers["retry-after"]) >= 1


def test_upload_limit_does_not_throttle_reads(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_uploads_per_minute", 1)
    for _ in range(5):
        assert client.get("/reports").status_code == 200


def test_general_rate_limit_applies_to_all_api_routes(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    codes = [client.get("/reports").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert client.get("/health").status_code == 200   # health is never limited


def test_limiter_window_expires():
    lim = security.SlidingWindowLimiter()
    assert lim.check("k", 1, window=0.05) == 0
    assert lim.check("k", 1, window=0.05) > 0
    import time
    time.sleep(0.07)
    assert lim.check("k", 1, window=0.05) == 0


# ------------------------------------------------- headers and request id

def test_security_headers_present(client):
    r = client.get("/reports")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "strict-transport-security" not in r.headers      # plain http


def test_hsts_only_over_https(client):
    r = client.get("/reports", headers={"X-Forwarded-Proto": "https"})
    assert "max-age" in r.headers["strict-transport-security"]


def test_swagger_docs_are_not_blocked_by_csp(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "content-security-policy" not in r.headers


def test_request_id_is_generated_and_echoed(client):
    a = client.get("/health").headers["x-request-id"]
    b = client.get("/health").headers["x-request-id"]
    assert a != b and len(a) >= 6
    assert client.get("/health", headers={"X-Request-ID": "trace-abc-123"}).headers["x-request-id"] == "trace-abc-123"


def test_unsafe_request_id_is_replaced(client):
    r = client.get("/health", headers={"X-Request-ID": "bad id\r\nX-Evil: 1"})
    assert r.headers["x-request-id"] != "bad id\r\nX-Evil: 1"
    assert "x-evil" not in r.headers


# ---------------------------------------------------------- log redaction

def test_redact_masks_known_secret_shapes(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "my-configured-gemini-key-123")
    monkeypatch.setattr(settings, "api_key", "my-app-shared-key-456")
    text = (
        "call failed key=AIzaSyA1234567890abcdefghijklmnopqrstu "
        "auth Bearer abcdefghijklmnopqrstuvwxyz0123 sk-abcdefghijklmnop1234 "
        "x-api-key: topsecretvalue99 cfg my-configured-gemini-key-123 and my-app-shared-key-456"
    )
    out = security.redact(text)
    for leaked in ("AIzaSyA1234567890", "abcdefghijklmnopqrstuvwxyz0123", "sk-abcdefghijklmnop1234",
                   "topsecretvalue99", "my-configured-gemini-key-123", "my-app-shared-key-456"):
        assert leaked not in out
    assert out.count("[REDACTED]") >= 5
    assert "call failed" in out                 # surrounding text is preserved


def test_log_filter_redacts_message_args_and_tracebacks(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "super-secret-gemini-key-000")
    flt = security.LogSafetyFilter()
    try:
        raise RuntimeError("upstream said key super-secret-gemini-key-000 invalid")
    except RuntimeError:
        import sys
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed with %s",
                                ("super-secret-gemini-key-000",), sys.exc_info())
    assert flt.filter(rec)
    assert "super-secret" not in rec.getMessage()
    assert "super-secret" not in rec.exc_text
    assert rec.request_id == "-"


def test_log_filter_stamps_current_request_id():
    token = security.request_id_var.set("req-xyz-789")
    try:
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        security.LogSafetyFilter().filter(rec)
        assert rec.request_id == "req-xyz-789"
    finally:
        security.request_id_var.reset(token)


def test_configure_logging_installs_filter_once():
    security.configure_logging()
    security.configure_logging()
    for h in logging.getLogger().handlers:
        assert sum(isinstance(f, security.LogSafetyFilter) for f in h.filters) <= 1


# ------------------------------------------------- prompt-injection guard

def test_extraction_prompt_treats_page_text_as_untrusted():
    p = llm_client.EXTRACTION_PROMPT
    assert "untrusted data" in p and "ignore these rules" in p


def test_mapping_prompt_treats_fields_as_untrusted():
    assert "untrusted document" in llm_client.MAPPING_PROMPT


def test_prompts_still_format_cleanly():
    out = llm_client.EXTRACTION_PROMPT.format(text_layer="WBC 6.8")
    assert "WBC 6.8" in out and "{" in out          # JSON shape braces survive formatting
    llm_client.MAPPING_PROMPT.format(
        original_name="a", normalized_name="b", value="1", unit="u",
        specimen="s", method="m", timing="t", candidates="c")
