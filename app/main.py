"""FastAPI entrypoint for the AWIA Warehouse Analyst Control Tower."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status

from app.services.inventory_health import analyze_inventory_health, build_cycle_count_plan
from app.services.kitting_baseline import analyze_kitting_baseline
from app.services.kitting_transactions import inspect_kitting_transactions
from app.services.mapping_prioritizer import analyze_mapping_priorities
from app.services.traceability_health import analyze_traceability_health
from odoo_client import OdooClientError, OdooWarehouseClient

logger = logging.getLogger(__name__)

app = FastAPI(
    title="AWIA Warehouse Analyst Control Tower",
    description=(
        "Read-oriented API layer for the Agentic Warehouse Inventory Analyst "
        "connected to Odoo. Operational mutations remain outside this API until "
        "they are protected by explicit human-in-the-loop approval workflows."
    ),
    version="0.7.0",
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

KITTING_BASELINE_MO_FIELDS = (
    "id",
    "name",
    "origin",
    "picking_ids",
)

KITTING_TRANSACTION_MO_FIELDS = (
    "id",
    "name",
    "origin",
    "state",
    "product_id",
    "bom_id",
    "picking_ids",
    "date_start",
    "date_finished",
)

KITTING_TRANSACTION_MOVE_FIELDS = (
    "id",
    "picking_id",
    "product_id",
    "product_uom_qty",
    "quantity",
    "state",
    "picked",
    "has_tracking",
    "location_id",
    "location_dest_id",
)

KITTING_TRANSACTION_LINE_FIELDS = (
    "id",
    "move_id",
    "picking_id",
    "product_id",
    "quantity",
    "picked",
    "tracking",
    "location_id",
    "location_dest_id",
    "state",
    "date",
    "lot_id",
    "lot_name",
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


def _build_mapping_priority_report(
    client: OdooWarehouseClient,
    *,
    source_limit: int,
    lookback_days: int,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        products = client.fetch_products(
            domain=[["active", "=", True]],
            fields=ANALYSIS_PRODUCT_FIELDS,
            limit=source_limit,
        )
        quants = client.fetch_stock_quants(
            domain=[["quantity", "!=", 0]],
            limit=source_limit,
        )
        moves = client.fetch_stock_move_lines(
            domain=[["date", ">=", cutoff]],
            limit=source_limit,
        )
        location_fields = client._resolve_fields(
            "stock.location",
            ("id", "complete_name", "display_name", "name", "usage", "barcode"),
            ("id", "complete_name", "display_name", "name", "usage", "barcode"),
        )
        locations = client.search_read(
            "stock.location",
            domain=[["usage", "=", "internal"]],
            fields=location_fields,
            limit=source_limit,
            order="id asc",
        )
        bom_line_fields = client._resolve_fields(
            "mrp.bom.line",
            ("id", "bom_id", "product_id", "product_qty", "active"),
            ("id", "bom_id", "product_id", "product_qty", "active"),
        )
        bom_lines = client.search_read(
            "mrp.bom.line",
            domain=[],
            fields=bom_line_fields,
            limit=source_limit,
            order="id asc",
        )
    except OdooClientError as exc:
        raise _odoo_unavailable(exc) from exc

    report = analyze_mapping_priorities(
        products,
        quants,
        moves,
        locations,
        bom_lines=bom_lines,
        as_of=now,
        lookback_days=lookback_days,
    )
    report["source_snapshot"] = {
        "products": len(products),
        "quants": len(quants),
        "move_lines": len(moves),
        "locations": len(locations),
        "bom_lines": len(bom_lines),
        "source_limit_per_model": source_limit,
        "truncated_possible": any(
            len(records) >= source_limit
            for records in (products, quants, moves, locations, bom_lines)
        ),
    }
    return report


def _build_traceability_health_report(
    client: OdooWarehouseClient,
    *,
    source_limit: int,
    location_prefix: str,
) -> dict[str, Any]:
    try:
        products = client.fetch_products(
            domain=[["active", "=", True]],
            fields=("id", "default_code", "name", "tracking"),
            limit=source_limit,
        )
        quants = client.fetch_stock_quants(
            domain=[["quantity", ">", 0]],
            limit=source_limit,
        )
    except OdooClientError as exc:
        raise _odoo_unavailable(exc) from exc

    report = analyze_traceability_health(
        products,
        quants,
        location_prefix=location_prefix,
    )
    report["source_snapshot"] = {
        "products": len(products),
        "quants": len(quants),
        "source_limit_per_model": source_limit,
        "truncated_possible": any(len(records) >= source_limit for records in (products, quants)),
    }
    return report


def _build_kitting_baseline_report(
    client: OdooWarehouseClient,
    *,
    source_limit: int,
    origin_prefix: str,
    picking_type_contains: str,
) -> dict[str, Any]:
    try:
        mo_fields = client._resolve_fields(
            "mrp.production", KITTING_BASELINE_MO_FIELDS, KITTING_BASELINE_MO_FIELDS
        )
        picking_fields = client._resolve_fields(
            "stock.picking", KITTING_PICKING_FIELDS, KITTING_PICKING_FIELDS
        )
        move_fields = client._resolve_fields(
            "stock.move.line", KITTING_MOVE_FIELDS, KITTING_MOVE_FIELDS
        )

        manufacturing_orders: list[dict[str, Any]] = []
        picking_domain: list[Any] = [["state", "=", "done"]]
        if origin_prefix:
            manufacturing_orders = client.search_read(
                "mrp.production",
                domain=[["origin", "=ilike", f"{origin_prefix}%"]],
                fields=mo_fields,
                limit=source_limit,
                order="id asc",
            )
            awia_picking_ids = sorted(
                {
                    picking_id
                    for mo in manufacturing_orders
                    for picking_id in (mo.get("picking_ids") or [])
                    if isinstance(picking_id, int) and not isinstance(picking_id, bool)
                }
            )
            picking_domain.append(["id", "in", awia_picking_ids])

        pickings = client.search_read(
            "stock.picking",
            domain=picking_domain,
            fields=picking_fields,
            limit=source_limit,
            order="date_done desc, id desc",
        )
        picking_ids = [
            record["id"]
            for record in pickings
            if isinstance(record.get("id"), int) and not isinstance(record.get("id"), bool)
        ]
        moves: list[dict[str, Any]] = []
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
        "origin_prefix": origin_prefix,
        "manufacturing_orders": len(manufacturing_orders),
        "done_pickings": len(pickings),
        "move_lines": len(moves),
        "source_limit_per_model": source_limit,
        "truncated_possible": any(
            len(records) >= source_limit
            for records in (manufacturing_orders, pickings, moves)
        ),
    }
    return report


def _build_kitting_transaction_report(
    client: OdooWarehouseClient,
    *,
    source_limit: int,
    origin_prefix: str,
    picking_type_contains: str,
) -> dict[str, Any]:
    try:
        mo_fields = client._resolve_fields(
            "mrp.production", KITTING_TRANSACTION_MO_FIELDS, KITTING_TRANSACTION_MO_FIELDS
        )
        picking_fields = client._resolve_fields(
            "stock.picking", KITTING_PICKING_FIELDS, KITTING_PICKING_FIELDS
        )
        move_fields = client._resolve_fields(
            "stock.move", KITTING_TRANSACTION_MOVE_FIELDS, KITTING_TRANSACTION_MOVE_FIELDS
        )
        line_fields = client._resolve_fields(
            "stock.move.line", KITTING_TRANSACTION_LINE_FIELDS, KITTING_TRANSACTION_LINE_FIELDS
        )

        mo_domain: list[Any] = []
        if origin_prefix:
            mo_domain.append(["origin", "=ilike", f"{origin_prefix}%"])
        manufacturing_orders = client.search_read(
            "mrp.production",
            domain=mo_domain,
            fields=mo_fields,
            limit=source_limit,
            order="id asc",
        )

        picking_ids: set[int] = set()
        for mo in manufacturing_orders:
            for picking_id in mo.get("picking_ids") or []:
                if isinstance(picking_id, int) and not isinstance(picking_id, bool):
                    picking_ids.add(picking_id)

        pickings: list[dict[str, Any]] = []
        if picking_ids:
            pickings = client.search_read(
                "stock.picking",
                domain=[["id", "in", sorted(picking_ids)]],
                fields=picking_fields,
                limit=source_limit,
                order="id asc",
            )

        resolved_picking_ids = [
            row["id"]
            for row in pickings
            if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)
        ]
        stock_moves: list[dict[str, Any]] = []
        move_lines: list[dict[str, Any]] = []
        if resolved_picking_ids:
            stock_moves = client.search_read(
                "stock.move",
                domain=[["picking_id", "in", resolved_picking_ids]],
                fields=move_fields,
                limit=source_limit,
                order="picking_id asc, id asc",
            )
            move_lines = client.search_read(
                "stock.move.line",
                domain=[["picking_id", "in", resolved_picking_ids]],
                fields=line_fields,
                limit=source_limit,
                order="picking_id asc, id asc",
            )
    except OdooClientError as exc:
        raise _odoo_unavailable(exc) from exc

    report = inspect_kitting_transactions(
        manufacturing_orders,
        pickings,
        stock_moves,
        move_lines,
        origin_prefix=origin_prefix,
        picking_type_contains=picking_type_contains,
    )
    report["source_snapshot"] = {
        "manufacturing_orders": len(manufacturing_orders),
        "pickings": len(pickings),
        "stock_moves": len(stock_moves),
        "move_lines": len(move_lines),
        "source_limit_per_model": source_limit,
        "truncated_possible": any(
            len(records) >= source_limit
            for records in (manufacturing_orders, pickings, stock_moves, move_lines)
        ),
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
            "mapping_priorities": "/api/mapping-priorities",
            "traceability_health": "/api/traceability-health",
            "kitting_baseline": "/api/kitting-baseline",
            "kitting_transactions": "/api/kitting-transactions",
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


@app.get("/api/mapping-priorities", tags=["warehouse", "analytics"])
def mapping_priorities(
    limit: int = Query(default=10, ge=1, le=100),
    source_limit: int = Query(default=5000, ge=100, le=20000),
    lookback_days: int = Query(default=90, ge=7, le=365),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    report = _build_mapping_priority_report(
        client,
        source_limit=source_limit,
        lookback_days=lookback_days,
    )
    report["areas"] = report["areas"][:limit]
    report["returned_areas"] = len(report["areas"])
    return report


@app.get("/api/traceability-health", tags=["warehouse", "analytics"])
def traceability_health(
    source_limit: int = Query(default=5000, ge=100, le=20000),
    location_prefix: str = Query(default="", min_length=0, max_length=200),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    return _build_traceability_health_report(
        client,
        source_limit=source_limit,
        location_prefix=location_prefix,
    )


@app.get("/api/kitting-transactions", tags=["manufacturing", "analytics"])
def kitting_transactions(
    source_limit: int = Query(default=5000, ge=100, le=20000),
    origin_prefix: str = Query(default="AWIA-MOCK-MO-", min_length=0, max_length=100),
    picking_type_contains: str = Query(default="Pick Components", min_length=0, max_length=100),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    return _build_kitting_transaction_report(
        client,
        source_limit=source_limit,
        origin_prefix=origin_prefix,
        picking_type_contains=picking_type_contains,
    )


@app.get("/api/kitting-baseline", tags=["manufacturing", "analytics"])
def kitting_baseline(
    source_limit: int = Query(default=5000, ge=100, le=20000),
    origin_prefix: str = Query(default="AWIA-MOCK-MO-", min_length=0, max_length=100),
    picking_type_contains: str = Query(default="Pick Components", min_length=0, max_length=100),
    client: OdooWarehouseClient = Depends(get_odoo_client),
) -> dict[str, Any]:
    return _build_kitting_baseline_report(
        client,
        source_limit=source_limit,
        origin_prefix=origin_prefix,
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
