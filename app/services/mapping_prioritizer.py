"""Odoo-only warehouse mapping prioritization for AWIA.

This module answers a deployment question that comes before XYZ mapping:
which logical storage area is most worth mapping first?

It deliberately does not estimate walking distance or labor savings. Without a
validated physical geometry/path graph, Odoo can support relative opportunity
signals only: operational movement activity, velocity concentration,
reservations, traceability/criticality, BOM relevance, and inventory dispersion.

Inventory-adjustment moves are reported separately and excluded from physical
movement scoring because they can represent bookkeeping, inventory-mode seeding,
or count corrections rather than picker/cart travel.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from math import ceil
from typing import Any, Iterable, Mapping

Record = Mapping[str, Any]
GENERIC_STOCK_CONTAINERS = {"awia mock", "mock", "bins"}


def _number(value: Any) -> float:
    if value in (None, False, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, int) and not isinstance(first, bool) and first > 0:
            return int(first)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        if len(value) > 1 and value[1] not in (None, False):
            return str(value[1])
        if value:
            return str(value[0])
    return "" if value in (None, False) else str(value)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _location_name(row: Record) -> str:
    for key in ("complete_name", "display_name", "name"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _path_parts(location_name: str) -> list[str]:
    return [part.strip() for part in str(location_name).split("/") if part.strip()]


def _is_candidate_storage_location(location: Record) -> bool:
    usage = str(location.get("usage") or "internal").lower()
    if usage != "internal":
        return False
    return any(part.lower() == "stock" for part in _path_parts(_location_name(location)))


def _is_inventory_adjustment_location(value: Any, location_by_id: Mapping[int, Record]) -> bool:
    location_id = _m2o_id(value)
    location = location_by_id.get(location_id or -1, {})
    usage = str(location.get("usage") or "").lower()
    if usage == "inventory":
        return True
    name = (_location_name(location) or _m2o_name(value)).strip().lower()
    return "inventory adjustment" in name or "inventory adjustments" in name


def derive_logical_area(location_name: str) -> tuple[str, str]:
    """Derive a stable pre-geometry mapping area from an Odoo location name."""
    parts = _path_parts(location_name)
    if not parts:
        return "UNMAPPED", "unmapped"

    stock_index = next(
        (index for index, part in enumerate(parts) if part.lower() == "stock"),
        None,
    )
    if stock_index is not None:
        suffix = parts[stock_index + 1 :]
        base_parts = parts[: stock_index + 1]
        if not suffix:
            return "/".join(base_parts), "stock_parent_only"

        first = suffix[0]
        if first.lower() in GENERIC_STOCK_CONTAINERS and len(suffix) >= 2:
            base_parts.append(first)
            token = suffix[1]
            match = re.match(r"^([A-Za-z]+)[-_ ]?\d", token)
            if match:
                return (
                    f"{'/'.join(base_parts)}/Aisle {match.group(1).upper()}",
                    "nested_flat_aisle_prefix",
                )
            return f"{'/'.join(base_parts)}/{token}", "nested_child_below_stock"

        base = "/".join(base_parts)
        if len(suffix) >= 2:
            return f"{base}/{first}", "hierarchy_below_stock"

        match = re.match(r"^([A-Za-z]+)[-_ ]?\d", first)
        if match:
            return f"{base}/Aisle {match.group(1).upper()}", "flat_aisle_prefix"
        return f"{base}/{first}", "single_child_below_stock"

    if len(parts) >= 2:
        return "/".join(parts[:-1]), "parent_path_fallback"
    return parts[0], "single_location_fallback"


def _velocity_classes(product_touches: Mapping[int, int]) -> dict[int, str]:
    active = sorted(
        (product_id for product_id, touches in product_touches.items() if touches > 0),
        key=lambda product_id: (-product_touches[product_id], product_id),
    )
    if not active:
        return {}
    a_cut = max(1, ceil(len(active) * 0.20))
    b_cut = max(a_cut, ceil(len(active) * 0.50))
    result: dict[int, str] = {}
    for index, product_id in enumerate(active):
        if index < a_cut:
            result[product_id] = "HIGH"
        elif index < b_cut:
            result[product_id] = "MEDIUM"
        else:
            result[product_id] = "LOW"
    return result


def _relative(values: Mapping[str, float]) -> dict[str, float]:
    maximum = max(values.values(), default=0.0)
    if maximum <= 0:
        return {key: 0.0 for key in values}
    return {key: min(max(value / maximum, 0.0), 1.0) for key, value in values.items()}


def _top_reasons(components: Mapping[str, float], metrics: Mapping[str, Any]) -> list[str]:
    labels = {
        "activity": "high recent operational stock-move activity",
        "velocity": "concentration of high-velocity SKUs/touches",
        "production": "strong BOM/component relevance",
        "criticality": "flight-critical inventory/activity concentration",
        "reservation": "reservation pressure on current stock",
        "traceability": "lot/serial traceability concentration",
        "dispersion": "SKUs dispersed across multiple storage bins/areas",
    }
    ordered = sorted(components, key=lambda key: (-components[key], key))
    reasons = [labels[key] for key in ordered if components[key] > 0][:3]
    if metrics.get("move_touches"):
        reasons.append(f"{int(metrics['move_touches'])} operational storage touches in the analysis window")
    if metrics.get("high_velocity_skus"):
        reasons.append(f"{int(metrics['high_velocity_skus'])} high-velocity SKUs currently stored here")
    return reasons[:5]


def analyze_mapping_priorities(
    products: Iterable[Record],
    quants: Iterable[Record],
    moves: Iterable[Record],
    locations: Iterable[Record],
    *,
    bom_lines: Iterable[Record] = (),
    as_of: datetime | None = None,
    lookback_days: int = 90,
) -> dict[str, Any]:
    """Rank logical Odoo storage areas for first physical XYZ/graph mapping."""
    if lookback_days <= 0:
        raise ValueError("lookback_days must be greater than zero")

    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    products_list = list(products)
    quants_list = list(quants)
    moves_list = list(moves)
    locations_list = list(locations)
    bom_lines_list = list(bom_lines)

    products_by_id: dict[int, Record] = {}
    for product in products_list:
        product_id = _m2o_id(product.get("id"))
        if product_id is not None:
            products_by_id[product_id] = product

    location_by_id: dict[int, Record] = {}
    for location in locations_list:
        location_id = _m2o_id(location.get("id"))
        if location_id is not None:
            location_by_id[location_id] = location

    eligible_storage_location_ids = {
        location_id
        for location_id, location in location_by_id.items()
        if _is_candidate_storage_location(location)
    }
    storage_location_ids: set[int] = set()
    excluded_quant_location_ids: set[int] = set()
    for quant in quants_list:
        location_id = _m2o_id(quant.get("location_id"))
        if location_id is None or _number(quant.get("quantity")) == 0:
            continue
        if location_id in eligible_storage_location_ids:
            storage_location_ids.add(location_id)
        else:
            excluded_quant_location_ids.add(location_id)

    area_by_location: dict[int, str] = {}
    grouping_strategy_by_area: dict[str, set[str]] = defaultdict(set)
    for location_id in storage_location_ids:
        location = location_by_id[location_id]
        area, strategy = derive_logical_area(_location_name(location))
        area_by_location[location_id] = area
        grouping_strategy_by_area[area].add(strategy)

    area_product_qty: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    area_product_reserved: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    area_locations: dict[str, set[int]] = defaultdict(set)
    product_locations: dict[int, set[int]] = defaultdict(set)
    product_areas: dict[int, set[str]] = defaultdict(set)

    for quant in quants_list:
        product_id = _m2o_id(quant.get("product_id"))
        location_id = _m2o_id(quant.get("location_id"))
        area = area_by_location.get(location_id or -1)
        if product_id is None or location_id is None or not area:
            continue
        quantity = _number(quant.get("quantity"))
        reserved = max(_number(quant.get("reserved_quantity")), 0.0)
        area_product_qty[area][product_id] += quantity
        area_product_reserved[area][product_id] += reserved
        area_locations[area].add(location_id)
        if quantity != 0:
            product_locations[product_id].add(location_id)
            product_areas[product_id].add(area)

    product_touches: dict[int, int] = defaultdict(int)
    area_touches: dict[str, int] = defaultdict(int)
    area_move_qty: dict[str, float] = defaultdict(float)
    area_product_touches: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    area_counterparts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    area_adjustment_touches: dict[str, int] = defaultdict(int)
    inventory_adjustment_move_lines_excluded = 0
    operational_move_lines_used = 0

    for move in moves_list:
        move_date = _parse_datetime(move.get("date") or move.get("write_date"))
        if move_date is None or move_date > now:
            continue
        age_days = (now - move_date).total_seconds() / 86400.0
        if age_days < 0 or age_days >= lookback_days:
            continue

        product_id = _m2o_id(move.get("product_id"))
        if product_id is None:
            continue

        source_value = move.get("location_id")
        dest_value = move.get("location_dest_id")
        source_id = _m2o_id(source_value)
        dest_id = _m2o_id(dest_value)
        source_area = area_by_location.get(source_id or -1)
        dest_area = area_by_location.get(dest_id or -1)
        touched_areas = {area for area in (source_area, dest_area) if area}
        if not touched_areas:
            continue

        if _is_inventory_adjustment_location(source_value, location_by_id) or _is_inventory_adjustment_location(dest_value, location_by_id):
            inventory_adjustment_move_lines_excluded += 1
            for area in touched_areas:
                area_adjustment_touches[area] += 1
            continue

        operational_move_lines_used += 1
        product_touches[product_id] += 1
        quantity = abs(_number(move.get("quantity")) or _number(move.get("qty_done")))
        for area in touched_areas:
            area_touches[area] += 1
            area_move_qty[area] += quantity
            area_product_touches[area][product_id] += 1

            counterpart_name = ""
            if area == source_area and dest_id is not None:
                counterpart_name = _location_name(location_by_id.get(dest_id, {})) or _m2o_name(dest_value)
            elif area == dest_area and source_id is not None:
                counterpart_name = _location_name(location_by_id.get(source_id, {})) or _m2o_name(source_value)
            if counterpart_name:
                area_counterparts[area][counterpart_name] += 1

    velocity_by_product = _velocity_classes(product_touches)

    bom_occurrences_by_product: dict[int, int] = defaultdict(int)
    for line in bom_lines_list:
        product_id = _m2o_id(line.get("product_id"))
        if product_id is not None:
            bom_occurrences_by_product[product_id] += 1

    raw: dict[str, dict[str, Any]] = {}
    for area in sorted(area_product_qty):
        product_qty = area_product_qty[area]
        product_ids = {product_id for product_id, qty in product_qty.items() if qty != 0}
        on_hand_units = sum(product_qty.values())
        reserved_units = sum(area_product_reserved[area].values())
        inventory_value = 0.0
        high_velocity_skus = 0
        high_velocity_touches = 0
        critical_skus = 0
        critical_touches = 0
        tracked_skus = 0
        multi_location_skus = 0
        multi_area_skus = 0
        bom_occurrences = 0

        for product_id in product_ids:
            product = products_by_id.get(product_id, {})
            quantity = product_qty.get(product_id, 0.0)
            inventory_value += max(quantity, 0.0) * max(_number(product.get("standard_price")), 0.0)
            if velocity_by_product.get(product_id) == "HIGH":
                high_velocity_skus += 1
                high_velocity_touches += area_product_touches[area].get(product_id, 0)
            if bool(product.get("x_is_flight_critical")):
                critical_skus += 1
                critical_touches += area_product_touches[area].get(product_id, 0)
            if str(product.get("tracking") or "none").lower() in {"lot", "serial"}:
                tracked_skus += 1
            if len(product_locations.get(product_id, set())) > 1:
                multi_location_skus += 1
            if len(product_areas.get(product_id, set())) > 1:
                multi_area_skus += 1
            bom_occurrences += bom_occurrences_by_product.get(product_id, 0)

        counterpart_rows = sorted(
            area_counterparts[area].items(),
            key=lambda row: (-row[1], row[0]),
        )[:5]
        raw[area] = {
            "logical_area": area,
            "grouping_strategies": sorted(grouping_strategy_by_area[area]),
            "storage_locations": len(area_locations[area]),
            "inventory_skus": len(product_ids),
            "on_hand_units": round(on_hand_units, 3),
            "reserved_units": round(reserved_units, 3),
            "reserved_ratio": round(reserved_units / on_hand_units, 4) if on_hand_units > 0 else 0.0,
            "inventory_value": round(inventory_value, 2),
            "move_touches": area_touches.get(area, 0),
            "move_quantity": round(area_move_qty.get(area, 0.0), 3),
            "inventory_adjustment_touches_excluded": area_adjustment_touches.get(area, 0),
            "high_velocity_skus": high_velocity_skus,
            "high_velocity_touches": high_velocity_touches,
            "flight_critical_skus": critical_skus,
            "flight_critical_touches": critical_touches,
            "tracked_skus": tracked_skus,
            "bom_component_occurrences": bom_occurrences,
            "multi_location_skus": multi_location_skus,
            "multi_area_skus": multi_area_skus,
            "top_flow_counterparts": [
                {"location": name, "touches": count}
                for name, count in counterpart_rows
            ],
        }

    activity_norm = _relative({area: float(row["move_touches"]) for area, row in raw.items()})
    velocity_norm = _relative({area: float(row["high_velocity_touches"]) for area, row in raw.items()})
    production_norm = _relative({area: float(row["bom_component_occurrences"]) for area, row in raw.items()})
    critical_norm = _relative(
        {
            area: float(row["flight_critical_touches"] + row["flight_critical_skus"])
            for area, row in raw.items()
        }
    )
    reservation_norm = _relative({area: float(row["reserved_ratio"]) for area, row in raw.items()})
    traceability_norm = _relative({area: float(row["tracked_skus"]) for area, row in raw.items()})
    dispersion_norm = _relative(
        {
            area: float(row["multi_location_skus"] + row["multi_area_skus"])
            for area, row in raw.items()
        }
    )

    weights = {
        "activity": 30.0,
        "velocity": 20.0,
        "production": 15.0,
        "criticality": 15.0,
        "reservation": 10.0,
        "traceability": 5.0,
        "dispersion": 5.0,
    }

    ranked: list[dict[str, Any]] = []
    for area, metrics in raw.items():
        normalized = {
            "activity": activity_norm[area],
            "velocity": velocity_norm[area],
            "production": production_norm[area],
            "criticality": critical_norm[area],
            "reservation": reservation_norm[area],
            "traceability": traceability_norm[area],
            "dispersion": dispersion_norm[area],
        }
        components = {
            key: round(weights[key] * normalized[key], 3)
            for key in weights
        }
        score = round(sum(components.values()), 2)
        ranked.append(
            {
                **metrics,
                "opportunity_score": score,
                "score_components": components,
                "why_map_this_area": _top_reasons(components, metrics),
            }
        )

    ranked.sort(key=lambda row: (-row["opportunity_score"], row["logical_area"]))
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index

    primary = ranked[0] if ranked else None
    recommended_scope = None
    if primary:
        counterparts = [row["location"] for row in primary["top_flow_counterparts"][:3]]
        recommended_scope = {
            "logical_area": primary["logical_area"],
            "opportunity_score": primary["opportunity_score"],
            "map_first": True,
            "include": [
                "all active storage bins in the selected logical area",
                "aisle centerlines and cross-aisles serving those bins",
                "legal pedestrian/cart paths from the area",
                *[f"connection to frequent operational flow point: {name}" for name in counterparts],
            ],
            "do_not_claim_yet": [
                "physical walking distance",
                "travel-time savings",
                "optimal XYZ position",
                "labor ROI from a relocation",
            ],
            "next_measurement": "capture XYZ coordinates and legal-path graph only for this scoped area plus its key operational flow connections",
        }

    available_flight_field = any("x_is_flight_critical" in product for product in products_list)
    return {
        "generated_at": now.isoformat(),
        "mode": "read_only_odoo_mapping_prioritization",
        "odoo_mutated": False,
        "lookback_days": lookback_days,
        "methodology": {
            "purpose": "rank where physical warehouse mapping is most likely to be useful before XYZ/path data exists",
            "area_grouping": "rank only internal Stock descendants; prefer meaningful hierarchy below Stock and fall back to aisle-like prefixes",
            "velocity": "relative operational stock-move touch rank among active products; top 20% HIGH, next 30% MEDIUM, remainder LOW",
            "inventory_adjustments": "reported separately and excluded from physical movement/velocity scoring",
            "score": "relative 0-100 opportunity index across areas, not a savings estimate",
            "weights": weights,
            "physical_distance": "not used because validated XYZ/legal-path geometry is intentionally absent at this stage",
            "execution": "advisory only; no Odoo records are changed",
        },
        "data_capabilities": {
            "bom_data_used": bool(bom_lines_list),
            "flight_critical_field_available": available_flight_field,
            "xyz_required_for_this_stage": False,
            "xyz_required_for_later_physical_optimization": True,
        },
        "summary": {
            "logical_areas_ranked": len(ranked),
            "storage_locations_seen": len(storage_location_ids),
            "quant_locations_excluded_from_storage_ranking": len(excluded_quant_location_ids),
            "products_seen": len(products_by_id),
            "move_lines_seen": len(moves_list),
            "operational_move_lines_used": operational_move_lines_used,
            "inventory_adjustment_move_lines_excluded": inventory_adjustment_move_lines_excluded,
            "bom_lines_seen": len(bom_lines_list),
            "recommended_first_area": primary["logical_area"] if primary else None,
        },
        "recommended_mapping_scope": recommended_scope,
        "areas": ranked,
    }
