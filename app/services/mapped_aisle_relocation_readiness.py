"""Read-only relocation readiness gate for mapped-aisle slotting recommendations.

This service sits after geometry-aware slotting and matched route validation.
It verifies live Odoo quantities/reservations/traceability against the proposed
source/target pair, applies product-weight metadata to target capacity, and
summarizes completed-route benefit that is attributable to each recommendation.

It never writes Odoo and never declares a move executable solely from modeled
travel benefit. Geometry/capacity provenance, reservations, traceability,
material handling, and human approval remain explicit gates.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from app.services.warehouse_geometry import build_adjacency, shortest_path

Record = Mapping[str, Any]


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, int) and not isinstance(first, bool) and first > 0:
            return int(first)
    return None


def _number(value: Any) -> float:
    if value in (None, False, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def analyze_relocation_readiness(
    geometry: Record,
    slotting_result: Record,
    route_validation: Record,
    products: Iterable[Record],
    quants: Iterable[Record],
    *,
    product_metadata: Mapping[str, Mapping[str, Any]],
    walking_speed_ft_s: float = 3.5,
) -> dict[str, Any]:
    if walking_speed_ft_s <= 0:
        raise ValueError("walking_speed_ft_s must be positive")
    if geometry.get("schema_version") != "awia-warehouse-geometry-v1":
        raise ValueError("Unsupported or missing canonical geometry schema_version")

    recommendations = list(slotting_result.get("recommendations") or [])
    completed = route_validation.get("completed_historical_validation") or {}
    per_picking = list(completed.get("per_picking") or [])
    primary = route_validation.get("primary_result") or completed.get("result") or {}

    products_by_id = {
        product_id: row
        for row in products
        for product_id in [_m2o_id(row.get("id"))]
        if product_id is not None
    }
    locations_by_id = {
        int(row["odoo_location_id"]): row
        for row in geometry.get("locations", [])
        if row.get("odoo_location_id") is not None
    }
    node_rows = list((geometry.get("graph") or {}).get("nodes") or [])
    edge_rows = list((geometry.get("graph") or {}).get("edges") or [])
    adjacency = build_adjacency(node_rows, edge_rows)

    quant_by_product_location: dict[tuple[int, int], dict[str, Any]] = defaultdict(
        lambda: {
            "quantity": 0.0,
            "reserved_quantity": 0.0,
            "traced_quantity": 0.0,
            "anonymous_quantity": 0.0,
            "lot_ids": set(),
            "quant_rows": 0,
        }
    )
    total_by_location: dict[int, float] = defaultdict(float)
    for quant in quants:
        product_id = _m2o_id(quant.get("product_id"))
        location_id = _m2o_id(quant.get("location_id"))
        if product_id is None or location_id is None:
            continue
        quantity = _number(quant.get("quantity"))
        if quantity <= 0:
            continue
        reserved = max(_number(quant.get("reserved_quantity")), 0.0)
        lot_id = _m2o_id(quant.get("lot_id"))
        state = quant_by_product_location[(product_id, location_id)]
        state["quantity"] += quantity
        state["reserved_quantity"] += reserved
        state["quant_rows"] += 1
        if lot_id is not None:
            state["traced_quantity"] += quantity
            state["lot_ids"].add(lot_id)
        else:
            state["anonymous_quantity"] += quantity
        total_by_location[location_id] += quantity

    attributed_completed_savings: dict[int, float] = defaultdict(float)
    ambiguous_shared_savings = 0.0
    for picking in per_picking:
        affected = [int(value) for value in picking.get("affected_recommended_product_ids") or []]
        savings = _number(picking.get("distance_saved_ft"))
        if len(affected) == 1:
            attributed_completed_savings[affected[0]] += savings
        elif len(affected) > 1:
            ambiguous_shared_savings += savings

    measurement_statuses = set(geometry.get("summary", {}).get("measurement_statuses") or [])
    geometry_field_verified = measurement_statuses == {"FIELD_VERIFIED"}

    rows: list[dict[str, Any]] = []
    capacity_screened_product_ids: set[int] = set()
    for recommendation in recommendations:
        product_id = int(recommendation["product_id"])
        code = str(recommendation.get("product_code") or "")
        product = products_by_id.get(product_id, {})
        metadata = dict(product_metadata.get(code, {}))
        source = recommendation.get("source") or {}
        target = recommendation.get("candidate") or {}
        source_id = int(source["odoo_location_id"])
        target_id = int(target["odoo_location_id"])
        source_location = locations_by_id.get(source_id)
        target_location = locations_by_id.get(target_id)
        if source_location is None or target_location is None:
            raise ValueError(f"Recommendation {code!r} references location outside canonical geometry")

        live_source = quant_by_product_location[(product_id, source_id)]
        live_target_product = quant_by_product_location[(product_id, target_id)]
        target_total_live = total_by_location.get(target_id, 0.0)
        quantity = float(live_source["quantity"])
        reserved = float(live_source["reserved_quantity"])
        unit_weight = _number(metadata.get("weight_lb"))
        total_weight = quantity * unit_weight if unit_weight > 0 else None
        target_capacity_units = target_location.get("capacity_units")
        target_capacity_weight = target_location.get("capacity_weight_lb")

        unit_capacity_known = target_capacity_units not in (None, "")
        weight_capacity_known = target_capacity_weight not in (None, "")
        unit_capacity_pass = (
            quantity <= float(target_capacity_units) if unit_capacity_known else None
        )
        weight_capacity_pass = (
            total_weight <= float(target_capacity_weight)
            if weight_capacity_known and total_weight is not None
            else None
        )
        target_empty_now = target_total_live == 0.0
        capacity_screen_pass = bool(
            target_empty_now
            and unit_capacity_pass is True
            and weight_capacity_pass is True
        )
        if capacity_screen_pass:
            capacity_screened_product_ids.add(product_id)

        tracking = str(product.get("tracking") or recommendation.get("tracking") or "none").lower()
        traced_qty = float(live_source["traced_quantity"])
        anonymous_qty = float(live_source["anonymous_quantity"])
        full_traceability = tracking not in {"lot", "serial"} or (
            quantity > 0 and anonymous_qty == 0 and traced_qty >= quantity
        )

        source_node = str(source_location.get("graph_node_id") or "")
        target_node = str(target_location.get("graph_node_id") or "")
        relocation_distance, relocation_path = shortest_path(
            adjacency, source_node, target_node
        )

        blockers = ["human_approval_required", "material_handling_method_not_defined"]
        if not target_empty_now:
            blockers.append("target_no_longer_empty")
        if unit_capacity_pass is not True:
            blockers.append("target_unit_capacity_not_proven")
        if weight_capacity_pass is not True:
            blockers.append("target_weight_capacity_not_proven")
        if reserved > 0:
            blockers.append("live_reservations_require_controlled_release_or_reassignment")
        if tracking in {"lot", "serial"} and not full_traceability:
            blockers.append("tracked_stock_not_fully_lot_or_serial_identified")
        elif tracking in {"lot", "serial"}:
            blockers.append("lot_or_serial_relocation_workflow_required")
        if not geometry_field_verified:
            blockers.append("geometry_and_capacity_not_field_verified")
        if not metadata:
            blockers.append("approved_product_physical_metadata_missing")

        route_saved = round(attributed_completed_savings.get(product_id, 0.0), 3)
        rows.append(
            {
                "rank": recommendation.get("rank"),
                "product_id": product_id,
                "product_code": code,
                "product_name": recommendation.get("product_name"),
                "tracking": tracking,
                "live_source": {
                    "location": source_location.get("complete_name"),
                    "quantity": round(quantity, 3),
                    "reserved_quantity": round(reserved, 3),
                    "traced_quantity": round(traced_qty, 3),
                    "anonymous_quantity": round(anonymous_qty, 3),
                    "lot_ids": sorted(live_source["lot_ids"]),
                    "quant_rows": int(live_source["quant_rows"]),
                },
                "live_target": {
                    "location": target_location.get("complete_name"),
                    "total_live_quantity_all_products": round(target_total_live, 3),
                    "same_product_quantity": round(float(live_target_product["quantity"]), 3),
                    "empty_now": target_empty_now,
                },
                "physical_load": {
                    "unit_weight_lb": round(unit_weight, 3) if unit_weight > 0 else None,
                    "relocation_quantity": round(quantity, 3),
                    "estimated_total_weight_lb": round(total_weight, 3) if total_weight is not None else None,
                    "metadata_source": "simulation_fixture" if metadata else "missing",
                },
                "target_capacity_screen": {
                    "capacity_units": target_capacity_units,
                    "projected_units": round(quantity, 3),
                    "unit_capacity_pass": unit_capacity_pass,
                    "capacity_weight_lb": target_capacity_weight,
                    "projected_weight_lb": round(total_weight, 3) if total_weight is not None else None,
                    "weight_capacity_pass": weight_capacity_pass,
                    "screen_pass": capacity_screen_pass,
                    "capacity_provenance": "canonical geometry intake; MOCK_FIXTURE remains untrusted for real execution"
                    if not geometry_field_verified
                    else "field_verified_geometry_intake",
                },
                "relocation_geometry": {
                    "source_graph_node": source_node,
                    "target_graph_node": target_node,
                    "legal_transfer_distance_ft": round(relocation_distance, 3),
                    "path_nodes": relocation_path,
                },
                "completed_route_benefit": {
                    "attributable_modeled_aisle_subroute_distance_saved_ft": route_saved,
                    "walking_only_equivalent_seconds": round(route_saved / walking_speed_ft_s, 2),
                    "note": "Walking-only equivalent is not observed labor time and excludes handling/setup.",
                },
                "execution_blockers": blockers,
                "capacity_screen_pass": capacity_screen_pass,
                "safe_to_execute": False,
            }
        )

    capacity_screened_savings = 0.0
    for picking in per_picking:
        affected = [int(value) for value in picking.get("affected_recommended_product_ids") or []]
        if affected and all(product_id in capacity_screened_product_ids for product_id in affected):
            capacity_screened_savings += _number(picking.get("distance_saved_ft"))

    baseline = _number(primary.get("baseline_total_distance_ft"))
    capacity_candidate = baseline - capacity_screened_savings
    return {
        "mode": "read_only_mapped_aisle_relocation_readiness",
        "odoo_mutated": False,
        "safe_to_execute": False,
        "walking_speed_ft_s_for_equivalent_only": walking_speed_ft_s,
        "summary": {
            "recommendations_evaluated": len(rows),
            "capacity_screen_passed": len(capacity_screened_product_ids),
            "capacity_screen_failed": len(rows) - len(capacity_screened_product_ids),
            "completed_baseline_aisle_subroute_distance_ft": round(baseline, 3),
            "all_recommendations_completed_route_saved_ft": round(
                _number(primary.get("modeled_distance_saved_ft")), 3
            ),
            "capacity_screened_subset_route_saved_ft": round(capacity_screened_savings, 3),
            "capacity_screened_subset_candidate_distance_ft": round(capacity_candidate, 3),
            "capacity_screened_subset_reduction_pct": round(
                capacity_screened_savings / baseline * 100.0, 2
            )
            if baseline
            else None,
            "ambiguous_shared_completed_savings_ft": round(ambiguous_shared_savings, 3),
        },
        "recommendations": rows,
        "guardrails": [
            "Capacity screening uses supplied product physical metadata; simulation fixture metadata is not a production source of truth.",
            "A capacity-screen pass does not mean execution-ready: reservations, traceability, handling method, geometry provenance, and approval remain separate gates.",
            "Route benefit is a modeled mapped-aisle subroute, not observed labor time or whole-warehouse kit travel.",
            "No Odoo writes are performed.",
        ],
    }
