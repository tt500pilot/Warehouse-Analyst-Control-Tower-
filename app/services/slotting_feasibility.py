"""Read-only physical/operational feasibility checks for AWIA slotting candidates.

This service does not mutate Odoo. It validates candidate slotting recommendations
against live Odoo bin occupancy plus deterministic sandbox capacity metadata.
It also makes reservation conflicts explicit: open assigned holdout transfers are
already reserved at current source locations, so relocating those units requires
a separate native Odoo unreserve/reassign workflow after human approval.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.services.slotting_optimizer import load_slotting_product_metadata
from app.services.virtual_picker import Geometry, location_tail
from odoo_client import OdooWarehouseClient


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int) and not isinstance(value[0], bool):
        return int(value[0])
    return None


def _available_fields(client: OdooWarehouseClient, model: str, wanted: tuple[str, ...]) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in wanted if field in available]


def _location_name(row: dict[str, Any]) -> str:
    for field in ("complete_name", "display_name", "name"):
        value = row.get(field)
        if value:
            return str(value)
    return ""


def build_live_location_index(client: OdooWarehouseClient) -> dict[str, dict[str, Any]]:
    fields = _available_fields(
        client,
        "stock.location",
        ("id", "complete_name", "display_name", "name", "barcode", "usage"),
    )
    rows = client.search_read(
        "stock.location",
        domain=[["usage", "=", "internal"]],
        fields=fields,
        limit=5000,
        order="id asc",
    )
    by_tail: dict[str, dict[str, Any]] = {}
    duplicates: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        tail = location_tail(_location_name(row))
        row_id = row.get("id")
        if not tail or not isinstance(row_id, int) or isinstance(row_id, bool):
            continue
        if tail in by_tail:
            duplicates[tail].append(int(row_id))
            continue
        by_tail[tail] = row
    if duplicates:
        raise RuntimeError(f"Duplicate live Odoo bin tails prevent deterministic mapping: {dict(duplicates)}")
    return by_tail


def _product_index(client: OdooWarehouseClient, product_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not product_ids:
        return {}
    fields = _available_fields(
        client,
        "product.product",
        ("id", "default_code", "name", "tracking"),
    )
    rows = client.search_read(
        "product.product",
        domain=[["id", "in", sorted(product_ids)]],
        fields=fields,
        limit=5000,
        order="id asc",
    )
    return {
        int(row["id"]): row
        for row in rows
        if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)
    }


def _live_quants_for_locations(
    client: OdooWarehouseClient,
    location_ids: list[int],
) -> list[dict[str, Any]]:
    if not location_ids:
        return []
    fields = _available_fields(
        client,
        "stock.quant",
        ("id", "product_id", "location_id", "quantity", "reserved_quantity", "lot_id"),
    )
    return client.search_read(
        "stock.quant",
        domain=[["location_id", "in", location_ids], ["quantity", ">", 0]],
        fields=fields,
        limit=10000,
        order="location_id asc, product_id asc, id asc",
    )


def _holdout_demand(
    holdout_reservations_by_picking: dict[int, list[dict[str, Any]]],
) -> tuple[dict[str, float], dict[str, set[str]]]:
    required: dict[str, float] = defaultdict(float)
    source_tails: dict[str, set[str]] = defaultdict(set)
    for reservations in holdout_reservations_by_picking.values():
        for row in reservations:
            code = str(row.get("product_code") or "").strip()
            if not code:
                continue
            required[code] += float(row.get("quantity") or 0.0)
            tail = str(row.get("location_tail") or "").strip()
            if tail:
                source_tails[code].add(tail)
    return dict(required), source_tails


def audit_slotting_candidate(
    client: OdooWarehouseClient,
    *,
    recommendations: list[dict[str, Any]],
    holdout_reservations_by_picking: dict[int, list[dict[str, Any]]],
    geometry: Geometry,
    data_dir: Any,
) -> dict[str, Any]:
    live_locations = build_live_location_index(client)
    product_metadata = load_slotting_product_metadata(data_dir)
    required_by_code, source_tails_by_code = _holdout_demand(holdout_reservations_by_picking)

    target_tails = sorted(
        {
            str(row.get("candidate_location") or "")
            for row in recommendations
            if row.get("candidate_location")
        }
    )
    missing_targets = [tail for tail in target_tails if tail not in live_locations]
    target_location_ids = [
        int(live_locations[tail]["id"])
        for tail in target_tails
        if tail in live_locations
    ]
    quants = _live_quants_for_locations(client, target_location_ids)
    product_ids = {
        product_id
        for row in quants
        for product_id in [_m2o_id(row.get("product_id"))]
        if product_id is not None
    }
    product_by_id = _product_index(client, product_ids)

    quants_by_location: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for quant in quants:
        location_id = _m2o_id(quant.get("location_id"))
        if location_id is not None:
            quants_by_location[location_id].append(quant)

    rows: list[dict[str, Any]] = []
    for recommendation in recommendations:
        code = str(recommendation.get("product_code") or "")
        target_tail = str(recommendation.get("candidate_location") or "")
        target_geom = geometry.locations_by_tail.get(target_tail)
        live_target = live_locations.get(target_tail)
        required_qty = float(required_by_code.get(code, 0.0))
        metadata = product_metadata.get(code, {})
        unit_weight_lb = float(metadata.get("weight_lb") or 0.0)

        if target_geom is None or live_target is None:
            rows.append(
                {
                    "product_code": code,
                    "candidate_location": target_tail,
                    "target_found_in_geometry": target_geom is not None,
                    "target_found_in_odoo": live_target is not None,
                    "holdout_required_qty": required_qty,
                    "feasible": False,
                    "blockers": ["target_location_missing"],
                }
            )
            continue

        location_id = int(live_target["id"])
        incumbent_rows: list[dict[str, Any]] = []
        incumbent_units = 0.0
        incumbent_weight_lb = 0.0
        unknown_incumbent_weight = False
        incumbent_other_products = 0
        for quant in quants_by_location.get(location_id, []):
            quantity = float(quant.get("quantity") or 0.0)
            reserved_quantity = float(quant.get("reserved_quantity") or 0.0)
            product_id = _m2o_id(quant.get("product_id"))
            product = product_by_id.get(product_id or -1, {})
            incumbent_code = str(product.get("default_code") or "").strip() or None
            incumbent_meta = product_metadata.get(incumbent_code or "", {})
            incumbent_unit_weight = float(incumbent_meta.get("weight_lb") or 0.0)
            if quantity > 0 and incumbent_unit_weight <= 0:
                unknown_incumbent_weight = True
            incumbent_units += quantity
            incumbent_weight_lb += quantity * incumbent_unit_weight
            if incumbent_code and incumbent_code != code and quantity > 0:
                incumbent_other_products += 1
            incumbent_rows.append(
                {
                    "product_code": incumbent_code,
                    "quantity": quantity,
                    "reserved_quantity": reserved_quantity,
                    "lot_id": _m2o_id(quant.get("lot_id")),
                }
            )

        capacity_units = float(target_geom.get("capacity_units") or 0.0)
        capacity_weight_lb = float(target_geom.get("capacity_weight_lb") or 0.0)
        projected_units = incumbent_units + required_qty
        projected_weight_lb = incumbent_weight_lb + required_qty * unit_weight_lb
        units_capacity_pass = capacity_units > 0 and projected_units <= capacity_units
        weight_capacity_known = unit_weight_lb > 0 and not unknown_incumbent_weight
        weight_capacity_pass = (
            capacity_weight_lb > 0
            and weight_capacity_known
            and projected_weight_lb <= capacity_weight_lb
        )
        exclusive_bin_ready = incumbent_other_products == 0
        already_at_candidate = target_tail in source_tails_by_code.get(code, set())
        reservation_reallocation_required = required_qty > 0 and not already_at_candidate

        blockers: list[str] = []
        if not units_capacity_pass:
            blockers.append("unit_capacity")
        if not weight_capacity_known:
            blockers.append("weight_capacity_unknown")
        elif not weight_capacity_pass:
            blockers.append("weight_capacity")
        if not exclusive_bin_ready:
            blockers.append("target_occupied_by_other_product")
        if reservation_reallocation_required:
            blockers.append("open_holdout_reservations_at_current_sources")

        rows.append(
            {
                "product_code": code,
                "candidate_location": target_tail,
                "candidate_zone": target_geom.get("zone"),
                "candidate_level": int(target_geom.get("level") or 1),
                "live_odoo_location_id": location_id,
                "holdout_required_qty": round(required_qty, 3),
                "current_reserved_source_locations": sorted(source_tails_by_code.get(code, set())),
                "reservation_reallocation_required": reservation_reallocation_required,
                "incumbent_positive_quant_rows": incumbent_rows,
                "incumbent_units": round(incumbent_units, 3),
                "incumbent_weight_lb_estimate": round(incumbent_weight_lb, 3),
                "incumbent_other_product_count": incumbent_other_products,
                "exclusive_bin_ready": exclusive_bin_ready,
                "capacity_units": capacity_units,
                "projected_units_for_holdout_test": round(projected_units, 3),
                "unit_capacity_pass": units_capacity_pass,
                "capacity_weight_lb": capacity_weight_lb,
                "candidate_unit_weight_lb": unit_weight_lb,
                "projected_weight_lb_for_holdout_test": round(projected_weight_lb, 3),
                "weight_capacity_known": weight_capacity_known,
                "weight_capacity_pass": weight_capacity_pass,
                "physical_capacity_pass": units_capacity_pass and weight_capacity_pass,
                "feasible_without_reservation_changes": not blockers,
                "blockers": blockers,
            }
        )

    physically_feasible = [row for row in rows if row.get("physical_capacity_pass") and row.get("exclusive_bin_ready")]
    reservation_conflicts = [row for row in rows if row.get("reservation_reallocation_required")]
    occupied_conflicts = [row for row in rows if not row.get("exclusive_bin_ready", False)]
    capacity_conflicts = [
        row
        for row in rows
        if not row.get("physical_capacity_pass", False)
    ]

    return {
        "mode": "read_only_feasibility_audit",
        "odoo_mutated": False,
        "targets": len(rows),
        "target_locations_missing_in_odoo": missing_targets,
        "physically_feasible_exclusive_targets": len(physically_feasible),
        "reservation_reallocation_conflicts": len(reservation_conflicts),
        "occupied_target_conflicts": len(occupied_conflicts),
        "capacity_conflicts_or_unknown": len(capacity_conflicts),
        "safe_to_execute_relocations_now": False,
        "reason_not_safe_now": (
            "Open holdout transfers are already assigned/reserved at current source bins. "
            "Candidate relocations remain advisory until occupancy/capacity checks pass and a "
            "native Odoo reservation-release/reassignment plan is explicitly approved."
        ),
        "rows": rows,
        "next_gate": {
            "require_all_target_locations_mapped": True,
            "require_physical_capacity": True,
            "prefer_exclusive_pick_face_bins": True,
            "require_native_reservation_reallocation_plan": True,
            "require_relocation_cost_roi_model": True,
            "require_human_approval": True,
        },
    }
