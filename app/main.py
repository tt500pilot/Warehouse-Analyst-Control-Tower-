"""FastAPI entrypoint for the AWIA Warehouse Analyst Control Tower."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status

from app.services.inventory_health import analyze_inventory_health, build_cycle_count_plan
from app.services.kitting_baseline import analyze_kitting_baseline
from odoo_client import OdooClientError, OdooWarehouseClient

logger = logging.getLogger(__name__)

app = FastAPI(
    title="AWIA Warehouse Analyst Control Tower",
    description=(
        "Read-oriented API layer for the Agentic Warehouse Inventory Analyst "
        "connected to Odoo. Operational mutations remain outside this API until "
        "they are protected by explicit human-in-the-loop approval workflows."
    ),
    version="0.4.0",
)

ANALYSIS_PRODUCT_FIELDS = (
    "id",
    "default_code",
    "name",
    "standard_price",
    "tracking",
    "x_is_flight_critical",
)

KITTING_PICKING_FIELDS = (
    "id",
    "name",
    "origin",
    "picking_type_id",
    "location_id",
    "location_dest_id",
    "create_date",
    "scheduled_date",
    "date_done",
    "state",
    "user_id",
)

KITTING_MOVE_FIELDS = (
    "id",
    "picking_id",
    "product_id",
    "location_id",
    "location_dest_id",
    "quantity",
    "qty_done",
    "date",
    "state",
    "write_uid",
)


@lru_cache(maxsize=1)
def get_odoo_client() -> OdooWarehouseClient:
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


def _build_inventory_health_report(client: OdooWarehouseClient, *, source_limit: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    move_cutoff = (now - timedelta(days=28)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        products = client.fetch_products(domain=[["active", "=", True]], fields=ANALYSIS_PRODUCT_FIELDS, limit=source_limit)
        quants = client.fetch_stock_quants(domain=[["quantity", "!=", 0]], limit=source_limit)
        moves = client.fetch_stock_move_lines(domain=[["date", ">=", move_cutoff]], limit=source_limit)
    except OdooClientError as exc:
        raise _odoo_unavailable(exc) from exc

    report = analyze_inventory_health(products, quants, moves, as_of=now)
    report["source_snapshot"] = {
        "products": len(products),
        "quants": len(quants),
        "move_lines_28d": len(moves),
        "source_limit_per_model": source_limit,
        "truncated_possible": any(len(records) >= source_limit for records in (products, quants, moves)),
    }
    return report


def _build_kitting_baseline_report(
    client: OdooWarehouseClient,
    *,
    source_limit: int,
    picking_type_contains: str,
) -> dict[str, Any]:
    try:
        picking_fields = client._resolve_fields("stock.picking", KITTING_PICKING_FIELDS, KITTING_PICKING_FIELDS)
        move_fields = client._resolve_fields("stock.move.line", KITTING_MOVE_FIELDS, KITTING_MOVE_FIELDS)
        pickings = client.search_read(
            "stock.picking",
            domain=[["state", "=", "done"]],
            fields=picking_fields,
            limit=source_limit,
            order="date_done desc, id desc",
        )
        picking_ids = [record["id"] for record in pickings if isinstance(record.get("id"), int)]
        moves = []
        if picking_ids:
            moves = client.search_read(
                "stock.move.line",
                domain=[["picking_id", "in", picking_ids]],
                fields=move_fields,
                limit=source_limit,
                order="date asc, id asc",
            )
    except OdooClientError as exc:
        raise _odoo_unavailable(exc) from exc

    report = analyze_kitting_baseline(
        pickings,
        moves,
        picking_type_contains=picking_type_contains,
    )
    report["source_snapshot"] = {
        "done_pickings": len(pickings),
        "move_lines": len(moves),
        "source_limit_per_model": source_limit,
        "truncated_possible": len(pickings) >= source_limit or len(moves) >= source_limit,
    }
    return report


@app.get("/", tags=["system"])
def root() -> dict[str, Any]:
    return {
        "service": "AWIA Warehouse Analyst Control Tower",
        "version": app.version,
        "status": "running",
        "links": {
            "health": "/health",
            "odoo_health": "/health/odoo",
            "inventory_health": "/api/inventory-health",
            "cycle_count_plan": "/api/cycle-count-plan",
            "kitting_baseline": "/api/kitting-baseline",
            "docs": "/docs",
        },
    }


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "awia-api"}


@app.get("/health/odoo", tags=["system"])
def odoo_health(client: OdooWarehouseClient = Depends(get_odoo_client)) -> dict[str, Any]:
    try:
        readable = client.check_read_access("product.product")
    except OdooClientError as exc:
        raise _odoo_unavailable(exc) from exc
    if not readable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Odoo connection succeeded but product.product read access was denied.")
    return {"status": "ok", "odoo": "connected", "database": client.database, "uid": client.uid}


@app.get("/api/products", tags=["warehouse"])
def products(limit: int = Query(default=100, ge=1, le=5000), client: OdooWarehouseClient = Depends(get_odoo_client)) -> dict[str, Any]:
    return _fetch_records(client.fetch_products, limit)


@app.get("/api/inventory", tags=["warehouse"])
def inventory(limit: int = Query(default=100, ge=1, le=5000), client: OdooWarehouseClient = Depends(get_odoo_client)) -> dict[str, Any]:
    return _fetch_records(client.fetch_stock_quants, limit)


@app.get("/api/moves", tags=["warehouse"])
def stock_moves(limit: int = Query(default=100, ge=1, le=5000), client: OdooWarehouseClient = Depends(get_odoo_client)) -> dict[str, Any]:
    return _fetch_records(client.fetch_stock_move_lines, limit)


@app.get("/api/manufacturing-orders", tags=["manufacturing"])
def manufacturing_orders(limit: int = Query(default=100, ge=1, le=5000), client: OdooWarehouseClient = Depends(get_odoo_client)) -> dict[str, Any]:
    return _fetch_records(client.fetch_manufacturing_orders, limit)


@app.get("/api/kitting-baseline", tags=["manufacturing", "analytics"])
def kitting_baseline(
    source_limit: int = Query(default=5000, ge=100, le=20000),
    picking_type_contains: str = Query(default="Pick Components", min_length=0, max_length=100),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    return _build_kitting_baseline_report(
        client,
        source_limit=source_limit,
        picking_type_contains=picking_type_contains,
    )


@app.get("/api/inventory-health", tags=["module-a"])
def inventory_health(limit: int = Query(default=100, ge=1, le=1000), source_limit: int = Query(default=5000, ge=100, le=20000), client: OdooWarehouseClient = Depends(get_odoo_client)) -> dict[str, Any]:
    report = _build_inventory_health_report(client, source_limit=source_limit)
    report["items"] = report["items"][:limit]
    report["returned_items"] = len(report["items"])
    return report


@app.get("/api/cycle-count-plan", tags=["module-a"])
def cycle_count_plan(limit: int = Query(default=50, ge=1, le=500), source_limit: int = Query(default=5000, ge=100, le=20000), client: OdooWarehouseClient = Depends(get_odoo_client)) -> dict[str, Any]:
    report = _build_inventory_health_report(client, source_limit=source_limit)
    plan = build_cycle_count_plan(report["items"], limit=limit)
    return {"generated_at": report["generated_at"], "summary": report["summary"], "source_snapshot": report["source_snapshot"], **plan}
