from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


def _number(value: Any) -> float:
    if value in (None, False, ""):
        return 0.0
    return float(value)


def _parse_scenarios(value: str) -> list[float]:
    result: list[float] = []
    for item in value.split(","):
        text = item.strip()
        if not text:
            continue
        number = float(text)
        if number <= 0:
            raise ValueError("setup-minute scenarios must be positive")
        result.append(number)
    if not result:
        raise ValueError("at least one setup-minute scenario is required")
    return result


def _components(nodes: set[int], edges: dict[int, set[int]]) -> list[set[int]]:
    remaining = set(nodes)
    result: list[set[int]] = []
    while remaining:
        start = min(remaining)
        queue = deque([start])
        component: set[int] = set()
        while queue:
            node = queue.popleft()
            if node in component:
                continue
            component.add(node)
            remaining.discard(node)
            queue.extend(sorted(edges.get(node, set()) - component))
        result.append(component)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate joint economics for capacity-screened mapped-aisle recommendations whose route benefit is shared "
            "because the products are co-picked. Advisory/read-only only; no Odoo writes."
        )
    )
    parser.add_argument("--readiness", required=True)
    parser.add_argument("--route-validation", required=True)
    parser.add_argument(
        "--setup-minutes",
        default="5,15,30,60",
        help="Comma-separated hypothetical TOTAL setup minutes for each whole co-pick package.",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    readiness_path = Path(args.readiness)
    route_path = Path(args.route_validation)
    if not readiness_path.exists():
        raise FileNotFoundError(readiness_path)
    if not route_path.exists():
        raise FileNotFoundError(route_path)

    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    route = json.loads(route_path.read_text(encoding="utf-8"))
    scenarios = _parse_scenarios(args.setup_minutes)
    walking_speed = _number(readiness.get("walking_speed_ft_s_for_equivalent_only")) or 3.5

    readiness_rows = list(readiness.get("recommendations") or [])
    by_product = {
        int(row["product_id"]): row
        for row in readiness_rows
        if row.get("product_id") is not None
    }
    capacity_pass_ids = {
        product_id for product_id, row in by_product.items() if row.get("capacity_screen_pass")
    }

    completed = route.get("completed_historical_validation") or {}
    co_pick_pairs = list(completed.get("recommended_product_co_pick_pairs") or [])
    edges: dict[int, set[int]] = defaultdict(set)
    co_pick_nodes: set[int] = set()
    for pair in co_pick_pairs:
        ids = [int(value) for value in pair.get("product_ids") or []]
        ids = [value for value in ids if value in capacity_pass_ids]
        if len(ids) < 2 or int(pair.get("pickings_together") or 0) <= 0:
            continue
        for left in ids:
            for right in ids:
                if left == right:
                    continue
                edges[left].add(right)
                co_pick_nodes.add(left)
                co_pick_nodes.add(right)

    multi_components = [
        component for component in _components(co_pick_nodes, edges) if len(component) >= 2
    ]

    hard_blocker_names = {
        "tracked_stock_not_fully_lot_or_serial_identified",
        "target_unit_capacity_not_proven",
        "target_weight_capacity_not_proven",
        "target_no_longer_empty",
        "approved_product_physical_metadata_missing",
    }

    per_picking = list(completed.get("per_picking") or [])
    package_rows: list[dict[str, Any]] = []
    package_member_ids: set[int] = set()

    for index, component in enumerate(sorted(multi_components, key=lambda item: sorted(item)), start=1):
        package_member_ids.update(component)
        codes = [str(by_product[pid].get("product_code") or pid) for pid in sorted(component)]
        qualifying_pickings: list[dict[str, Any]] = []
        excluded_contaminated_pickings = 0
        for picking in per_picking:
            affected = {int(value) for value in picking.get("affected_recommended_product_ids") or []}
            overlap = affected & component
            if not overlap:
                continue
            # The route delta in a picking is only package-clean when every affected recommendation
            # in that picking belongs to this capacity-screened connected component.
            if affected - component:
                excluded_contaminated_pickings += 1
                continue
            qualifying_pickings.append(picking)

        package_saved_ft = sum(_number(row.get("distance_saved_ft")) for row in qualifying_pickings)
        affected_pickings = len(qualifying_pickings)
        joint_pickings = sum(
            1
            for row in qualifying_pickings
            if len({int(value) for value in row.get("affected_recommended_product_ids") or []} & component) >= 2
        )
        individual_attributable_ft = sum(
            _number(
                (by_product[pid].get("completed_route_benefit") or {}).get(
                    "attributable_modeled_aisle_subroute_distance_saved_ft"
                )
            )
            for pid in component
        )
        shared_joint_ft = max(package_saved_ft - individual_attributable_ft, 0.0)
        avg_saved_ft = package_saved_ft / affected_pickings if affected_pickings else 0.0
        avg_saved_minutes = avg_saved_ft / walking_speed / 60.0 if walking_speed > 0 else 0.0

        blockers = sorted(
            {
                str(blocker)
                for pid in component
                for blocker in by_product[pid].get("execution_blockers") or []
            }
        )
        hard_preconditions = [blocker for blocker in blockers if blocker in hard_blocker_names]
        relocation_distance_sum_ft = sum(
            _number((by_product[pid].get("relocation_geometry") or {}).get("legal_transfer_distance_ft"))
            for pid in component
        )

        package_rows.append(
            {
                "package_id": f"COPICK-{index:02d}",
                "product_ids": sorted(component),
                "product_codes": codes,
                "product_count": len(component),
                "completed_affected_pickings": affected_pickings,
                "completed_joint_pickings": joint_pickings,
                "excluded_pickings_with_other_recommendations": excluded_contaminated_pickings,
                "package_modeled_route_saved_ft": round(package_saved_ft, 3),
                "sum_individually_attributable_saved_ft": round(individual_attributable_ft, 3),
                "shared_joint_route_saved_ft": round(shared_joint_ft, 3),
                "shared_benefit_requires_package_evaluation": shared_joint_ft > 0,
                "modeled_saved_ft_per_affected_picking": round(avg_saved_ft, 3),
                "walking_only_saved_minutes_per_affected_picking": round(avg_saved_minutes, 4),
                "sum_individual_relocation_legal_distance_ft": round(relocation_distance_sum_ft, 3),
                "execution_blockers": blockers,
                "hard_preconditions_before_pilot": hard_preconditions,
                "package_economics_status": (
                    "hypothetical_only_until_hard_preconditions_are_cleared"
                    if hard_preconditions
                    else "joint_sensitivity_only_not_execution_ready"
                ),
                "setup_sensitivity": [
                    {
                        "hypothetical_total_package_setup_minutes": setup_minutes,
                        "payback_affected_pickings": (
                            round(setup_minutes / avg_saved_minutes, 1)
                            if avg_saved_minutes > 0
                            else None
                        ),
                    }
                    for setup_minutes in scenarios
                ],
                "safe_to_execute": False,
            }
        )

    singleton_rows = []
    for product_id in sorted(capacity_pass_ids - package_member_ids):
        row = by_product[product_id]
        benefit = row.get("completed_route_benefit") or {}
        singleton_rows.append(
            {
                "product_id": product_id,
                "product_code": row.get("product_code"),
                "attributable_completed_route_saved_ft": _number(
                    benefit.get("attributable_modeled_aisle_subroute_distance_saved_ft")
                ),
                "note": "This capacity-screened recommendation is not part of a multi-product completed co-pick component; individual economics remain the appropriate view.",
            }
        )

    readiness_ambiguous = _number(
        (readiness.get("summary") or {}).get("ambiguous_shared_completed_savings_ft")
    )
    package_shared_total = sum(_number(row.get("shared_joint_route_saved_ft")) for row in package_rows)

    result = {
        "mode": "read_only_mapped_aisle_copick_package_economics",
        "odoo_mutated": False,
        "safe_to_execute": False,
        "classification": "joint_route_sensitivity_not_production_roi",
        "summary": {
            "capacity_screened_recommendations": len(capacity_pass_ids),
            "multi_product_copick_packages": len(package_rows),
            "products_in_copick_packages": len(package_member_ids),
            "singleton_capacity_screened_recommendations": len(singleton_rows),
            "readiness_ambiguous_shared_completed_savings_ft": round(readiness_ambiguous, 3),
            "package_shared_joint_route_saved_ft": round(package_shared_total, 3),
            "shared_savings_reconciliation_difference_ft": round(
                readiness_ambiguous - package_shared_total, 3
            ),
        },
        "packages": package_rows,
        "singletons": singleton_rows,
        "guardrails": [
            "A co-pick package is a connected component of capacity-screened recommendations observed together in completed modeled pickings.",
            "Package route benefit is summed only from completed pickings whose affected recommendations are fully contained in that package; contaminated pickings are excluded rather than arbitrarily allocated.",
            "Shared route savings are intentionally not allocated to individual SKUs because the benefit exists jointly through route consolidation.",
            "Package setup minutes are hypothetical total package setup times, not the sum of measured relocation labor.",
            "Walking-only savings are modeled mapped-aisle subroute equivalents, not observed labor or annual ROI.",
            "Synthetic MOCK_FIXTURE inputs must never be represented as Firefly production performance.",
            "No Odoo writes are performed.",
        ],
        "readiness_file": str(readiness_path),
        "route_validation_file": str(route_path),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
