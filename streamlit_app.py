"""Streamlit front end for the AWIA Warehouse Analyst Control Tower."""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import streamlit as st

from app.ui.api_client import ControlTowerAPIError, get_json
from app.ui.dataframe import make_frame
from app.ui.optimization_artifacts import discover_analysis_areas, load_analysis_artifacts


st.set_page_config(
    page_title="AWIA Control Tower",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

DEFAULT_API_URL = os.getenv("AWIA_API_URL", "http://127.0.0.1:8000")
DEFAULT_ANALYSIS_DIR = os.getenv("AWIA_ANALYSIS_DIR", "data/analysis")


@st.cache_data(ttl=30, show_spinner=False)
def api_get(base_url: str, path: str, params_key: tuple[tuple[str, Any], ...] = ()) -> dict[str, Any]:
    return get_json(base_url, path, params=dict(params_key))


def fetch(base_url: str, path: str, **params: Any) -> dict[str, Any]:
    return api_get(base_url, path, tuple(sorted(params.items())))


def metric_count(mapping: dict[str, Any] | None, *keys: str) -> int:
    mapping = mapping or {}
    return sum(int(mapping.get(key, 0) or 0) for key in keys)


def money(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return "$0"


def number(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def usable_artifact(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("artifact_error"):
        return {}
    return value


def location_name_to_area_slug(value: Any) -> str:
    leaf = str(value or "").strip().split("/")[-1]
    normalized = leaf.replace("_", " ").lower().strip()
    return "-".join(normalized.split())


def show_connection_banner(base_url: str) -> None:
    try:
        api_health = fetch(base_url, "/health")
        odoo_health = fetch(base_url, "/health/odoo")
    except ControlTowerAPIError as exc:
        st.error(str(exc))
        return

    st.success(
        "FastAPI online • Odoo connected • "
        f"Database: {odoo_health.get('database', 'unknown')} • "
        f"UID: {odoo_health.get('uid', 'unknown')}"
    )
    if api_health.get("status") != "ok" or odoo_health.get("status") != "ok":
        st.warning("A service reported a non-OK health state.")


def render_control_tower(base_url: str, source_limit: int) -> None:
    st.subheader("Digital Control Tower")
    st.caption("Live read-only warehouse risk picture from Odoo through the AWIA FastAPI service.")

    try:
        report = fetch(base_url, "/api/inventory-health", limit=1000, source_limit=source_limit)
    except ControlTowerAPIError as exc:
        st.error(str(exc))
        return

    summary = report.get("summary", {})
    risk_levels = summary.get("risk_levels", {})
    source = report.get("source_snapshot", {})

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Inventory value", money(summary.get("total_inventory_value")))
    c2.metric("Locations evaluated", int(summary.get("locations_evaluated", 0) or 0))
    c3.metric("Critical risk", metric_count(risk_levels, "critical"))
    c4.metric("High risk", metric_count(risk_levels, "high"))
    c5.metric("Products evaluated", int(summary.get("products_evaluated", 0) or 0))

    if source.get("truncated_possible"):
        st.warning(
            "The Odoo source snapshot reached the configured source limit. "
            "Increase Source rows in the sidebar for a more complete analysis."
        )

    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.markdown("#### Risk distribution")
        risk_frame = pd.DataFrame({
            "Risk": ["critical", "high", "medium", "low"],
            "Locations": [int(risk_levels.get(k, 0) or 0) for k in ("critical", "high", "medium", "low")],
        }).set_index("Risk")
        st.bar_chart(risk_frame)

    with chart_right:
        st.markdown("#### ABC inventory classes")
        abc = summary.get("abc_classes", {})
        abc_frame = pd.DataFrame({
            "Class": ["A", "B", "C"],
            "Locations": [int(abc.get(k, 0) or 0) for k in ("A", "B", "C")],
        }).set_index("Class")
        st.bar_chart(abc_frame)

    st.markdown("#### Highest-risk inventory locations")
    frame = make_frame(report.get("items", [])[:15])
    preferred = [
        "risk_level", "risk_score", "default_code", "product_name", "location_name",
        "on_hand_qty", "available_qty", "inventory_value", "abc_class", "xyz_class",
        "touches_7d", "unique_users_7d", "reasons",
    ]
    columns = [column for column in preferred if column in frame.columns]
    st.dataframe(frame[columns] if columns else frame, width="stretch", hide_index=True)

    st.caption(
        f"Generated {report.get('generated_at', 'unknown')} • "
        f"Products read: {source.get('products', 0)} • "
        f"Quants read: {source.get('quants', 0)} • "
        f"28-day move lines: {source.get('move_lines_28d', 0)}"
    )


def render_inventory_health(base_url: str, source_limit: int) -> None:
    st.subheader("Inventory Health")
    st.caption("Explainable Module A risk scoring by product and Odoo location.")

    try:
        report = fetch(base_url, "/api/inventory-health", limit=1000, source_limit=source_limit)
    except ControlTowerAPIError as exc:
        st.error(str(exc))
        return

    frame = make_frame(report.get("items", []))
    if frame.empty:
        st.info("No non-zero inventory locations were returned by Odoo.")
        return

    f1, f2, f3, f4 = st.columns([1.3, 1, 1, 2])
    with f1:
        risk_selected = st.multiselect("Risk level", ["critical", "high", "medium", "low"], default=["critical", "high", "medium", "low"])
    with f2:
        abc_selected = st.multiselect("ABC", ["A", "B", "C"], default=["A", "B", "C"])
    with f3:
        xyz_selected = st.multiselect("XYZ", ["X", "Y", "Z"], default=["X", "Y", "Z"])
    with f4:
        search_text = st.text_input("Search part or location", placeholder="Part number, name, or WH/Stock/A-01")

    filtered = frame[
        frame["risk_level"].isin(risk_selected)
        & frame["abc_class"].isin(abc_selected)
        & frame["xyz_class"].isin(xyz_selected)
    ].copy()

    if search_text:
        needle = search_text.lower().strip()
        searchable = (
            filtered.get("default_code", pd.Series(index=filtered.index, dtype=str)).fillna("").astype(str)
            + " "
            + filtered.get("product_name", pd.Series(index=filtered.index, dtype=str)).fillna("").astype(str)
            + " "
            + filtered.get("location_name", pd.Series(index=filtered.index, dtype=str)).fillna("").astype(str)
        ).str.lower()
        filtered = filtered[searchable.str.contains(needle, regex=False)]

    st.metric("Matching locations", len(filtered))
    preferred = [
        "risk_level", "risk_score", "default_code", "product_name", "location_name",
        "on_hand_qty", "reserved_qty", "available_qty", "unit_cost", "inventory_value",
        "abc_class", "xyz_class", "activity_cv_28d", "touches_7d", "unique_users_7d",
        "tracking", "recommended_count_interval_days", "reasons",
    ]
    columns = [column for column in preferred if column in filtered.columns]
    st.dataframe(filtered[columns] if columns else filtered, width="stretch", hide_index=True)

    with st.expander("Scoring methodology"):
        for name, description in report.get("methodology", {}).items():
            st.markdown(f"**{name.replace('_', ' ').title()}** — {description}")


def render_cycle_count_plan(base_url: str, source_limit: int) -> None:
    st.subheader("Daily Cycle Count Plan")
    st.caption("Advisory route only. Nothing is written back to Odoo from this screen.")

    count_limit = st.slider("Locations to include", min_value=5, max_value=200, value=25, step=5)
    try:
        plan = fetch(base_url, "/api/cycle-count-plan", limit=count_limit, source_limit=source_limit)
    except ControlTowerAPIError as exc:
        st.error(str(exc))
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Count tasks", int(plan.get("count", 0) or 0))
    c2.metric("Locations evaluated", int(plan.get("summary", {}).get("locations_evaluated", 0) or 0))
    c3.metric("Inventory value", money(plan.get("summary", {}).get("total_inventory_value")))

    st.info(plan.get("execution", "Advisory only."))
    st.caption(plan.get("route_strategy", ""))

    frame = make_frame(plan.get("entries", []))
    preferred = [
        "sequence", "priority_band", "risk_level", "risk_score", "location_name",
        "default_code", "product_name", "system_on_hand_qty", "abc_class", "xyz_class",
        "recommended_count_interval_days", "reasons",
    ]
    columns = [column for column in preferred if column in frame.columns]
    st.dataframe(frame[columns] if columns else frame, width="stretch", hide_index=True)


def render_warehouse_optimization(base_url: str, source_limit: int) -> None:
    st.subheader("Warehouse Optimization & Production Pilot")
    st.caption(
        "Manager view of mapping priority, modeled slotting evidence, pilot decisions, traceability health, and the production-readiness boundary. "
        "AWIA remains advisory and this page performs no Odoo writes."
    )

    areas = discover_analysis_areas(DEFAULT_ANALYSIS_DIR)
    if not areas:
        st.warning(
            f"No production-readiness artifacts were found in {DEFAULT_ANALYSIS_DIR}. "
            "Run a mapped-area decision pipeline and production-pilot readiness evaluation first."
        )
        return

    selected_area = st.selectbox("Mapped analysis area", areas, index=len(areas) - 1)
    bundle = load_analysis_artifacts(selected_area, DEFAULT_ANALYSIS_DIR)
    artifacts = bundle.get("artifacts", {})

    readiness = usable_artifact(artifacts.get("production_pilot_readiness"))
    intake = usable_artifact(artifacts.get("production_pilot_intake"))
    decision_report = usable_artifact(artifacts.get("decision_report"))
    pilot_decision = usable_artifact(artifacts.get("pilot_decision"))
    package_decision = usable_artifact(artifacts.get("copick_package_pilot_decision"))
    package_economics = usable_artifact(artifacts.get("copick_package_economics"))
    slotting = usable_artifact(artifacts.get("slotting"))
    route = usable_artifact(artifacts.get("route_validation"))

    if bundle.get("missing_artifacts"):
        st.info("Some optional analysis artifacts are not present: " + ", ".join(bundle["missing_artifacts"]))

    st.markdown("#### Current decision state")
    route_primary = route.get("primary_result", {})
    route_saved = route_primary.get("modeled_distance_saved_ft")
    route_reduction = route_primary.get("modeled_distance_reduction_pct")
    readiness_summary = readiness.get("summary", {})
    individual_summary = pilot_decision.get("summary", {})
    package_summary = package_decision.get("summary", {})

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Production status", readiness.get("status", "unknown"))
    c2.metric("Readiness checks", f"{int(readiness_summary.get('passed', 0) or 0)}/{int(readiness_summary.get('checks', 0) or 0)}")
    c3.metric("Modeled route savings", f"{number(route_saved)} ft")
    c4.metric("Modeled reduction", f"{number(route_reduction, 2)}%")
    c5.metric("Individual READY", int(individual_summary.get("READY_FOR_CONTROLLED_PILOT", 0) or 0))
    c6.metric("Package READY", int(package_summary.get("READY_FOR_CONTROLLED_PILOT", 0) or 0))

    if readiness.get("status") == "BLOCKED":
        st.error(
            "Production pilot is BLOCKED. Sandbox/model results may be reviewed, but they are not evidence that a real warehouse move is safe or approved."
        )
    elif readiness.get("ready_for_read_only_production_pilot"):
        st.success(
            "Read-only production-pilot prerequisites are recorded as complete. This still does not authorize inventory movement or Odoo writes."
        )

    st.markdown("#### Live mapping opportunity context")
    selected_location_prefix = ""
    try:
        mapping = fetch(base_url, "/api/mapping-priorities", limit=10, source_limit=source_limit, lookback_days=90)
        mapping_rows = mapping.get("areas", [])
        for row in mapping_rows:
            complete_name = row.get("area_complete_name")
            if location_name_to_area_slug(complete_name) == selected_area:
                selected_location_prefix = str(complete_name or "")
                break

        mapping_frame = make_frame(mapping_rows)
        if mapping_frame.empty:
            st.info("No mapping-priority areas were returned by the API.")
        else:
            preferred = [
                "rank", "area_complete_name", "opportunity_score", "active_storage_locations",
                "sku_count", "move_touches", "high_velocity_skus", "tracked_skus", "bom_occurrences",
            ]
            columns = [column for column in preferred if column in mapping_frame.columns]
            st.dataframe(mapping_frame[columns] if columns else mapping_frame, width="stretch", hide_index=True)
    except ControlTowerAPIError as exc:
        st.warning(f"Mapping-priority API unavailable: {exc}")

    st.markdown("#### Traceability health before relocation analysis")
    if not selected_location_prefix:
        st.info(
            "The selected analysis slug could not be matched to a live Odoo mapping area, so area-scoped traceability was not requested."
        )
    else:
        try:
            traceability = fetch(
                base_url,
                "/api/traceability-health",
                source_limit=source_limit,
                location_prefix=selected_location_prefix,
            )
            trace_summary = traceability.get("summary", {})
            t1, t2, t3, t4, t5 = st.columns(5)
            t1.metric("Traceability coverage", f"{number(trace_summary.get('traceability_coverage_pct'), 2)}%")
            t2.metric("Tracked positions", int(trace_summary.get("tracked_inventory_positions", 0) or 0))
            t3.metric("Blocked positions", int(trace_summary.get("blocked_positions", 0) or 0))
            t4.metric("Blocked products", int(trace_summary.get("blocked_products", 0) or 0))
            t5.metric("Anonymous tracked qty", number(trace_summary.get("anonymous_quantity"), 1))

            if int(trace_summary.get("blocked_positions", 0) or 0) > 0:
                st.error(
                    "Tracked inventory with missing lot/serial identity exists in this mapped area. Those positions are blocked from relocation analysis until reconciled through approved inventory/quality procedures."
                )
            else:
                st.success(
                    "No anonymous positive on-hand quantity was found for lot/serial-tracked inventory in this mapped area. Reservations and other execution gates still apply."
                )

            trace_items = traceability.get("items") or []
            if trace_items:
                trace_frame = make_frame(trace_items)
                preferred = [
                    "status", "product_code", "product_name", "tracking", "location_name",
                    "on_hand_quantity", "reserved_quantity", "identified_quantity",
                    "anonymous_quantity", "traceability_coverage_pct", "reasons",
                ]
                columns = [column for column in preferred if column in trace_frame.columns]
                st.dataframe(trace_frame[columns] if columns else trace_frame, width="stretch", hide_index=True)

            trace_source = traceability.get("source_snapshot", {})
            if trace_source.get("truncated_possible"):
                st.warning(
                    "Traceability source data reached the configured source limit. Increase Source rows before treating coverage as complete."
                )
            st.caption(f"Traceability scope: `{selected_location_prefix}` • Read-only Odoo analysis")
        except ControlTowerAPIError as exc:
            st.warning(f"Traceability-health API unavailable: {exc}")

    st.markdown("#### Slotting and route evidence")
    slotting_summary = slotting.get("summary", {})
    route_completed = route.get("completed_historical_validation", {})
    e1, e2, e3, e4 = st.columns(4)
    e1.metric("Travel-actionable moves", int(slotting_summary.get("recommendations", 0) or 0))
    e2.metric("Geometry-only suppressed", int(slotting_summary.get("geometry_only_candidates_suppressed", 0) or 0))
    e3.metric("Completed modeled pickings", int(route_completed.get("modeled_pickings", 0) or 0))
    e4.metric("Affected completed pickings", int(route_completed.get("affected_pickings", 0) or 0))

    recommendations = decision_report.get("recommendations") or []
    if not recommendations:
        recommendations = pilot_decision.get("decisions") or []
    if recommendations:
        st.markdown("##### Individual relocation decisions")
        frame = make_frame(recommendations)
        preferred = [
            "product_code", "source", "candidate", "decision", "capacity_screen_pass",
            "completed_route_saved_ft", "payback_affected_pickings_at_selected_scenario",
            "observed_completed_affected_pickings", "reasons",
        ]
        columns = [column for column in preferred if column in frame.columns]
        st.dataframe(frame[columns] if columns else frame, width="stretch", hide_index=True)

    packages = decision_report.get("copick_packages") or package_decision.get("decisions") or []
    if packages:
        st.markdown("##### Co-pick package decisions")
        package_frame = make_frame(packages)
        preferred = [
            "package_id", "product_codes", "decision", "shared_joint_route_saved_ft",
            "package_modeled_route_saved_ft", "payback_affected_pickings_at_selected_scenario",
            "completed_affected_pickings", "reasons",
        ]
        columns = [column for column in preferred if column in package_frame.columns]
        st.dataframe(package_frame[columns] if columns else package_frame, width="stretch", hide_index=True)

    package_summary_econ = package_economics.get("summary", {})
    if package_summary_econ:
        st.caption(
            "Shared co-pick savings reconciliation: "
            f"{number(package_summary_econ.get('package_shared_joint_route_saved_ft'))} ft package shared benefit; "
            f"difference {number(package_summary_econ.get('shared_savings_reconciliation_difference_ft'), 3)} ft."
        )

    st.markdown("#### Production-readiness gate")
    checks = readiness.get("checks") or []
    if checks:
        check_frame = make_frame(checks)
        preferred = ["check_id", "passed", "detail", "evidence_reference"]
        columns = [column for column in preferred if column in check_frame.columns]
        st.dataframe(check_frame[columns] if columns else check_frame, width="stretch", hide_index=True)
    else:
        st.info("No production-readiness artifact is available for this area.")

    st.markdown("#### Production-pilot intake actions")
    phase_summary = intake.get("phase_summary") or []
    if phase_summary:
        phase_frame = make_frame(phase_summary)
        st.dataframe(phase_frame, width="stretch", hide_index=True)

    intake_items = intake.get("items") or []
    if intake_items:
        blocked_only = st.toggle("Show blocked actions only", value=True)
        rows = [row for row in intake_items if not row.get("passed")] if blocked_only else intake_items
        action_frame = make_frame(rows)
        preferred = [
            "phase", "status", "check_id", "suggested_owner", "required_evidence",
            "next_action", "current_evidence_reference",
        ]
        columns = [column for column in preferred if column in action_frame.columns]
        st.dataframe(action_frame[columns] if columns else action_frame, width="stretch", hide_index=True)
    else:
        st.info("No production-pilot intake packet is available for this area.")

    with st.expander("Artifact and execution guardrails"):
        st.write(f"Analysis directory: {bundle.get('analysis_dir')}")
        st.write("Loaded area slug:", bundle.get("area_slug"))
        st.write("Odoo mutated by artifact loader: No")
        st.write("Inventory execution authorized: No")
        for item in readiness.get("guardrails") or []:
            st.markdown(f"- {item}")
        for item in intake.get("guardrails") or []:
            st.markdown(f"- {item}")


def render_data_explorer(base_url: str) -> None:
    st.subheader("Odoo Data Explorer")
    st.caption("Read-only inspection of the four Odoo models currently exposed by AWIA.")

    endpoint_map = {
        "Products": "/api/products",
        "Inventory quants": "/api/inventory",
        "Stock move lines": "/api/moves",
        "Manufacturing orders": "/api/manufacturing-orders",
    }
    c1, c2 = st.columns([2, 1])
    with c1:
        label = st.selectbox("Dataset", list(endpoint_map))
    with c2:
        limit = st.number_input("Rows", min_value=1, max_value=1000, value=100, step=25)

    try:
        payload = fetch(base_url, endpoint_map[label], limit=int(limit))
    except ControlTowerAPIError as exc:
        st.error(str(exc))
        return

    st.metric("Rows returned", int(payload.get("count", 0) or 0))
    st.dataframe(make_frame(payload.get("records", [])), width="stretch", hide_index=True)


st.title("AWIA • Warehouse Analyst Control Tower")
st.caption("Agentic Warehouse Inventory Analyst • Odoo 19 development environment")

with st.sidebar:
    st.header("Control Tower")
    api_url = st.text_input("AWIA API URL", value=DEFAULT_API_URL)
    source_limit = st.select_slider(
        "Source rows per Odoo model",
        options=[500, 1000, 2500, 5000, 10000, 20000],
        value=5000,
    )
    page = st.radio(
        "View",
        [
            "Control Tower",
            "Inventory Health",
            "Cycle Count Plan",
            "Warehouse Optimization",
            "Data Explorer",
        ],
    )
    if st.button("Refresh data", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.caption("Read-only development UI. HITL approval is required before future Odoo mutations.")

show_connection_banner(api_url)

if page == "Control Tower":
    render_control_tower(api_url, source_limit)
elif page == "Inventory Health":
    render_inventory_health(api_url, source_limit)
elif page == "Cycle Count Plan":
    render_cycle_count_plan(api_url, source_limit)
elif page == "Warehouse Optimization":
    render_warehouse_optimization(api_url, source_limit)
else:
    render_data_explorer(api_url)
