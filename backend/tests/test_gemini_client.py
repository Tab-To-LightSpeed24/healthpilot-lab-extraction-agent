import json
from unittest.mock import MagicMock, patch

import pytest

from app.services import gemini_client


def _fake_model(return_json):
    model = MagicMock()
    model.generate_content.return_value = MagicMock(text=json.dumps(return_json))
    return model


def test_extract_page_text_only_input_does_not_send_empty_image_part():
    """Reproduces the plain-text-report code path where pdf_utils.load_pages
    returns image_png=b'' (no rasterized page). extract_page must not send a
    broken empty inline_data image part to the API in that case."""
    with patch.object(gemini_client, "_ensure_configured"), \
         patch.object(gemini_client.genai, "GenerativeModel") as mock_model_cls:
        mock_model_cls.return_value = _fake_model({"tests": [], "page_notes": None})

        gemini_client.extract_page(image_png=b"", text_layer="Iron 70 ug/dL")

        call_args = mock_model_cls.return_value.generate_content.call_args
        contents = call_args[0][0]
        assert len(contents) == 1, "text-only call must not include an image part"
        assert "Iron 70 ug/dL" in contents[0]


def test_extract_page_with_image_includes_both_prompt_and_image_part():
    with patch.object(gemini_client, "_ensure_configured"), \
         patch.object(gemini_client.genai, "GenerativeModel") as mock_model_cls:
        mock_model_cls.return_value = _fake_model({"tests": [], "page_notes": None})

        gemini_client.extract_page(image_png=b"\x89PNGfakebytes", text_layer="some text")

        contents = mock_model_cls.return_value.generate_content.call_args[0][0]
        assert len(contents) == 2
        assert contents[1] == {"mime_type": "image/png", "data": b"\x89PNGfakebytes"}


def test_extract_page_raises_on_no_image_and_no_text():
    with patch.object(gemini_client, "_ensure_configured"):
        with pytest.raises(ValueError):
            gemini_client.extract_page(image_png=b"", text_layer=None)


def test_extract_page_missing_api_key_fails_fast_without_retry_backoff():
    import time
    from app.core.config import settings

    original_key = settings.gemini_api_key
    settings.gemini_api_key = ""
    gemini_client._configured = False
    try:
        start = time.monotonic()
        with pytest.raises(gemini_client.ConfigurationError):
            gemini_client.extract_page(image_png=b"fake", text_layer=None)
        elapsed = time.monotonic() - start
    finally:
        settings.gemini_api_key = original_key

    assert elapsed < 1.0, f"missing API key should fail immediately, took {elapsed:.2f}s (retry backoff not skipped)"
