"""Read-only aisle-level slotting advisor using validated AWIA geometry.

This service is intentionally conservative. It evaluates products currently
stored in a mapped aisle and recommends only moves into bins that were empty at
the start of the analysis. It does not displace incumbents, chain relocations,
or write Odoo.

Benefit is reported as gross independent-touch graph-distance potential from
observed source->anchor operational moves. That is not a route simulation or a
labor-savings claim. Location access height/offset are reported separately.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

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


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    if isinstance(value, str):
        return value
    return ""


def _number(value: Any) -> float:
    if value in (None, False, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _relative(values: Mapping[int, float]) -> dict[int, float]:
    maximum = max(values.values(), default=0.0)
    if maximum <= 0:
        return {key: 0.0 for key in values}
    return {key: max(0.0, value) / maximum for key, value in values.items()}


def _position_key(row: Record) -> tuple[float, float, float, str]:
    access = row.get("access_geometry") or {}
    return (
        float(row.get("graph_distance_to_access_ft") or 0.0),
        float(access.get("vertical_reach_ft") or row.get("vertical_reach_ft") or 0.0),
        float(access.get("horizontal_offset_ft") or row.get("horizontal_access_offset_ft") or 0.0),
        str(row.get("complete_name") or ""),
    )


def _pareto_better(candidate: Record, source: Record) -> bool:
    cand = _position_key(candidate)[:3]
    current = _position_key(source)[:3]
    no_worse = all(left <= right for left, right in zip(cand, current))
    strictly_better = any(left < right for left, right in zip(cand, current))
    return no_worse and strictly_better


def analyze_aisle_slotting(
    geometry: Record,
    products: Iterable[Record],
    quants: Iterable[Record],
    moves: Iterable[Record],
    *,
    bom_lines: Iterable[Record] = (),
    lookback_days: int = 90,
) -> dict[str, Any]:
    if geometry.get("schema_version") != "awia-warehouse-geometry-v1":
        raise ValueError("Unsupported or missing canonical geometry schema_version")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be greater than zero")

    product_rows = list(products)
    quant_rows = list(quants)
    move_rows = list(moves)
    bom_rows = list(bom_lines)
    products_by_id = {
        product_id: row
        for row in product_rows
        for product_id in [_m2o_id(row.get("id"))]
        if product_id is not None
    }

    storage_locations = [
        dict(row) for row in geometry.get("locations", []) if row.get("record_type") == "storage_bin"
    ]
    if not storage_locations:
        raise ValueError("Canonical geometry has no storage bins")
    distance_by_id = {
        int(row["odoo_location_id"]): dict(row)
        for row in geometry.get("anchor_distances", [])
        if row.get("odoo_location_id") is not None
    }
    location_by_id: dict[int, dict[str, Any]] = {}
    for location in storage_locations:
        location_id = location.get("odoo_location_id")
        if location_id is None:
            continue
        merged = dict(location)
        merged.update(distance_by_id.get(int(location_id), {}))
        location_by_id[int(location_id)] = merged

    mapped_ids = set(location_by_id)
    anchor_name = str((geometry.get("anchor") or {}).get("complete_name") or "")

    # Current state from live quants. Positive quantity means the bin was
    # occupied at the beginning of the analysis; those bins are never used as
    # candidate targets in this conservative first slice.
    by_product_location: dict[int, dict[int, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"quantity": 0.0, "reserved": 0.0})
    )
    bin_total_qty: dict[int, float] = defaultdict(float)
    for quant in quant_rows:
        product_id = _m2o_id(quant.get("product_id"))
        location_id = _m2o_id(quant.get("location_id"))
        if product_id is None or location_id not in mapped_ids:
            continue
        quantity = _number(quant.get("quantity"))
        reserved = max(_number(quant.get("reserved_quantity")), 0.0)
        by_product_location[product_id][location_id]["quantity"] += quantity
        by_product_location[product_id][location_id]["reserved"] += reserved
        if quantity > 0:
            bin_total_qty[location_id] += quantity

    current_product_ids = {
        product_id
        for product_id, positions in by_product_location.items()
        if any(values["quantity"] > 0 for values in positions.values())
    }
    initially_empty_ids = sorted(mapped_ids - {loc for loc, qty in bin_total_qty.items() if qty > 0})

    touches: dict[int, int] = defaultdict(int)
    moved_qty: dict[int, float] = defaultdict(float)
    for move in move_rows:
        product_id = _m2o_id(move.get("product_id"))
        source_id = _m2o_id(move.get("location_id"))
        destination_name = _m2o_name(move.get("location_dest_id"))
        if product_id not in current_product_ids or source_id not in mapped_ids:
            continue
        if anchor_name and destination_name != anchor_name:
            continue
        touches[product_id] += 1
        moved_qty[product_id] += abs(_number(move.get("quantity")) or _number(move.get("qty_done")))

    bom_occurrences: dict[int, int] = defaultdict(int)
    for line in bom_rows:
        product_id = _m2o_id(line.get("product_id"))
        if product_id in current_product_ids:
            bom_occurrences[product_id] += 1

    touch_rel = _relative({pid: float(touches[pid]) for pid in current_product_ids})
    qty_rel = _relative({pid: float(moved_qty[pid]) for pid in current_product_ids})
    bom_rel = _relative({pid: float(bom_occurrences[pid]) for pid in current_product_ids})

    profiles: list[dict[str, Any]] = []
    for product_id in sorted(current_product_ids):
        product = products_by_id.get(product_id, {})
        positions = [
            {
                "location_id": loc_id,
                "quantity": round(values["quantity"], 3),
                "reserved_quantity": round(values["reserved"], 3),
                "location": location_by_id[loc_id],
            }
            for loc_id, values in by_product_location[product_id].items()
            if values["quantity"] > 0
        ]
        positions.sort(key=lambda row: row["location"]["complete_name"])
        score_components = {
            "operational_touch_frequency": round(70.0 * touch_rel.get(product_id, 0.0), 3),
            "bom_relevance": round(20.0 * bom_rel.get(product_id, 0.0), 3),
            "operational_quantity": round(10.0 * qty_rel.get(product_id, 0.0), 3),
        }
        profiles.append(
            {
                "product_id": product_id,
                "product_code": str(product.get("default_code") or "") or None,
                "product_name": str(product.get("name") or _m2o_name(positions[0].get("product_id")) if positions else ""),
                "tracking": str(product.get("tracking") or "none"),
                "flight_critical_known": "x_is_flight_critical" in product,
                "flight_critical": bool(product.get("x_is_flight_critical")) if "x_is_flight_critical" in product else None,
                "operational_touches_to_anchor": touches[product_id],
                "operational_quantity_to_anchor": round(moved_qty[product_id], 3),
                "bom_component_occurrences": bom_occurrences[product_id],
                "priority_score": round(sum(score_components.values()), 2),
                "score_components": score_components,
                "positions": positions,
                "total_on_hand_in_scope": round(sum(row["quantity"] for row in positions), 3),
                "total_reserved_in_scope": round(sum(row["reserved_quantity"] for row in positions), 3),
            }
        )

    profiles.sort(key=lambda row: (-row["priority_score"], row.get("product_code") or "", row["product_id"]))
    available_targets = sorted(
        (location_by_id[location_id] for location_id in initially_empty_ids),
        key=_position_key,
    )
    used_targets: set[int] = set()
    recommendations: list[dict[str, Any]] = []
    not_recommended: list[dict[str, Any]] = []

    for profile in profiles:
        positions = profile["positions"]
        if len(positions) != 1:
            not_recommended.append(
                {
                    "product_code": profile["product_code"],
                    "reason": "product_currently_spans_multiple_mapped_bins",
                    "mapped_position_count": len(positions),
                }
            )
            continue
        source_position = positions[0]
        source = source_position["location"]
        candidates = [
            target
            for target in available_targets
            if int(target["odoo_location_id"]) not in used_targets
            and _pareto_better(target, source)
        ]
        if not candidates:
            not_recommended.append(
                {
                    "product_code": profile["product_code"],
                    "reason": "no_initially_empty_bin_pareto_dominates_current_position",
                }
            )
            continue
        target = min(candidates, key=_position_key)
        target_id = int(target["odoo_location_id"])
        used_targets.add(target_id)

        source_distance = float(source.get("graph_distance_to_access_ft") or 0.0)
        target_distance = float(target.get("graph_distance_to_access_ft") or 0.0)
        source_access = source.get("access_geometry") or {}
        target_access = target.get("access_geometry") or {}
        distance_saved = max(source_distance - target_distance, 0.0)
        vertical_reduction = max(
            float(source_access.get("vertical_reach_ft") or 0.0)
            - float(target_access.get("vertical_reach_ft") or 0.0),
            0.0,
        )
        horizontal_reduction = max(
            float(source_access.get("horizontal_offset_ft") or 0.0)
            - float(target_access.get("horizontal_offset_ft") or 0.0),
            0.0,
        )
        blockers: list[str] = ["human_approval_required"]
        if profile["total_reserved_in_scope"] > 0:
            blockers.append("live_reservations_require_controlled_release_or_reassignment")
        if profile["tracking"].lower() in {"lot", "serial"}:
            blockers.append("lot_or_serial_traceability_requires_verified_relocation_workflow")
        measurement_statuses = set(geometry.get("summary", {}).get("measurement_statuses") or [])
        if measurement_statuses != {"FIELD_VERIFIED"}:
            blockers.append("physical_capacity_and_geometry_not_field_verified")
        if not profile["flight_critical_known"]:
            blockers.append("flight_critical_status_not_available_in_odoo_subset")

        recommendations.append(
            {
                "rank": 0,
                "product_id": profile["product_id"],
                "product_code": profile["product_code"],
                "product_name": profile["product_name"],
                "tracking": profile["tracking"],
                "priority_score": profile["priority_score"],
                "score_components": profile["score_components"],
                "operational_touches_to_anchor": profile["operational_touches_to_anchor"],
                "bom_component_occurrences": profile["bom_component_occurrences"],
                "on_hand_quantity": profile["total_on_hand_in_scope"],
                "reserved_quantity": profile["total_reserved_in_scope"],
                "source": {
                    "odoo_location_id": source["odoo_location_id"],
                    "complete_name": source["complete_name"],
                    "graph_distance_to_anchor_ft": source_distance,
                    "vertical_reach_ft": float(source_access.get("vertical_reach_ft") or 0.0),
                    "horizontal_access_offset_ft": float(source_access.get("horizontal_offset_ft") or 0.0),
                },
                "candidate": {
                    "odoo_location_id": target["odoo_location_id"],
                    "complete_name": target["complete_name"],
                    "graph_distance_to_anchor_ft": target_distance,
                    "vertical_reach_ft": float(target_access.get("vertical_reach_ft") or 0.0),
                    "horizontal_access_offset_ft": float(target_access.get("horizontal_offset_ft") or 0.0),
                    "was_empty_at_analysis_start": True,
                },
                "modeled_improvement": {
                    "graph_distance_saved_per_independent_touch_ft": round(distance_saved, 3),
                    "gross_independent_touch_distance_potential_ft": round(
                        distance_saved * profile["operational_touches_to_anchor"], 3
                    ),
                    "vertical_reach_reduction_ft": round(vertical_reduction, 3),
                    "horizontal_access_offset_reduction_ft": round(horizontal_reduction, 3),
                },
                "execution_blockers": blockers,
                "safe_to_execute": False,
            }
        )

    recommendations.sort(
        key=lambda row: (
            -float(row["modeled_improvement"]["gross_independent_touch_distance_potential_ft"]),
            -float(row["priority_score"]),
            str(row.get("product_code") or ""),
        )
    )
    for index, row in enumerate(recommendations, start=1):
        row["rank"] = index

    total_gross = sum(
        float(row["modeled_improvement"]["gross_independent_touch_distance_potential_ft"])
        for row in recommendations
    )
    return {
        "mode": "read_only_mapped_aisle_slotting_advisor",
        "odoo_mutated": False,
        "safe_to_execute": False,
        "geometry_schema_version": geometry.get("schema_version"),
        "anchor": geometry.get("anchor"),
        "lookback_days": lookback_days,
        "methodology": {
            "scope": "products currently stored in the mapped aisle only",
            "candidate_targets": "bins empty at analysis start only; no displacement or chain moves",
            "priority_score": "70% relative observed source-to-anchor touch frequency + 20% relative BOM occurrence + 10% relative moved quantity",
            "location_improvement": "candidate must Pareto-dominate source on graph distance, vertical reach, and horizontal access offset; no tradeoff weights are assumed",
            "benefit": "gross independent-touch graph-distance potential; not multi-stop route or labor savings",
        },
        "summary": {
            "mapped_storage_bins": len(location_by_id),
            "initially_occupied_bins": len(mapped_ids) - len(initially_empty_ids),
            "initially_empty_bins": len(initially_empty_ids),
            "current_products_in_scope": len(current_product_ids),
            "recommendations": len(recommendations),
            "gross_independent_touch_distance_potential_ft": round(total_gross, 3),
            "products_with_live_reservations": sum(1 for row in profiles if row["total_reserved_in_scope"] > 0),
            "tracked_products": sum(1 for row in profiles if row["tracking"].lower() in {"lot", "serial"}),
        },
        "recommendations": recommendations,
        "not_recommended": not_recommended,
        "profiles": profiles,
        "guardrails": [
            "No Odoo writes are performed.",
            "Recommendations do not prove capacity feasibility or relocation economics.",
            "Gross independent-touch distance is not equivalent to actual multi-stop picker-route savings.",
            "Reservations and lot/serial traceability require controlled execution planning.",
            "Flight-critical policy cannot be enforced when the approved Odoo subset does not expose that attribute.",
        ],
    }
