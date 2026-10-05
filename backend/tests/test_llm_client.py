import json
from unittest.mock import MagicMock, patch

import pytest

from app.services import llm_client


def _fake_completion(return_json):
    message = MagicMock(content=json.dumps(return_json))
    choice = MagicMock(message=message)
    return MagicMock(choices=[choice])


def test_extract_page_text_only_input_does_not_send_empty_image_part():
    """Reproduces the plain-text-report code path where pdf_utils.load_pages
    returns image_png=b'' (no rasterized page). extract_page must not send a
    broken empty image part to the API in that case."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion({"tests": [], "page_notes": None})

    with patch.object(llm_client, "_get_client", return_value=fake_client):
        llm_client.extract_page(image_png=b"", text_layer="Iron 70 ug/dL")

        call_kwargs = fake_client.chat.completions.create.call_args.kwargs
        content = call_kwargs["messages"][0]["content"]
        assert len(content) == 1, "text-only call must not include an image part"
        assert "Iron 70 ug/dL" in content[0]["text"]


def test_extract_page_with_image_includes_both_prompt_and_image_part():
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_completion({"tests": [], "page_notes": None})

    with patch.object(llm_client, "_get_client", return_value=fake_client):
        llm_client.extract_page(image_png=b"\x89PNGfakebytes", text_layer="some text")

        call_kwargs = fake_client.chat.completions.create.call_args.kwargs
        content = call_kwargs["messages"][0]["content"]
        assert len(content) == 2
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_extract_page_raises_on_no_image_and_no_text():
    fake_client = MagicMock()
    with patch.object(llm_client, "_get_client", return_value=fake_client):
        with pytest.raises(ValueError):
            llm_client.extract_page(image_png=b"", text_layer=None)


def test_extract_page_missing_api_key_fails_fast_without_retry_backoff():
    import time
    from app.core.config import settings

    original_key = settings.gemini_api_key
    settings.gemini_api_key = ""
    llm_client._client = None
    try:
        start = time.monotonic()
        with pytest.raises(llm_client.ConfigurationError):
            llm_client.extract_page(image_png=b"fake", text_layer=None)
        elapsed = time.monotonic() - start
    finally:
        settings.gemini_api_key = original_key
        llm_client._client = None

    assert elapsed < 1.0, f"missing API key should fail immediately, took {elapsed:.2f}s (retry backoff not skipped)"
