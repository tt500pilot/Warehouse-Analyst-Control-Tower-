"""Kitting baseline analysis for native Odoo warehouse transactions.

The service intentionally distinguishes timestamps Odoo actually records from
fine-grained labor events AWIA does not yet observe.  In particular,
``create_date -> date_done`` is reported as *gross transfer cycle time*, not
picker labor time.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any, Iterable


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int):
        return value[0]
    return None


def _m2o_label(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1] or "")
    if isinstance(value, str):
        return value
    return ""


def _parse_odoo_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            parsed = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def _round_or_none(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def analyze_kitting_baseline(
    pickings: Iterable[dict[str, Any]],
    move_lines: Iterable[dict[str, Any]],
    *,
    picking_type_contains: str = "Pick Components",
) -> dict[str, Any]:
    """Build a conservative kitting baseline from Odoo pickings and move lines.

    ``picking_type_contains`` is matched case-insensitively against the display
    label of ``stock.picking.picking_type_id``.  Passing an empty string keeps
    all supplied pickings.
    """

    token = picking_type_contains.strip().lower()
    relevant_pickings: list[dict[str, Any]] = []
    for picking in pickings:
        picking_type = _m2o_label(picking.get("picking_type_id"))
        if token and token not in picking_type.lower():
            continue
        relevant_pickings.append(picking)

    moves_by_picking: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for move in move_lines:
        picking_id = _m2o_id(move.get("picking_id"))
        if picking_id is not None:
            moves_by_picking[picking_id].append(move)

    rows: list[dict[str, Any]] = []
    gross_cycles: list[float] = []
    move_event_spans: list[float] = []
    line_counts: list[float] = []
    unique_location_counts: list[float] = []

    for picking in relevant_pickings:
        picking_id = picking.get("id")
        if not isinstance(picking_id, int):
            continue
        lines = moves_by_picking.get(picking_id, [])
        created = _parse_odoo_datetime(picking.get("create_date"))
        done = _parse_odoo_datetime(picking.get("date_done"))
        gross_cycle = None
        if created and done and done >= created:
            gross_cycle = (done - created).total_seconds() / 60.0
            gross_cycles.append(gross_cycle)

        line_dates = sorted(
            timestamp
            for timestamp in (_parse_odoo_datetime(line.get("date")) for line in lines)
            if timestamp is not None
        )
        move_span = None
        if len(line_dates) >= 2 and line_dates[-1] >= line_dates[0]:
            move_span = (line_dates[-1] - line_dates[0]).total_seconds() / 60.0
            move_event_spans.append(move_span)

        product_ids = {_m2o_id(line.get("product_id")) for line in lines}
        product_ids.discard(None)
        source_location_ids = {_m2o_id(line.get("location_id")) for line in lines}
        source_location_ids.discard(None)
        users = {_m2o_id(line.get("write_uid")) for line in lines}
        users.discard(None)

        line_count = len(lines)
        line_counts.append(float(line_count))
        unique_location_counts.append(float(len(source_location_ids)))

        rows.append(
            {
                "picking_id": picking_id,
                "picking_name": picking.get("name"),
                "origin": picking.get("origin"),
                "picking_type": _m2o_label(picking.get("picking_type_id")),
                "scheduled_date": picking.get("scheduled_date"),
                "date_done": picking.get("date_done"),
                "gross_cycle_minutes": _round_or_none(gross_cycle),
                "move_event_span_minutes_proxy": _round_or_none(move_span),
                "move_line_count": line_count,
                "unique_skus": len(product_ids),
                "unique_source_locations": len(source_location_ids),
                "unique_users": len(users),
            }
        )

    rows.sort(
        key=lambda row: (
            row["date_done"] or "",
            row["picking_id"],
        ),
        reverse=True,
    )

    summary = {
        "kits_analyzed": len(rows),
        "gross_cycle_minutes": {
            "average": _round_or_none(mean(gross_cycles) if gross_cycles else None),
            "median": _round_or_none(median(gross_cycles) if gross_cycles else None),
            "p90": _round_or_none(_percentile(gross_cycles, 0.90)),
            "measured_kits": len(gross_cycles),
        },
        "move_event_span_minutes_proxy": {
            "average": _round_or_none(mean(move_event_spans) if move_event_spans else None),
            "median": _round_or_none(median(move_event_spans) if move_event_spans else None),
            "measured_kits": len(move_event_spans),
        },
        "move_lines_per_kit": _round_or_none(mean(line_counts) if line_counts else None),
        "source_locations_per_kit": _round_or_none(
            mean(unique_location_counts) if unique_location_counts else None
        ),
    }

    return {
        "summary": summary,
        "kits": rows,
        "methodology": {
            "gross_cycle_time": "stock.picking create_date to date_done; includes queue/wait time and must not be interpreted as picker labor time",
            "move_event_span_proxy": "first to last stock.move.line date within the picking; useful only when line timestamps are genuinely distinct in the configured Odoo workflow",
            "picking_filter": picking_type_contains or "all supplied picking types",
        },
        "data_coverage": {
            "actual_walking_distance": "not available until AWIA warehouse geometry is linked",
            "picker_start_stop_events": "not assumed; requires workflow-specific scan/timer evidence",
            "search_time": "not natively separated",
            "shortage_delay": "not natively separated in this first slice",
            "first_pass_kit_complete": "not inferred from completed transfers; requires explicit availability/event logic",
        },
    }
