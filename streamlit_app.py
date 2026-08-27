"""Streamlit front end for the AWIA Warehouse Analyst Control Tower."""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import streamlit as st

from app.ui.api_client import ControlTowerAPIError, get_json
from app.ui.dataframe import make_frame


st.set_page_config(
    page_title="AWIA Control Tower",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

DEFAULT_API_URL = os.getenv("AWIA_API_URL", "http://127.0.0.1:8000")


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
    page = st.radio("View", ["Control Tower", "Inventory Health", "Cycle Count Plan", "Data Explorer"])
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
else:
    render_data_explorer(api_url)