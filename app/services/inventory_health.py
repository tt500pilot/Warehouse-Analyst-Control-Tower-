"""Deterministic inventory-health and cycle-count prioritization for AWIA Module A.

This first slice intentionally uses only data already available through the proven
Odoo client: products, quants, and stock move lines. Optional aerospace-specific
fields (for example ``x_is_flight_critical``) are honored when they exist, but are
not required for the engine to run.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import ceil
from statistics import mean, pstdev
from typing import Any, Iterable, Mapping, Sequence

Record = Mapping[str, Any]

ABC_INTERVAL_DAYS = {"A": 30, "B": 90, "C": 180}
ABC_WEIGHTS = {"A": 35.0, "B": 20.0, "C": 10.0}
XYZ_WEIGHTS = {"X": 5.0, "Y": 15.0, "Z": 25.0}


def _number(value: Any) -> float:
    if value in (None, False, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _many2one_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _many2one_name(value: Any) -> str:
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


def _move_quantity(move: Record) -> float:
    quantity = _number(move.get("quantity"))
    if quantity == 0:
        quantity = _number(move.get("qty_done"))
    return abs(quantity)


def _abc_classes(product_values: Mapping[int, float]) -> dict[int, str]:
    """Classify ranked SKUs using the specification's 20% / 30% / 50% split."""
    ranked = sorted(
        product_values,
        key=lambda product_id: (-product_values[product_id], product_id),
    )
    count = len(ranked)
    if count == 0:
        return {}

    a_cut = max(1, ceil(count * 0.20))
    b_cut = max(a_cut, ceil(count * 0.50))

    result: dict[int, str] = {}
    for index, product_id in enumerate(ranked):
        if index < a_cut:
            result[product_id] = "A"
        elif index < b_cut:
            result[product_id] = "B"
        else:
            result[product_id] = "C"
    return result


def _xyz_class(weekly_activity: Sequence[float]) -> tuple[str, float]:
    """Classify transaction predictability from four weekly activity buckets.

    Until AWIA distinguishes demand/consumption moves from every other stock move,
    this is intentionally labeled an activity-predictability proxy rather than a
    demand forecast.
    """
    if not weekly_activity or max(weekly_activity, default=0.0) == 0:
        return "X", 0.0

    average = mean(weekly_activity)
    if average <= 0:
        return "X", 0.0

    cv = pstdev(weekly_activity) / average
    if cv <= 0.50:
        return "X", cv
    if cv <= 1.00:
        return "Y", cv
    return "Z", cv


def _velocity_points(touches_7d: int) -> tuple[float, str | None]:
    if touches_7d >= 40:
        return 20.0, "40+ physical transaction touches in the last 7 days"
    if touches_7d >= 25:
        return 15.0, "25+ physical transaction touches in the last 7 days"
    if touches_7d >= 10:
        return 8.0, "10+ physical transaction touches in the last 7 days"
    if touches_7d > 0:
        return 3.0, "recent physical transaction activity"
    return 0.0, None


def _user_points(unique_users_7d: int) -> tuple[float, str | None]:
    if unique_users_7d >= 5:
        return 5.0, "5+ unique users touched this product/location in 7 days"
    if unique_users_7d >= 3:
        return 3.0, "3+ unique users touched this product/location in 7 days"
    if unique_users_7d >= 1:
        return 1.0, None
    return 0.0, None


def _risk_level(score: float) -> str:
    if score >= 80:
        return "critical"
    if score >= 60:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def location_quantities_product_ids(
    location_quantities: Mapping[tuple[int, int], Any],
) -> set[int]:
    return {product_id for product_id, _location_id in location_quantities}


def _count_values(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row[key])] += 1
    return dict(sorted(counts.items()))


