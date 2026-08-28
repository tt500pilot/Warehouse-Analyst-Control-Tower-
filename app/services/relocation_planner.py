"""Dependency-aware, read-only relocation planning for AWIA slotting candidates.

This module converts logical product-to-bin recommendations into an operational
transformation plan. It reads live Odoo quants, models incumbent displacement,
tracks reservation conflicts, detects primary-move dependency cycles, assigns
empty fallback bins for displaced non-candidate SKUs, and estimates total move
legs/units/weight/distance.

It never writes Odoo. A returned plan is advisory and is intentionally blocked
from execution while reservations, capacity exceptions, unresolved displacement,
or staging requirements remain.
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from app.services.slotting_feasibility import (
    _live_quants_for_locations,
    _m2o_id,
    _product_index,
    build_live_location_index,
)
from app.services.slotting_optimizer import load_slotting_product_metadata
from app.services.virtual_picker import Geometry, shortest_path
from odoo_client import OdooWarehouseClient


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sku_eligible(metadata: dict[str, Any], location: dict[str, Any]) -> bool:
    zone = str(location.get("zone") or "").upper()
    if bool(metadata.get("secure_required")) and not _truthy(location.get("secure")):
        return False
    if bool(metadata.get("flight_critical")):
        if not _truthy(location.get("flight_critical_allowed")):
            return False
        if zone == "BULK":
            return False
    return True


def _weight(metadata: dict[str, Any], quantity: float) -> float:
    return float(metadata.get("weight_lb") or 0.0) * quantity


def _build_live_state(
    client: OdooWarehouseClient,
    geometry: Geometry,
    product_metadata: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    live_locations = build_live_location_index(client)
    mapped_tails = sorted(set(live_locations) & set(geometry.locations_by_tail))
    location_ids = [int(live_locations[tail]["id"]) for tail in mapped_tails]
    quants = _live_quants_for_locations(client, location_ids)
    product_ids = {
        product_id
        for row in quants
        for product_id in [_m2o_id(row.get("product_id"))]
        if product_id is not None
    }
    product_by_id = _product_index(client, product_ids)
    tail_by_location_id = {
        int(live_locations[tail]["id"]): tail
        for tail in mapped_tails
    }

    bins: dict[str, dict[str, Any]] = {}
    for tail in mapped_tails:
        geom = geometry.locations_by_tail[tail]
        bins[tail] = {
            "tail": tail,
            "live_odoo_location_id": int(live_locations[tail]["id"]),
            "zone": str(geom.get("zone") or ""),
            "level": int(geom.get("level") or 1),
            "pick_tier": geom.get("pick_tier"),
            "graph_node_id": str(geom.get("graph_node_id") or ""),
            "capacity_units": float(geom.get("capacity_units") or 0.0),
            "capacity_weight_lb": float(geom.get("capacity_weight_lb") or 0.0),
            "secure": _truthy(geom.get("secure")),
            "flight_critical_allowed": _truthy(geom.get("flight_critical_allowed")),
            "occupants": {},
            "total_units": 0.0,
            "total_weight_lb": 0.0,
            "unknown_weight_units": 0.0,
        }

    product_positions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for quant in quants:
        location_id = _m2o_id(quant.get("location_id"))
        product_id = _m2o_id(quant.get("product_id"))
        tail = tail_by_location_id.get(location_id or -1)
        product = product_by_id.get(product_id or -1, {})
        code = str(product.get("default_code") or "").strip()
        quantity = float(quant.get("quantity") or 0.0)
        reserved = float(quant.get("reserved_quantity") or 0.0)
        if not tail or not code or quantity <= 0:
            continue
        metadata = product_metadata.get(code, {})
        unit_weight = float(metadata.get("weight_lb") or 0.0)
        entry = bins[tail]["occupants"].setdefault(
            code,
            {
                "product_code": code,
                "quantity": 0.0,
                "reserved_quantity": 0.0,
                "weight_lb": 0.0,
                "weight_known": unit_weight > 0,
                "quant_rows": 0,
                "lot_ids": set(),
            },
        )
        entry["quantity"] += quantity
        entry["reserved_quantity"] += reserved
        entry["weight_lb"] += unit_weight * quantity
        entry["weight_known"] = bool(entry["weight_known"] and unit_weight > 0)
        entry["quant_rows"] += 1
        lot_id = _m2o_id(quant.get("lot_id"))
        if lot_id is not None:
            entry["lot_ids"].add(lot_id)
        bins[tail]["total_units"] += quantity
        if unit_weight > 0:
            bins[tail]["total_weight_lb"] += unit_weight * quantity
        else:
            bins[tail]["unknown_weight_units"] += quantity

    for tail, bin_row in bins.items():
        for code, occupant in bin_row["occupants"].items():
            occupant["quantity"] = round(float(occupant["quantity"]), 3)
            occupant["reserved_quantity"] = round(float(occupant["reserved_quantity"]), 3)
            occupant["weight_lb"] = round(float(occupant["weight_lb"]), 3)
            occupant["lot_ids"] = sorted(occupant["lot_ids"])
            product_positions[code].append(
                {
                    "tail": tail,
                    "quantity": occupant["quantity"],
                    "reserved_quantity": occupant["reserved_quantity"],
                    "weight_lb": occupant["weight_lb"],
                    "weight_known": occupant["weight_known"],
                    "lot_ids": list(occupant["lot_ids"]),
                }
            )
        bin_row["total_units"] = round(float(bin_row["total_units"]), 3)
        bin_row["total_weight_lb"] = round(float(bin_row["total_weight_lb"]), 3)
        bin_row["unknown_weight_units"] = round(float(bin_row["unknown_weight_units"]), 3)

    current_capacity_violations: list[dict[str, Any]] = []
    for tail, bin_row in bins.items():
        unit_over = bin_row["capacity_units"] > 0 and bin_row["total_units"] > bin_row["capacity_units"]
        weight_known = bin_row["unknown_weight_units"] == 0
        weight_over = (
            bin_row["capacity_weight_lb"] > 0
            and weight_known
            and bin_row["total_weight_lb"] > bin_row["capacity_weight_lb"]
        )
        if unit_over or weight_over or not weight_known:
            current_capacity_violations.append(
                {
                    "location": tail,
                    "total_units": bin_row["total_units"],
                    "capacity_units": bin_row["capacity_units"],
                    "total_weight_lb": bin_row["total_weight_lb"],
                    "capacity_weight_lb": bin_row["capacity_weight_lb"],
                    "weight_known": weight_known,
                    "unit_over_capacity": unit_over,
                    "weight_over_capacity": weight_over,
                }
            )
    return bins, dict(product_positions), current_capacity_violations


def _distance_between(geometry: Geometry, source_tail: str, target_tail: str) -> float:
    source = geometry.locations_by_tail[source_tail]
    target = geometry.locations_by_tail[target_tail]
    distance, _ = shortest_path(
        geometry.adjacency,
        str(source["graph_node_id"]),
        str(target["graph_node_id"]),
    )
    return float(distance)


def _fallback_zone_penalty(metadata: dict[str, Any], zone: str) -> float:
    zone = zone.upper()
    if bool(metadata.get("secure_required")):
        return 0.0 if zone == "CONTROLLED" else 10000.0
    velocity = str(metadata.get("velocity_profile") or "MEDIUM").upper()
    preferred = {
        "HIGH": {"FAST": 0.0, "STANDARD": 120.0, "CONTROLLED": 180.0, "BULK": 300.0},
        "MEDIUM": {"STANDARD": 0.0, "FAST": 30.0, "CONTROLLED": 90.0, "BULK": 160.0},
        "LOW": {"BULK": 0.0, "STANDARD": 25.0, "FAST": 100.0, "CONTROLLED": 160.0},
    }
    return preferred.get(velocity, preferred["MEDIUM"]).get(zone, 500.0)


def _choose_empty_fallback(
    *,
    code: str,
    source_tail: str,
    quantity: float,
    metadata: dict[str, Any],
    bins: dict[str, dict[str, Any]],
    geometry: Geometry,
    excluded_tails: set[str],
) -> tuple[str | None, list[str]]:
    blockers: list[str] = []
    unit_weight = float(metadata.get("weight_lb") or 0.0)
    if quantity > 0 and unit_weight <= 0:
        blockers.append("product_weight_unknown")
        return None, blockers

    candidates: list[tuple[float, str]] = []
    for tail, bin_row in bins.items():
        if tail in excluded_tails:
            continue
        if bin_row["total_units"] > 0:
            continue
        geom = geometry.locations_by_tail[tail]
        if not _sku_eligible(metadata, geom):
            continue
        if float(bin_row["capacity_units"]) <= 0 or quantity > float(bin_row["capacity_units"]):
            continue
        if float(bin_row["capacity_weight_lb"]) <= 0 or quantity * unit_weight > float(bin_row["capacity_weight_lb"]):
            continue
        distance = _distance_between(geometry, source_tail, tail)
        score = distance + _fallback_zone_penalty(metadata, str(bin_row["zone"]))
        if int(bin_row["level"]) >= 2:
            score += 12.0
        candidates.append((score, tail))
    if not candidates:
        blockers.append("no_empty_eligible_fallback_bin")
        return None, blockers
    return min(candidates, key=lambda item: (item[0], item[1]))[1], blockers


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in graph.get(node, set()):
            if neighbor not in indices:
                visit(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbor])
        if lowlink[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            result.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return result


def _topological_primary_order(graph: dict[str, set[str]], cycle_nodes: set[str]) -> list[str]:
    dependencies = {
        node: {dep for dep in deps if dep not in cycle_nodes}
        for node, deps in graph.items()
        if node not in cycle_nodes
    }
    reverse: dict[str, set[str]] = defaultdict(set)
    for node, deps in dependencies.items():
        for dep in deps:
            reverse[dep].add(node)
    queue = deque(sorted(node for node, deps in dependencies.items() if not deps))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in sorted(reverse.get(node, set())):
            dependencies[child].discard(node)
            if not dependencies[child] and child not in order and child not in queue:
                queue.append(child)
    return order


def build_relocation_plan(
    client: OdooWarehouseClient,
    *,
    recommendations: list[dict[str, Any]],
    geometry: Geometry,
    data_dir: str | Path,
) -> dict[str, Any]:
    product_metadata = load_slotting_product_metadata(Path(data_dir))
    bins, product_positions, current_capacity_violations = _build_live_state(
        client,
        geometry,
        product_metadata,
    )

    primary_targets = {
        str(row["product_code"]): str(row["candidate_location"])
        for row in recommendations
        if row.get("product_code") and row.get("candidate_location")
    }
    target_tails = set(primary_targets.values())
    primary_moves: list[dict[str, Any]] = []
    dependency_graph: dict[str, set[str]] = {code: set() for code in primary_targets}
    displacement_moves: list[dict[str, Any]] = []
    unresolved_displacements: list[dict[str, Any]] = []
    fallback_bins_used: set[str] = set()
    reserved_move_codes: set[str] = set()
    capacity_blocked_primary: set[str] = set()

    # First, model full-stock consolidation for each optimized SKU.
    for code in sorted(primary_targets):
        target_tail = primary_targets[code]
        positions = product_positions.get(code, [])
        total_qty = sum(float(row["quantity"]) for row in positions)
        total_reserved = sum(float(row["reserved_quantity"]) for row in positions)
        metadata = product_metadata.get(code, {})
        total_weight = _weight(metadata, total_qty)
        target_bin = bins.get(target_tail)
        blockers: list[str] = []
        if target_bin is None:
            blockers.append("target_missing_from_live_state")
        else:
            if not _sku_eligible(metadata, geometry.locations_by_tail[target_tail]):
                blockers.append("target_policy_ineligible")
            if total_qty > float(target_bin["capacity_units"]):
                blockers.append("full_stock_unit_capacity")
            if total_qty > 0 and float(metadata.get("weight_lb") or 0.0) <= 0:
                blockers.append("full_stock_weight_unknown")
            elif total_weight > float(target_bin["capacity_weight_lb"]):
                blockers.append("full_stock_weight_capacity")
        if any(blocker.startswith("full_stock_") or blocker.startswith("target_") for blocker in blockers):
            capacity_blocked_primary.add(code)
        if total_reserved > 0:
            reserved_move_codes.add(code)
        moving_positions = [row for row in positions if row["tail"] != target_tail]
        primary_moves.append(
            {
                "product_code": code,
                "target_location": target_tail,
                "source_locations": positions,
                "full_on_hand_quantity": round(total_qty, 3),
                "reserved_quantity": round(total_reserved, 3),
                "estimated_weight_lb": round(total_weight, 3),
                "already_has_stock_at_target": any(row["tail"] == target_tail for row in positions),
                "move_legs_required": len(moving_positions),
                "blockers": blockers,
            }
        )

    # Second, inspect every final target and plan incumbent displacement.
    for incoming_code, target_tail in sorted(primary_targets.items()):
        target_bin = bins.get(target_tail)
        if target_bin is None:
            continue
        for incumbent_code, incumbent in sorted(target_bin["occupants"].items()):
            if incumbent_code == incoming_code:
                continue
            quantity = float(incumbent["quantity"])
            if quantity <= 0:
                continue
            reserved = float(incumbent["reserved_quantity"])
            if incumbent_code in primary_targets and primary_targets[incumbent_code] != target_tail:
                dependency_graph[incoming_code].add(incumbent_code)
                if reserved > 0:
                    reserved_move_codes.add(incumbent_code)
                continue

            metadata = product_metadata.get(incumbent_code, {})
            excluded = target_tails | fallback_bins_used | {target_tail}
            fallback, fallback_blockers = _choose_empty_fallback(
                code=incumbent_code,
                source_tail=target_tail,
                quantity=quantity,
                metadata=metadata,
                bins=bins,
                geometry=geometry,
                excluded_tails=excluded,
            )
            if fallback is None:
                unresolved_displacements.append(
                    {
                        "blocking_target_for": incoming_code,
                        "incumbent_product_code": incumbent_code,
                        "source_location": target_tail,
                        "quantity": round(quantity, 3),
                        "reserved_quantity": round(reserved, 3),
                        "blockers": fallback_blockers,
                    }
                )
                continue
            fallback_bins_used.add(fallback)
            distance = _distance_between(geometry, target_tail, fallback)
            displacement_moves.append(
                {
                    "blocking_target_for": incoming_code,
                    "product_code": incumbent_code,
                    "source_location": target_tail,
                    "target_location": fallback,
                    "quantity": round(quantity, 3),
                    "reserved_quantity": round(reserved, 3),
                    "estimated_weight_lb": round(_weight(metadata, quantity), 3),
                    "graph_distance_ft": round(distance, 3),
                    "requires_reservation_handling": reserved > 0,
                    "reason": "clear_occupied_candidate_target",
                }
            )
            if reserved > 0:
                reserved_move_codes.add(incumbent_code)

    components = _strongly_connected_components(dependency_graph)
    cycles = [
        component
        for component in components
        if len(component) > 1
        or (len(component) == 1 and component[0] in dependency_graph.get(component[0], set()))
    ]
    cycle_nodes = {node for component in cycles for node in component}
    ordered_primary_codes = _topological_primary_order(dependency_graph, cycle_nodes)

    # Estimate implementation effort for primary moves excluding stock already at target.
    primary_by_code = {row["product_code"]: row for row in primary_moves}
    primary_move_legs: list[dict[str, Any]] = []
    for code in sorted(primary_targets):
        target_tail = primary_targets[code]
        metadata = product_metadata.get(code, {})
        for position in product_positions.get(code, []):
            source_tail = str(position["tail"])
            if source_tail == target_tail:
                continue
            distance = _distance_between(geometry, source_tail, target_tail)
            primary_move_legs.append(
                {
                    "product_code": code,
                    "source_location": source_tail,
                    "target_location": target_tail,
                    "quantity": round(float(position["quantity"]), 3),
                    "reserved_quantity": round(float(position["reserved_quantity"]), 3),
                    "estimated_weight_lb": round(_weight(metadata, float(position["quantity"])), 3),
                    "graph_distance_ft": round(distance, 3),
                    "requires_reservation_handling": float(position["reserved_quantity"]) > 0,
                    "primary_blockers": list(primary_by_code[code]["blockers"]),
                }
            )

    all_legs = primary_move_legs + displacement_moves
    total_units = sum(float(row["quantity"]) for row in all_legs)
    total_weight = sum(float(row["estimated_weight_lb"]) for row in all_legs)
    total_distance = sum(float(row["graph_distance_ft"]) for row in all_legs)
    reservation_affected_legs = sum(1 for row in all_legs if row.get("requires_reservation_handling"))

    blockers: list[str] = []
    if current_capacity_violations:
        blockers.append("current_state_capacity_violations_present")
    if capacity_blocked_primary:
        blockers.append("candidate_full_stock_capacity_failures")
    if unresolved_displacements:
        blockers.append("unresolved_incumbent_displacements")
    if cycles:
        blockers.append("primary_dependency_cycles_require_staging")
    if reserved_move_codes:
        blockers.append("reserved_inventory_requires_native_reservation_workflow")

    return {
        "mode": "read_only_relocation_plan",
        "odoo_mutated": False,
        "scope": {
            "primary_candidate_products": len(primary_targets),
            "candidate_target_bins": len(target_tails),
            "full_on_hand_consolidation_modeled": True,
            "holdout_reservations_preserved": True,
        },
        "summary": {
            "primary_move_legs": len(primary_move_legs),
            "displacement_move_legs": len(displacement_moves),
            "total_projected_move_legs": len(all_legs),
            "projected_units_moved": round(total_units, 3),
            "projected_weight_lb_moved": round(total_weight, 3),
            "projected_graph_distance_ft": round(total_distance, 3),
            "reservation_affected_move_legs": reservation_affected_legs,
            "reservation_affected_product_codes": sorted(reserved_move_codes),
            "candidate_products_with_full_stock_capacity_blockers": sorted(capacity_blocked_primary),
            "occupied_targets_resolved_with_fallback": len(displacement_moves),
            "unresolved_displacements": len(unresolved_displacements),
            "dependency_edges": sum(len(value) for value in dependency_graph.values()),
            "dependency_cycles": len(cycles),
            "temporary_staging_required": bool(cycles),
            "current_state_capacity_violations": len(current_capacity_violations),
        },
        "dependency_graph": {key: sorted(value) for key, value in sorted(dependency_graph.items()) if value},
        "dependency_cycles": cycles,
        "acyclic_primary_move_order": ordered_primary_codes,
        "fallback_bins_used": sorted(fallback_bins_used),
        "primary_moves": primary_moves,
        "primary_move_legs": primary_move_legs,
        "displacement_moves": displacement_moves,
        "unresolved_displacements": unresolved_displacements,
        "current_state_capacity_violations": current_capacity_violations,
        "execution_gate": {
            "safe_to_execute": False,
            "blockers": blockers,
            "requires_temporary_staging_selection_for_cycles": bool(cycles),
            "requires_native_unreserve_reassign_plan": bool(reserved_move_codes),
            "requires_capacity_exception_resolution": bool(capacity_blocked_primary or current_capacity_violations),
            "requires_relocation_cost_roi": True,
            "requires_human_approval": True,
        },
        "methodology": {
            "candidate_skus": "Model full positive on-hand consolidation into each candidate slot, not merely holdout demand.",
            "incumbents": "If an incumbent has its own candidate target, create a dependency; otherwise move it to an empty eligible fallback bin.",
            "fallback_policy": "Use empty non-candidate bins only; enforce secure/flight-critical policy and unit/weight capacity.",
            "cycles": "Detect strongly connected primary dependencies; do not fabricate an executable sequence until a temporary staging slot is selected.",
            "reservations": "Never move reserved inventory in simulation without flagging the need for native Odoo unreserve/reassignment.",
        },
    }
