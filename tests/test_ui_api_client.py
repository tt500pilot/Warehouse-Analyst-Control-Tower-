"""Unit tests for Streamlit API client helpers."""

import pytest

from app.ui.api_client import normalize_base_url


def test_normalize_base_url_removes_trailing_slash() -> None:
    assert normalize_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"


def test_normalize_base_url_requires_http_scheme() -> None:
    with pytest.raises(ValueError):
        normalize_base_url("127.0.0.1:8000")