def analyze_inventory_health(
    products: Iterable[Record],
    quants: Iterable[Record],
    moves: Iterable[Record],
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Build product/location inventory-health rows and Module A risk scores."""
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    products_by_id: dict[int, Record] = {}
    for product in products:
        product_id = _many2one_id(product.get("id"))
        if product_id is not None:
            products_by_id[product_id] = product

    location_quantities: dict[tuple[int, int], dict[str, Any]] = {}
    product_total_qty: dict[int, float] = defaultdict(float)

    for quant in quants:
        product_id = _many2one_id(quant.get("product_id"))
        location_id = _many2one_id(quant.get("location_id"))
        if product_id is None or location_id is None:
            continue

        key = (product_id, location_id)
        bucket = location_quantities.setdefault(
            key,
            {
                "quantity": 0.0,
                "reserved_quantity": 0.0,
                "location_name": _many2one_name(quant.get("location_id")),
            },
        )
        quantity = _number(quant.get("quantity"))
        bucket["quantity"] += quantity
        bucket["reserved_quantity"] += _number(quant.get("reserved_quantity"))
        product_total_qty[product_id] += quantity

    product_values: dict[int, float] = {}
    for product_id in location_quantities_product_ids(location_quantities):
        product = products_by_id.get(product_id, {})
        unit_cost = max(_number(product.get("standard_price")), 0.0)
        product_values[product_id] = (
            max(product_total_qty.get(product_id, 0.0), 0.0) * unit_cost
        )

    abc_by_product = _abc_classes(product_values)

    weekly_activity: dict[int, list[float]] = defaultdict(
        lambda: [0.0, 0.0, 0.0, 0.0]
    )
    touches_7d: dict[tuple[int, int], int] = defaultdict(int)
    users_7d: dict[tuple[int, int], set[int]] = defaultdict(set)

    for move in moves:
        product_id = _many2one_id(move.get("product_id"))
        if product_id is None:
            continue
        move_date = _parse_datetime(move.get("date") or move.get("write_date"))
        if move_date is None or move_date > now:
            continue

        age_days = (now - move_date).total_seconds() / 86400.0
        if 0 <= age_days < 28:
            week_index = min(int(age_days // 7), 3)
            weekly_activity[product_id][week_index] += _move_quantity(move)

        if not 0 <= age_days < 7:
            continue

        user_id = _many2one_id(move.get("write_uid"))
        location_ids = {
            location_id
            for location_id in (
                _many2one_id(move.get("location_id")),
                _many2one_id(move.get("location_dest_id")),
            )
            if location_id is not None
        }
        for location_id in location_ids:
            key = (product_id, location_id)
            touches_7d[key] += 1
            if user_id is not None:
                users_7d[key].add(user_id)

    xyz_by_product: dict[int, tuple[str, float]] = {
        product_id: _xyz_class(weekly_activity.get(product_id, [0.0] * 4))
        for product_id in product_values
    }

    rows: list[dict[str, Any]] = []
    for (product_id, location_id), quant in location_quantities.items():
        product = products_by_id.get(product_id, {})
        quantity = _number(quant["quantity"])
        reserved = max(_number(quant["reserved_quantity"]), 0.0)
        unit_cost = max(_number(product.get("standard_price")), 0.0)
        inventory_value = max(quantity, 0.0) * unit_cost
        abc = abc_by_product.get(product_id, "C")
        xyz, activity_cv = xyz_by_product.get(product_id, ("X", 0.0))
        touch_count = touches_7d.get((product_id, location_id), 0)
        unique_users = len(users_7d.get((product_id, location_id), set()))

        score = ABC_WEIGHTS[abc] + XYZ_WEIGHTS[xyz]
        reasons = [
            f"ABC {abc}: ranked inventory value tier",
            f"XYZ {xyz}: transaction-activity predictability",
        ]

        velocity_score, velocity_reason = _velocity_points(touch_count)
        score += velocity_score
        if velocity_reason:
            reasons.append(velocity_reason)

        user_score, user_reason = _user_points(unique_users)
        score += user_score
        if user_reason:
            reasons.append(user_reason)

        tracking = str(product.get("tracking") or "none").lower()
        if tracking in {"lot", "serial"}:
            score += 5.0
            reasons.append(f"{tracking}-tracked component")

        if quantity < 0:
            score += 10.0
            reasons.append("negative on-hand quantity requires investigation")

        flight_critical = bool(product.get("x_is_flight_critical"))
        if flight_critical:
            score *= 1.75
            reasons.append("flight-critical 1.75x risk multiplier")

        score = round(min(score, 100.0), 2)
        available_qty = quantity - reserved

        rows.append(
            {
                "product_id": product_id,
                "default_code": str(product.get("default_code") or ""),
                "product_name": str(product.get("name") or ""),
                "location_id": location_id,
                "location_name": quant["location_name"],
                "on_hand_qty": round(quantity, 4),
                "reserved_qty": round(reserved, 4),
                "available_qty": round(available_qty, 4),
                "unit_cost": round(unit_cost, 4),
                "inventory_value": round(inventory_value, 2),
                "abc_class": abc,
                "xyz_class": xyz,
                "activity_cv_28d": round(activity_cv, 4),
                "touches_7d": touch_count,
                "unique_users_7d": unique_users,
                "tracking": tracking,
                "flight_critical": flight_critical,
                "recommended_count_interval_days": ABC_INTERVAL_DAYS[abc],
                "risk_score": score,
                "risk_level": _risk_level(score),
                "reasons": reasons,
            }
        )

    rows.sort(
        key=lambda row: (
            -row["risk_score"],
            row["location_name"],
            row["default_code"],
            row["product_id"],
        )
    )

    summary = {
        "locations_evaluated": len(rows),
        "products_evaluated": len({row["product_id"] for row in rows}),
        "total_inventory_value": round(
            sum(row["inventory_value"] for row in rows), 2
        ),
        "risk_levels": _count_values(rows, "risk_level"),
        "abc_classes": _count_values(rows, "abc_class"),
        "xyz_classes": _count_values(rows, "xyz_class"),
        "flight_critical_locations": sum(
            1 for row in rows if row["flight_critical"]
        ),
    }

    return {
        "generated_at": now.isoformat(),
        "methodology": {
            "abc": "SKUs ranked by current inventory value; top 20% A, next 30% B, bottom 50% C",
            "xyz": "4-week stock-move activity coefficient-of-variation proxy (X <= 0.50, Y <= 1.00, Z > 1.00)",
            "touch_risk": "7-day source/destination move-line touches; 40+ touches receives maximum velocity points",
            "flight_critical": "optional x_is_flight_critical field applies a 1.75x score multiplier when present",
            "execution": "advisory only; no Odoo inventory adjustments are written",
        },
        "summary": summary,
        "items": rows,
    }


def build_cycle_count_plan(
    health_items: Sequence[Mapping[str, Any]],
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Create an advisory daily cycle-count route from inventory-health rows."""
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    def priority_band(score: float) -> int:
        if score >= 80:
            return 1
        if score >= 60:
            return 2
        if score >= 40:
            return 3
        return 4

    selected = sorted(
        health_items,
        key=lambda row: (
            priority_band(_number(row.get("risk_score"))),
            str(row.get("location_name") or ""),
            -_number(row.get("risk_score")),
            str(row.get("default_code") or ""),
        ),
    )[:limit]

    entries: list[dict[str, Any]] = []
    for sequence, item in enumerate(selected, start=1):
        entries.append(
            {
                "sequence": sequence,
                "priority_band": priority_band(_number(item.get("risk_score"))),
                "location_id": item.get("location_id"),
                "location_name": item.get("location_name"),
                "product_id": item.get("product_id"),
                "default_code": item.get("default_code"),
                "product_name": item.get("product_name"),
                "system_on_hand_qty": item.get("on_hand_qty"),
                "risk_score": item.get("risk_score"),
                "risk_level": item.get("risk_level"),
                "abc_class": item.get("abc_class"),
                "xyz_class": item.get("xyz_class"),
                "recommended_count_interval_days": item.get(
                    "recommended_count_interval_days"
                ),
                "reasons": item.get("reasons", []),
            }
        )

    return {
        "count": len(entries),
        "route_strategy": (
            "risk bands first; lexical Odoo location-code order within each band"
        ),
        "execution": (
            "advisory only; analyst approval is required before creating Odoo count tasks"
        ),
        "entries": entries,
    }
