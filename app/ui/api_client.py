"""HTTP client helpers used by the AWIA Streamlit control tower."""

from __future__ import annotations

from typing import Any, Mapping

import httpx


class ControlTowerAPIError(RuntimeError):
    """Raised when the Streamlit UI cannot retrieve data from the AWIA API."""


def normalize_base_url(base_url: str) -> str:
    """Return a normalized HTTP(S) FastAPI base URL."""
    cleaned = (base_url or "").strip().rstrip("/")
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError("API URL must start with http:// or https://")
    return cleaned


def get_json(
    base_url: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """GET JSON from the AWIA FastAPI service with user-facing error messages."""
    base = normalize_base_url(base_url)
    endpoint = f"{base}/{path.lstrip('/')}"
    try:
        response = httpx.get(endpoint, params=params, timeout=timeout_seconds)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ControlTowerAPIError(
            f"AWIA API timed out while requesting {path}."
        ) from exc
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            payload = exc.response.json()
            detail = str(payload.get("detail") or "")
        except (ValueError, AttributeError):
            pass
        suffix = f" Detail: {detail}" if detail else ""
        raise ControlTowerAPIError(
            f"AWIA API returned HTTP {exc.response.status_code} for {path}.{suffix}"
        ) from exc
    except httpx.RequestError as exc:
        raise ControlTowerAPIError(
            f"Cannot reach the AWIA API at {base}. Start FastAPI on port 8000 first."
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ControlTowerAPIError(
            f"AWIA API returned a non-JSON response for {path}."
        ) from exc

    if not isinstance(payload, dict):
        raise ControlTowerAPIError(
            f"AWIA API returned an unexpected response shape for {path}."
        )
    return payload
