"""FastAPI entrypoint for the AWIA Warehouse Analyst Control Tower."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status

from odoo_client import OdooClientError, OdooWarehouseClient

logger = logging.getLogger(__name__)

app = FastAPI(
    title="AWIA Warehouse Analyst Control Tower",
    description=(
        "Read-oriented API layer for the Agentic Warehouse Inventory Analyst "
        "connected to Odoo. Operational mutations remain outside this API until "
        "they are protected by explicit human-in-the-loop approval workflows."
    ),
    version="0.2.0",
)


@lru_cache(maxsize=1)
def get_odoo_client() -> OdooWarehouseClient:
    """Create and cache the process-wide Odoo client from environment settings."""
    return OdooWarehouseClient.from_env()


def _odoo_unavailable(exc: OdooClientError) -> HTTPException:
    logger.exception("Odoo request failed: %s", exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Odoo is unavailable or rejected the request. Check server and credentials.",
    )


def _fetch_records(fetcher: Callable[..., list[dict[str, Any]]], limit: int) -> dict[str, Any]:
    try:
        records = fetcher(limit=limit)
    except OdooClientError as exc:
        raise _odoo_unavailable(exc) from exc

    return {"count": len(records), "records": records}


@app.get("/", tags=["system"])
def root() -> dict[str, Any]:
    return {
        "service": "AWIA Warehouse Analyst Control Tower",
        "version": app.version,
        "status": "running",
        "links": {
            "health": "/health",
            "odoo_health": "/health/odoo",
            "docs": "/docs",
        },
    }


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Process health check that does not require Odoo connectivity."""
    return {"status": "ok", "service": "awia-api"}


@app.get("/health/odoo", tags=["system"])
def odoo_health(
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    """Verify authentication and read access to Odoo's product model."""
    try:
        readable = client.check_read_access("product.product")
    except OdooClientError as exc:
        raise _odoo_unavailable(exc) from exc

    if not readable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Odoo connection succeeded but product.product read access was denied.",
        )

    return {
        "status": "ok",
        "odoo": "connected",
        "database": client.database,
        "uid": client.uid,
    }


@app.get("/api/products", tags=["warehouse"])
def products(
    limit: int = Query(default=100, ge=1, le=5000),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    return _fetch_records(client.fetch_products, limit)


@app.get("/api/inventory", tags=["warehouse"])
def inventory(
    limit: int = Query(default=100, ge=1, le=5000),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    return _fetch_records(client.fetch_stock_quants, limit)


@app.get("/api/moves", tags=["warehouse"])
def stock_moves(
    limit: int = Query(default=100, ge=1, le=5000),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    return _fetch_records(client.fetch_stock_move_lines, limit)


@app.get("/api/manufacturing-orders", tags=["manufacturing"])
def manufacturing_orders(
    limit: int = Query(default=100, ge=1, le=5000),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    return _fetch_records(client.fetch_manufacturing_orders, limit)
