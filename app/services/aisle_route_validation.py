"""Matched route-level validation for mapped-aisle slotting recommendations.

Uses Odoo picking groups plus validated AWIA legal-path geometry to compare the
same modeled transfers before/after advisory slot changes. Completed move lines
(`state == done`) are the primary historical-validation cohort. Open/planned
move lines are reported separately and never mixed into the primary result.

This is still a modeled aisle subroute, not observed human labor or whole-kit
warehouse travel. It improves on independent-touch arithmetic by accounting for
co-picks and shared source nodes within the mapped aisle.
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


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    if isinstance(value, str):
        return value
    return ""


def _route(
    adjacency: Mapping[str, list[tuple[str, float]]],
    *,
    anchor: str,
    required_nodes: set[str],
) -> dict[str, Any]:
    remaining = set(required_nodes)
    current = anchor
    total = 0.0
    legs: list[dict[str, Any]] = []

    while remaining:
        choices: list[tuple[float, str, list[str]]] = []
        for node in sorted(remaining):
            distance, path = shortest_path(adjacency, current, node)
            choices.append((distance, node, path))
        distance, node, path = min(choices, key=lambda item: (item[0], item[1]))
        legs.append(
            {
                "from_node": current,
                "to_node": node,
                "distance_ft": round(distance, 3),
                "path_nodes": path,
            }
        )
        total += distance
        current = node
        remaining.remove(node)

    return_distance, return_path = shortest_path(adjacency, current, anchor)
    legs.append(
        {
            "from_node": current,
            "to_node": anchor,
            "distance_ft": round(return_distance, 3),
            "path_nodes": return_path,
            "return_to_anchor": True,
        }
    )
    total += return_distance
    return {"distance_ft": round(total, 3), "legs": legs}


def _evaluate_grouped(
    grouped: Mapping[int, list[dict[str, Any]]],
    *,
    adjacency: Mapping[str, list[tuple[str, float]]],
    anchor_node: str,
    locations: Mapping[int, Record],
    recommendation_by_product: Mapping[int, dict[str, Any]],
) -> dict[str, Any]:
    per_picking: list[dict[str, Any]] = []
    total_baseline = 0.0
    total_candidate = 0.0
    affected_pickings = 0
    co_pick_overlap_pickings = 0
    recommendation_presence: dict[int, int] = defaultdict(int)
    recommendation_joint_presence: dict[tuple[int, int], int] = defaultdict(int)

    for picking_id in sorted(grouped):
        lines = grouped[picking_id]
        baseline_nodes = {row["source_node"] for row in lines}
        candidate_nodes: set[str] = set()
        affected_products: list[int] = []

        for line in lines:
            recommendation = recommendation_by_product.get(line["product_id"])
            if recommendation is None:
                candidate_nodes.add(line["source_node"])
                continue
            candidate_location_id = int(recommendation["candidate"]["odoo_location_id"])
            candidate_location = locations.get(candidate_location_id)
            if candidate_location is None:
                raise ValueError(
                    f"Recommendation target location {candidate_location_id} is not in canonical geometry"
                )
            candidate_nodes.add(str(candidate_location["graph_node_id"]))
            affected_products.append(line["product_id"])
            recommendation_presence[line["product_id"]] += 1

        unique_affected = sorted(set(affected_products))
        for index, left in enumerate(unique_affected):
            for right in unique_affected[index + 1 :]:
                recommendation_joint_presence[(left, right)] += 1
        if len(unique_affected) > 1:
            co_pick_overlap_pickings += 1

        baseline = _route(adjacency, anchor=anchor_node, required_nodes=baseline_nodes)
        candidate = _route(adjacency, anchor=anchor_node, required_nodes=candidate_nodes)
        savings = float(baseline["distance_ft"]) - float(candidate["distance_ft"])
        if unique_affected:
            affected_pickings += 1
        total_baseline += float(baseline["distance_ft"])
        total_candidate += float(candidate["distance_ft"])
        per_picking.append(
            {
                "picking_id": picking_id,
                "move_lines": len(lines),
                "unique_baseline_nodes": sorted(baseline_nodes),
                "unique_candidate_nodes": sorted(candidate_nodes),
                "affected_recommended_product_ids": unique_affected,
                "baseline_distance_ft": baseline["distance_ft"],
                "candidate_distance_ft": candidate["distance_ft"],
                "distance_saved_ft": round(savings, 3),
                "baseline_route": baseline["legs"],
                "candidate_route": candidate["legs"],
            }
        )

    total_saved = total_baseline - total_candidate
    recommendation_rows = []
    for product_id, recommendation in sorted(
        recommendation_by_product.items(),
        key=lambda item: int(item[1].get("rank") or 999999),
    ):
        recommendation_rows.append(
            {
                "product_id": product_id,
                "product_code": recommendation.get("product_code"),
                "pickings_with_product": recommendation_presence.get(product_id, 0),
                "gross_independent_touch_distance_potential_ft": (
                    recommendation.get("modeled_improvement") or {}
                ).get("gross_independent_touch_distance_potential_ft"),
            }
        )

    co_pick_pairs = [
        {
            "product_ids": [left, right],
            "pickings_together": count,
            "product_codes": [
                recommendation_by_product.get(left, {}).get("product_code"),
                recommendation_by_product.get(right, {}).get("product_code"),
            ],
        }
        for (left, right), count in sorted(
            recommendation_joint_presence.items(), key=lambda item: (-item[1], item[0])
        )
    ]

    return {
        "modeled_pickings": len(per_picking),
        "affected_pickings": affected_pickings,
        "pickings_with_multiple_recommended_products": co_pick_overlap_pickings,
        "result": {
            "baseline_total_distance_ft": round(total_baseline, 3),
            "candidate_total_distance_ft": round(total_candidate, 3),
            "modeled_distance_saved_ft": round(total_saved, 3),
            "modeled_distance_reduction_pct": round(total_saved / total_baseline * 100.0, 2)
            if total_baseline
            else None,
        },
        "recommendation_coverage": recommendation_rows,
        "recommended_product_co_pick_pairs": co_pick_pairs,
        "per_picking": per_picking,
    }


def evaluate_matched_route_impact(
    geometry: Record,
    moves: Iterable[Record],
    recommendations: Iterable[Record],
) -> dict[str, Any]:
    if geometry.get("schema_version") != "awia-warehouse-geometry-v1":
        raise ValueError("Unsupported or missing canonical geometry schema_version")

    anchor = geometry.get("anchor") or {}
    anchor_name = str(anchor.get("complete_name") or "")
    anchor_node = str(anchor.get("graph_node_id") or "")
    if not anchor_name or not anchor_node:
        raise ValueError("Canonical geometry anchor is incomplete")

    node_rows = list((geometry.get("graph") or {}).get("nodes") or [])
    edge_rows = list((geometry.get("graph") or {}).get("edges") or [])
    adjacency = build_adjacency(node_rows, edge_rows)

    locations = {
        int(row["odoo_location_id"]): row
        for row in geometry.get("locations", [])
        if row.get("record_type") == "storage_bin" and row.get("odoo_location_id") is not None
    }
    recommendation_by_product: dict[int, dict[str, Any]] = {}
    for row in recommendations:
        product_id = row.get("product_id")
        candidate = row.get("candidate") or {}
        if product_id is None or candidate.get("odoo_location_id") is None:
            continue
        recommendation_by_product[int(product_id)] = dict(row)

    grouped_done: dict[int, list[dict[str, Any]]] = defaultdict(list)
    grouped_open: dict[int, list[dict[str, Any]]] = defaultdict(list)
    eligible_move_lines = 0
    move_lines_without_picking = 0
    done_move_lines = 0
    open_move_lines = 0
    state_counts: dict[str, int] = defaultdict(int)

    for move in moves:
        source_id = _m2o_id(move.get("location_id"))
        if source_id not in locations:
            continue
        if _m2o_name(move.get("location_dest_id")) != anchor_name:
            continue
        eligible_move_lines += 1
        state = str(move.get("state") or "unknown").strip().lower() or "unknown"
        state_counts[state] += 1
        picking_id = _m2o_id(move.get("picking_id"))
        if picking_id is None:
            move_lines_without_picking += 1
            continue
        product_id = _m2o_id(move.get("product_id"))
        if product_id is None:
            continue
        row = {
            "move_line_id": _m2o_id(move.get("id")),
            "product_id": product_id,
            "source_location_id": source_id,
            "source_node": str(locations[source_id]["graph_node_id"]),
            "state": state,
        }
        if state == "done":
            grouped_done[picking_id].append(row)
            done_move_lines += 1
        else:
            grouped_open[picking_id].append(row)
            open_move_lines += 1

    completed = _evaluate_grouped(
        grouped_done,
        adjacency=adjacency,
        anchor_node=anchor_node,
        locations=locations,
        recommendation_by_product=recommendation_by_product,
    )
    planned = _evaluate_grouped(
        grouped_open,
        adjacency=adjacency,
        anchor_node=anchor_node,
        locations=locations,
        recommendation_by_product=recommendation_by_product,
    )

    return {
        "mode": "matched_mapped_aisle_route_simulation",
        "classification": "modeled_not_observed_human",
        "odoo_mutated": False,
        "anchor": anchor,
        "methodology": {
            "primary_validation_cohort": "completed stock.move.line rows only (state=done)",
            "planned_cohort": "non-done move lines reported separately and excluded from primary historical result",
            "grouping": "Odoo stock.move.line rows grouped by picking_id within each state cohort",
            "scope": "mapped-aisle source lines whose destination is the geometry anchor; this is an aisle subroute, not whole-warehouse kit travel",
            "baseline": "nearest-neighbor route over unique current access nodes, returning to anchor",
            "candidate": "same picking lines and graph, with recommended products remapped to candidate access nodes",
            "important": "shared/co-picked nodes are visited once per modeled picking, preventing independent-touch double counting",
        },
        "coverage": {
            "eligible_move_lines": eligible_move_lines,
            "move_lines_with_picking_id": eligible_move_lines - move_lines_without_picking,
            "move_lines_without_picking_id_excluded": move_lines_without_picking,
            "state_counts": dict(sorted(state_counts.items())),
            "completed_move_lines_used": done_move_lines,
            "planned_or_open_move_lines_separated": open_move_lines,
            "completed_modeled_pickings": completed["modeled_pickings"],
            "planned_modeled_pickings": planned["modeled_pickings"],
        },
        "completed_historical_validation": completed,
        "planned_or_open_simulation": planned,
        "primary_result": completed["result"],
        "guardrails": [
            "Primary result uses completed move lines only; open/planned lines are never mixed into historical validation.",
            "This is a deterministic modeled aisle subroute, not measured picker behavior, labor time, or whole-kit warehouse travel.",
            "Candidate relocation feasibility, capacity, reservations, traceability, and approval remain separate gates.",
        ],
    }
